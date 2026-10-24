"""核心插件：ASR 语音转字幕 + 对白提取（增强版 v3，C2/A5 补齐）。

引擎（engine 参数，auto 自动降级并写入 report）：
- local：faster-whisper（CTranslate2）本地转写；离线、免费、支持 VAD/词级时间戳/语言自动检测。
  model 取 tiny/base/small/medium/large-v3 或本地模型目录；HF 缓存命中即离线可用。
- remote：OpenAI 兼容 /audio/transcriptions（api_key + base_url）；模型名 remote_model（默认 whisper-1）。
- gemini：Gemini input_audio 直听（60s 窗切分，逐窗 generateContent，含 inline audio）；
  直接从语音识别"角色 | 台词"（角色如 旁白/男子/女子），输出带 role 的对白；
  需 api_key（支持 @secret: 引用）与 base_url（默认 generativelanguage.googleapis.com）。

C2/A5 角色字段（dialogue[{start,end,role,text}]，role ∈ narrator/speaker/unknown）：
- gemini 引擎：解析 `[mm:ss - mm:ss] 角色 | 台词` → role 取自角色文本（旁白→narrator，其余说话→speaker）；
- local/remote（Whisper 句级）：启发式 role 标注（引号/问句/叹词/称呼→speaker，叙述性长句→narrator，
  时间贴近同角色延续），确定性可复现、无 API 依赖；
- 零 key 兜底：engine=auto 本地优先（faster-whisper 离线），无需任何密钥不崩；
  engine=gemini 显式且无 key → 明确 contract 报错（与 remote 无 key 一致）。

输出契约（v3 新增 dialogue 端口）：
- subtitles: SUBTITLE 行 {index,start,end,text,confidence?,words?,role?}（秒，向后兼容）；
- dialogues: JSON [{start,end,role,text,raw_role?,confidence?,source?}]（A5 对白，含角色）；
- srt: STRING 文本；report: JSON（engine_used/fallback 链/角色统计）。

任一步骤失败显式报错，不伪造空字幕。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

LOCAL_SIZES = ("tiny", "base", "small", "medium", "large-v3", "large-v2", "large", "distil-small.en", "distil-medium.en", "distil-large-v3")


def _fmt_srt_ts(t: float) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def build_srt(rows: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for i, r in enumerate(rows, 1):
        lines.append(str(i))
        lines.append(f"{_fmt_srt_ts(float(r['start']))} --> {_fmt_srt_ts(float(r['end']))}")
        lines.append(str(r["text"]).strip())
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- remote (OpenAI 兼容)
def _multipart_upload(url: str, api_key: str, fields: Dict[str, str],
                      file_field: str, filename: str, file_bytes: bytes,
                      mime: str, timeout: float = 300.0) -> Any:
    import urllib.request
    boundary = "----fw_asr_" + uuid.uuid4().hex
    buf = bytearray()
    def _add_field(name: str, value: str) -> None:
        buf.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))
    for k, v in fields.items():
        _add_field(k, v)
    buf.extend(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode("utf-8"))
    buf.extend(file_bytes)
    buf.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(url, data=bytes(buf), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")


# ---------------------------------------------------------------- C2/A5：role 启发式标注（Whisper 句级）

_ROLE_SPEAKER_MARKS = (
    "他说", "她说", "说道", "喊道", "问道", "回答", "回答:", "呐喊", "怒吼", "笑道",
    "说过", "讲道", "开口", "插话", "接话", "感叹", "惊呼", "低声说", "大声说", "喃喃",
)
_ROLE_NARRATOR_MARKS = (
    "旁白", "解说", "字幕", "旁白：", "解说：",
)
# 问候/指称/疑问/感叹等口语特征 → 判定为说话人（speaker）
_ROLE_SPEAKER_PAT = re.compile(
    r"[！!？?~～]$"          # 末尾叹/问（句号/分号不算，叙述句会误判）
    r"|^(喂|哎|嗨|你好|哈喽|兄弟|哥们|姐妹|各位|各位观众|观众|老铁|大家|你们|我们|你|我)\s*[,，:：]"
    r"|[,，:：](这是|那|你看|你们|我|大伙|兄弟姐妹|大哥|大姐|老师|老板)"
)
# 叙述性长句 / 明显解说腔 → narrator
_ROLE_NARRATOR_PAT = re.compile(
    r"^(镜头|画面|此时|此刻|随后|接着|然后|就这样|这就是|只见|原来|然而|最终|经过|在这)"
    r"|(的|了|着|地)一个(男人|女人|少年|女孩|身影|身影追)|(的故事|的故事。|背后|因而)"
)


def heuristic_role(text: str, prev_role: Optional[str] = None, gap: Optional[float] = None) -> str:
    """Whisper 句级启发式角色标注：确定性、零 API。

    - 显式提及说话/引用 → speaker；
    - 旁白/解说 字面 → narrator；
    - 末尾叹/问/称呼/口语指称 → speaker；
    - 叙述性开篇/长句 → narrator；
    - 与上句 gap 极小且同语义延续 → 沿用 prev_role；
    - 其余 → unknown。
    """
    t = (text or "").strip()
    if not t:
        return "unknown"
    if any(m in t for m in _ROLE_NARRATOR_MARKS):
        return "narrator"
    for m in _ROLE_SPEAKER_MARKS:
        if m in t:
            return "speaker"
    if _ROLE_SPEAKER_PAT.search(t):
        return "speaker"
    if _ROLE_NARRATOR_PAT.search(t):
        return "narrator"
    if prev_role and prev_role != "unknown" and gap is not None and gap < 0.8:
        return prev_role
    return "unknown"


def _whisper_gap(rows: List[Dict[str, Any]], i: int) -> Optional[float]:
    if i == 0 or not rows:
        return None
    return round(float(rows[i].get("start", 0)) - float(rows[i - 1].get("end", 0)), 3)


def annotate_roles(rows: List[Dict[str, Any]], with_role: bool = True) -> List[Dict[str, Any]]:
    """就地/拷贝为 subtitle 行补 role（确定性启发式）。"""
    out: List[Dict[str, Any]] = []
    prev = None
    for i, r in enumerate(rows):
        row = dict(r)
        if with_role:
            gap = _whisper_gap(rows, i)
            role = heuristic_role(str(row.get("text", "")), prev, gap)
            row["role"] = role
            prev = role
        out.append(row)
    return out


def to_dialogues(rows: List[Dict[str, Any]], source: str = "") -> List[Dict[str, Any]]:
    """subtitle/对白行 → A5 dialogue[{start,end,role,text}] 契约（role ∈ narrator/speaker/unknown）。"""
    out: List[Dict[str, Any]] = []
    for r in rows:
        role = str(r.get("role") or "unknown")
        if role not in ("narrator", "speaker"):
            role = "unknown"
        d: Dict[str, Any] = {
            "start": round(float(r.get("start", 0)), 3),
            "end": round(float(r.get("end", 0)), 3),
            "role": role,
            "text": str(r.get("text", "")).strip(),
        }
        raw_role = r.get("raw_role")
        if raw_role:
            d["raw_role"] = str(raw_role)
        if r.get("confidence") is not None:
            d["confidence"] = r.get("confidence")
        if source:
            d["source"] = source
        out.append(d)
    return out


# ---------------------------------------------------------------- C2/A5：Gemini input_audio 直听（60s 窗）

_GEMM_LINE_RE = re.compile(
    r"^\s*\[?\s*(?:(\d{1,2}):(\d{2})\s*[-–—~]\s*(\d{1,2}):(\d{2}))?"
    r"\s*\]?\s*(?:[|｜]\s*)?(?:【([^】]+)】|([^|｜:：，,]+)(?:\s*[|｜:：]\s*))?(.+?)\s*$"
)


def _parse_time(s: str) -> float:
    """mm:ss → 秒（容错）。"""
    s = (s or "").strip()
    if not s:
        return 0.0
    parts = s.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _map_role(raw_role: str) -> str:
    r = (raw_role or "").strip()
    if not r:
        return "unknown"
    if r in ("旁白", "旁白:", "解说", "解说:", "narrator", "Narration"):
        return "narrator"
    return "speaker"


def parse_gemini_dialogue(text: str, window_start: float = 0.0,
                          window_end: Optional[float] = None) -> List[Dict[str, Any]]:
    """解析 Gemini 直听返回：`[mm:ss - mm:ss] 角色 | 台词`（逐行，绝对时间）。

    时间缺失时按窗口内均匀插值（确定性 fallback），绝不让下游拿到空时间戳。
    """
    lines = [ln for ln in (text or "").splitlines() if ln and ln.strip() and "|" in ln]
    if not lines:
        # 无竖线：尝试按行直接当台词（角色 unknown）
        lines = [ln for ln in (text or "").splitlines() if ln and ln.strip()]
    out: List[Dict[str, Any]] = []
    for ln in lines:
        m = _GEMM_LINE_RE.match(ln)
        if not m:
            continue
        start = _parse_time(f"{int(m.group(1))}:{m.group(2)}") if m.group(1) else 0.0
        end = _parse_time(f"{int(m.group(3))}:{m.group(4)}") if m.group(3) else 0.0
        raw_role = (m.group(5) or m.group(6) or "").strip()
        body = (m.group(7) or "").strip()
        if not body:
            continue
        if body in ("无语音", "无语音。", "无声", "无对话", "语音空"):
            continue
        out.append({
            "start": round(start + window_start, 3),
            "end": round((end + window_start) if (end > 0) else (start + 5.0 + window_start), 3),
            "raw_role": raw_role or "",
            "role": _map_role(raw_role),
            "text": body,
            "source": "gemini",
        })
    if not out:
        return out
    # 对解析到的时间做窗口夹取；全部时间都缺失（零起始）时按窗口均匀插值
    wend = window_end if window_end is not None else (window_start + 60.0)
    has_real_time = any(o["start"] > window_start for o in out)
    if has_real_time:
        for o in out:
            o["start"] = max(window_start, min(o["start"], wend))
            o["end"] = max(o["start"] + 0.3, min(o["end"], wend))
        return out
    n = len(out)
    span = max(0.3, (wend - window_start) / max(n, 1))
    for i, o in enumerate(out):
        o["start"] = round(window_start + i * span, 3)
        o["end"] = round(o["start"] + span, 3)
    return out


def _chat_gemini_audio(audio_b64: str, mime: str, prompt: str, api_key: str,
                       base_url: str, model: str, temperature: float = 0.2,
                       max_tokens: int = 2048) -> str:
    """Gemini generateContent 直听音频（input_audio 等价：inline_data audio）。"""
    import urllib.request
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": audio_b64}},
            ],
        }],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url.rstrip('/')}/models/{model}:generateContent{sep}key={api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("gemini: 响应解析失败: " + json.dumps(data, ensure_ascii=False)[:300]) from e


def _ffmpeg_probe_duration(src_path: str) -> Optional[float]:
    """ffprobe 取媒体时长（秒）；失败返回 None（不阻断，fallback 60s 窗）。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    if not r.get("ready"):
        return None
    import subprocess
    try:
        p = subprocess.run(
            [r["path"], "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", src_path],
            capture_output=True, timeout=30)
        if p.returncode == 0:
            return float((p.stdout or b"").decode("utf-8", "replace").strip())
    except Exception:  # noqa: BLE001
        pass
    return None


def _ffmpeg_cut_window(src_path: str, start: float, end: float,
                       out_dir: str, idx: int, sample_rate: int = 16000) -> Optional[str]:
    """ffmpeg 切 60s 窗 → 16k 单声道 wav；失败返回 None（该窗跳过，不阻断全片）。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    if not r.get("ready"):
        return None
    import subprocess
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"win_{idx:03d}.wav")
    cmd = [r["path"], "-v", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
           "-i", src_path, "-ac", "1", "-ar", str(sample_rate),
           "-c:a", "pcm_s16le", "-y", out]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=180)
        if p.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 44:
            return out
    except Exception:  # noqa: BLE001
        pass
    return None


def _looks_gemini(base_url: str) -> bool:
    b = (base_url or "").strip().lower()
    return ("generativelanguage" in b) or b.startswith("https://generativelanguage")


def _gemini_base(base_url: str) -> str:
    """base_url 归一：空/默认 OpenAI 地址 → Gemini 官方根。"""
    b = (base_url or "").strip().rstrip("/")
    if not b or b == "https://api.openai.com/v1":
        return "https://generativelanguage.googleapis.com/v1beta"
    return b


class ASRNode(NodeBase):
    """语音转字幕 + 对白提取。type_id: core/asr"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/asr",
            title="语音转字幕",
            category="分析",
            description="ASR 字幕 + A5 对白提取（含 role）：faster-whisper 本地转录 / OpenAI 兼容远程 / Gemini input_audio 直听 60s 窗；输出 SUBTITLE 与 dialogue[{start,end,role,text}]",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=False),
                PortSpec("audio", PortType.AUDIO, label="音频(可选)", required=False),
            ],
            outputs=[
                PortSpec("subtitles", PortType.SUBTITLE, label="字幕", required=True),
                PortSpec("dialogues", PortType.JSON, label="对白(含角色)", required=False,
                         description="A5 dialogue[{start,end,role,text}]；role=narrator/speaker/unknown"),
                PortSpec("srt", PortType.STRING, label="SRT 文本", required=False),
                PortSpec("report", PortType.JSON, label="转写报告", required=False),
            ],
            params=[
                PortSpec("engine", PortType.STRING, label="引擎", default="auto",
                         widget="select", options=["auto", "local", "remote", "gemini"]),
                PortSpec("model", PortType.STRING, label="本地模型", default="small",
                         widget="select", options=["tiny", "base", "small", "medium", "large-v3"]),
                PortSpec("language", PortType.STRING, label="语言", default="zh",
                         widget="select", options=["zh", "en", "ja", "ko", "auto"]),
                PortSpec("device", PortType.STRING, label="设备", default="auto",
                         widget="select", options=["auto", "cpu", "cuda"]),
                PortSpec("compute_type", PortType.STRING, label="计算精度", default="int8",
                         widget="select", options=["int8", "float16", "float32"]),
                PortSpec("vad", PortType.BOOL, label="VAD 过滤静音", default=True, widget="toggle"),
                PortSpec("word_timestamps", PortType.BOOL, label="词级时间戳", default=False, widget="toggle"),
                PortSpec("api_key", PortType.STRING, label="API Key", required=False, default="",
                         widget="password", description="gemini/remote 引擎用；支持 @secret:名称 引用保险箱"),
                PortSpec("base_url", PortType.STRING, label="中转地址",
                         default="https://generativelanguage.googleapis.com/v1beta",
                         widget="text",
                         description="openai 兼容 /audio/transcriptions 或 Gemini generateContent 根"),
                PortSpec("remote_model", PortType.STRING, label="远程模型", default="whisper-1", widget="text",
                         description="remote 引擎的 OpenAI 兼容模型名；gemini 引擎请用 gemini_model"),
                PortSpec("gemini_model", PortType.STRING, label="Gemini 模型", default="gemini-1.5-flash",
                         widget="text", description="gemini 引擎的 generateContent 模型名"),
                PortSpec("window_seconds", PortType.FLOAT, label="Gemini 直听窗(秒)", default=60.0,
                         widget="number", description="gemini 引擎按此窗长切分逐窗直听"),
                PortSpec("add_roles", PortType.BOOL, label="标注角色(role)", default=True, widget="toggle",
                         description="为字幕行补 role 并输出 dialogues 端口（本地 Whisper 用启发式）"),
                PortSpec("enable_diarization", PortType.BOOL, label="说话人区分", default=False,
                         widget="toggle",
                         description="启用 sherpa-onnx 说话人区分（TK 流程）；为字幕行补 speaker 字段（speaker_0/1...）"),
                PortSpec("diarization_model_dir", PortType.STRING, label="Diarization 模型目录", default="",
                         widget="text",
                         description="sherpa-onnx 说话人区分模型目录（含 model.onnx/segmentation 与 embedding 模型）；留空自动探测 models/diarization"),
            ],
            version="3.1.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        audio_asset = inputs.get("audio")
        if video_asset is None and audio_asset is None:
            raise RuntimeError("invalid: 需要 video 或 audio 输入")
        src_asset = audio_asset if audio_asset is not None else video_asset
        src_path = getattr(src_asset, "path", "") or ""
        if not src_path or not os.path.exists(src_path):
            raise RuntimeError(f"invalid: 输入媒体文件不存在: {src_path}")

        engine = str(params.get("engine", "auto") or "auto")
        model = str(params.get("model", "small") or "small").strip() or "small"
        language = str(params.get("language", "zh") or "zh")
        device = str(params.get("device", "auto") or "auto")
        compute_type = str(params.get("compute_type", "int8") or "int8")
        vad = bool(params.get("vad", True))
        word_ts = bool(params.get("word_timestamps", False))
        add_roles = bool(params.get("add_roles", True))
        api_key_raw = str(params.get("api_key", "") or "").strip()
        base_url = str(params.get("base_url", "") or "").strip()
        remote_model = str(params.get("remote_model", "whisper-1") or "whisper-1").strip() or "whisper-1"
        gemini_model = str(params.get("gemini_model", "gemini-1.5-flash") or "gemini-1.5-flash").strip() or "gemini-1.5-flash"
        window_seconds = float(params.get("window_seconds", 60.0) or 60.0)
        enable_diarization = bool(params.get("enable_diarization", False))
        diarization_model_dir = str(params.get("diarization_model_dir", "") or "").strip()

        progress = ctx.get("progress") or (lambda p: None)
        warnings: List[str] = []
        engine_used = ""
        rows: List[Dict[str, Any]] = []
        lang_info: Dict[str, Any] = {}

        if engine in ("auto", "local"):
            try:
                rows, lang_info, warnings = await self._transcribe_local(
                    src_path, model, language, device, compute_type, vad, word_ts, progress)
                engine_used = "local"
            except Exception as e:  # noqa: BLE001
                if engine == "local":
                    raise RuntimeError(f"local: 本地转写失败: {e}") from e
                warnings.append(f"local 失败（{type(e).__name__}: {e}），尝试远程")
                engine_used = ""

        if engine_used == "" and engine in ("auto", "remote") and not _looks_gemini(base_url):
            api_key = _resolve_secret(ctx, api_key_raw)
            if not api_key:
                raise RuntimeError(
                    "contract: 远程引擎需要 api_key（可填 @secret:名称）；或安装 faster-whisper 走 local")
            rows, lang_info, warnings = await self._transcribe_remote(
                src_path, base_url, api_key, remote_model, language, progress)
            engine_used = "remote"

        if engine_used == "" and engine in ("gemini", "auto"):
            api_key = _resolve_secret(ctx, api_key_raw)
            if not api_key:
                if engine == "gemini":
                    raise RuntimeError(
                        "contract: gemini 引擎需要 api_key（可填 @secret:名称）；或改 engine=local 走 faster-whisper")
                warnings.append("gemini 无 key，跳过")
            else:
                try:
                    rows, lang_info, warnings = await self._transcribe_gemini(
                        src_path, api_key, _gemini_base(base_url), gemini_model,
                        window_seconds, progress)
                    engine_used = "gemini"
                except Exception as e:  # noqa: BLE001
                    if engine == "gemini":
                        raise RuntimeError(f"gemini: 直听失败: {e}") from e
                    warnings.append(f"gemini 失败（{type(e).__name__}: {e}），尝试本地/远程")
                    engine_used = ""

        if engine_used == "" and engine in ("auto", "remote") and _looks_gemini(base_url):
            # base_url 指向 generativelanguage 且 gemini 无 key 失败 → 明确 contract 错误
            raise RuntimeError(
                "contract: base_url 是 Gemini（generativelanguage），但无 gemini 引擎可用——"
                "请提供 api_key 或用 engine=local/remote 指定 OpenAI 兼容端点")

        if not engine_used:
            raise RuntimeError(
                "contract: 无可用的 ASR 引擎——安装 faster-whisper（local）或提供 api_key（gemini/remote）")

        # G2：可选说话人区分（sherpa-onnx SpeakerDiarization；模型缺失/失败 → 降级警告不阻断）
        diar_warnings: List[str] = []
        if enable_diarization:
            try:
                from app.plugin import deps
                mdir = diarization_model_dir or ""
                if not mdir:
                    mr = deps.resolve_model("sherpa-onnx-diarization",
                                            params={"model_dir": diarization_model_dir})
                    mdir = str(mr.get("path") or "") if mr.get("ready") else ""
                if not mdir or not os.path.isdir(mdir):
                    diar_warnings.append(
                        "enable_diarization=开但模型目录不可用（sherpa-onnx 说话人区分模型未就绪），跳过 speaker 标注")
                else:
                    rows, diar_warnings = await self._diarize_speakers(
                        src_path, mdir, rows, diar_warnings)
            except Exception as e:  # noqa: BLE001
                diar_warnings.append(f"说话人区分失败（{type(e).__name__}: {e}），跳过 speaker 标注")

        # C2/A5：角色标注（本地/远程句级用启发式；gemini 已在解析时带 role）
        if add_roles and engine_used != "gemini":
            rows = annotate_roles(rows, with_role=True)
        if add_roles:
            src_lbl = "gemini" if engine_used == "gemini" else "whisper"
            dialogues = to_dialogues(rows, source=src_lbl)
        else:
            dialogues = []

        role_stats: Dict[str, int] = {}
        for d in dialogues:
            role_stats[d["role"]] = role_stats.get(d["role"], 0) + 1
        if diar_warnings:
            warnings = list(warnings) + diar_warnings

        store = ctx["store"]

        # G2：diarization 语义沿用到 dialogues（speaker 字段并入）
        if enable_diarization and not diar_warnings:
            for d in dialogues:
                sp = str(d.get("speaker") or "")
                if sp:
                    d["raw_role"] = d.get("raw_role") or d.get("role")
                    d["role"] = "speaker"
        sub_asset = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value,
                  meta={"count": len(rows), "engine": engine_used, "model": model,
                        "language": lang_info.get("language", language)}),
            payload=rows)
        dlg_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(dialogues), "engine": engine_used, "roles": role_stats}),
            payload=dialogues)
        srt_text = build_srt(rows)
        srt_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "srt", "engine": engine_used}),
            payload={"text": srt_text})
        report = {
            "engine_used": engine_used,
            "engine_requested": engine,
            "model": (remote_model if engine_used == "remote" else model),
            "gemini_model": gemini_model if engine_used == "gemini" else "",
            "language": lang_info.get("language", language),
            "language_probability": lang_info.get("language_probability"),
            "media_duration": lang_info.get("duration"),
            "segment_count": len(rows),
            "dialogue_count": len(dialogues),
            "role_stats": role_stats,
            "word_count": sum(len(r.get("words") or []) for r in rows),
            "vad": vad,
            "word_timestamps": word_ts,
            "window_seconds": window_seconds if engine_used == "gemini" else None,
            "media": {"path": src_path, "kind": getattr(src_asset, "kind", "")},
            "warnings": warnings,
            "diarization": {"enabled": enable_diarization,
                            "model_dir": diarization_model_dir or "",
                            "ok": bool(enable_diarization and not diar_warnings),
                            "warnings": diar_warnings},
            "dependency": self._dep_blob(model, engine_used),
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"engine": engine_used}), payload=report)
        return {"subtitles": sub_asset, "dialogues": dlg_asset,
                "srt": srt_asset, "report": report_asset}

    @staticmethod
    def _dep_blob(model: str, engine_used: str) -> Dict[str, Any]:
        """报告 dependency 字段：本地引擎记录模型就绪状态；远程引擎记录远程模型名。"""
        from app.plugin import deps
        blob: Dict[str, Any] = {"engine": engine_used, "model": model}
        if engine_used == "local":
            mid = "faster-whisper-" + str(model).strip() if not os.path.isdir(str(model)) else str(model).strip()
            mr = deps.resolve_model(mid, params={"model": model})
            blob.update({"ready": bool(mr.get("ready")), "source": mr.get("source", ""),
                         "path": mr.get("path", ""), "warnings": list(mr.get("warnings") or []),
                         "repair_hint": mr.get("repair_hint", "")})
        else:
            blob.update({"ready": True, "source": "remote"})
        return blob

    async def _transcribe_local(self, src_path: str, model: str, language: str, device: str,
                                compute_type: str, vad: bool, word_ts: bool,
                                progress) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
        import asyncio
        warnings: List[str] = []

        def _load():
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                from app.plugin import deps
                raise RuntimeError(deps.coded_message(
                    "dependency_missing", "faster-whisper",
                    hint="pip install faster-whisper；或改 asr_engine=remote 用云端转写")) from e
            dev = device
            if dev == "auto":
                dev = "cpu"  # 服务器默认 CPU+int8 稳定；GPU 用户显式选 cuda + float16
            ct = compute_type
            if dev == "cuda" and ct == "int8":
                ct = "int8_float16"
            # 统一模型解析：本地模型目录（DATA_DIR/models / env / HF 缓存）优先于默认下载
            resolved = model
            try:
                from app.plugin import deps
                mid = "faster-whisper-" + str(model).strip() if not os.path.isdir(str(model)) else ""
                mr = deps.resolve_model(mid or str(model).strip(), params={"model": model})
                if mr["ready"] and mr.get("path") and os.path.isdir(mr["path"]):
                    resolved = mr["path"]
                elif mid and not mr["ready"] and mr.get("warnings"):
                    pass  # 未就绪不阻止默认下载（warnings 透传 report）
            except Exception:  # noqa: BLE001  解析失败不阻断本地转写
                pass
            try:
                return WhisperModel(resolved, device=dev, compute_type=ct)
            except Exception as e:  # noqa: BLE001
                if dev != "cpu":
                    warnings.append(f"{dev} 加载失败（{e}），回退 cpu/int8")
                    return WhisperModel(resolved, device="cpu", compute_type="int8")
                raise

        model_obj = await asyncio.to_thread(_load)
        lang = None if language == "auto" else language
        segments_iter, info = model_obj.transcribe(
            src_path, language=lang, vad_filter=vad, word_timestamps=word_ts,
            beam_size=5, condition_on_previous_text=False)
        rows: List[Dict[str, Any]] = []
        segs = list(segments_iter)
        total = max(len(segs), 1)
        for i, seg in enumerate(segs):
            row: Dict[str, Any] = {
                "index": i,
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": str(seg.text).strip(),
                "confidence": None,
            }
            try:
                row["confidence"] = round(float(seg.avg_logprob), 4)
            except Exception:  # noqa: BLE001
                pass
            if word_ts and getattr(seg, "words", None):
                row["words"] = [{
                    "word": str(w.word).strip(),
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                } for w in seg.words]
            rows.append(row)
            progress(min(0.9, 0.05 + 0.85 * (i + 1) / total))
        lang_info = {
            "language": getattr(info, "language", language),
            "language_probability": round(float(info.language_probability), 4)
            if getattr(info, "language_probability", None) is not None else None,
            "duration": round(float(info.duration), 3) if getattr(info, "duration", None) else None,
        }
        progress(1.0)
        return rows, lang_info, warnings

    async def _transcribe_remote(self, src_path: str, base_url: str, api_key: str,
                                 model: str, language: str,
                                 progress) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
        import asyncio
        ext = os.path.splitext(src_path)[1].lower().lstrip(".") or "mp3"
        mime = {
            "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "aac": "audio/aac",
            "ogg": "audio/ogg", "flac": "audio/flac", "mp4": "video/mp4", "webm": "video/webm",
            "mov": "video/quicktime",
        }.get(ext, "application/octet-stream")
        url = base_url.rstrip("/") + "/audio/transcriptions"
        fields: Dict[str, str] = {"model": model, "response_format": "verbose_json"}
        if language != "auto":
            fields["language"] = language
        with open(src_path, "rb") as f:
            data = f.read()

        def _upload_once(fmt: str) -> Any:
            f2 = dict(fields, response_format=fmt)
            try:
                return _multipart_upload(url, api_key, f2, "file",
                                         os.path.basename(src_path), data, mime, timeout=300.0)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"remote: /audio/transcriptions 请求失败: {e}") from e

        def _upload() -> Any:
            try:
                return _upload_once("verbose_json")
            except Exception:  # noqa: BLE001
                # 部分中转仅支持 json（仅 text）；降级重试一次
                return _upload_once("json")

        progress(0.2)
        resp = await asyncio.to_thread(_upload)

        def _est(s: str) -> Tuple[float, float]:
            # 无时间戳时的粗估：0.3s/字符
            dur = max(1.0, len(s) * 0.3)
            return dur

        rows: List[Dict[str, Any]] = []
        segs = resp.get("segments") or []
        if segs:
            for i, sg in enumerate(segs):
                rows.append({
                    "index": i,
                    "start": round(float(sg.get("start", 0)), 3),
                    "end": round(float(sg.get("end", 0)), 3),
                    "text": str(sg.get("text", "")).strip(),
                    "confidence": sg.get("avg_logprob"),
                })
        else:
            text = str(resp.get("text", "") or "").strip()
            if text:
                dur = _est(text)
                rows.append({"index": 0, "start": 0.0, "end": round(dur, 3), "text": text})
        if not rows:
            raise RuntimeError("contract: 远程返回空转写（响应：" + json.dumps(resp, ensure_ascii=False)[:300] + "）")
        lang_info = {"language": resp.get("language", language),
                     "language_probability": resp.get("language_probability"),
                     "duration": resp.get("duration")}
        progress(1.0)
        return rows, lang_info, []

    async def _transcribe_gemini(self, src_path: str, api_key: str, base_url: str,
                                 model: str, window_seconds: float,
                                 progress) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
        """Gemini input_audio 直听：60s（可配）窗切分 → 逐窗 generateContent → 角色对白。

        台词时间以窗内 mm:ss 为基准解析（相对窗起点）；窗与窗之间 gap 保留为独立对白。
        切窗/探测失败跳过该窗不阻断；全部失败 → 明确报错。
        """
        import asyncio
        warnings: List[str] = []
        dur = await asyncio.to_thread(_ffmpeg_probe_duration, src_path)
        total = dur if (dur and dur > 0) else 0.0
        win = max(10.0, float(window_seconds or 60.0))
        if total <= 0:
            # 时长未知 → 单窗按 win 处理（从 0 起）
            start_points = [0.0]
            total = win
        else:
            start_points = [t for t in range(0, int(total) + 1, max(1, int(win)))]
        prompt = (
            "请把这段音频中的对话逐句识别出来，按以下三列格式输出：\n"
            "[开始时间 - 结束时间] 角色 | 台词\n"
            "时间使用 mm:ss（相对音频起点）。角色只能是：旁白 / 男子 / 女子 / 其他，"
            "无法判断时用「其他」。台词只输出语音原文，不要加引号。\n"
            "示例：\n[00:00 - 00:03] 旁白 | 故事发生在一个雨夜\n"
            "[00:03 - 00:07] 男子 | 你到底是谁？\n"
            "如果音频无人声，输出：无语音\n"
        )
        rows: List[Dict[str, Any]] = []
        n = len(start_points)
        for i, sp in enumerate(start_points):
            ep = min(sp + win, total)
            wav = await asyncio.to_thread(_ffmpeg_cut_window, src_path, sp, ep,
                                          ctx_workdir(src_path), i)
            if not wav:
                warnings.append(f"窗 {i}（{sp:.1f}s-{ep:.1f}s）切取失败，跳过")
                progress(min(0.95, 0.05 + 0.9 * (i + 1) / max(n, 1)))
                continue
            with open(wav, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode("ascii")
            try:
                resp_text = await asyncio.to_thread(
                    _chat_gemini_audio, audio_b64, "audio/wav", prompt,
                    api_key, base_url, model, 0.2, 2048)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"窗 {i} gemini 调用失败（{type(e).__name__}: {e}），跳过")
                progress(min(0.95, 0.05 + 0.9 * (i + 1) / max(n, 1)))
                continue
            if "无语音" in resp_text:
                progress(min(0.95, 0.05 + 0.9 * (i + 1) / max(n, 1)))
                continue
            parsed = parse_gemini_dialogue(resp_text, window_start=sp, window_end=ep)
            if not parsed:
                warnings.append(f"窗 {i} 未解析出对白（原文 {len(resp_text)} 字），跳过")
                progress(min(0.95, 0.05 + 0.9 * (i + 1) / max(n, 1)))
                continue
            for j, d in enumerate(parsed):
                rows.append({
                    "index": len(rows),
                    "start": d["start"],
                    "end": d["end"],
                    "text": d["text"],
                    "role": d["role"],
                    "raw_role": d.get("raw_role", ""),
                    "confidence": None,
                    "source": "gemini",
                })
            progress(min(0.95, 0.05 + 0.9 * (i + 1) / max(n, 1)))
        rows.sort(key=lambda r: r["start"])
        for idx, r in enumerate(rows):
            r["index"] = idx
        if not rows:
            raise RuntimeError(
                "contract: gemini 直听未产出任何对白（全部窗口失败/无语音）——"
                "请检查 api_key 与模型名，或改用 engine=local")
        lang_info = {"language": language_guess(rows), "language_probability": None, "duration": total}
        progress(1.0)
        return rows, lang_info, warnings

    # ---------------------------------------------------------------- G2：说话人区分（sherpa-onnx）
    async def _diarize_speakers(self, media_path: str, model_dir: str,
                                rows: List[Dict[str, Any]],
                                warnings: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
        """sherpa-onnx SpeakerDiarization：为字幕行补 speaker 字段（按时间中心重叠匹配）。

        - 模型目录需含 segmentation（如 sherpa-onnx-pyannote-segmentation-3-0）与
          embedding（如 nemo_en_titanet_large）两大类 onnx 文件（模型中心 diarization 惯例）。
        - 分段返回 [{start,end,speaker}]；字幕行时间中心落在分段内 → 标 speaker_n（按出现顺序重映射）。
        - 失败以 warnings 降级（不抛错），返回原 rows。
        """
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return rows, warnings + [
                "sherpa-onnx 未安装（pip install sherpa-onnx），说话人区分跳过——字幕仍由 ASR 引擎产出"]
        try:
            seg_path = os.path.join(model_dir, "segmentation.onnx")
            emb_dir = os.path.join(model_dir, "embedding")
            seg_ok = os.path.isfile(seg_path)
            emb_ok = os.path.isdir(emb_dir)
            if not seg_ok:
                # 兼容平铺目录：model.onnx + 任意 embedding onnx
                seg_path = os.path.join(model_dir, "model.onnx")
                seg_ok = os.path.isfile(seg_path)
                emb_ok = any(f.endswith(".onnx") for f in os.listdir(model_dir)
                             if f != os.path.basename(seg_path))
            if not seg_ok or not emb_ok:
                return rows, warnings + [
                    f"说话人区分模型目录不完整（缺 segmentation/embedding onnx）：{model_dir}"]
            from sherpa_onnx import (SpeakerDiarization,  # noqa: E402
                                     SpeakerDiarizationConfig,
                                     SegModelConfig, EmbeddingModelConfig)

            def _seg_models(md: str) -> SegModelConfig:
                if os.path.isfile(os.path.join(md, "segmentation.onnx")):
                    return SegModelConfig(model=os.path.join(md, "segmentation.onnx"))
                return SegModelConfig(model=os.path.join(md, "model.onnx"))

            def _emb_models(md: str) -> EmbeddingModelConfig:
                cands = [os.path.join(md, "embedding", f) for f in os.listdir(
                    os.path.join(md, "embedding")) if f.endswith(".onnx")] \
                    if os.path.isdir(os.path.join(md, "embedding")) else []
                if not cands:
                    cands = [os.path.join(md, f) for f in os.listdir(md)
                             if f.endswith(".onnx") and f not in ("segmentation.onnx", "model.onnx")]
                if not cands:
                    raise RuntimeError("embedding onnx 未找到")
                return EmbeddingModelConfig(model=max(cands, key=os.path.getsize))

            cfg = SpeakerDiarizationConfig(
                segmentation=_seg_models(model_dir),
                embedding=_emb_models(model_dir),
                min_duration_on=0.2, min_duration_off=0.5)
            diar = SpeakerDiarization(cfg)
            wav, sample_rate = self._load_wav(media_path)
            if wav is None:
                return rows, warnings + ["说话人区分：音频解码失败，跳过"]
            segs = diar.process(wav, sample_rate)
            if not segs:
                return rows, warnings + ["说话人区分：未检测到说话人分段"]
            # 分段 → 时间中心匹配
            mapping = {}
            for s in segs:
                start = float(getattr(s, "start", 0.0))
                end = float(getattr(s, "end", 0.0))
                spk = str(getattr(s, "speaker", "speaker_0") or "speaker_0")
                if spk not in mapping:
                    mapping[spk] = f"speaker_{len(mapping)}"
                for r in rows:
                    c = (float(r.get("start", 0)) + float(r.get("end", 0))) / 2.0
                    if start <= c < end:
                        r["speaker"] = mapping[spk]
            return rows, warnings + [f"说话人区分完成：{len(set(r.get('speaker') for r in rows if r.get('speaker')))} 人"]
        except Exception as e:  # noqa: BLE001
            return rows, warnings + [f"说话人区分失败（{type(e).__name__}: {e}），跳过 speaker 标注"]

    @staticmethod
    def _load_wav(path: str):
        """加载音频为 (float32 单声道数组, sample_rate)；失败返回 None。"""
        import asyncio
        import subprocess as _sp
        ff = "ffmpeg"
        try:
            from app.plugin import deps
            r = deps.resolve_binary("ffmpeg", env=os.environ)
            if r.get("ready"):
                ff = r["path"]
        except Exception:  # noqa: BLE001
            pass
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), f"fw_asr_diar_{os.getpid()}_{uuid.uuid4().hex[:8]}.wav")
        try:
            proc = _sp.run([ff, "-y", "-i", path, "-ac", "1", "-ar", "16000", "-f", "f32le", tmp],
                           capture_output=True, timeout=300)
            if proc.returncode != 0 or not os.path.exists(tmp):
                return None
            import array
            with open(tmp, "rb") as f:
                raw = f.read()
            fmt = "f" if sys.byteorder == "little" else ">f"
            samples = array.array(fmt, raw)
            return samples, 16000
        except Exception:  # noqa: BLE001
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


def ctx_workdir(src_path: str) -> str:
    """窗口切取落点：媒体同目录 .fw_asr_win/（可写且被沙箱允许）。"""
    d = os.path.join(os.path.dirname(os.path.abspath(src_path)), ".fw_asr_win")
    os.makedirs(d, exist_ok=True)
    return d


def language_guess(rows: List[Dict[str, Any]]) -> str:
    """极简语言猜测（无 API）：len(text) 均 < 2 的 ASCII 字符占比高 → en，否则 zh。"""
    t = "".join(str(r.get("text", "")) for r in rows)
    ascii_chars = sum(1 for c in t if c.isascii() and c.isalpha())
    non_ascii = sum(1 for c in t if not c.isascii())
    if not t:
        return "zh"
    return "en" if ascii_chars > non_ascii else "zh"