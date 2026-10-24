"""TTS 配音节点 v2：多引擎 + 长文本分块 + 分段批量合成。

引擎：
- edge:     edge-tts 免费多音色（支持 rate/pitch/volume，长文本按标点分块并行合成后合并）
- manual:   用户导入音频文件（音频直通）
- indextts: IndexTTS2 / OmniVoice 本地克隆。
            端口/令牌协议统一：优先 3900 OmniVoice（/engines+health 探测，
            POST /generate multipart，无令牌）→ 回退 8791 voice_clone
            （POST /synthesize JSON+base64，带 X-FW-Local-Token 令牌门）；
            auto_start_server 拉起后从服务日志读取真实 bound port（不硬编码轮询）；
            合成后按 duration_factor 变速。
- cloud:    OpenAI 兼容 /audio/speech（base_url 可配，Key 支持 @secret: 引用）
- aliyun:   阿里云双模式——NLS REST（/stream/v1/tts，AppKey+Token，speech_rate/
            volume/pitch_rate）与 CosyVoice（DashScope text2audio SSE，speed 倍率）；
            Token 支持预签发（aliyun_token）或 AK/SK 签名获取（aliyun_ak_id+_secret）。
- volcano:  豆包（火山引擎）合成 REST（POST /api/v1/tts，AppID+AccessToken，
            speed_ratio/volume_ratio/pitch_ratio），ICL2.0 克隆音色以 SpeakerID
            作 voice_type 直接复用（training 链在火山控制台/OpenAPI 侧完成）。

批量语义：连接 segments(SEGMENTS) 时按段合成（每段取 text/narration/source_text），
段级并发 ≤ max_concurrent、失败隔离、可选 ffmpeg 合并；未连接 segments 时按
script/direct_text 单条合成（超 chunk_chars 自动分块）。
"""
from __future__ import annotations
import asyncio
import base64
import json
import os
import re
import shutil
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

# Edge-TTS 常用中文音色
EDGE_VOICES = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunjianNeural",
    "zh-CN-YunxiNeural", "zh-CN-YunxiaNeural", "zh-CN-YunyangNeural",
    "zh-CN-liaoning-XiaobeiNeural", "zh-CN-shaanxi-XiaoniNeural",
    "zh-CN-XiaomoNeural", "zh-CN-XiaoruiNeural", "zh-CN-XiaoshuangNeural",
    "zh-CN-XiaoxuanNeural", "zh-CN-XiaoxueNeural", "zh-CN-XiaoyanNeural",
    "zh-CN-XiaoyouNeural", "zh-CN-XiaozhenNeural", "zh-CN-YunfengNeural",
    "zh-CN-YunhaoNeural", "zh-CN-YunzeNeural",
    "zh-TW-HsiaoChenNeural", "zh-TW-HsiaoYuNeural", "zh-TW-YunJheNeural",
    "zh-HK-HiuGaaiNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
    "en-US-JennyNeural", "en-US-AriaNeural", "en-US-GuyNeural",
    "en-US-ChristopherNeural", "en-US-EricNeural", "en-US-MichelleNeural",
    "en-GB-SoniaNeural", "en-GB-RyanNeural", "en-AU-NatashaNeural",
    "ja-JP-NanamiNeural", "ja-JP-KeitaNeural", "ja-JP-AoiNeural",
    "ko-KR-SunHiNeural", "ko-KR-InJoonNeural",
]

_PUNCT_SPLIT = re.compile(r"(?<=[。！？；，、,.!?;:])")

# ---- S3-T8（C3）：音色库动态加载（单一数据源 = core_voice_library/data/tts_library.json）----
# T6 双份数据不复发：214 条音色只存在 core_voice_library 资源中（packages/ 与 user_nodes/
# 安装态均为同级目录），本节点动态读取，绝不内嵌第二份清单。
_VOICE_LIB_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "core_voice_library", "data", "tts_library.json")


def voice_library_ids() -> List[str]:
    """从 core_voice_library 资源（唯一数据源）读取 {provider}:{native_id} 音色 id。

    资源缺失（core_voice_library 未安装）→ 空列表（回落内置 EDGE_VOICES）。
    """
    try:
        with open(_VOICE_LIB_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    voices = (data or {}).get("voices") or []
    return [str(v["id"]) for v in voices if isinstance(v, dict) and v.get("id")]


def voice_options() -> List[str]:
    """音色下拉动态选项：内置 39 edge + 音色库 214（去重；edge 保留裸 id 向后兼容）。"""
    out = list(EDGE_VOICES)
    seen = set(out)
    for vid in voice_library_ids():
        if vid not in seen:
            out.append(vid)
            seen.add(vid)
    return out


def resolve_edge_voice(voice: str) -> str:
    """edge 引擎音色归一：接受 {provider}:{native_id} 统一模型 → native_id。

    非 edge 提供商（volcano/aliyun/index）→ 明确报错（需切换到对应引擎）；
    裸 id（无冒号）按 edge 兼容处理。
    """
    v = str(voice or "").strip()
    if not v:
        return "zh-CN-XiaoxiaoNeural"
    if ":" in v:
        provider, native = v.split(":", 1)
        if provider != "edge":
            raise RuntimeError(
                f"invalid: 音色 {v!r} 属于 {provider} 引擎，edge 引擎仅支持 edge:* 音色——"
                "请切换到 aliyun/volcano/index 对应引擎")
        return native
    return v


def resolve_vendor_voice(engine: str, voice: str, mode: str = "") -> str:
    """aliyun/volcano 引擎音色归一：接受 {provider}:{native_id} → native_id。

    - volcano：接受 volcano:*（含 ICL2.0 克隆 SpeakerID，直接作 voice_type）；
    - aliyun：接受 aliyun:*/aliyun-nls:*/aliyun-cosy:*（nls/cosyvoice 双模式共用，
      mode 由 aliyun_mode 参数决定；带模式前缀与参数冲突时明确报错）。
    裸 id（无冒号）按该引擎原生音色 id 兼容处理。
    """
    v = str(voice or "").strip()
    if not v:
        return ""
    if ":" not in v:
        return v
    provider, native = v.split(":", 1)
    if provider == engine:
        return native
    if engine == "aliyun" and provider in ("aliyun-nls", "aliyun-cosy"):
        if mode == "nls" and provider == "aliyun-cosy":
            raise RuntimeError(
                f"invalid: 音色 {v!r} 是 CosyVoice 音色，请把 aliyun_mode 切换为 cosyvoice")
        if mode == "cosyvoice" and provider == "aliyun-nls":
            raise RuntimeError(
                f"invalid: 音色 {v!r} 是 NLS 音色，请把 aliyun_mode 切换为 nls")
        return native
    raise RuntimeError(
        f"invalid: 音色 {v!r} 不属于 {engine} 引擎——请选择 {engine}:* 音色（edge/index 音色请切换引擎）")


def _ratio_param(raw: Any, default: float = 1.0, lo: float = 0.5, hi: float = 2.0) -> float:
    """把 edge 语法参数（+10% / -20% / +0Hz / 1.2）转换为比率（1.0=正常）。

    aliyun/volcano 引擎请求里 rate/volume/pitch 一律按比率语义映射：
      "+10%" → 1.10；"-20%" → 0.80；"+50Hz"（音调）→ 1.50 近似；裸数字 → 直取。
    解析失败回落 default，并限幅到 [lo, hi]。
    """
    s = str(raw or "").strip()
    if not s:
        return default
    sign = 1.0 if not s.startswith("-") else -1.0
    if s.endswith("%"):
        num = re.sub(r"[^0-9.]", "", s)
        try:
            return max(lo, min(hi, 1.0 + sign * float(num) / 100.0))
        except ValueError:
            return default
    if s.lower().endswith("hz"):
        num = re.sub(r"[^0-9.]", "", s)
        try:
            # 音调 +xHz → 比率近似 1 + x/100（有限幅度内线性近似）
            return max(lo, min(hi, 1.0 + float(num) / 100.0))
        except ValueError:
            return default
    try:
        return max(lo, min(hi, float(s)))
    except ValueError:
        return default


def _split_long_text(text: str, chunk_chars: int) -> List[str]:
    """按标点分块，块长尽量不超过 chunk_chars（单句超长时硬切）。"""
    if chunk_chars <= 0:
        chunk_chars = 800
    parts: List[str] = []
    cur = ""
    for seg in _PUNCT_SPLIT.split(text):
        seg = seg.strip()
        if not seg:
            continue
        if len(cur) + len(seg) <= chunk_chars:
            cur += seg
        else:
            if cur:
                parts.append(cur)
            if len(seg) > chunk_chars:
                # 超长硬切（按字符）
                for i in range(0, len(seg), chunk_chars):
                    parts.append(seg[i:i + chunk_chars])
            else:
                cur = seg
    if cur:
        parts.append(cur)
    if not parts and text.strip():
        parts = [text]
    return parts


def _resolve_ffmpeg(ffmpeg_path: str = "") -> str:
    """解析 ffmpeg 可执行路径（统一依赖解析层：参数 → 环境变量 → PATH → imageio → 默认位）。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", requested=ffmpeg_path, env=os.environ,
                            params={"ffmpeg_path": ffmpeg_path})
    if r["ready"]:
        return r["path"]
    # 兜底裸命令（交给 OS PATH；与历史行为一致）
    return "ffmpeg"


def _ffprobe_bin(ffmpeg: str) -> str:
    """ffprobe 路径：优先与已解析 ffmpeg 同目录，其次统一 resolver。"""
    from app.plugin import deps
    d = os.path.dirname(ffmpeg)
    for cand in (os.path.join(d, "ffprobe.exe"), os.path.join(d, "ffprobe")):
        if cand and os.path.isfile(cand):
            return cand
    r = deps.resolve_binary("ffprobe", env=os.environ)
    if r["ready"]:
        return r["path"]
    return "ffprobe"


async def _probe_duration(ctx, ffmpeg_path: str, media_path: str) -> Optional[float]:
    """ffprobe 音频时长（秒）。失败返回 None（不阻断主流程）。

    run_tracked 默认继承 stdout（不捕获）——显式 PIPE 捕获，否则输出落入父进程
    控制台、stdout=None，时长探测恒失败（duration/marks 退化为 0）。
    """
    import subprocess as _sp
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    ffprobe = _ffprobe_bin(ffmpeg)
    try:
        out = await ctx["run_tracked"](
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", media_path],
            timeout=60, stdout=_sp.PIPE, stderr=_sp.PIPE)
        if out.returncode != 0 or not out.stdout:
            return None
        data = json.loads(out.stdout.decode("utf-8", "replace"))
        dur = float((data.get("format") or {}).get("duration", 0) or 0)
        return round(dur, 3) if dur > 0 else None
    except Exception:
        return None


async def _ffmpeg_concat(ctx, ffmpeg_path: str, inputs: List[str], out_path: str,
                         keep_same_codec: bool = True) -> None:
    """ffmpeg 合并音频（concat demuxer）。成功抛异常则调用方处理。"""
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    list_file = out_path + ".concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in inputs:
            f.write("file '%s'\n" % p.replace("'", "'\\''"))
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out_path]
    try:
        out = await ctx["run_tracked"](cmd, timeout=300)
        if out.returncode != 0:
            raise RuntimeError("ffmpeg 合并失败: %s" % (out.stderr.decode("utf-8", "replace")[-500:]))
    finally:
        try:
            if os.path.exists(list_file):
                os.remove(list_file)
        except OSError:
            pass


def _read_script_text(store, asset: Asset) -> str:
    if asset is None:
        return ""
    if asset.path and asset.path.endswith(".json"):
        data = store.read_json(asset.id) or {}
        if isinstance(data, dict):
            return str(data.get("text", "") or "")
        return str(data)
    if asset.path:
        try:
            with open(asset.path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
    return ""


def _resolve_secret(ctx, key: str) -> str:
    """解析 @secret: 引用；缺省回退保险箱。"""
    if key.startswith("@secret:"):
        ref = key.split(":", 1)[1]
        resolver = ctx.get("resolve_secret") if hasattr(ctx, "get") else None
        if resolver:
            return str(resolver(ref) or "")
        try:
            from app.secrets import read_secret
            return str(read_secret(ref) or "")
        except Exception:
            return ""
    return key


class TTSNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/tts",
            title="TTS 配音（多引擎）",
            category="音频",
            description="文字转语音：Edge免费 / IndexTTS2本地克隆 / OpenAI兼容 / 阿里NLS+CosyVoice / 豆包火山；长文本分块与分段批量合成",
            inputs=[
                PortSpec(name="script", type=PortType.SCRIPT, label="文案", required=False),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="分段列表", required=False),
                PortSpec(name="audio_in", type=PortType.AUDIO, label="人工音频", required=False),
                PortSpec(name="audio_ref", type=PortType.AUDIO, label="克隆参考音", required=False),
            ],
            outputs=[
                PortSpec(name="audio", type=PortType.AUDIO, label="配音音频"),
                PortSpec(name="duration", type=PortType.FLOAT, label="音频时长(秒)"),
                PortSpec(name="marks", type=PortType.JSON, label="时间戳标记"),
                PortSpec(name="report", type=PortType.JSON, label="合成报告"),
            ],
            params=[
                PortSpec(name="direct_text", type=PortType.STRING, label="直接输入文案",
                         default="", widget="text", description="留空时使用上游文案输入"),
                PortSpec(name="engine", type=PortType.STRING, label="引擎", default="edge",
                         widget="select",
                         options=["edge", "manual", "indextts", "cloud", "aliyun", "volcano"]),
                PortSpec(name="voice", type=PortType.STRING, label="音色", default="zh-CN-XiaoxiaoNeural",
                         widget="select", options=voice_options(),
                         description="edge 音色（内置 39 + 音色库 214 动态加载，id=provider:native_id 统一模型）；volcano 选 volcano:*（含 ICL 克隆 SpeakerID）；aliyun 选 aliyun:*（mode 由 aliyun_mode 决定）"),
                PortSpec(name="rate", type=PortType.STRING, label="语速", default="+0%",
                         widget="text", description="edge-tts 语速如 +10%"),
                PortSpec(name="pitch", type=PortType.STRING, label="音调", default="+0Hz", widget="text"),
                PortSpec(name="volume", type=PortType.STRING, label="音量", default="+0%", widget="text"),
                PortSpec(name="output_format", type=PortType.STRING, label="输出格式", default="mp3",
                         widget="select", options=["mp3", "wav", "ogg"]),
                PortSpec(name="lang", type=PortType.STRING, label="克隆语言", default="ZH",
                         widget="select", options=["ZH", "EN", "JA", "ES", "AR"]),
                PortSpec(name="duration_factor", type=PortType.STRING, label="语速倍率", default="1.0",
                         widget="text", description="0.5~2.0，1.0 为原速"),
                PortSpec(name="server_url", type=PortType.STRING, label="克隆服务地址",
                         default="http://127.0.0.1:3900", widget="text",
                         description="IndexTTS2/OmniVoice 常驻服务（兼容旧版 8791）"),
                PortSpec(name="auto_start_server", type=PortType.BOOL, label="自动拉起服务", default=True,
                         widget="toggle", description="首次用时自动启动克隆服务"),
                PortSpec(name="api_key", type=PortType.STRING, label="API Key",
                         widget="password", description="云端 TTS Key；可填 @secret:名称"),
                PortSpec(name="base_url", type=PortType.STRING, label="中转地址",
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec(name="cloud_model", type=PortType.STRING, label="云端模型",
                         default="tts-1", widget="text"),
                PortSpec(name="cloud_voice", type=PortType.STRING, label="云端音色",
                         default="alloy", widget="text"),
                PortSpec(name="cloud_speed", type=PortType.STRING, label="云端语速", default="1.0",
                         widget="text", description="0.25~4.0"),
                PortSpec(name="aliyun_mode", type=PortType.STRING, label="阿里模式", default="nls",
                         widget="select", options=["nls", "cosyvoice"],
                         description="nls=NLS REST 标准音色；cosyvoice=DashScope 大模型音色"),
                PortSpec(name="aliyun_appkey", type=PortType.STRING, label="阿里 AppKey",
                         widget="text", description="NLS AppKey（可 @secret:名称 引用）"),
                PortSpec(name="aliyun_token", type=PortType.STRING, label="阿里 Token",
                         widget="text", description="NLS 预签发 Token 或 DashScope API Key（可 @secret:名称 引用）；留空且填 AK/SK 时自动换取"),
                PortSpec(name="aliyun_ak_id", type=PortType.STRING, label="阿里 AccessKey ID",
                         widget="text", description="NLS Token 换取用 AccessKey ID（可 @secret:名称 引用）"),
                PortSpec(name="aliyun_ak_secret", type=PortType.STRING, label="阿里 AccessKey Secret",
                         widget="password", description="NLS Token 换取用 AccessKey Secret（可 @secret:名称 引用）"),
                PortSpec(name="aliyun_model", type=PortType.STRING, label="CosyVoice 模型",
                         default="cosyvoice-v2", widget="text",
                         description="cosyvoice-v2 / cosyvoice-v3-flash"),
                PortSpec(name="aliyun_base_url", type=PortType.STRING,
                         label="阿里合成端点", default="https://nls-gateway-cn-shanghai.aliyuncs.com",
                         widget="text", description="nls 模式=NLS /stream/v1/tts 网关；cosyvoice 模式=DashScope base（默认 https://dashscope.aliyuncs.com，可配代理/地域端点）"),
                PortSpec(name="aliyun_token_url", type=PortType.STRING,
                         label="NLS Token 端点", default="https://nls-meta.cn-shanghai.aliyuncs.com/oauth2/token",
                         widget="text", description="AK/SK 换取 NLS Token 的 RPC 端点"),
                PortSpec(name="volcano_appid", type=PortType.STRING, label="豆包 AppID",
                         widget="text", description="火山引擎豆包 TTS AppID（可 @secret:名称 引用）"),
                PortSpec(name="volcano_token", type=PortType.STRING, label="豆包 AccessToken",
                         widget="password", description="火山引擎豆包 TTS AccessToken（可 @secret:名称 引用）"),
                PortSpec(name="volcano_cluster", type=PortType.STRING, label="豆包集群",
                         default="volcano_tts", widget="text"),
                PortSpec(name="volcano_base_url", type=PortType.STRING,
                         label="豆包合成端点", default="https://openspeech.bytedance.com",
                         widget="text", description="POST /api/v1/tts 基址"),
                PortSpec(name="chunk_chars", type=PortType.INT, label="长文本分块字数", default=800,
                         widget="number", description="edge/indextts 按标点分块合成再合并"),
                PortSpec(name="max_concurrent", type=PortType.INT, label="批量并发", default=5,
                         widget="number", description="分段/分块并行上限（provider 默认不高于 5）"),
                PortSpec(name="retries", type=PortType.INT, label="单条重试次数", default=3,
                         widget="number", description="网络/超时类错误指数退避重试"),
                PortSpec(name="merge_audio", type=PortType.BOOL, label="批量合并音频", default=True,
                         widget="toggle", description="分段批量时是否合并为单个音频"),
                PortSpec(name="ffmpeg_path", type=PortType.STRING, label="ffmpeg 路径", default="",
                         widget="text", description="留空自动探测"),
            ],
            version="2.1.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        segments_asset = inputs.get("segments")
        engine = str(params.get("engine", "edge") or "edge")

        # S3-T8（C3）：edge 引擎音色归一（{provider}:{native_id} 统一模型；非 edge 提供商明确报错）
        if engine == "edge":
            params = {**params, "voice": resolve_edge_voice(
                str(params.get("voice", "zh-CN-XiaoxiaoNeural")))}

        if segments_asset is not None:
            return await self._run_batch(ctx, inputs, params, segments_asset, engine, progress)

        # ---- 单条合成 ----
        if engine == "manual":
            # 人工配音无需文案，直接直通音频
            return await self._run_manual(ctx, inputs, params, progress)

        script_asset = inputs.get("script")
        direct = str(params.get("direct_text", "") or "").strip()
        text = direct if direct else _read_script_text(store, script_asset)
        if not text.strip():
            raise RuntimeError("文案为空，无法配音（连接 script 或填写直接输入文案）")

        if engine == "edge":
            return await self._run_edge_single(ctx, inputs, params, text, progress)
        if engine == "indextts":
            return await self._run_indextts_single(ctx, inputs, params, text, progress)
        if engine in ("cloud", "aliyun", "volcano"):
            # OpenAI 兼容 / 阿里 NLS+CosyVoice / 豆包火山 共用分块-并发-合并骨架
            return await self._run_vendor_single(engine, ctx, inputs, params, text, progress)
        raise RuntimeError(f"未知引擎: {engine}")

    # ------------------------------------------------------------ 公共工具

    def _out_dir(self, store) -> str:
        out_dir = os.path.join(store.files_dir, "tts")
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _uniq_path(self, out_dir: str, prefix: str, ext: str) -> str:
        return os.path.join(out_dir, f"{prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}.{ext}")

    def _params(self, params) -> Dict[str, Any]:
        return {
            "voice": str(params.get("voice", "zh-CN-XiaoxiaoNeural")),
            "rate": str(params.get("rate", "+0%")),
            "pitch": str(params.get("pitch", "+0Hz")),
            "volume": str(params.get("volume", "+0%")),
            "output_format": str(params.get("output_format", "mp3") or "mp3").lstrip("."),
            "retries": max(0, int(params.get("retries", 3) or 3)),
            "chunk_chars": max(50, int(params.get("chunk_chars", 800) or 800)),
            "max_concurrent": max(1, min(5, int(params.get("max_concurrent", 5) or 5))),
            "ffmpeg_path": str(params.get("ffmpeg_path", "") or ""),
            # 引擎专属透传
            "lang": str(params.get("lang", "ZH") or "ZH").upper(),
            "duration_factor": float(params.get("duration_factor", "1.0") or "1.0"),
            "server_url": str(params.get("server_url", "http://127.0.0.1:3900") or ""),
            "auto_start_server": bool(params.get("auto_start_server", True)),
            "api_key": str(params.get("api_key", "") or ""),
            "base_url": str(params.get("base_url", "https://ameaaos.com/v1") or ""),
            "cloud_model": str(params.get("cloud_model", "tts-1") or "tts-1"),
            "cloud_voice": str(params.get("cloud_voice", "alloy") or "alloy"),
            "cloud_speed": str(params.get("cloud_speed", "1.0") or "1.0"),
            # 阿里 / 豆包 引擎（C4）
            "aliyun_mode": str(params.get("aliyun_mode", "nls") or "nls").lower(),
            "aliyun_appkey": str(params.get("aliyun_appkey", "") or ""),
            "aliyun_token": str(params.get("aliyun_token", "") or ""),
            "aliyun_ak_id": str(params.get("aliyun_ak_id", "") or ""),
            "aliyun_ak_secret": str(params.get("aliyun_ak_secret", "") or ""),
            "aliyun_model": str(params.get("aliyun_model", "cosyvoice-v2") or "cosyvoice-v2"),
            "aliyun_base_url": str(params.get("aliyun_base_url",
                                              "https://nls-gateway-cn-shanghai.aliyuncs.com") or ""),
            "aliyun_token_url": str(params.get("aliyun_token_url",
                                               "https://nls-meta.cn-shanghai.aliyuncs.com/oauth2/token") or ""),
            "volcano_appid": str(params.get("volcano_appid", "") or ""),
            "volcano_token": str(params.get("volcano_token", "") or ""),
            "volcano_cluster": str(params.get("volcano_cluster", "volcano_tts") or "volcano_tts"),
            "volcano_base_url": str(params.get("volcano_base_url",
                                               "https://openspeech.bytedance.com") or ""),
        }

    async def _save_audio(self, store, path: str, meta: Dict[str, Any],
                          duration: Optional[float] = None) -> Asset:
        asset = Asset(id="", kind=PortType.AUDIO.value, path=path, meta=dict(meta))
        if duration is not None:
            asset.meta["duration"] = duration
        store.save_asset(asset)
        return asset

    async def _synth_worker(self, ctx, engine: str, p: Dict[str, Any],
                            text: str, out_path: str,
                            inputs: Optional[dict] = None) -> Dict[str, Any]:
        """合成一段文本到 out_path；返回 {path, duration}。内部负责引擎分发与重试。"""
        if engine == "edge":
            await self._edge_synth_with_retry(ctx, p, text, out_path)
        elif engine == "indextts":
            await self._indextts_synth_with_retry(ctx, inputs or {}, params=p,
                                                  text=text, out_path=out_path)
        elif engine == "cloud":
            await self._cloud_synth_with_retry(ctx, p, text, out_path)
        elif engine == "aliyun":
            await self._aliyun_synth_with_retry(ctx, p, text, out_path)
        elif engine == "volcano":
            await self._volcano_synth_with_retry(ctx, p, text, out_path)
        else:
            raise RuntimeError(f"批量不支持引擎: {engine}")
        duration = await _probe_duration(ctx, p.get("ffmpeg_path", ""), out_path)
        return {"path": out_path, "duration": duration}

    # ------------------------------------------------------------ edge

    async def _edge_synth_with_retry(self, ctx, p: Dict[str, Any], text: str, out_path: str) -> None:
        import edge_tts
        retries = int(p.get("retries", 3))
        attempt = 0
        while True:
            attempt += 1
            try:
                comm = edge_tts.Communicate(text, voice=p["voice"], rate=p["rate"],
                                            pitch=p["pitch"], volume=p["volume"])
                await comm.save(out_path)
                if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                    raise RuntimeError("edge-tts 输出为空")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt > retries:
                    raise RuntimeError(f"edge-tts 第 {attempt} 次失败: {e}")
                await asyncio.sleep(min(30, 0.25 * (2 ** attempt)))

    async def _run_edge_single(self, ctx, inputs, params, text: str, progress) -> Dict[str, Any]:
        p = self._params(params)
        out_dir = self._out_dir(ctx["store"])
        progress(0.05)
        chunks = _split_long_text(text, p["chunk_chars"])
        ext = p["output_format"]
        paths: List[str] = []
        durations: List[float] = []
        weights: List[int] = []
        sem = asyncio.Semaphore(p["max_concurrent"])
        started = time.time()

        async def one(chunk: str) -> Dict[str, Any]:
            async with sem:
                path = self._uniq_path(out_dir, "edge_chunk", ext)
                await self._edge_synth_with_retry(ctx, p, chunk, path)
                dur = await _probe_duration(ctx, p["ffmpeg_path"], path)
                return {"path": path, "duration": dur, "chars": len(chunk)}

        results = await asyncio.gather(*(one(c) for c in chunks))
        for i, r in enumerate(results):
            paths.append(r["path"])
            if r["duration"]:
                durations.append(r["duration"])
            weights.append(r["chars"])
            progress(0.05 + 0.85 * (i + 1) / len(chunks))

        if len(paths) == 1:
            final_path = paths[0]
        else:
            final_path = self._uniq_path(out_dir, "edge_merged", ext)
            await _ffmpeg_concat(ctx, p["ffmpeg_path"], paths, final_path)
            for pth in paths:
                if pth != final_path:
                    try:
                        os.remove(pth)
                    except OSError:
                        pass

        total_dur = await _probe_duration(ctx, p["ffmpeg_path"], final_path) or round(sum(durations), 3)
        progress(1.0)
        asset = await self._save_audio(ctx["store"], final_path,
                                       {"engine": "edge", "voice": p["voice"], "rate": p["rate"],
                                        "chunks": len(chunks), "rt_s": round(time.time() - started, 1)},
                                       duration=total_dur)
        marks = self._build_marks(chunks, durations, weights, total_dur)
        report = {"engine": "edge", "voice": p["voice"], "rate": p["rate"],
                  "chunks": len(chunks), "duration": total_dur,
                  "chars": len(text), "rt_s": round(time.time() - started, 1),
                  "warnings": [f"分块合成 {len(chunks)} 段并合并"] if len(chunks) > 1 else []}
        return {"audio": asset, "duration": total_dur, "marks": marks, "report": report}

    @staticmethod
    def _build_marks(chunks: List[str], durations: List[float], weights: List[int],
                     total_dur: Optional[float]) -> Dict[str, Any]:
        """按实测块时长（缺则按字符占比估计）累计时间戳。"""
        segments = []
        cursor = 0.0
        for i, c in enumerate(chunks):
            if i < len(durations) and durations[i]:
                d = durations[i]
            else:
                d = (total_dur or 0.0) * weights[i] / max(sum(weights), 1)
            segments.append({"index": i, "text": c, "start": round(cursor, 3),
                             "end": round(cursor + d, 3), "duration": round(d, 3)})
            cursor += d
        return {"voice": None, "rate": None, "text_len": sum(len(c) for c in chunks),
                "segments": segments, "total_duration": round(cursor, 3)}

    # ------------------------------------------------------------ manual

    async def _run_manual(self, ctx, inputs, params, progress) -> Dict[str, Any]:
        audio_in = inputs.get("audio_in")
        if audio_in is None or not audio_in.path:
            raise RuntimeError("manual 模式需要连接音频输入（audio_in）")
        duration = await _probe_duration(ctx, str(params.get("ffmpeg_path", "") or ""), audio_in.path)
        progress(1.0)
        asset = await self._save_audio(ctx["store"], audio_in.path,
                                       {"engine": "manual", "source": "user"}, duration=duration)
        marks = {"engine": "manual", "segments": [], "total_duration": duration}
        report = {"engine": "manual", "duration": duration, "warnings": []}
        return {"audio": asset, "duration": duration or 0.0, "marks": marks, "report": report}

    # ------------------------------------------------------------ indextts

    _INDEX_TTS_SERVER_PROC = None
    # 端口判定：3900 = OmniVoice / IndexTTS2 侧车（/generate multipart，无令牌）；
    # 8791 = 旧版 voice_clone（/synthesize JSON+base64，带 X-FW-Local-Token 令牌门）
    _PORT_OMNIVOICE = 3900
    _PORT_VOICE_CLONE = 8791
    _PROBE_TIMEOUT = 3
    _START_DEADLINE = 300
    _BOUND_PORT_RE = re.compile(
        r"(?i)(?:running\s+on\s+(?:http://)?(?:127\.0\.0\.1|0\.0\.0\.0|localhost):"
        r"|listen(?:ing)?\s+on\s+(?:port\s+)?(?:[\w.:]+:)?"
        r"|bound\s+to\s+(?:port\s+)?(?:\d{4,5}|[\w.:]+:)"
        r"|port\s*[:=]\s*|uvicorn\s+port\s*)(\d{4,5})")

    @classmethod
    def _probe_omnivoice(cls, base: str) -> bool:
        """OmniVoice 健康检查：GET /engines（期望 200）。"""
        import urllib.request
        try:
            with urllib.request.urlopen(base + "/engines", timeout=cls._PROBE_TIMEOUT) as r:
                return r.status == 200
        except Exception:
            return False

    @classmethod
    def _probe_health(cls, base: str, headers: Optional[dict] = None) -> bool:
        """GET /health（可选令牌头）；200 即存活。"""
        import urllib.request
        try:
            req = urllib.request.Request(base + "/health", headers=dict(headers or {}))
            with urllib.request.urlopen(req, timeout=cls._PROBE_TIMEOUT) as r:
                return r.status == 200
        except Exception:
            return False

    @classmethod
    def _port_open(cls, base: str) -> bool:
        """TCP 端口可达性探测（1s 超时）；端口兜底仅在该端口确有服务时生效。"""
        import socket
        from urllib.parse import urlparse
        try:
            u = urlparse(base)
            host = u.hostname or "127.0.0.1"
            port = u.port or (443 if u.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except Exception:
            return False

    @classmethod
    def _classify_probe(cls, url: str, headers: Optional[dict] = None) -> Optional[Dict[str, Any]]:
        """单 URL 双协议探测；返回 {'base','proto'} 或 None。

        顺序：OmniVoice（/engines，默认 3900）→ voice_clone（/health，默认 8791）；
        自定义端口先 /engines 后 /health，以存活为准。
        端口兜底（旧服务只有 /synthesize 无健康端点）仅在 TCP 端口实际可达时生效。
        """
        url = url.strip().rstrip("/")
        if not url:
            return None
        port = 443 if url.startswith("https://") else 80
        m = re.search(r":(\d{4,5})/?$", url)
        if m:
            port = int(m.group(1))
        if port == cls._PORT_OMNIVOICE or cls._probe_omnivoice(url):
            if cls._probe_omnivoice(url):
                return {"base": url, "proto": "omnivoice"}
        if cls._probe_health(url, headers=headers):
            proto = "voice_clone" if port == cls._PORT_VOICE_CLONE else "omnivoice"
            return {"base": url, "proto": proto}
        # 兜底：/health 也可能 404（旧服务只有 /synthesize）——端口可达即视为 voice_clone
        if port == cls._PORT_VOICE_CLONE and cls._port_open(url):
            return {"base": url, "proto": "voice_clone"}
        return None

    @classmethod
    def _find_clone_server(cls, params: Dict[str, Any],
                           token: str = "") -> Optional[Dict[str, Any]]:
        """探测/拉起 IndexTTS2 常驻服务。

        返回 {'base': str, 'proto': 'omnivoice'|'voice_clone'} 或 None。
        探测顺序：显式 server_url/INDEX_TTS_URL → 3900 OmniVoice → 8791 voice_clone；
        auto_start_server 拉起后从服务日志读取真实 bound port（不硬编码轮询 3900，
        仅将 3900/8791 作为历史端口兼容兜底）。
        """
        import subprocess
        import urllib.request
        headers = {"X-FW-Local-Token": token} if token else {}
        explicit = str(params.get("server_url", "") or os.environ.get("INDEX_TTS_URL", "")).strip()
        candidates: List[str] = []
        for u in (explicit,
                  f"http://127.0.0.1:{cls._PORT_OMNIVOICE}",
                  f"http://127.0.0.1:{cls._PORT_VOICE_CLONE}"):
            if u and u not in candidates:
                candidates.append(u)
        for url in candidates:
            hit = cls._classify_probe(url, headers=headers)
            if hit:
                return hit
        if os.environ.get("INDEX_TTS_SKIP_START") == "1" or not params.get("auto_start_server", True):
            return None
        return cls._start_server(params, headers)

    @classmethod
    def _start_server(cls, params: Dict[str, Any], headers: dict) -> Optional[Dict[str, Any]]:
        """拉起 voice_clone/index_tts_server.py，读进程日志提取真实 bound port 再探测。"""
        import subprocess
        import urllib.request
        proj = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidates = []
        env_root = os.environ.get("INDEX_TTS_ROOT", "")
        if env_root:
            candidates.append(env_root)
        if getattr(sys, "frozen", False):
            base_ext = os.environ.get("APPDATA") or os.path.expanduser("~")
            candidates.append(os.path.join(base_ext, "FrameWeave", "indextts"))
            candidates.append(os.path.join(os.path.dirname(sys.executable), "indextts"))
        else:
            candidates.append(os.path.join(proj, "models", "index-tts-code"))
        venv_py, srv = "", ""
        if not getattr(sys, "frozen", False):
            vp = os.path.join(proj, "models", "index-tts-code", ".venv", "Scripts", "python.exe")
            sp = os.path.join(proj, "voice_clone", "index_tts_server.py")
            if os.path.isfile(vp) and os.path.isfile(sp):
                venv_py, srv = vp, sp
        if not venv_py:
            for cand in candidates:
                vp = os.path.join(cand, ".venv", "Scripts", "python.exe")
                sp = os.path.join(cand, "voice_clone", "index_tts_server.py")
                if os.path.isfile(vp) and os.path.isfile(sp):
                    venv_py, srv = vp, sp
                    break
        if not venv_py:
            from app.plugin import deps
            raise RuntimeError(deps.coded_message(
                "model_missing", "index-tts",
                hint="IndexTTS2 完整目录（checkpoints/.venv/voice_clone/index_tts_server.py）未找到；"
                     "可用环境变量 INDEX_TTS_ROOT 指定，或 INDEX_TTS_URL 指向已运行服务"))
        log_path = os.path.join(proj if not getattr(sys, "frozen", False) else os.path.dirname(sys.executable),
                                "data", "indextts_server.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        logf = open(log_path, "a", encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # 令牌透传给侧车（若旧服务读环境变量令牌门）
        if headers.get("X-FW-Local-Token"):
            env["FRAMEWEAVE_LOCAL_TOKEN"] = headers["X-FW-Local-Token"]
        proc = subprocess.Popen([venv_py, "-u", srv], env=env, stdout=logf, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000008)
        TTSNode._INDEX_TTS_SERVER_PROC = proc
        deadline = time.time() + cls._START_DEADLINE
        while time.time() < deadline:
            time.sleep(1)
            if proc.poll() is not None:
                raise RuntimeError("IndexTTS2 服务异常退出: %s" % proc.poll())
            # 1) 从服务日志解析真实 bound port（主路径，不硬编码轮询 3900）
            bound = cls._read_bound_port(log_path)
            if bound:
                if headers.get("X-FW-Local-Token"):
                    env["FRAMEWEAVE_LOCAL_TOKEN"] = headers["X-FW-Local-Token"]  # noqa: 幂等
                url = f"http://127.0.0.1:{bound}"
                hit = cls._classify_probe(url, headers=headers)
                if hit:
                    return hit
            # 2) 兼容兜底：历史端口 3900/8791（仅当日志未给出端口或新端口未就绪）
            for port in (cls._PORT_OMNIVOICE, cls._PORT_VOICE_CLONE):
                url = f"http://127.0.0.1:{port}"
                if cls._probe_health(url, headers=headers) or cls._probe_omnivoice(url):
                    return cls._classify_probe(url, headers=headers) or {"base": url, "proto": "voice_clone"}
        raise RuntimeError("IndexTTS2 服务启动超时")

    @classmethod
    def _read_bound_port(cls, log_path: str) -> Optional[int]:
        """从服务日志尾部解析真实监听端口（如 Uvicorn running on http://127.0.0.1:8791）。"""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                tail = f.read()[-65536:]
        except OSError:
            return None
        for m in reversed(list(cls._BOUND_PORT_RE.finditer(tail))):
            try:
                port = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 1024 <= port <= 65535:
                return port
        return None

    async def _run_indextts_single(self, ctx, inputs, params, text: str, progress) -> Dict[str, Any]:
        p = self._params(params)
        out_dir = self._out_dir(ctx["store"])
        progress(0.1)
        chunks = _split_long_text(text, p["chunk_chars"])
        paths: List[str] = []
        durations: List[float] = []
        chars: List[int] = []
        started = time.time()
        sem = asyncio.Semaphore(min(2, p["max_concurrent"]))

        async def one(chunk: str) -> Dict[str, Any]:
            async with sem:
                path = self._uniq_path(out_dir, "clone_chunk", "wav")
                await self._indextts_synth_with_retry(ctx, inputs, p, chunk, path)
                dur = await _probe_duration(ctx, p["ffmpeg_path"], path)
                return {"path": path, "duration": dur, "chars": len(chunk)}

        results = await asyncio.gather(*(one(c) for c in chunks))
        for i, r in enumerate(results):
            paths.append(r["path"])
            if r["duration"]:
                durations.append(r["duration"])
            chars.append(r["chars"])
            progress(0.1 + 0.8 * (i + 1) / len(chunks))

        if len(paths) == 1:
            final_path = paths[0]
        else:
            final_path = self._uniq_path(out_dir, "clone_merged", "wav")
            await _ffmpeg_concat(ctx, p["ffmpeg_path"], paths, final_path)
            for pth in paths:
                if pth != final_path:
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
        total_dur = await _probe_duration(ctx, p["ffmpeg_path"], final_path) or round(sum(durations), 3)
        progress(1.0)
        asset = await self._save_audio(ctx["store"], final_path,
                                       {"engine": "indextts", "lang": p["lang"],
                                        "duration_factor": p["duration_factor"],
                                        "chunks": len(chunks), "rt_s": round(time.time() - started, 1)},
                                       duration=total_dur)
        marks = self._build_marks(chunks, durations, chars, total_dur)
        report = {"engine": "indextts", "chunks": len(chunks), "duration": total_dur,
                  "rt_s": round(time.time() - started, 1),
                  "warnings": [f"分块合成 {len(chunks)} 段并合并"] if len(chunks) > 1 else []}
        return {"audio": asset, "duration": total_dur, "marks": marks, "report": report}

    async def _indextts_synth_once(self, ctx, params, text: str, ref_path: str, out_path: str,
                                   lang: str, duration_factor: float, base: str,
                                   proto: str, token: str = "") -> Dict[str, Any]:
        """按服务协议类型调用合成。

        proto=omnivoice：POST /generate（multipart，无令牌；约定 3900 端口）；
        proto=voice_clone：POST /synthesize（JSON+base64，带 X-FW-Local-Token 令牌门，约定 8791）。
        超时 600s；HTTPError/超时/网络错误分层报告。
        """
        import urllib.error
        import urllib.request
        if proto == "omnivoice":
            return await self._omnivoice_generate(ctx, params, text, ref_path, out_path,
                                                  duration_factor, base)
        return await self._voice_clone_synthesize(ctx, params, text, ref_path, out_path,
                                                  lang, duration_factor, base, token)

    async def _omnivoice_generate(self, ctx, params, text: str, ref_path: str, out_path: str,
                                  duration_factor: float, base: str) -> Dict[str, Any]:
        """OmniVoice：POST /generate multipart（无令牌）。失败给出明确错误。"""
        import urllib.error
        import urllib.request
        boundary = "----fw" + uuid.uuid4().hex[:16]
        fields = [("text", text), ("engine", "indextts2"), ("effect_preset", "raw"),
                  ("postprocess_output", "false"), ("max_chunk_chars", "0")]
        body = bytearray()
        for k, v in fields:
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
        with open(ref_path, "rb") as f:
            ref_bytes = f.read()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"{os.path.basename(ref_path)}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        body += ref_bytes
        body += f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(base + "/generate", data=bytes(body), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                wav_data = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"OmniVoice /generate HTTP {e.code}: "
                               f"{e.read().decode('utf-8', 'replace')[:300]}")
        except Exception as e:
            raise RuntimeError(f"OmniVoice /generate 网络/超时错误: {e}")
        if not wav_data or len(wav_data) < 1024:
            raise RuntimeError("OmniVoice 返回空音频")
        with open(out_path, "wb") as f:
            f.write(wav_data)
        if not _validate_wav(out_path):
            raise RuntimeError("OmniVoice 返回的 WAV 容器校验失败")
        return await self._post_speed(ctx, params, out_path, duration_factor)

    async def _voice_clone_synthesize(self, ctx, params, text: str, ref_path: str, out_path: str,
                                      lang: str, duration_factor: float, base: str,
                                      token: str = "") -> Dict[str, Any]:
        """voice_clone：POST /synthesize（JSON+base64，带 X-FW-Local-Token 令牌门）。"""
        import urllib.error
        import urllib.request
        with open(ref_path, "rb") as f:
            ref_b64 = base64.b64encode(f.read()).decode("ascii")
        payload = {"text": text, "lang": lang, "duration_factor": duration_factor,
                   "ref_audio_base64": ref_b64}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base + "/synthesize", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-FW-Local-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                wav_data = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"voice_clone /synthesize HTTP {e.code}: "
                               f"{e.read().decode('utf-8', 'replace')[:300]}")
        except Exception as e:
            raise RuntimeError(f"voice_clone /synthesize 网络/超时错误: {e}")
        if not wav_data or len(wav_data) < 1024:
            raise RuntimeError("voice_clone 返回空音频")
        with open(out_path, "wb") as f:
            f.write(wav_data)
        return await self._post_speed(ctx, params, out_path, duration_factor)

    async def _post_speed(self, ctx, params, wav_path: str, duration_factor: float) -> Dict[str, Any]:
        """IndexTTS2 无 rate 参数：合成后 ffmpeg 变速（atempo），接近原速时跳过。"""
        if abs(duration_factor - 1.0) < 0.01:
            return {"path": wav_path, "duration_factor": 1.0}
        ffmpeg = _resolve_ffmpeg(str(params.get("ffmpeg_path", "") or ""))
        tmp = wav_path + ".tmp.wav"
        cmd = [ffmpeg, "-y", "-i", wav_path, "-filter:a", f"atempo={duration_factor:.4f}", "-c:a", "pcm_s16le", tmp]
        out = await ctx["run_tracked"](cmd, timeout=120)
        if out.returncode != 0:
            raise RuntimeError("变速失败: %s" % out.stderr.decode("utf-8", "replace")[-300:])
        os.replace(tmp, wav_path)
        return {"path": wav_path, "duration_factor": duration_factor}

    async def _indextts_synth_with_retry(self, ctx, inputs_ref, params: Dict[str, Any],
                                         text: str, out_path: str) -> None:
        ref_asset = inputs_ref.get("audio_ref") if isinstance(inputs_ref, dict) else inputs_ref
        if ref_asset is None or not getattr(ref_asset, "path", ""):
            raise RuntimeError("声音克隆需要连接参考音频（audio_ref）")
        lang = str(params.get("lang", "ZH") or "ZH").upper()
        try:
            duration_factor = float(params.get("duration_factor", "1.0") or "1.0")
            duration_factor = max(0.5, min(2.0, duration_factor))
        except (TypeError, ValueError):
            duration_factor = 1.0
        token = self._ctx_token(ctx)
        server = self._find_clone_server(params, token=token)
        if server is None:
            from app.plugin import deps
            raise RuntimeError(deps.coded_message(
                "service_unavailable", "index-tts",
                hint="3900 OmniVoice / 8791 voice_clone 均不可达且自动拉起失败；请启动克隆服务，"
                     "或设置 INDEX_TTS_URL=http://127.0.0.1:3900"))
        base = server["base"]
        proto = server["proto"]
        retries = int(params.get("retries", 3) or 3)
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._indextts_synth_once(ctx, params, text, ref_asset.path, out_path,
                                                lang, duration_factor, base, proto, token)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt > retries:
                    raise RuntimeError(f"克隆合成第 {attempt} 次失败（{proto} @ {base}）: {e}")
                await asyncio.sleep(min(30, 0.25 * (2 ** attempt)))

    @staticmethod
    def _ctx_token(ctx) -> str:
        """取节点本地令牌（ctx.local_token）；旧接口回退环境变量。"""
        try:
            if hasattr(ctx, "get"):
                fn = ctx.get("local_token")
                if callable(fn):
                    return str(fn() or "")
        except Exception:
            pass
        return os.environ.get("FRAMEWEAVE_LOCAL_TOKEN", "")

    # ------------------------------------------------------------ cloud

    async def _cloud_synth_with_retry(self, ctx, p: Dict[str, Any], text: str, out_path: str) -> None:
        import urllib.error
        import urllib.request
        api_key = _resolve_secret(ctx, str(p.get("api_key", "") or ""))
        if not api_key:
            raise RuntimeError("cloud 引擎需要 API Key（参数面板填写 或 @secret:名称 引用）")
        base_url = str(p.get("base_url", "https://ameaaos.com/v1") or "https://ameaaos.com/v1").rstrip("/")
        model = str(p.get("cloud_model", "tts-1") or "tts-1")
        voice = str(p.get("cloud_voice", "alloy") or "alloy")
        try:
            speed = float(p.get("cloud_speed", "1.0") or "1.0")
        except (TypeError, ValueError):
            speed = 1.0
        payload = {"model": model, "input": text, "voice": voice,
                   "response_format": "mp3", "speed": speed}
        url = base_url + "/audio/speech"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + api_key)
        retries = int(p.get("retries", 3) or 3)
        attempt = 0
        while True:
            attempt += 1
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    audio = r.read()
                if not audio or len(audio) < 512:
                    raise RuntimeError("云端 TTS 返回空音频")
                with open(out_path, "wb") as f:
                    f.write(audio)
                return
            except urllib.error.HTTPError as e:
                if attempt > retries:
                    detail = e.read().decode("utf-8", "replace")[:500]
                    raise RuntimeError(f"云端 TTS 错误({e.code}): {detail}")
                await asyncio.sleep(min(30, 0.25 * (2 ** attempt)))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt > retries:
                    raise RuntimeError(f"云端 TTS 网络错误: {e}")
                await asyncio.sleep(min(30, 0.25 * (2 ** attempt)))

    async def _run_cloud_single(self, ctx, inputs, params, text: str, progress) -> Dict[str, Any]:
        return await self._run_vendor_single("cloud", ctx, inputs, params, text, progress)

    async def _run_vendor_single(self, engine: str, ctx, inputs, params, text: str,
                                 progress) -> Dict[str, Any]:
        """厂商 REST 引擎单条合成（cloud/aliyun/volcano 共用）：分块-并发-合并-资产骨架。

        分块策略与 edge/cloud 一致：按 chunk_chars 标点分块、段级并发 ≤ max_concurrent、
        多块时 ffmpeg concat 合并；时长探测失败不阻断。
        """
        p = self._params(params)
        out_dir = self._out_dir(ctx["store"])
        progress(0.1)
        chunks = _split_long_text(text, p["chunk_chars"])
        paths: List[str] = []
        durations: List[float] = []
        started = time.time()
        sem = asyncio.Semaphore(p["max_concurrent"])

        async def one(chunk: str) -> Dict[str, Any]:
            async with sem:
                path = self._uniq_path(out_dir, engine + "_chunk", p["output_format"])
                await self._synth_worker(ctx, engine, p, chunk, path)
                dur = await _probe_duration(ctx, p["ffmpeg_path"], path)
                return {"path": path, "duration": dur, "chars": len(chunk)}

        results = await asyncio.gather(*(one(c) for c in chunks))
        for i, r in enumerate(results):
            paths.append(r["path"])
            if r["duration"]:
                durations.append(r["duration"])
            progress(0.1 + 0.8 * (i + 1) / len(chunks))

        if len(paths) == 1:
            final_path = paths[0]
        else:
            final_path = self._uniq_path(out_dir, engine + "_merged", p["output_format"])
            await _ffmpeg_concat(ctx, p["ffmpeg_path"], paths, final_path)
            for pth in paths:
                if pth != final_path:
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
        total_dur = await _probe_duration(ctx, p["ffmpeg_path"], final_path) or round(sum(durations), 3)
        progress(1.0)
        voice = str(params.get("voice", ""))
        asset = await self._save_audio(ctx["store"], final_path,
                                       {"engine": engine, "voice": voice,
                                        "chunks": len(chunks),
                                        "rt_s": round(time.time() - started, 1)}, duration=total_dur)
        marks = self._build_marks(chunks, durations, [r["chars"] for r in results], total_dur)
        report = {"engine": engine, "voice": voice,
                  "chunks": len(chunks), "duration": total_dur,
                  "rt_s": round(time.time() - started, 1),
                  "warnings": [f"分块合成 {len(chunks)} 段并合并"] if len(chunks) > 1 else []}
        return {"audio": asset, "duration": total_dur, "marks": marks, "report": report}

    # ------------------------------------------------------------ aliyun（NLS + CosyVoice，C4）

    @staticmethod
    def _nls_fetch_token(ak: str, sk: str, token_url: str) -> str:
        """阿里云 RPC 签名（HMAC-SHA1）换取 NLS Token（nls-meta /oauth2/token CreateToken）。"""
        import base64
        import hashlib
        import hmac
        import urllib.parse
        import urllib.request
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "AccessKeyId": ak, "Action": "CreateToken", "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1", "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0", "Timestamp": ts, "Version": "2019-02-28",
        }

        def enc(v: str) -> str:
            return urllib.parse.quote(str(v), safe="")

        canonical = "&".join("%s=%s" % (enc(k), enc(params[k])) for k in sorted(params))
        string_to_sign = "GET&%2F&" + urllib.parse.quote(canonical, safe="")
        sig = base64.b64encode(hmac.new(
            (sk + "&").encode("utf-8"), string_to_sign.encode("utf-8"),
            hashlib.sha1).digest()).decode("utf-8")
        url = token_url + "?" + canonical + "&Signature=" + urllib.parse.quote(sig, safe="")
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 —— 网络/解析失败统一收口
            raise RuntimeError(f"aliyun NLS 获取 Token 失败: {e}") from e
        tok = ((data.get("Token") or {}).get("Id")) or ""
        if not tok:
            raise RuntimeError(
                "invalid: 阿里云 NLS 换取 Token 失败（检查 AccessKey ID/Secret）: %s"
                % json.dumps(data, ensure_ascii=False)[:300])
        return tok

    async def _aliyun_nls_synth_once(self, ctx, p: Dict[str, Any], text: str,
                                     out_path: str, token: str) -> None:
        """NLS REST：POST /stream/v1/tts（appkey+token，speech_rate/volume/pitch_rate）。"""
        import urllib.error
        import urllib.parse
        import urllib.request
        appkey = _resolve_secret(ctx, str(p.get("aliyun_appkey", "") or ""))
        if not appkey:
            raise RuntimeError("invalid: 需配置密钥——aliyun NLS 需要 AppKey（aliyun_appkey，可 @secret: 引用）")
        voice = resolve_vendor_voice("aliyun", p.get("voice", ""), "nls")
        if not voice:
            raise RuntimeError("invalid: aliyun NLS 需要 aliyun:* 音色（音色库或原生 NLS 音色名）")
        speed = _ratio_param(p.get("rate", "+0%"))
        volume = _ratio_param(p.get("volume", "+0%"), default=1.0, lo=0.0, hi=1.5)
        pitch = _ratio_param(p.get("pitch", "+0Hz"))
        query = {
            "appkey": appkey, "token": token,
            "format": p["output_format"], "sample_rate": "16000", "voice": voice,
            "speech_rate": str(max(-500, min(500, round((speed - 1.0) * 100)))),
            "pitch_rate": str(max(-500, min(500, round((pitch - 1.0) * 100)))),
            "volume": str(max(0, min(100, round(volume * 50)))),
            "text_type": "plain", "text": text,
        }
        url = str(p.get("aliyun_base_url", "") or "https://nls-gateway-cn-shanghai.aliyuncs.com").rstrip("/")
        url += "/stream/v1/tts?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                audio = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"aliyun NLS /stream/v1/tts HTTP {e.code}: "
                               f"{e.read().decode('utf-8', 'replace')[:300]}") from e
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"aliyun NLS /stream/v1/tts 网络/超时错误: {e}") from e
        if not audio or len(audio) < 512:
            raise RuntimeError("aliyun NLS 返回空音频")
        with open(out_path, "wb") as f:
            f.write(audio)

    async def _aliyun_cosy_synth_once(self, ctx, p: Dict[str, Any], text: str,
                                      out_path: str, api_key: str) -> None:
        """CosyVoice（DashScope）：POST text2audio/generation，SSE 事件内 base64 音频。"""
        import base64 as _b64
        import urllib.error
        import urllib.request
        voice = resolve_vendor_voice("aliyun", p.get("voice", ""), "cosyvoice")
        if not voice:
            raise RuntimeError("invalid: aliyun CosyVoice 需要 aliyun:* 音色（大模型音色）")
        speed = _ratio_param(p.get("rate", "+0%"), lo=0.5, hi=2.0)
        payload = {
            "model": str(p.get("aliyun_model", "cosyvoice-v2") or "cosyvoice-v2"),
            "input": {"text": text},
            "voice": voice,
            "parameters": {"format": p["output_format"], "sample_rate": 24000, "speed": speed},
        }
        base = str(p.get("aliyun_base_url", "") or "").rstrip("/") or "https://dashscope.aliyuncs.com"
        url = base + "/api/v1/services/aigc/text2audio/generation"
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + api_key)
        req.add_header("X-DashScope-SSE", "enable")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"aliyun CosyVoice text2audio HTTP {e.code}: "
                               f"{e.read().decode('utf-8', 'replace')[:300]}") from e
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"aliyun CosyVoice text2audio 网络/超时错误: {e}") from e
        # SSE 解析：逐行 data: {json}，output.audio 为 base64 音频（可多段拼接）；
        # 非 SSE（mock/代理直回二进制）→ 原文落盘。
        text_body = raw.decode("utf-8", "replace")
        audio_parts: List[bytes] = []
        for line in text_body.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except ValueError:
                continue
            out = obj.get("output") or {}
            if out.get("audio"):
                try:
                    audio_parts.append(_b64.b64decode(str(out["audio"])))
                except Exception:  # noqa: BLE001
                    continue
        if audio_parts:
            audio = b"".join(audio_parts)
        else:
            audio = raw
        if not audio or len(audio) < 512:
            raise RuntimeError("aliyun CosyVoice 返回空音频")
        with open(out_path, "wb") as f:
            f.write(audio)

    async def _aliyun_synth_with_retry(self, ctx, p: Dict[str, Any], text: str, out_path: str) -> None:
        mode = str(p.get("aliyun_mode", "nls") or "nls").lower()
        retries = int(p.get("retries", 3) or 3)
        attempt = 0
        while True:
            attempt += 1
            try:
                if mode == "nls":
                    token = _resolve_secret(ctx, str(p.get("aliyun_token", "") or ""))
                    if not token:
                        ak = _resolve_secret(ctx, str(p.get("aliyun_ak_id", "") or ""))
                        sk = _resolve_secret(ctx, str(p.get("aliyun_ak_secret", "") or ""))
                        if not ak or not sk:
                            raise RuntimeError(
                                "invalid: 需配置密钥——aliyun NLS 需要预签发 Token（aliyun_token）或 "
                                "AccessKey ID/Secret（aliyun_ak_id+aliyun_ak_secret），均可 @secret: 引用")
                        token = self._nls_fetch_token(ak, sk, str(p.get("aliyun_token_url", "")))
                    await self._aliyun_nls_synth_once(ctx, p, text, out_path, token)
                elif mode == "cosyvoice":
                    api_key = _resolve_secret(ctx, str(p.get("aliyun_token", "") or ""))
                    if not api_key:
                        api_key = _resolve_secret(ctx, str(p.get("api_key", "") or ""))
                    if not api_key:
                        raise RuntimeError(
                            "invalid: 需配置密钥——aliyun CosyVoice 需要 DashScope API Key"
                            "（aliyun_token 或 api_key，可 @secret: 引用）")
                    await self._aliyun_cosy_synth_once(ctx, p, text, out_path, api_key)
                else:
                    raise RuntimeError(f"invalid: 未知 aliyun_mode: {mode!r}（nls|cosyvoice）")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 重试网络类错误，配置类错误原样上抛
                if attempt > retries:
                    raise RuntimeError(f"aliyun {mode} 第 {attempt} 次失败: {e}") from e
                await asyncio.sleep(min(30, 0.25 * (2 ** attempt)))

    # ------------------------------------------------------------ volcano（豆包，C4）

    async def _volcano_synth_once(self, ctx, p: Dict[str, Any], text: str, out_path: str,
                                  appid: str, token: str) -> None:
        """豆包合成 REST：POST /api/v1/tts（speed_ratio/volume_ratio/pitch_ratio）。"""
        import urllib.error
        import urllib.request
        voice = resolve_vendor_voice("volcano", p.get("voice", ""))
        if not voice:
            raise RuntimeError("invalid: volcano 引擎需要 volcano:* 音色（音色库或 ICL 克隆 SpeakerID）")
        cluster = str(p.get("volcano_cluster", "volcano_tts") or "volcano_tts")
        encoding = p["output_format"]
        if encoding not in ("mp3", "wav", "pcm", "ogg"):
            encoding = "mp3"
        payload = {
            "app": {"appid": appid, "token": token, "cluster": cluster},
            "user": {"uid": "frameweave_user"},
            "audio": {
                "voice_type": voice, "encoding": encoding,
                "speed_ratio": _ratio_param(p.get("rate", "+0%")),
                "volume_ratio": _ratio_param(p.get("volume", "+0%"), default=1.0, lo=0.0, hi=1.5),
                "pitch_ratio": _ratio_param(p.get("pitch", "+0Hz")),
            },
            "request": {"reqid": uuid.uuid4().hex, "text": text,
                        "text_type": "plain", "operation": "query"},
        }
        url = str(p.get("volcano_base_url", "") or "https://openspeech.bytedance.com").rstrip("/")
        url += "/api/v1/tts"
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer;" + token)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                audio = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"volcano /api/v1/tts HTTP {e.code}: "
                               f"{e.read().decode('utf-8', 'replace')[:300]}") from e
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"volcano /api/v1/tts 网络/超时错误: {e}") from e
        # 业务错误以 JSON 返回（code == 3000 为成功；成功时为二进制音频，JSON 解析失败属正常）
        err = None
        try:
            parsed = json.loads(audio.decode("utf-8", "replace"))
            if isinstance(parsed, dict):
                code = parsed.get("code")
                if code not in (None, 3000):
                    raise RuntimeError(f"volcano /api/v1/tts 业务错误: {parsed}")
        except (UnicodeDecodeError, ValueError):
            pass
        if len(audio) < 512:
            raise RuntimeError("volcano /api/v1/tts 返回空音频")
        with open(out_path, "wb") as f:
            f.write(audio)

    async def _volcano_synth_with_retry(self, ctx, p: Dict[str, Any], text: str, out_path: str) -> None:
        appid = _resolve_secret(ctx, str(p.get("volcano_appid", "") or ""))
        token = _resolve_secret(ctx, str(p.get("volcano_token", "") or ""))
        if not appid:
            raise RuntimeError(
                "invalid: 需配置密钥——volcano 引擎需要 AppID（volcano_appid，可 @secret: 引用）")
        if not token:
            raise RuntimeError(
                "invalid: 需配置密钥——volcano 引擎需要 AccessToken（volcano_token，可 @secret: 引用）")
        retries = int(p.get("retries", 3) or 3)
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._volcano_synth_once(ctx, p, text, out_path, appid, token)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt > retries:
                    raise RuntimeError(f"volcano 第 {attempt} 次失败: {e}") from e
                await asyncio.sleep(min(30, 0.25 * (2 ** attempt)))

    # ------------------------------------------------------------ 批量（分段）

    async def _run_batch(self, ctx, inputs, params, segments_asset, engine: str, progress) -> Dict[str, Any]:
        from app import batchlib
        store = ctx["store"]
        data = store.read_json(segments_asset.id)
        if isinstance(data, dict):
            data = data.get("segments") or data.get("items") or []
        if not isinstance(data, list) or not data:
            raise RuntimeError("分段列表为空或格式不正确（需 SEGMENTS 数组）")
        p = self._params(params)
        voice = str(params.get("voice", "zh-CN-XiaoxiaoNeural"))
        segs = []
        for i, seg in enumerate(data):
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text") or seg.get("narration") or seg.get("source_text") or "").strip()
            if not text:
                continue
            segs.append({"index": i, "item_id": str(seg.get("id") or seg.get("segment_id") or f"seg_{i}"),
                         "text": text, "start": seg.get("start"), "end": seg.get("end"),
                         "voice": seg.get("voice") or voice})
        if not segs:
            raise RuntimeError("分段列表中没有可配音的文本（text/narration/source_text 字段）")
        out_dir = self._out_dir(store)
        ext = p["output_format"]
        started = time.time()
        cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints", str(ctx.get("node_id", "tts")) + ".json")

        async def worker(item: Dict[str, Any]) -> Dict[str, Any]:
            inp = item.get("input") or {}
            if not isinstance(inp, dict):
                raise ValueError("条目输入必须是对象")
            seg_voice = inp.get("voice") or voice
            path = self._uniq_path(out_dir, "seg", ext)
            # 段内长文本分块（edge/cloud 已支持块内并行；indextts 块内串行）
            res = await self._synth_worker(ctx, engine, {**p, "voice": seg_voice}, inp["text"], path,
                                           inputs=inputs)
            return {"path": res["path"], "duration": res.get("duration"),
                    "segment_id": inp.get("item_id"), "index": inp.get("index"),
                    "start": inp.get("start"), "end": inp.get("end")}

        # batchlib 信封：条目 input = 分段字段（seg 自带 index/item_id 供结果引用）
        items = [dict(s) for s in segs]
        batch = batchlib.new_batch(items, source="core/tts", options={"engine": engine, "voice": voice})
        batch = await batchlib.run_items(batch, worker, concurrency=p["max_concurrent"],
                                         checkpoint_path=cp, progress=progress,
                                         retries=0, resume=True)
        report_base = batchlib.summarize(batch)

        succeeded = [i for i in batch["items"] if i.get("status") == "succeeded"]
        if not succeeded:
            raise RuntimeError("批量合成全部失败：%s" % str(report_base.get("error_categories", {})))

        marks_items = []
        durations: List[float] = []
        for it in batch["items"]:
            r = it.get("result")
            rec = {"segment_id": (r or {}).get("segment_id") if isinstance(r, dict) else None
                   or it.get("item_id"),
                   "index": (r or {}).get("index") if isinstance(r, dict) else it.get("index"),
                   "status": it.get("status")}
            if isinstance(r, dict):
                rec["index"] = r.get("index") if r.get("index") is not None else it.get("index")
                rec["path"] = r.get("path")
                rec["start"] = r.get("start")
                rec["end"] = r.get("end")
                rec["duration"] = r.get("duration")
                if r.get("duration"):
                    durations.append(r["duration"])
            if it.get("error"):
                rec["error"] = it.get("error")
            marks_items.append(rec)

        merge = bool(params.get("merge_audio", True))
        if merge and len(succeeded) > 1:
            final_path = self._uniq_path(out_dir, "seg_merged", ext)
            await _ffmpeg_concat(ctx, p["ffmpeg_path"], [i["result"]["path"] for i in succeeded], final_path)
            total_dur = await _probe_duration(ctx, p["ffmpeg_path"], final_path)
            first_id = succeeded[0]["result"].get("path")
        elif len(succeeded) == 1:
            final_path = succeeded[0]["result"]["path"]
            total_dur = succeeded[0]["result"].get("duration")
            first_id = final_path
        else:
            final_path = succeeded[0]["result"]["path"]
            total_dur = succeeded[0]["result"].get("duration")
            first_id = final_path
        if not total_dur:
            total_dur = round(sum(durations), 3)

        asset = await self._save_audio(store, final_path,
                                       {"engine": engine, "voice": voice, "batch": True,
                                        "merged": merge, "segments": len(segs),
                                        "rt_s": round(time.time() - started, 1)}, duration=total_dur)
        marks = {"engine": engine, "voice": voice, "batch": True, "segments": marks_items,
                 "total_duration": total_dur}
        report = {"engine": engine, "voice": voice, "batch": True,
                  "summary": {k: report_base[k] for k in ("total", "succeeded", "failed", "pending")},
                  "error_categories": report_base.get("error_categories", {}),
                  "duration": total_dur, "rt_s": round(time.time() - started, 1),
                  "warnings": []}
        return {"audio": asset, "duration": total_dur, "marks": marks, "report": report}


def _validate_wav(path: str) -> bool:
    """校验 WAV 容器头完整性（IndexTTS2 返回音频容器校验）。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            n = w.getnframes()
            rate = w.getframerate()
            ch = w.getnchannels()
            width = w.getsampwidth()
            return n > 0 and rate > 0 and ch > 0 and width > 0
    except Exception:
        return False
