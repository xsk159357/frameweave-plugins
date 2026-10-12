"""配音-字幕时间戳对齐节点。

语义（对齐逆向实测，独立实现）：
1. 读分镜文案（纯文本自动按 avg_segment_chars 分段；列表按行）。
2. 音频时长 ffprobe。
3. Whisper 词级转写（faster-whisper，模型 handle 缓存）→ difflib 字符级对齐：
   归一化（去标点/空白/小写）→ 词序列与分镜归一化文本做 SequenceMatcher，
   匹配块内词时间映射到对应分镜。
4. 后处理：重叠修正 → 未匹配分镜线性插值 → 尾段空隙延伸（≤ tail_pad）→
   完全失败按字符占比等分降级（fallback）。
5. 报告：match_ratio / missing_segments / extra_words / diagnostics / strategy_used。
"""
from __future__ import annotations
import json
import os
import re
import shutil
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_PUNCT_RE = re.compile(r"[\s，。！？；：、,.!?;:'\"“”‘’（）()\[\]【】《》<>〈〉\-—_/\\|]+")


def _norm(s: str) -> str:
    return _PUNCT_RE.sub("", str(s)).lower()


def _auto_split_text(text: str, avg_chars: int) -> List[str]:
    """按标点把长文案切成 ≈avg_chars 字的若干段。"""
    if avg_chars <= 0:
        avg_chars = 25
    pieces = re.split(r"(?<=[。！？；，,.!?;:])", text)
    out: List[str] = []
    cur = ""
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if len(_norm(cur)) + len(_norm(p)) <= avg_chars:
            cur += p
        else:
            if cur:
                out.append(cur)
            if len(_norm(p)) > avg_chars * 1.6:
                for i in range(0, len(p), avg_chars):
                    out.append(p[i:i + avg_chars])
            else:
                cur = p
    if cur:
        out.append(cur)
    if not out and text.strip():
        out = [text]
    return out


def _split_aligned(seq: str, counts: List[int]) -> List[str]:
    out = []
    idx = 0
    for c in counts:
        out.append(seq[idx:idx + c])
        idx += c
    return out


def _guess_language(text: str) -> Optional[str]:
    if not text:
        return None
    cjk = len(_CJK_RE.findall(text))
    if cjk / max(len(text.replace(" ", "")), 1) > 0.3:
        return "zh"
    return None


class _WhisperCache:
    """进程内模型句柄缓存（按 model_dir+device 复用）。"""

    _handle = None
    _key = ""


_WHISPER_CACHE = _WhisperCache()


def _run_whisper_words(audio_path: str, model_dir: str, language: str,
                       model_name: str) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """faster-whisper 词级转写；返回 (words, error) 或 (None, err)。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        return None, f"faster-whisper 未安装: {e}"
    key = f"{model_dir}|{model_name}"
    try:
        if _WHISPER_CACHE._key != key or _WHISPER_CACHE._handle is None:
            model_path = model_dir or _default_model_dir(model_name)
            device = "auto"
            handle = WhisperModel(model_path, device=device, compute_type="int8")
            _WHISPER_CACHE._handle = handle
            _WHISPER_CACHE._key = key
        handle = _WHISPER_CACHE._handle
        lang = None if language in ("auto", "", None) else language
        segments, info = handle.transcribe(audio_path, language=lang, vad_filter=True,
                                           word_timestamps=True)
        words: List[Dict[str, Any]] = []
        for seg in segments:
            for w in (seg.words or []):
                words.append({"word": w.word.strip(), "start": float(w.start),
                              "end": float(w.end)})
        if not words:
            return None, "Whisper 未产出词级时间戳（无匹配语音？）"
        return words, ""
    except Exception as e:
        return None, f"Whisper 转写失败: {e}"


def _default_model_dir(model_name: str) -> str:
    """统一模型解析：env → DATA_DIR/models → HF 缓存；未就绪交给 faster-whisper 默认下载。"""
    try:
        from app.plugin import deps
        mr = deps.resolve_model("faster-whisper-" + model_name)
        if mr.get("ready") and mr.get("path"):
            return mr["path"]
    except Exception:  # noqa: BLE001
        pass
    return model_name  # 交给 faster-whisper 默认下载行为


def align_words_to_segments(words: List[Dict[str, Any]], seg_texts: List[str]
                            ) -> Tuple[List[List[Dict[str, Any]]], float]:
    """difflib 字符级对齐：返回每段命中的词列表与整段匹配率。"""
    seg_norms = [_norm(s) for s in seg_texts]
    # 分镜字符 → 段索引
    offsets: List[Tuple[int, int]] = []  # (seg_idx, char_pos_in_seg)
    total_text = ""
    for i, sn in enumerate(seg_norms):
        for j, ch in enumerate(sn):
            offsets.append((i, j))
            total_text += ch
    # 词归一化字符 → 词索引
    word_idxs: List[int] = []  # 每个 W 字符属于哪个 word
    word_text = ""
    for wi, w in enumerate(words):
        for _ch in _norm(w["word"]):
            word_idxs.append(wi)
            word_text += _ch
    if not word_text or not total_text:
        return [[] for _ in seg_texts], 0.0
    matcher = SequenceMatcher(None, word_text, total_text, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]
    total_match_chars = sum(b.size for b in blocks)
    ratio = 2.0 * total_match_chars / max(len(word_text) + len(total_text), 1)

    assigned: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(len(seg_texts))}
    for wi, ti, size in blocks:
        seg_idx = offsets[ti][0] if (0 <= ti < len(offsets)) else None
        for k in range(size):
            w_char_idx = wi + k
            if w_char_idx >= len(word_idxs):
                break
            wid = word_idxs[w_char_idx]
            if seg_idx is not None:
                wrec = words[wid]
                if wrec not in assigned[seg_idx]:
                    assigned[seg_idx].append(wrec)
    out = []
    for i in range(len(seg_texts)):
        wl = assigned[i]
        wl.sort(key=lambda w: w["start"])
        out.append(wl)
    return out, round(ratio, 3)


def _cuts_from_words(seg_words: List[List[Dict[str, Any]]], audio_duration: float,
                     tail_pad: float) -> Tuple[List[Tuple[float, float]], List[int]]:
    """由每段命中词计算 [start,end]；返回 (cuts, missing_idx)。"""
    cuts: List[Tuple[float, float]] = []
    missing: List[int] = []
    for i, wl in enumerate(seg_words):
        if wl:
            cuts.append((wl[0]["start"], wl[-1]["end"]))
        else:
            cuts.append((0.0, 0.0))
            missing.append(i)
    # 无词段线性插值
    known = [i for i in range(len(seg_words)) if i not in missing]
    for i in missing:
        prev_end = cuts[known[0]][0] if known and known[0] < i else 0.0
        nxt_start = audio_duration
        for j in known:
            if j > i:
                nxt_start = cuts[j][0]
                break
        if known:
            if i < known[0]:
                cuts[i] = (0.0, cuts[known[0]][0])
            elif i > known[-1]:
                cuts[i] = (cuts[known[-1]][1], audio_duration)
            else:
                cuts[i] = (prev_end, nxt_start)
    # 重叠修正
    for i in range(1, len(cuts)):
        if cuts[i][0] < cuts[i - 1][1]:
            cuts[i] = (cuts[i - 1][1], max(cuts[i][1], cuts[i - 1][1]))
    # 尾段延伸
    guard = 0.03
    for i in range(len(cuts) - 1):
        gap = cuts[i + 1][0] - cuts[i][1]
        if gap > guard:
            extend = min(tail_pad, gap - guard)
            cuts[i] = (cuts[i][0], cuts[i][1] + extend)
    if cuts:
        last = cuts[-1]
        tail_gap = audio_duration - last[1]
        if 0 < tail_gap <= tail_pad + 0.05:
            cuts[-1] = (last[0], audio_duration)
    return [tuple(round(x, 3) for x in c) for c in cuts], missing


def _fallback_cuts(seg_texts: List[str], audio_duration: float,
                   mode: str) -> Tuple[List[Tuple[float, float]], str]:
    if mode == "equal":
        step = audio_duration / max(len(seg_texts), 1)
        cuts = [(i * step, (i + 1) * step) for i in range(len(seg_texts))]
        return cuts, "equal"
    chars = [max(len(_norm(s)), 1) for s in seg_texts]
    total = sum(chars)
    cuts = []
    cursor = 0.0
    for c in chars:
        end = min(audio_duration, cursor + audio_duration * c / total)
        cuts.append((cursor, end))
        cursor = end
    if cuts:
        cuts[-1] = (cuts[-1][0], audio_duration)
    return cuts, "proportional"


class AudioAlignNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/audio_align",
            title="配音-字幕对齐",
            category="音频",
            description="Whisper 词级转写 + difflib 字符级对齐；插值/尾pad/降级等分",
            inputs=[
                PortSpec(name="audio", type=PortType.AUDIO, label="配音音频", required=True),
                PortSpec(name="script", type=PortType.SCRIPT, label="分镜文案", required=True),
            ],
            outputs=[
                PortSpec(name="aligned", type=PortType.SUBTITLE, label="对齐字幕"),
                PortSpec(name="report", type=PortType.JSON, label="对齐报告"),
            ],
            params=[
                PortSpec(name="whisper_model", type=PortType.STRING, label="Whisper 模型",
                         default="large-v3", widget="select",
                         options=["large-v3", "medium", "small", "base"]),
                PortSpec(name="language", type=PortType.STRING, label="语言", default="auto",
                         widget="select", options=["auto", "zh", "en", "ja", "ko", "es"]),
                PortSpec(name="model_dir", type=PortType.STRING, label="模型目录", default="",
                         widget="text"),
                PortSpec(name="tail_pad", type=PortType.FLOAT, label="尾段延伸(秒)", default=0.3,
                         widget="number"),
                PortSpec(name="fallback", type=PortType.STRING, label="降级策略",
                         default="proportional", widget="select", options=["proportional", "equal"]),
                PortSpec(name="avg_segment_chars", type=PortType.INT, label="自动分段字数",
                         default=25, widget="number"),
                PortSpec(name="ffmpeg_path", type=PortType.STRING, label="ffmpeg 路径", default="",
                         widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        audio = inputs.get("audio")
        script_asset = inputs.get("script")
        if audio is None or not getattr(audio, "path", "") or not os.path.exists(audio.path):
            raise RuntimeError("缺少配音音频输入")
        if script_asset is None:
            raise RuntimeError("缺少分镜文案输入（script）")
        audio_path = audio.path
        started = time.time()

        audio_duration = await _probe_duration(ctx, str(params.get("ffmpeg_path", "") or ""), audio_path)
        if audio_duration is None or audio_duration <= 0:
            raise RuntimeError(f"无法读取配音音频时长: {audio_path}")

        # ---- 文案 → 分镜文本 ----
        raw = store.read_json(script_asset.id)
        seg_texts: List[str] = []
        if isinstance(raw, dict):
            t = raw.get("text")
            if isinstance(t, str) and t.strip():
                seg_texts = _auto_split_text(t, int(params.get("avg_segment_chars", 25) or 25))
            elif isinstance(raw.get("segments"), list):
                seg_texts = [str(s.get("text", "")) for s in raw["segments"] if isinstance(s, dict)]
        elif isinstance(raw, list):
            seg_texts = [str(s.get("text", "")) for s in raw if isinstance(s, dict) and s.get("text")]
        elif raw is None and script_asset.path:
            with open(script_asset.path, "r", encoding="utf-8") as f:
                seg_texts = _auto_split_text(f.read(), int(params.get("avg_segment_chars", 25) or 25))
        seg_texts = [s.strip() for s in seg_texts if s and s.strip()]
        if not seg_texts:
            raise RuntimeError("分镜文案为空")
        progress(0.05)

        # ---- Whisper 词级转写 ----
        whisper_model = str(params.get("whisper_model", "large-v3") or "large-v3")
        language = str(params.get("language", "auto") or "auto")
        if language == "auto":
            guess = _guess_language("".join(seg_texts))
            if guess:
                language = guess
        model_dir = str(params.get("model_dir", "") or "").strip()
        tail_pad = max(0.0, float(params.get("tail_pad", 0.3) or 0.3))
        fallback_mode = str(params.get("fallback", "proportional") or "proportional")
        progress(0.1)

        words, whisper_err = _run_whisper_words(audio_path, model_dir, language, whisper_model)
        strategy = ""
        if words:
            seg_words, ratio = align_words_to_segments(words, seg_texts)
            cuts, missing = _cuts_from_words(seg_words, audio_duration, tail_pad)
            extra_words = [w for w in words if w not in [x for y in seg_words for x in y]]
            if ratio >= 0.25:
                strategy = "whisper_difflib"
            else:
                cuts, _mode = _fallback_cuts(seg_texts, audio_duration, fallback_mode)
                missing = list(range(len(seg_texts)))
                ratio = 0.0
                strategy = f"fallback_{_mode}（whisper 匹配率过低 {ratio}）"
        else:
            cuts, _mode = _fallback_cuts(seg_texts, audio_duration, fallback_mode)
            missing = list(range(len(seg_texts)))
            extra_words = []
            ratio = 0.0
            strategy = f"fallback_{_mode}（{whisper_err or 'whisper 不可用'}）"
        progress(0.7)

        rows = []
        for i, (s, e) in enumerate(cuts):
            rows.append({"index": i, "text": seg_texts[i], "start": round(s, 3),
                         "end": round(max(e, s + 0.01), 3)})
        aligned_asset = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value, meta={"count": len(rows), "strategy": strategy}),
            payload=rows)
        report = {
            "strategy_used": strategy,
            "audio_duration": round(audio_duration, 3),
            "segments_total": len(seg_texts),
            "match_ratio": ratio,
            "missing_segments": missing,
            "extra_words_count": len(extra_words) if strategy.startswith("whisper") else None,
            "extra_words": extra_words[:20] if strategy.startswith("whisper") else [],
            "whisper_error": whisper_err if not words else None,
            "warnings": ([f"Whisper 不可用，已按字符占比降级分切"] if strategy.startswith("fallback") else []),
            "rt_s": round(time.time() - started, 2),
        }
        r_asset = store.save_asset(Asset(id="", kind=PortType.JSON.value, meta={"role": "align_report"}),
                                   payload=report)
        progress(1.0)
        return {"aligned": aligned_asset, "report": r_asset}


async def _probe_duration(ctx, ffmpeg_path: str, media_path: str) -> Optional[float]:
    import json as _json
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    ffprobe = _ffprobe_bin(ffmpeg)
    try:
        out = await ctx["run_tracked"](
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", media_path], timeout=60)
        if out.returncode != 0 or not out.stdout:
            return None
        data = _json.loads(out.stdout.decode("utf-8", "replace"))
        d = float((data.get("format") or {}).get("duration", 0) or 0)
        return round(d, 3) if d > 0 else None
    except Exception:
        return None


def _resolve_ffmpeg(ffmpeg_path: str = "") -> str:
    """统一依赖解析层。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", requested=ffmpeg_path, env=os.environ,
                            params={"ffmpeg_path": ffmpeg_path})
    return r["path"] if r["ready"] else "ffmpeg"


def _ffprobe_bin(ffmpeg: str) -> str:
    """统一依赖解析层：ffmpeg 同目录优先。"""
    from app.plugin import deps
    d = os.path.dirname(ffmpeg)
    for cand in (os.path.join(d, "ffprobe.exe"), os.path.join(d, "ffprobe")):
        if cand and os.path.isfile(cand):
            return cand
    r = deps.resolve_binary("ffprobe", env=os.environ)
    return r["path"] if r["ready"] else "ffprobe"