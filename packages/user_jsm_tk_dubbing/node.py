# -*- coding: utf-8 -*-
"""TK 出海配音流水线编排节点（user/jsm_tk_dubbing，C4，对齐解说猫 core.tk_dubbing 语义）。

5 步一键链（单节点触发，复用平台既有节点/协议）：
  1. ASR：core/asr（local faster-whisper 离线优先 / remote 火山兼容 / auto 自动降级）；
  2. 文本处理：资源化 prompt（data/tk_rewrite.txt / tk_translate.txt / cantonese_to_mandarin.txt）
     单次 LLM pass（编号行协议 + 单条降级 + 标记保留）——mode=translate/rewrite/cantonese_to_mandarin；
  3. 间隙合并：gap < gap_merge_threshold（参考 _GAP_MERGE_THRESHOLD=1.5s）并入前段；
  4. 大空隙插 OST：合并后 gap >= ost_gap_seconds 时插入原片穿插条目（报告可审计）；
  5. TTS + 草稿导出：core/tts（edge 免费默认 / 离线降级）/ core/draft_export（剪映草稿）。

对齐项：
- GAP/OST 规则与参考 _GAP_MERGE_THRESHOLD=1.5 一致；OST 插入规则报告审计（acceptance R-节点-2）。
- S8 修复：TK 单行字数参数化（max_chars_per_line，不再写死）；语音按目标语言映射（language_voices）。
- 离线可用：无火山/云 API Key 时 engine=local faster-whisper + edge TTS（免费网络层）即可跑通。
- prompt 资源动态加载：包内 data/*.txt（随包分发，不硬编码进 node.py）。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_PKG_DIR, "data")

GAP_MERGE_THRESHOLD_DEFAULT = 1.5   # 参考 core.tk_dubbing.pipeline._GAP_MERGE_THRESHOLD
OST_GAP_SECONDS_DEFAULT = 3.0       # 大空隙插 OST 阈值

# （显示名, 语言码）——与 user/jsm_translate_batch LANGS 同表
_LANGS = [
    ("中文", "zh"), ("英语", "en"), ("日语", "ja"), ("韩语", "ko"), ("泰语", "th"),
    ("印尼语", "id"), ("越南语", "vi"), ("马来语", "ms"), ("西班牙语", "es"),
    ("葡萄牙语", "pt"), ("俄语", "ru"), ("法语", "fr"), ("德语", "de"),
]
_DISPLAY_TO_CODE = {d: c for d, c in _LANGS}

_MARKER_RE = re.compile(r"(\[pause\]|\[emphasis\])", re.I)


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def load_prompt(name: str) -> str:
    """从包内 data/ 动态加载 prompt 资源；缺失时报错（资源化红线：不硬编码）。"""
    path = os.path.join(_DATA_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def normalize_subtitles(raw: Any) -> List[Dict[str, Any]]:
    """字幕行归一化：{index,start,end,text}（秒，round(3)）。缺失字段容错。"""
    out: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("rows") or raw.get("subtitles") or []
    if not isinstance(raw, list):
        return out
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("content") or "").strip()
        if not text:
            continue
        s = round(_num(row.get("start"), _num(row.get("start_time"), 0.0)), 3)
        e = round(_num(row.get("end"), _num(row.get("end_time"), s)), 3)
        out.append({"index": int(row.get("index", i)), "start": s,
                    "end": max(e, s), "text": text})
    out.sort(key=lambda r: (r["start"], r["index"]))
    return out


def resolve_language_code(value: str) -> Tuple[str, str]:
    """显示名/语言码 → (display, code)；未知显式报错。"""
    v = str(value or "").strip()
    if v in _DISPLAY_TO_CODE:
        return v, _DISPLAY_TO_CODE[v]
    for d, c in _LANGS:
        if v.lower() in (d.lower(), c):
            return d, c
    raise ValueError(f"未知语言: {v!r}（支持: {', '.join(_DISPLAY_TO_CODE)}）")


# ---------------------------------------------------------------- LLM 文本处理（编号行协议，与 translate_batch 一致）

def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")


def _llm_chat(messages: List[Dict[str, str]], api_key: str, base_url: str, model: str,
              retries: int, timeout: float) -> str:
    import urllib.error
    import urllib.request
    url = str(base_url).rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 8192}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(max(1, retries + 1)):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", "Bearer " + api_key)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            return str(json.loads(body)["choices"][0]["message"]["content"] or "")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            last_err = RuntimeError(f"HTTP {e.code}: {raw[:300]}")
            if e.code in (400, 401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = RuntimeError(f"网络错误: {e}")
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    raise last_err or RuntimeError("LLM 请求失败")


def _parse_numbered_output(raw: str, expected: int) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for line in str(raw).splitlines():
        m = re.match(r"^\s*\[(\d+)\]\s*(.*)$", line, re.S)
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    if not out or len(out) < expected:
        raise ValueError(f"输出条数不符（期望{expected}，解析{len(out)}）")
    return out


def process_texts(texts: List[str], mode: str, target_display: str, target_code: str,
                  api_key: str, base_url: str, model: str) -> Tuple[List[Dict[str, Any]], str]:
    """资源化 prompt 单次 LLM pass → 逐行结果；失败降级单条；保留 [pause]/[emphasis]。"""
    if mode == "translate":
        system = load_prompt("tk_translate.txt")
        system += f"\n目标语言：{target_display}（{target_code}）"
        system += "\n输出格式：每行 '[序号] 译文'，只输出译文。"
    elif mode == "rewrite":
        system = load_prompt("tk_rewrite.txt")
        system += "\n输出格式：每行 '[序号] 重构结果'，只输出结果。"
    elif mode == "cantonese_to_mandarin":
        system = load_prompt("cantonese_to_mandarin.txt")
        system += "\n输出格式：每行 '[序号] 普通话字幕'，只输出结果。"
    else:
        raise ValueError(f"未知文本模式: {mode!r}（translate/rewrite/cantonese_to_mandarin）")

    srcs: List[str] = []
    markers: List[List[str]] = []
    for t in texts:
        found: List[str] = []

        def _sub(m, _found=found):
            _found.append(m.group(0))
            return f"__M{len(_found)}__"

        srcs.append(_MARKER_RE.sub(_sub, t))
        markers.append(found)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(f"[{i}] {t}" for i, t in enumerate(srcs))},
    ]
    mode_used = "batch"
    translated: List[str] = []
    try:
        raw = _llm_chat(messages, api_key, base_url, model, retries=2, timeout=180.0)
        parsed = _parse_numbered_output(raw, len(srcs))
        translated = [parsed.get(i, "") for i in range(len(srcs))]
        if any(not s for s in translated):
            raise ValueError("存在空译文")
    except Exception as e:  # noqa: BLE001
        mode_used = f"fallback_single({type(e).__name__})"
        for i, t in enumerate(srcs):
            try:
                raw1 = _llm_chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"[0] {t}"},
                ], api_key, base_url, model, retries=1, timeout=120.0)
                parsed1 = _parse_numbered_output(raw1, 1)
                translated.append(parsed1.get(0, ""))
            except Exception:  # noqa: BLE001
                translated.append("")

    results: List[Dict[str, Any]] = []
    for i, (src, tgt) in enumerate(zip(texts, translated)):
        if markers[i]:
            for k, m in enumerate(markers[i]):
                tgt = tgt.replace(f"__M{k + 1}__", m)
        results.append({
            "index": i, "text": src,
            "text_out": tgt.strip() or src,
            "status": "succeeded" if tgt.strip() else "failed",
        })
    return results, mode_used


# ---------------------------------------------------------------- GAP 合并 / OST 插入（对齐 _GAP_MERGE_THRESHOLD=1.5）

def merge_gaps(rows: List[Dict[str, Any]], threshold: float) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """gap < threshold 并入前段。返回 (新行, 合并审计 [{indexes,start,end,text}] )。"""
    if not rows:
        return [], []
    merged: List[Dict[str, Any]] = []
    audit: List[Dict[str, Any]] = []
    cur = dict(rows[0])
    cur_idx = [rows[0]["index"]]
    for nxt in rows[1:]:
        gap = nxt["start"] - cur["end"]
        if gap < threshold:
            cur = {
                "index": cur["index"],
                "start": cur["start"],
                "end": nxt["end"],
                "text": cur["text"] + "。" + nxt["text"] if cur["text"] and nxt["text"]
                       else (cur["text"] or nxt["text"]),
            }
            cur_idx.append(nxt["index"])
        else:
            merged.append(dict(cur))
            audit.append({"merged_indexes": cur_idx, "start": cur["start"],
                          "end": cur["end"], "text": cur["text"]})
            cur = dict(nxt)
            cur_idx = [nxt["index"]]
    merged.append(dict(cur))
    audit.append({"merged_indexes": cur_idx, "start": cur["start"],
                  "end": cur["end"], "text": cur["text"]})
    return merged, audit


def build_timeline(rows: List[Dict[str, Any]], gap_merge_threshold: float,
                   ost_gap_seconds: float, enable_ost: bool) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """合并间隙并插入 OST → 时间线条目（narration 顺序号 0..k-1；OST 用负序号防冲突）。

    返回 (entries, ost_audit)。entries 每项：{segment_index, text, start, end, is_ost,
    src_start, src_end}（narration 段顺序号即 TTS 顺序号，供 marks 对齐）。
    """
    merged, _audit = merge_gaps(rows, gap_merge_threshold)
    entries: List[Dict[str, Any]] = []
    ost_audit: List[Dict[str, Any]] = []
    narr_no = 0
    for i, row in enumerate(merged):
        if i > 0:
            prev = merged[i - 1]
            gap = row["start"] - prev["end"]
            if enable_ost and gap >= ost_gap_seconds:
                ost_idx = -(len(ost_audit) + 1)
                entries.append({"segment_index": ost_idx, "text": "（原声片段）",
                                "start": round(prev["end"], 3), "end": round(row["start"], 3),
                                "is_ost": True, "src_start": round(prev["end"], 3),
                                "src_end": round(row["start"], 3)})
                ost_audit.append({"segment_index": ost_idx, "start": round(prev["end"], 3),
                                  "end": round(row["start"], 3), "gap": round(gap, 3)})
        entries.append({"segment_index": narr_no, "text": row["text"],
                        "start": row["start"], "end": row["end"], "is_ost": False,
                        "src_start": row["start"], "src_end": row["end"]})
        narr_no += 1
    return entries, ost_audit


def split_long_lines(rows: List[Dict[str, Any]], max_chars: int) -> List[Dict[str, Any]]:
    """S8 修复：TK 单行字数参数化——超过 max_chars 的行按标点切分（0=不切）。"""
    if max_chars <= 0:
        return rows
    out: List[Dict[str, Any]] = []
    for row in rows:
        text = row["text"]
        if len(text) <= max_chars:
            out.append(row)
            continue
        pieces: List[str] = []
        cur = ""
        for ch in text:
            cur += ch
            if len(cur) >= max_chars:
                pieces.append(cur)
                cur = ""
        if cur:
            pieces.append(cur)
        span = max((row["end"] - row["start"]) / max(len(pieces), 1), 0.2)
        for k, piece in enumerate(pieces):
            piece = piece.strip()
            if not piece:
                continue
            out.append({"index": row["index"], "start": round(row["start"] + k * span, 3),
                        "end": round(row["start"] + (k + 1) * span, 3), "text": piece})
    return out


# ---------------------------------------------------------------- 节点

class JsmTkDubbingNode(NodeBase):
    """TK 出海配音流水线。type_id: user/jsm_tk_dubbing"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="user/jsm_tk_dubbing",
            title="TK 出海配音流水线",
            category="用户节点",
            description="一键链：ASR→洗稿/翻译→间隙合并→OST 穿插→TTS→剪映草稿（离线 Whisper+edge TTS 可用）",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=True),
                PortSpec("subtitles_in", PortType.SUBTITLE, label="已有字幕(可跳过ASR)", required=False),
            ],
            outputs=[
                PortSpec("subtitles", PortType.SUBTITLE, label="TK 字幕", required=False),
                PortSpec("segments", PortType.SEGMENTS, label="时间线条目", required=False),
                PortSpec("audio", PortType.AUDIO, label="配音音频", required=False),
                PortSpec("marks", PortType.JSON, label="TTS 标记", required=False),
                PortSpec("draft", PortType.JSON, label="剪映草稿", required=False),
                PortSpec("report", PortType.JSON, label="流水线报告", required=True),
            ],
            params=[
                # ---- ASR
                PortSpec("asr_engine", PortType.STRING, label="ASR 引擎", default="auto",
                         widget="select", options=["auto", "local", "remote"]),
                PortSpec("asr_model", PortType.STRING, label="本地 ASR 模型", default="small",
                         widget="select", options=["tiny", "base", "small", "medium", "large-v3"]),
                PortSpec("asr_language", PortType.STRING, label="源语言", default="zh",
                         widget="select", options=["zh", "en", "ja", "ko", "auto"]),
                PortSpec("asr_api_key", PortType.STRING, label="ASR API Key(远程)", required=False,
                         default="", widget="password", description="支持 @secret:名称"),
                PortSpec("asr_base_url", PortType.STRING, label="ASR 中转地址", required=False,
                         default="https://api.openai.com/v1", widget="text"),
                PortSpec("enable_diarization", PortType.BOOL, label="说话人区分", default=False,
                         widget="toggle", description="透传 core/asr：sherpa-onnx 说话人区分（TK 多说话人片源）"),
                PortSpec("diarization_model_dir", PortType.STRING, label="Diarization 模型目录",
                         required=False, default="", widget="text",
                         description="sherpa-onnx 说话人区分模型目录；留空自动探测"),
                PortSpec("cover_original_subtitle", PortType.BOOL, label="遮盖原字幕", default=False,
                         widget="toggle",
                         description="草稿生成时在原字幕区域叠黑条遮盖（避免与新翻译字幕重叠）"),
                # ---- 文本处理
                PortSpec("mode", PortType.STRING, label="文本模式", default="translate",
                         widget="select", options=["translate", "rewrite", "cantonese_to_mandarin"]),
                PortSpec("target_language", PortType.STRING, label="目标语言", default="英语",
                         widget="select", options=[d for d, _c in _LANGS]),
                PortSpec("llm_api_key", PortType.STRING, label="文本 API Key", required=False,
                         default="", widget="password", description="支持 @secret:名称"),
                PortSpec("llm_base_url", PortType.STRING, label="文本中转地址", required=False,
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec("llm_model", PortType.STRING, label="文本模型", default="", widget="text"),
                PortSpec("keep_style", PortType.BOOL, label="保留口语风格", default=True, widget="toggle"),
                # ---- 间隙 / OST（参考 _GAP_MERGE_THRESHOLD=1.5）
                PortSpec("gap_merge_threshold", PortType.FLOAT, label="间隙合并阈值(秒)", default=1.5, widget="number"),
                PortSpec("ost_gap_seconds", PortType.FLOAT, label="OST 插入阈值(秒)", default=3.0, widget="number"),
                PortSpec("enable_ost", PortType.BOOL, label="大空隙插 OST", default=True, widget="toggle"),
                PortSpec("max_chars_per_line", PortType.INT, label="单行最大字数(0=不切)", default=0, widget="number"),
                # ---- TTS
                PortSpec("tts_engine", PortType.STRING, label="TTS 引擎", default="edge",
                         widget="select", options=["edge", "cloud", "indextts", "manual"]),
                PortSpec("edge_voice", PortType.STRING, label="Edge 音色", default="zh-CN-XiaoxiaoNeural",
                         widget="text", description="默认音色；language_voices 命中时按目标语言覆盖"),
                PortSpec("language_voices", PortType.STRING, label="多语音色映射(JSON)", required=False,
                         default="", widget="text",
                         description='例 {"en": "en-US-AriaNeural", "ja": "ja-JP-NanamiNeural"}'),
                PortSpec("tts_api_key", PortType.STRING, label="TTS API Key(云端)", required=False,
                         default="", widget="password", description="支持 @secret:名称"),
                PortSpec("tts_base_url", PortType.STRING, label="TTS 中转地址", required=False,
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec("tts_cloud_voice", PortType.STRING, label="云端音色", default="alloy", widget="text"),
                PortSpec("tts_cloud_model", PortType.STRING, label="云端模型", default="tts-1", widget="text"),
                PortSpec("tts_rate", PortType.STRING, label="TTS 语速", default="+0%", widget="text"),
                PortSpec("tts_output_format", PortType.STRING, label="TTS 输出格式", default="mp3",
                         widget="select", options=["mp3", "wav", "ogg"]),
                # ---- 草稿
                PortSpec("draft_name", PortType.STRING, label="草稿名", default="TK多语草稿", widget="text"),
                PortSpec("subtitle_style", PortType.STRING, label="字幕样式", default="经典-白字黑边",
                         widget="select", options=["经典-白字黑边", "黑底白字", "黄字红边", "默认"]),
                PortSpec("canvas_ratio", PortType.STRING, label="画布比例", default="original",
                         widget="select", options=["original", "16:9", "9:16", "1:1"]),
                PortSpec("enable_subtitle", PortType.BOOL, label="生成字幕", default=True, widget="toggle"),
            ],
            version="1.1.0",
        )

    # ------------------------------------------------------------ 编排
    async def _asr(self, ctx: dict, video_asset, p: Dict[str, Any]) -> Dict[str, Any]:
        from app.registry import get_class
        cls = get_class("core/asr")
        out = await cls().run(ctx, {"video": video_asset, "audio": None}, {
            "engine": p.get("asr_engine", "auto"),
            "model": p.get("asr_model", "small"),
            "language": p.get("asr_language", "zh"),
            "device": "auto", "compute_type": "int8", "vad": True,
            "word_timestamps": False,
            "api_key": p.get("asr_api_key", ""),
            "base_url": p.get("asr_base_url", "https://api.openai.com/v1"),
            "remote_model": "whisper-1",
            "enable_diarization": bool(p.get("enable_diarization", False)),
            "diarization_model_dir": str(p.get("diarization_model_dir", "") or ""),
        })
        return out

    async def _tts(self, ctx: dict, narr_segments: List[Dict[str, Any]], p: Dict[str, Any],
                   lang_code: str) -> Dict[str, Any]:
        from app.registry import get_class
        try:
            voices = json.loads(str(p.get("language_voices", "") or "{}") or "{}")
        except json.JSONDecodeError:
            voices = {}
        if not isinstance(voices, dict):
            voices = {}
        default_voice = str(p.get("edge_voice", "zh-CN-XiaoxiaoNeural") or "zh-CN-XiaoxiaoNeural")
        segs = []
        for s in narr_segments:
            seg = {"index": s["segment_index"], "text": s["text"],
                   "start": s["start"], "end": s["end"],
                   "voice": voices.get(lang_code) or default_voice}
            segs.append(seg)
        cls = get_class("core/tts")
        out = await cls().run(ctx, {"segments": _asset_from(ctx, segs), "script": None,
                                    "audio_in": None, "audio_ref": None}, {
            "engine": p.get("tts_engine", "edge"),
            "voice": default_voice,
            "rate": p.get("tts_rate", "+0%"),
            "pitch": "+0Hz", "volume": "+0%",
            "output_format": p.get("tts_output_format", "mp3"),
            "api_key": p.get("tts_api_key", ""),
            "base_url": p.get("tts_base_url", "https://ameaaos.com/v1"),
            "cloud_model": p.get("tts_cloud_model", "tts-1"),
            "cloud_voice": p.get("tts_cloud_voice", "alloy"),
            "cloud_speed": "1.0",
            "chunk_chars": 800, "max_concurrent": 3, "retries": 2, "merge_audio": True,
            "auto_start_server": False,
        })
        return out

    async def _draft(self, ctx: dict, video_asset, entries: List[Dict[str, Any]],
                     marks: Dict[str, Any], tts_audio, p: Dict[str, Any]) -> Dict[str, Any]:
        from app.registry import get_class
        store = ctx["store"]
        entries_asset = _save_asset(ctx, entries, PortType.SEGMENTS.value, "c4entries",
                                    meta={"count": len(entries),
                                          "ost": sum(1 for e in entries if e.get("is_ost"))})
        timeline: Dict[str, Any] = {}
        voice_items = []
        if isinstance(marks, dict):
            for m in marks.get("segments") or []:
                if m.get("status") == "succeeded" and m.get("path"):
                    voice_items.append({
                        "segment_index": m.get("index"),
                        "audio_path": m.get("path") or m.get("audio_path") or "",
                        "path": m.get("path") or "",
                        "duration": _num(m.get("duration"), 0.0),
                        "start": m.get("start"), "end": m.get("end"),
                    })
        if voice_items:
            timeline = {"voice_items": [v for v in voice_items if v.get("path")]}
        cls = get_class("core/draft_export")
        out = await cls().run(ctx, {
            "video": video_asset, "segments": entries_asset,
            "audio": tts_audio, "styled_subtitle": None,
            "timeline": (_save_asset(ctx, {"voice_items": timeline.get("voice_items", [])},
                                     PortType.TIMELINE.value, "c4tl",
                                     meta={"voice_items": len(timeline.get("voice_items", []))})
                         if timeline else None),
            "bgm": None,
        }, {
            "format": "jianying", "aspect": "16:9", "resolution": "1080p",
            "canvas_ratio": p.get("canvas_ratio", "original"),
            "draft_name": p.get("draft_name", "TK多语草稿"),
            "export_target": "jianying",
            "subtitle_style": p.get("subtitle_style", "经典-白字黑边"),
            "font_size": 0.0, "enable_subtitle": bool(p.get("enable_subtitle", True)),
            "fps": 30, "output_dir": "",
            "cover_original_subtitle": bool(p.get("cover_original_subtitle", False)),
        })
        return out

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        from app.registry import autodiscover, get_class
        import contextlib
        import io as _io
        # worker 安全模式（对齐官方 core_batch_render node.py:135）：默认 worker 隔离下
        # worker 子进程（host_bootstrap）只装载目标节点模块、不做 registry 扫描，
        # 本节点内部经 get_class 解析 core/asr、core/tts、core/draft_export 等兄弟节点
        # 必然 KeyError；故 run() 起始调用平台 autodiscover() 注册依赖节点。
        # 注意：declarative.scan() 会把包加载警告 print 到 stdout——worker 的 stdout 是
        # JSONL RPC 通道（supervisor 将非 JSON 行判为 EOF/WorkerCrash），故调用期间
        # 重定向 stdout，保证通道纯净。
        with contextlib.redirect_stdout(_io.StringIO()):
            autodiscover()
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        video_asset = inputs.get("video")
        if video_asset is None or not getattr(video_asset, "path", "") or not os.path.exists(video_asset.path):
            raise RuntimeError("invalid: 需要视频输入")
        sub_asset = inputs.get("subtitles_in")

        target_display, target_code = resolve_language_code(params.get("target_language", "英语"))
        mode = str(params.get("mode", "translate") or "translate")
        gap_threshold = max(0.0, _num(params.get("gap_merge_threshold", GAP_MERGE_THRESHOLD_DEFAULT),
                                      GAP_MERGE_THRESHOLD_DEFAULT))
        ost_gap = max(0.0, _num(params.get("ost_gap_seconds", OST_GAP_SECONDS_DEFAULT),
                                OST_GAP_SECONDS_DEFAULT))
        enable_ost = bool(params.get("enable_ost", True))
        max_chars = max(0, int(params.get("max_chars_per_line", 0) or 0))

        stages: List[Dict[str, Any]] = []
        warnings: List[str] = []

        def _stage(name: str) -> None:
            stages.append({"name": name, "status": "running"})

        def _finish(name: str, ok: bool, detail: Any = None) -> None:
            for st in stages:
                if st["name"] == name:
                    st["status"] = "succeeded" if ok else "failed"
                    if detail is not None:
                        st["detail"] = detail
                    return
            stages.append({"name": name, "status": "succeeded" if ok else "failed", "detail": detail})

        # ---- 第 1 步：ASR（有字幕输入时跳过）
        _stage("asr")
        asr_report = None
        if sub_asset is not None:
            rows = normalize_subtitles(store.read_json(sub_asset.id))
            _finish("asr", True, {"source": "input_subtitles", "rows": len(rows)})
        else:
            asr_out = await self._asr(ctx, video_asset, params)
            sub_out = asr_out.get("subtitles")
            if sub_out is None:
                raise RuntimeError("stage=asr: 未产出字幕资产")
            rows = normalize_subtitles(_read_payload(store, sub_out))
            asr_report = asr_out.get("report")
            _finish("asr", True, {"engine": sub_out.meta.get("engine"),
                                  "model": sub_out.meta.get("model"), "rows": len(rows)})
        if not rows:
            raise RuntimeError("stage=asr: 未识别到任何字幕（请检查视频含语音/换 remote 引擎）")
        progress(0.2)

        # ---- 第 2 步：文本处理（资源化 prompt 单次 pass）
        _stage("text_process")
        llm_api_key = _resolve_secret(ctx, str(params.get("llm_api_key", "") or ""))
        if not llm_api_key:
            raise RuntimeError("contract: 文本处理需要 llm_api_key（可填 @secret:名称）")
        texts = [r["text"] for r in rows]
        processed, mode_used = process_texts(
            texts, mode, target_display, target_code, llm_api_key,
            str(params.get("llm_base_url", "https://ameaaos.com/v1")).strip() or "https://ameaaos.com/v1",
            str(params.get("llm_model", "")).strip() or "")
        failed = [r for r in processed if r.get("status") == "failed"]
        if failed:
            warnings.append(f"文本处理 {len(failed)}/{len(processed)} 行失败（保留原文）")
        rows = [dict(r, text=(proc["text_out"]),
                     index=(proc["index"] if proc.get("index") is not None else r["index"]))
                for r, proc in zip(rows, processed)]
        _finish("text_process", True, {"mode": mode, "target": target_code,
                                       "llm_mode": mode_used, "rows": len(rows)})
        progress(0.45)

        # ---- 第 3/4 步：间隙合并 + 大空隙插 OST（GAP/OST 规则，报告可审计）
        _stage("gap_merge")
        merged, merge_audit = merge_gaps(rows, gap_threshold)
        _finish("gap_merge", True, {"threshold": gap_threshold,
                                    "rows_before": len(rows), "rows_after": len(merged),
                                    "merged_groups": len(merge_audit)})
        _stage("ost_insert")
        if max_chars > 0:
            merged = split_long_lines(merged, max_chars)
        entries, ost_audit = build_timeline(merged, gap_threshold, ost_gap, enable_ost)
        _finish("ost_insert", True, {"threshold": ost_gap, "inserted": len(ost_audit),
                                     "entries": len(entries)})
        progress(0.6)

        # ---- 第 5 步：TTS（仅解说条目；edge 免费 / 离线降级）
        _stage("tts")
        narr_entries = [e for e in entries if not e.get("is_ost")]
        tts_out: Dict[str, Any] = {}
        audio_asset = None
        marks = None
        if narr_entries:
            try:
                tts_ctx = _ctx_with(ctx, progress=(lambda v: None))
                tts_out = await self._tts(tts_ctx, narr_entries, params, target_code)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"TTS 失败（{type(e).__name__}: {e}）；降级为仅导出字幕/时间线")
                _finish("tts", False, {"error": str(e)[:300]})
            else:
                audio_asset = tts_out.get("audio")
                marks = tts_out.get("marks")
                if isinstance(marks, dict):
                    _finish("tts", True, {"engine": marks.get("engine", ""),
                                          "segments": len(narr_entries)})
                else:
                    _finish("tts", True, {"engine": "",
                                          "segments": len(narr_entries)})
        else:
            _finish("tts", False, {"error": "无解说条目（全部为 OST/空），跳过 TTS"})
        progress(0.78)

        # ---- 第 6 步：剪映草稿导出
        _stage("draft_export")
        draft_out = {}
        draft_asset = None
        try:
            draft_out = await self._draft(ctx, video_asset, entries, marks or {}, audio_asset, params)
            draft_asset = draft_out.get("draft")
            _draft_report = _read_payload(store, draft_out.get("report")) or {}
            _finish("draft_export", True,
                    {"draft_dir": (_draft_report.get("results") or {}).get("draft_dir")})
        except Exception as e:  # noqa: BLE001
            warnings.append(f"草稿导出失败（{type(e).__name__}: {e}）")
            _finish("draft_export", False, {"error": str(e)[:300]})
        progress(0.9)

        # ---- 输出资产
        final_subs = [{"index": i, "start": r["start"], "end": r["end"], "text": r["text"],
                       "lang": target_code} for i, r in enumerate(merged)]
        subs_asset = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value,
                  meta={"lang": target_code, "mode": mode, "count": len(final_subs)}),
            payload=final_subs)
        seg_asset = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"count": len(entries), "ost": sum(1 for e in entries if e.get("is_ost"))}),
            payload=entries)
        report = {
            "ok": True,
            "target_language": {"display": target_display, "code": target_code},
            "mode": mode,
            "stages": stages,
            "audit": {
                "gap_merge_threshold": gap_threshold,
                "gap_merges": merge_audit,
                "ost_gap_seconds": ost_gap,
                "ost_inserts": ost_audit,
            },
            "counts": {"asr_rows": len(rows), "merged_rows": len(merged),
                       "entries": len(entries), "ost": len(ost_audit),
                       "narration": len(narr_entries)},
            "prompt_resources": {"mode": mode,
                                 "file": {"translate": "tk_translate.txt",
                                          "rewrite": "tk_rewrite.txt",
                                          "cantonese_to_mandarin": "cantonese_to_mandarin.txt"}[mode]},
            "asr_report": getattr(asr_report, "id", ""),
            "tts_marks": bool(marks),
            "warnings": warnings,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": "tk"}), payload=report)
        marks_asset = None
        if isinstance(marks, dict):
            marks_asset = store.save_asset(
                Asset(id="", kind=PortType.JSON.value,
                      meta={"segments": len(marks.get("segments") or []),
                            "engine": marks.get("engine", ""), "batch": True}),
                payload=marks)
        progress(1.0)
        return {"subtitles": subs_asset, "segments": seg_asset,
                "audio": audio_asset, "marks": marks_asset,
                "draft": draft_asset, "report": report_asset}


def _asset_from(ctx: dict, payload: Any) -> Asset:
    """TTS 分段列表资产（worker 兼容登记，供 core/tts batch 经 read_json 回读）。"""
    return _save_asset(ctx, payload, PortType.SEGMENTS.value, "c4segs")


def _ctx_with(ctx: Any, **kw: Any) -> Dict[str, Any]:
    """派生子上下文（worker 兼容）：worker 内 ctx 为 WorkerContext，dict(ctx)
    会走旧序列迭代协议（ctx[0] → hasattr(self, 0)）抛 TypeError；改用 to_dict()
    导出平台上下文字典后再叠加覆盖项。in-process dict ctx 直接拷贝。"""
    if isinstance(ctx, dict):
        base = dict(ctx)
    elif hasattr(ctx, "to_dict"):
        base = ctx.to_dict()
    else:
        base = {}
    base.update(kw)
    return base


_SAVE_SEQ = [0]


def _save_asset(ctx: dict, payload: Any, kind: str, tag: str, meta: Optional[Dict[str, Any]] = None) -> Asset:
    """保存结构化载荷资产并登记 mid-run 回读索引（in-process / worker 双模式兼容）。

    worker 模式：WorkerStoreProxy.save_asset 只落盘 scratch/out（id 保持空），且
    host_bootstrap 仅把「输入」资产登记进 store._input_index；本编排节点 run() 内
    经 get_class 调用的兄弟节点（core/tts、core/draft_export）用
    store.read_json(asset.id) 回读 mid-run 资产时会查不到（id 空 / 未登记）。
    故此处给资产分配包内唯一 id，并登记 {id -> payload_file} 进 store._input_index，
    使双模式下游回读一致；in-process（LocalAssetStore 无 _input_index）自然跳过。
    """
    _SAVE_SEQ[0] += 1
    store = ctx["store"]
    asset = store.save_asset(Asset(id="%s_%d" % (tag, _SAVE_SEQ[0]), kind=kind, meta=meta or {}),
                             payload=payload)
    idx = getattr(store, "_input_index", None)
    if isinstance(idx, dict) and getattr(asset, "path", ""):
        idx[asset.id] = {"asset": asset, "payload_file": asset.path}
    return asset


def _read_payload(store: Any, asset: Any) -> Any:
    """跨模式读取资产结构化载荷：优先 store.read_json(id)；worker 内 mid-run 资产
    （id 为空/未登记）按 asset.path 直读落盘文件。"""
    if asset is None:
        return None
    try:
        data = store.read_json(getattr(asset, "id", "") or "")
        if data is not None:
            return data
    except Exception:  # noqa: BLE001
        pass
    path = getattr(asset, "path", "")
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    return None