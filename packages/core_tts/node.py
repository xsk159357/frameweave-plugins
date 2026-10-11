"""TTS 节点：四类配音引擎。

- edge: edge-tts 免费多音色（M1 默认）
- manual: 用户导入音频文件
- indextts: 本地声音克隆（预留，M2 接入）
- cloud: 云端高质量音色（预留）
"""
from __future__ import annotations
import json
import os
import sys
import uuid
from typing import Any, Dict

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app.registry import register

# Edge-TTS 常用中文音色
EDGE_VOICES = [
    # 中文
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunjianNeural",
    "zh-CN-YunxiNeural", "zh-CN-YunxiaNeural", "zh-CN-YunyangNeural",
    "zh-CN-liaoning-XiaobeiNeural", "zh-CN-shaanxi-XiaoniNeural",
    "zh-CN-XiaomoNeural", "zh-CN-XiaoruiNeural", "zh-CN-XiaoshuangNeural",
    "zh-CN-XiaoxuanNeural", "zh-CN-XiaoxueNeural", "zh-CN-XiaoyanNeural",
    "zh-CN-XiaoyouNeural", "zh-CN-XiaozhenNeural", "zh-CN-YunfengNeural",
    "zh-CN-YunhaoNeural", "zh-CN-YunjianNeural", "zh-CN-YunzeNeural",
    "zh-TW-HsiaoChenNeural", "zh-TW-HsiaoYuNeural", "zh-TW-YunJheNeural",
    "zh-HK-HiuGaaiNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
    # 英语
    "en-US-JennyNeural", "en-US-AriaNeural", "en-US-GuyNeural",
    "en-US-ChristopherNeural", "en-US-EricNeural", "en-US-MichelleNeural",
    "en-GB-SoniaNeural", "en-GB-RyanNeural", "en-AU-NatashaNeural",
    # 日韩
    "ja-JP-NanamiNeural", "ja-JP-KeitaNeural", "ja-JP-AoiNeural",
    "ko-KR-SunHiNeural", "ko-KR-InJoonNeural",
]


def _read_script_text(store, asset: Asset) -> str:
    if asset is None:
        return ""
    if asset.path and asset.path.endswith(".json"):
        data = store.read_json(asset.id) or {}
        if isinstance(data, dict):
            return data.get("text", "")
        return str(data)
    if asset.path:
        try:
            with open(asset.path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
    return ""


class TTSNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/tts",
            title="TTS 配音",
            category="音频",
            description="文字转语音：Edge免费 / 本地克隆 / 云端音色 / 人工配音",
            inputs=[
                PortSpec(name="script", type=PortType.SCRIPT, label="文案", required=True),
                PortSpec(name="audio_in", type=PortType.AUDIO, label="人工音频", required=False),
                PortSpec(name="audio_ref", type=PortType.AUDIO, label="克隆参考音", required=False),
            ],
            outputs=[
                PortSpec(name="audio", type=PortType.AUDIO, label="配音音频"),
                PortSpec(name="marks", type=PortType.JSON, label="时间戳标记"),
            ],
            params=[
                PortSpec(name="direct_text", type=PortType.STRING, label="直接输入文案",
                         default="", widget="text", description="留空时使用上游文案输入"),
                PortSpec(name="engine", type=PortType.STRING, label="引擎", default="edge",
                         widget="select", options=["edge", "manual", "indextts", "cloud"]),
                PortSpec(name="voice", type=PortType.STRING, label="音色", default="zh-CN-XiaoxiaoNeural",
                         widget="select", options=EDGE_VOICES),
                PortSpec(name="rate", type=PortType.STRING, label="语速", default="+0%",
                         widget="text", description="edge-tts 语速如 +10%"),
                PortSpec(name="pitch", type=PortType.STRING, label="音调", default="+0Hz", widget="text"),
                PortSpec(name="output_format", type=PortType.STRING, label="输出格式", default="mp3",
                         widget="select", options=["mp3", "wav", "ogg"]),
                PortSpec(name="lang", type=PortType.STRING, label="克隆语言", default="ZH",
                         widget="select", options=["ZH", "EN", "JA", "ES", "AR"]),
                PortSpec(name="duration_factor", type=PortType.STRING, label="语速倍率", default="1.0",
                         widget="text", description="0.5~2.0, 1.0 为原速"),
                PortSpec(name="server_url", type=PortType.STRING, label="克隆服务地址", default="http://127.0.0.1:8791",
                         widget="text", description="IndexTTS2 常驻服务"),
                PortSpec(name="auto_start_server", type=PortType.BOOL, label="自动拉起服务", default=True,
                         widget="toggle", description="首次用时自动启动克隆服务"),
                PortSpec(name="api_key", type=PortType.STRING, label="API Key",
                         widget="text", description="云端 TTS 用户自带 Key（ameaao 中转）"),
                PortSpec(name="base_url", type=PortType.STRING, label="中转地址",
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec(name="cloud_model", type=PortType.STRING, label="云端模型",
                         default="tts-1", widget="text"),
                PortSpec(name="cloud_voice", type=PortType.STRING, label="云端音色",
                         default="alloy", widget="text", description="OpenAI 音色: alloy/echo/fable/onyx/nova/shimmer"),
                PortSpec(name="cloud_speed", type=PortType.STRING, label="云端语速", default="1.0",
                         widget="text", description="0.25~4.0"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        engine = params.get("engine", "edge")
        store = ctx["store"]
        script_asset = inputs.get("script")
        direct = str(params.get("direct_text", "") or "").strip()
        text = direct if direct else _read_script_text(store, script_asset)
        if not text.strip():
            raise RuntimeError("文案为空，无法配音")

        out_dir = os.path.join(store.files_dir, "tts")
        os.makedirs(out_dir, exist_ok=True)

        if engine == "manual":
            audio_in = inputs.get("audio_in")
            if audio_in is None or not audio_in.path:
                raise RuntimeError("人工配音模式需要连接音频输入")
            asset = Asset(id="", kind=PortType.AUDIO.value, path=audio_in.path,
                          meta={"engine": "manual", "source": "user"})
            store.save_asset(asset)
            return {"audio": asset, "marks": store.save_asset(
                Asset(id="", kind=PortType.JSON.value, meta={"engine": "manual"}),
                payload={"segments": []})}

        if engine == "edge":
            # 唯一文件名：并发批量时同进程同秒也会生成不同文件（M1 修复，避免互相覆盖）
            out_path = os.path.join(out_dir, f"tts_{os.getpid()}_{uuid.uuid4().hex[:8]}.{params.get('output_format', 'mp3')}")
            rate = str(params.get("rate", "+0%"))
            pitch = str(params.get("pitch", "+0Hz"))
            voice = params.get("voice", "zh-CN-XiaoxiaoNeural")
            import edge_tts
            import asyncio
            communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
            await communicate.save(out_path)
            asset = Asset(id="", kind=PortType.AUDIO.value, path=out_path,
                          meta={"engine": "edge", "voice": voice, "rate": rate})
            store.save_asset(asset)
            marks = store.save_asset(Asset(id="", kind=PortType.JSON.value, meta={"engine": "edge"}),
                                     payload={"voice": voice, "rate": rate, "text_len": len(text)})
            return {"audio": asset, "marks": marks}

        if engine == "indextts":
            return await self._run_indextts(ctx, inputs, params, text, out_dir)

        if engine == "cloud":
            return await self._run_cloud(ctx, inputs, params, text, out_dir)

        raise RuntimeError(f"未知引擎: {engine}")

    # ---- IndexTTS2 本地声音克隆（HTTP 调常驻服务） ----
    _INDEX_TTS_SERVER_PROC = None

    @staticmethod
    def _find_clone_server():
        """查找/启动 IndexTTS2 常驻服务。返回 base_url 或 None。"""
        import subprocess, time, urllib.request
        base = os.environ.get("INDEX_TTS_URL", "http://127.0.0.1:8791")
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                return base
        except Exception:
            pass
        if os.environ.get("INDEX_TTS_SKIP_START") == "1":
            return None
        # 探测 IndexTTS2 环境位置（源码/打包 external/环境变量）
        proj = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidates = []
        env_tts_root = os.environ.get("INDEX_TTS_ROOT", "")
        if env_tts_root:
            candidates.append(env_tts_root)
        if getattr(sys, "frozen", False):
            # 打包模式：外部附加目录（%APPDATA%/FrameWeave/indextts 或安装目录 extras）
            base_ext = os.environ.get("APPDATA") or os.path.expanduser("~")
            candidates.append(os.path.join(base_ext, "FrameWeave", "indextts"))
            candidates.append(os.path.join(os.path.dirname(sys.executable), "indextts"))
        else:
            candidates.append(os.path.join(proj, "models", "index-tts-code"))
        venv_py = ""
        srv = ""
        if not getattr(sys, "frozen", False):
            # 源码模式：venv 在 models/index-tts-code，服务脚本在 voice_clone/
            prose = os.path.join(proj, "voice_clone", "index_tts_server.py")
            vp = os.path.join(proj, "models", "index-tts-code", ".venv", "Scripts", "python.exe")
            if os.path.isfile(vp) and os.path.isfile(prose):
                venv_py, srv = vp, prose
        if not venv_py:
            for cand in candidates:
                vp = os.path.join(cand, ".venv", "Scripts", "python.exe")
                sp = os.path.join(cand, "voice_clone", "index_tts_server.py")
                if os.path.isfile(vp) and os.path.isfile(sp):
                    venv_py, srv = vp, sp
                    break
        if not venv_py:
            raise RuntimeError("IndexTTS2 环境未找到（源码: models/index-tts-code；打包: 附加 indextts 目录）。"
                               "可用环境变量 INDEX_TTS_ROOT 指定")
        logf = open(os.path.join(proj if not getattr(sys, "frozen", False) else os.path.dirname(sys.executable), "data", "indextts_server.log"), "a", encoding="utf-8")
        env = dict(os.environ)
        # V1 令牌：把主进程本地令牌传给子服务（仅单值，非环境全文）
        _token = ctx.get("local_token")() if hasattr(ctx, "get") else os.environ.get("FRAMEWEAVE_LOCAL_TOKEN", "")
        if _token:
            env["FRAMEWEAVE_LOCAL_TOKEN"] = _token
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # M10 修复：PYTHONPATH 不再写死本机路径，改为环境变量 FW_PATCH_DIR（未设置则不注入）
        _patch = os.environ.get("FW_PATCH_DIR", "")
        if _patch:
            env["PYTHONPATH"] = _patch
        proc = subprocess.Popen(
            [venv_py, "-u", srv],
            env=env, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000008,
        )
        TTSNode._INDEX_TTS_SERVER_PROC = proc
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(2)
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    return base
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("IndexTTS2 服务异常退出: %s" % proc.poll())
        raise RuntimeError("IndexTTS2 服务启动超时")

    async def _run_indextts(self, ctx, inputs, params, text, out_dir):
        import base64, time, urllib.request
        store = ctx["store"]
        ref_asset = inputs.get("audio_ref")
        if ref_asset is None or not ref_asset.path:
            raise RuntimeError("声音克隆需要连接参考音频（audio_ref）")
        lang = str(params.get("lang", "ZH") or "ZH").upper()
        try:
            duration_factor = float(params.get("duration_factor", "1.0") or "1.0")
            duration_factor = max(0.5, min(2.0, duration_factor))
        except (TypeError, ValueError):
            duration_factor = 1.0

        base = self._find_clone_server()
        with open(ref_asset.path, "rb") as f:
            ref_b64 = base64.b64encode(f.read()).decode("ascii")

        payload = {
            "text": text,
            "lang": lang,
            "duration_factor": duration_factor,
            "ref_audio_base64": ref_b64,
        }
        body = json.dumps(payload).encode("utf-8")
        t0 = time.time()
        headers = {"Content-Type": "application/json"}
        _token = ctx.get("local_token")() if hasattr(ctx, "get") else os.environ.get("FRAMEWEAVE_LOCAL_TOKEN", "")
        if _token:
            headers["X-FW-Local-Token"] = _token     # V1 令牌鉴权
        req = urllib.request.Request(base + "/synthesize", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                wav_data = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError("克隆服务错误(%s): %s" % (e.code, detail))
        if not wav_data or len(wav_data) < 1024:
            raise RuntimeError("克隆服务返回空音频")
        out_path = os.path.join(out_dir, "clone_%d.wav" % int(time.time() * 1000))
        with open(out_path, "wb") as f:
            f.write(wav_data)
        asset = Asset(id="", kind=PortType.AUDIO.value, path=out_path,
                      meta={"engine": "indextts", "lang": lang,
                            "duration_factor": duration_factor, "rt_s": round(time.time() - t0, 1)})
        store.save_asset(asset)
        marks = store.save_asset(Asset(id="", kind=PortType.JSON.value, meta={"engine": "indextts"}),
                                 payload={"lang": lang, "text_len": len(text), "rt_s": round(time.time() - t0, 1)})
        return {"audio": asset, "marks": marks}

    async def _run_cloud(self, ctx, inputs, params, text, out_dir):
        """云端音色：OpenAI 兼容 /audio/speech（ameaao 中转, 用户自带 Key）。"""
        import base64, time, urllib.request, urllib.error
        api_key = str(params.get("api_key", "") or "").strip()
        if api_key.startswith("@secret:"):
            ref = api_key.split(":", 1)[1]
            # P3 迁移：优先 ctx.resolve_secret；缺省回退主进程保险箱
            resolver = ctx.get("resolve_secret") if hasattr(ctx, "get") else None
            if resolver:
                api_key = resolver(ref) or ""
            else:
                from app.secrets import read_secret
                api_key = read_secret(ref) or ""
        if not api_key:
            raise RuntimeError("cloud 引擎需要 API Key（参数面板填写 或 @secret:名称 引用）")
        base_url = str(params.get("base_url", "https://ameaaos.com/v1")).strip() or "https://ameaaos.com/v1"
        model = str(params.get("cloud_model", "tts-1")).strip() or "tts-1"
        voice = str(params.get("cloud_voice", "alloy")).strip() or "alloy"
        speed = 1.0
        try:
            speed = float(params.get("cloud_speed", "1.0") or "1.0")
        except (TypeError, ValueError):
            speed = 1.0
        payload = {
            "model": model, "input": text, "voice": voice,
            "response_format": "mp3", "speed": speed,
        }
        url = base_url.rstrip("/") + "/audio/speech"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + api_key)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                audio = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError("云端 TTS 错误(%s): %s" % (e.code, detail))
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError("云端 TTS 网络错误: %s（请检查中转可达性与 Key）" % e)
        if not audio or len(audio) < 512:
            raise RuntimeError("云端 TTS 返回空音频")
        out_path = os.path.join(out_dir, "cloud_%d.mp3" % int(time.time() * 1000))
        with open(out_path, "wb") as f:
            f.write(audio)
        asset = Asset(id="", kind=PortType.AUDIO.value, path=out_path,
                      meta={"engine": "cloud", "voice": voice, "model": model,
                            "rt_s": round(time.time() - t0, 1)})
        store = ctx["store"]
        store.save_asset(asset)
        marks = store.save_asset(Asset(id="", kind=PortType.JSON.value, meta={"engine": "cloud"}),
                                 payload={"voice": voice, "model": model, "text_len": len(text)})
        return {"audio": asset, "marks": marks}
