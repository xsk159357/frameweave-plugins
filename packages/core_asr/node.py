"""核心插件：ASR 语音转字幕（增强版 v2）。

引擎（engine 参数，auto 自动降级并写入 report）：
- local：faster-whisper（CTranslate2）本地转写；离线、免费、支持 VAD/词级时间戳/语言自动检测。
  model 取 tiny/base/small/medium/large-v3 或本地模型目录；HF 缓存命中即离线可用。
- remote：OpenAI 兼容 /audio/transcriptions（api_key + base_url）；模型名 remote_model（默认 whisper-1）。

增强点（相对解说猫 A5/Gemini 60s 窗直听）：
- 本地路径零 API：句级时间戳、word 级时间戳（供对话细切）、VAD 过滤静音、语言自动识别；
- auto 引擎：本地优先，失败才尝试 remote，全程可审计（report.engine_used/fallback 链）；
- 输出契约统一：subtitle 行 {index,start,end,text,confidence?,words?}（秒）；
  srt 文本资产可直接喂下游；任一步骤失败显式报错，不伪造空字幕。
"""
from __future__ import annotations

import json
import os
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


class ASRNode(NodeBase):
    """语音转字幕。type_id: core/asr"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/asr",
            title="语音转字幕",
            category="分析",
            description="ASR 字幕：faster-whisper 本地转写（离线免费）或 OpenAI 兼容远程；支持 VAD/词级时间戳",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=False),
                PortSpec("audio", PortType.AUDIO, label="音频(可选)", required=False),
            ],
            outputs=[
                PortSpec("subtitles", PortType.SUBTITLE, label="字幕", required=True),
                PortSpec("srt", PortType.STRING, label="SRT 文本", required=False),
                PortSpec("report", PortType.JSON, label="转写报告", required=False),
            ],
            params=[
                PortSpec("engine", PortType.STRING, label="引擎", default="auto",
                         widget="select", options=["auto", "local", "remote"]),
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
                         widget="password", description="远程引擎用；支持 @secret:名称 引用保险箱"),
                PortSpec("base_url", PortType.STRING, label="中转地址", default="https://api.openai.com/v1",
                         widget="text"),
                PortSpec("remote_model", PortType.STRING, label="远程模型", default="whisper-1", widget="text"),
            ],
            version="2.0.0",
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
        api_key_raw = str(params.get("api_key", "") or "").strip()
        base_url = str(params.get("base_url", "https://api.openai.com/v1") or "").strip()
        remote_model = str(params.get("remote_model", "whisper-1") or "whisper-1").strip() or "whisper-1"

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

        if engine_used == "" and engine in ("auto", "remote"):
            api_key = _resolve_secret(ctx, api_key_raw)
            if not api_key:
                raise RuntimeError(
                    "contract: 远程引擎需要 api_key（可填 @secret:名称）；或安装 faster-whisper 走 local")
            if "generativelanguage" in base_url:
                raise RuntimeError("contract: Gemini 无 /audio/transcriptions 接口，请用 local（faster-whisper）")
            rows, lang_info, warnings = await self._transcribe_remote(
                src_path, base_url, api_key, remote_model, language, progress)
            engine_used = "remote"

        if not engine_used:
            raise RuntimeError(
                "contract: 无可用的 ASR 引擎——安装 faster-whisper（local）或提供 api_key（remote）")

        store = ctx["store"]
        sub_asset = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value,
                  meta={"count": len(rows), "engine": engine_used, "model": model,
                        "language": lang_info.get("language", language)}),
            payload=rows)
        srt_text = build_srt(rows)
        srt_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "srt", "engine": engine_used}),
            payload={"text": srt_text})
        report = {
            "engine_used": engine_used,
            "engine_requested": engine,
            "model": (remote_model if engine_used == "remote" else model),
            "language": lang_info.get("language", language),
            "language_probability": lang_info.get("language_probability"),
            "media_duration": lang_info.get("duration"),
            "segment_count": len(rows),
            "word_count": sum(len(r.get("words") or []) for r in rows),
            "vad": vad,
            "word_timestamps": word_ts,
            "media": {"path": src_path, "kind": getattr(src_asset, "kind", "")},
            "warnings": warnings,
            "dependency": self._dep_blob(model, engine_used),
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"engine": engine_used}), payload=report)
        return {"subtitles": sub_asset, "srt": srt_asset, "report": report_asset}

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