"""时间线转片段节点：TIMELINE（+ 可选选材范围）→ SEGMENTS。

为什么必须有它：
    全仓 70 个节点里没有任何一个能把 TIMELINE 变成 SEGMENTS。唯一的
    user/jsm_highlight_mixcut 虽然输出 segments，但吃的是 rows/items 两个 JSON，
    接不上 timeline。结果是所有工作流只能把 scene_detect.scenes（覆盖全片）硬塞进
    video_render.segments —— 渲染于是变成「把整部电影按镜头重编码一遍」，而不是剪辑。

数据结构（关键，别搞混）：
    timeline 条目：
        {"start": 成片时间轴位置, "end": 成片时间轴结束, "tts_duration": ...,
         "narration_index": n, "text": 这句解说, "action": "single"|"montage",
         "clips": [{"scene_id": i, "start": 源片时间, "end": 源片时间, "text": "原片台词"}]}
    video_render / draft_export 消费的 segments：
        [{"start": 源片时间, "end": 源片时间, ...}]
    —— 一个用成片轴，一个用源片轴。本节点把「解说顺序」翻译成「源片取哪几段」。

处理规则（全部可关、全部进 report）：
    1. 按 narration_index 升序展平 clips（没有 index 就按列表顺序）。
    2. 给了 base（选材范围）且 clamp_to_base=on 时，只保留落在范围内、并按范围裁边。
    3. 单段短于 min_piece 秒 → 丢弃（记 dropped_short），不偷偷拉长（可能超片尾）。
       判定带 _EPS 容差：名义时长 == min_piece 的段保留（浮点减法会让 0.8 变成 0.7999…）。
    4. 相邻段间隔 < merge_gap → 合并成一段（记 merged）。
    5. 与前一段重叠 → 起点抬到前一段终点（记 clamped）；抬完不足 min_piece 就丢。
    6. 输出顺序 = 解说顺序，不是时间顺序（允许倒叙/插叙）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_EPS = 1e-6


def _num(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _rows_of(asset_payload: Any) -> List[Dict[str, Any]]:
    """把资产 payload / list / 包裹 dict 统一成 list[dict]。"""
    if isinstance(asset_payload, list):
        return [x for x in asset_payload if isinstance(x, dict)]
    if isinstance(asset_payload, dict):
        for key in ("items", "segments", "timeline", "clips", "rows"):
            v = asset_payload.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _clip_range(clip: Dict[str, Any]) -> Tuple[float, float]:
    # clips[] 形状用 start/end（gemini_matcher）；split_cam 形状用 src_start/src_end（timeline_builder）
    st = _num(clip.get("start"), _num(clip.get("scene_start"), _num(clip.get("src_start"), -1.0)))
    en = _num(clip.get("end"), _num(clip.get("scene_end"), _num(clip.get("src_end"), -1.0)))
    if en <= st:
        dur = _num(clip.get("duration"))
        if dur > 0:
            en = st + dur
    return st, en


_SHAPE_CLIPS = "clips"
_SHAPE_SPLIT_CAM = "split_cam"


def _entry_shape(e: Dict[str, Any]) -> str:
    """单条 timeline 行的形状：clips[]（gemini_matcher）/ split_cam（timeline_builder）。

    互斥判定：**先看 clips，再看 src_start/src_end**，不做"两边都试再取并集"——
    并集会在两边都有时产生重复段。
    """
    clips = e.get("clips")
    if isinstance(clips, list) and clips:
        return _SHAPE_CLIPS
    if ("src_start" in e) or ("src_end" in e):
        return _SHAPE_SPLIT_CAM
    return ""


def _ranges_of(e: Dict[str, Any], shape: str) -> List[Dict[str, Any]]:
    if shape == _SHAPE_CLIPS:
        return [c for c in (e.get("clips") or []) if isinstance(c, dict)]
    if shape == _SHAPE_SPLIT_CAM:
        return [e]
    return []


def _sort_key(e: Dict[str, Any], shape: str, idx: int) -> float:
    """排序键：split_cam 按 segment_index（源片段顺序），clips 按 narration_index。"""
    if shape == _SHAPE_SPLIT_CAM:
        return _num(e.get("segment_index"), _num(e.get("narration_index"), float(idx)))
    return _num(e.get("narration_index"), float(idx))


def detect_timeline_shape(entries: Sequence[Dict[str, Any]]) -> str:
    """整表形状：以**第一条可识别**的行为准；识别不了返回 ""（调用方报错）。"""
    for e in entries:
        sh = _entry_shape(e)
        if sh:
            return sh
    return ""


def flatten_timeline(timeline: Sequence[Dict[str, Any]],
                     minutes: Optional[List[Tuple[float, float]]] = None,
                     clamp_to_range: bool = True,
                     drop_outside: bool = True,
                     min_piece: float = 0.8,
                     merge_gap: float = 0.25) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """timeline → segments 行。纯函数，不碰 ctx，便于单测。"""
    entries = [e for e in (timeline or []) if isinstance(e, dict)]
    shape = detect_timeline_shape(entries)
    if entries and not shape:
        keys = sorted({str(k) for e in entries[:3] for k in e.keys()})
        raise ValueError(
            "TIMELINE 形状无法识别：条目键=%r，期望 clips[]（每项含 start/end，gemini_matcher 产）"
            " 或 src_start/src_end（timeline_builder 产的 split_cam 行）；"
            "两种形状都不匹配时不会静默返回 0 段" % (keys,))
    entries = [e for _i, e in sorted(enumerate(entries),
                                     key=lambda p: _sort_key(p[1], shape, p[0]))]

    dropped_short = 0
    dropped_outside = 0
    clamped = 0
    merged = 0
    considered = 0
    pieces: List[Dict[str, Any]] = []

    for ei, e in enumerate(entries):
        clips = _ranges_of(e, shape)
        ni = e.get("narration_index", e.get("segment_index", ei))
        for c in clips:
            considered += 1
            st, en = _clip_range(c)
            if not (en > st):
                dropped_short += 1
                continue

            if minutes and clamp_to_range:
                hit = None
                for (bs, be) in minutes:
                    if en > bs and st < be:
                        hit = (max(st, bs), min(en, be))
                        break
                if hit is None:
                    if drop_outside:
                        dropped_outside += 1
                        continue
                else:
                    if abs(hit[0] - st) > _EPS or abs(hit[1] - en) > _EPS:
                        clamped += 1
                    st, en = hit

            # 名义时长恰好等于 min_piece 的段必须保留：5.2-4.4 = 0.7999999999999998，
            # 直接比 <0.8 会把它当成"过短"丢掉。容差只放宽下界，不用 <=（方向会反）。
            if en - st < min_piece - _EPS:
                dropped_short += 1
                continue

            pieces.append({
                "start": round(st, 3),
                "end": round(en, 3),
                "duration": round(en - st, 3),
                "narration_index": ni,
                "scene_id": c.get("scene_id"),
                "text": str(c.get("text") or ""),
                "narration_text": str(e.get("text") or ""),
                "action": str(e.get("action") or "single"),
                "order": len(pieces) + 1,
            })

    # 合并 + 去重叠（按输出顺序）
    out: List[Dict[str, Any]] = []
    for p in pieces:
        if not out:
            out.append(p)
            continue
        prev = out[-1]
        gap = p["start"] - prev["end"]
        if gap >= -_EPS and gap < merge_gap:
            prev["end"] = round(p["end"], 3)
            prev["duration"] = round(prev["end"] - prev["start"], 3)
            prev["merged_count"] = int(prev.get("merged_count") or 1) + 1
            merged += 1
            continue
        if gap < -_EPS:
            # 与前一段重叠：起点抬到前一段终点
            new_start = prev["end"]
            if p["end"] - new_start < min_piece - _EPS:
                dropped_short += 1
                continue
            p["start"] = round(new_start, 3)
            p["duration"] = round(p["end"] - p["start"], 3)
            clamped += 1
        out.append(p)

    total = round(sum(p["duration"] for p in out), 3)
    report = {
        "shape": shape,
        "entries": len(entries),
        "clips_considered": considered,
        "segments": len(out),
        "total_seconds": total,
        "dropped_short": dropped_short,
        "dropped_outside": dropped_outside,
        "clamped": clamped,
        "merged": merged,
        "min_piece": min_piece,
        "merge_gap": merge_gap,
        "base_filtered": bool(minutes),
    }
    return out, report


class TimelineToSegmentsNode(NodeBase):
    """时间线（+选材范围）→ 剪辑片段表。type_id: core/timeline_to_segments"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/timeline_to_segments",
            title="时间线转片段",
            category="剪辑",
            description="把解说时间线翻译成源片取段表（TIMELINE → SEGMENTS），"
                        "可叠加选材范围过滤；这是渲染只剪选中片段的唯一通路",
            inputs=[
                PortSpec(name="timeline", type=PortType.TIMELINE, label="匹配时间线",
                         required=True, description="gemini_matcher.timeline / jsm_match_visual.timeline"),
                PortSpec(name="base", type=PortType.SEGMENTS, label="选材范围(可选)",
                         required=False, description="jsm_highlight_plan 的选材片段；接上则只在该范围内剪"),
            ],
            outputs=[
                PortSpec(name="segments", type=PortType.SEGMENTS, label="剪辑片段表", required=True),
                PortSpec(name="report", type=PortType.JSON, label="转换报告", required=True),
            ],
            params=[
                PortSpec(name="min_piece", type=PortType.FLOAT, label="最短片段(秒)", default=0.8,
                         widget="number", description="短于此的片段丢弃，不拉长（避免超出片尾）"),
                PortSpec(name="merge_gap", type=PortType.FLOAT, label="合并缝隙(秒)", default=0.25,
                         widget="number", description="相邻片段间隔小于此值则合并"),
                PortSpec(name="clamp_to_base", type=PortType.BOOL, label="按选材范围裁边", default=True,
                         widget="toggle", description="接上 base 时，超出范围的片段裁到范围内"),
                PortSpec(name="drop_outside", type=PortType.BOOL, label="丢弃范围外片段", default=True,
                         widget="toggle", description="关掉则整体忽略 base"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        timeline_in = inputs.get("timeline")
        base_in = inputs.get("base")
        store = ctx["store"]
        if timeline_in is None:
            raise RuntimeError("需要时间线输入（timeline）")

        timeline: Any = timeline_in
        tid = getattr(timeline_in, "id", "")
        if tid:
            try:
                timeline = store.read_json(tid)
            except Exception:
                timeline = getattr(timeline_in, "payload", None) or []
        entries = _rows_of(timeline)
        if not entries:
            raise RuntimeError("时间线为空：上游匹配没有产出任何条目")

        minutes: Optional[List[Tuple[float, float]]] = None
        base_count = 0
        if base_in is not None:
            raw: Any = base_in
            bid = getattr(base_in, "id", "")
            if bid:
                try:
                    raw = store.read_json(bid)
                except Exception:
                    raw = getattr(base_in, "payload", None) or []
            brows = _rows_of(raw)
            base_count = len(brows)
            if brows:
                minutes = []
                for b in brows:
                    bs, be = _clip_range(b)
                    if be > bs:
                        minutes.append((bs, be))
                if not minutes:
                    minutes = None

        min_piece = max(0.0, _num(params.get("min_piece", 0.8), 0.8))
        merge_gap = max(0.0, _num(params.get("merge_gap", 0.25), 0.25))
        clamp_to_base = bool(params.get("clamp_to_base", True))
        drop_outside = bool(params.get("drop_outside", True))

        rows, report = flatten_timeline(
            entries, minutes=minutes,
            clamp_to_range=clamp_to_base and minutes is not None,
            drop_outside=drop_outside,
            min_piece=min_piece, merge_gap=merge_gap)

        if not rows:
            # 0 段有三种完全不同的原因，文案必须能区分，否则用户/排障都会被引向错方向：
            #   ① 形状不匹配（在 flatten_timeline 里已抛，带键名）；
            #   ② 有片段但全被丢弃（过短/范围外）；
            #   ③ 时间线本身是空表。
            if not report["entries"]:
                raise RuntimeError("TIMELINE 为空：上游没有产出任何时间线条目（检查上游节点是否成功）")
            raise RuntimeError(
                "时间线可识别（shape=%s，条目 %d 条，候选片段 %d 个），但展平后没有有效片段："
                "丢弃过短 %d / 范围外 %d；请检查 min_piece 与选材范围设置"
                % (report.get("shape") or "?",
                   report["entries"], report["clips_considered"],
                   report["dropped_short"], report["dropped_outside"]))

        report["base_clips"] = base_count
        seg_asset = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"count": len(rows), "source": "core/timeline_to_segments",
                        "total_seconds": report["total_seconds"]}),
            payload=rows)
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"keys": list(report.keys())}),
            payload=report)
        return {"segments": seg_asset, "report": report_asset}
