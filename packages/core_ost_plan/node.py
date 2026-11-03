"""原片穿插编排节点：把「该留原声的地方」算成渲染端能吃的时间区间。

## 这个节点补的是契约的另一半

`core/ai_narrate` 打开 `ost_ratio` 时，会让模型在解说词里写
`【原片:12.5-18.0】` 这样的标记。但那个标记此前**只进 prompt、不进数据**：
落进 `text` 里，被下游当成解说正文去配音，于是成片里会听见有人把
「原片十二点五到十八」念出来，而原声一秒都没放。

本节点做两件事：
  1. 把标记从解说正文里摘出来（`core/ai_narrate` 3.1.0 起也会先摘一遍，
     这里兜住旧版本与手工文案）；
  2. 算成 `core/video_render` 已经支持的 `layout` 契约
     `{"original_clips": [{"start","end",...}]}`（源时间轴），
     渲染时 `audio_mix=mix` 就只在区间内放出原声。

## 为什么复用 layout 而不新造端口

`mixed_audio_layout` 这个契约在渲染端已经落地半年（`_layout_clip_ranges` +
`_map_clips_to_output`，含转场偏移递推与区间丢弃计数）。新造一套「ost 片段」
端口等于让渲染端认两份结构，迟早对不齐。这里只生产它已经在读的那份。

## 三种选段来源

- `marker`：只认解说词里的原片标记——模型的语义判断，最贴合叙事；
- `dialogue`：按台词密度选（需要 `descriptions` 带 lines）——没有标记时的兜底，
  优先保留「信息量最大的那句原话」；
- `mixed`（默认）：标记优先，达不到目标占比时用台词密度补，补了什么写进 meta。

## 夹取规则（不静默）

clip 落在时间线没有覆盖的源区间时，渲染端会把它算进 `dropped_seconds`。
既然注定放不出来，就在这里先夹掉并写告警——让用户看到「这一段原声没进去」，
而不是拿到一个安静得莫名其妙的成片。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

# 解说猫的标记写法：【原片:12.5-18.0】/ 【原片：12.5~18】/ [原片:12-18]
_MARK = re.compile(r"[【\[]\s*原片\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*[-~－—至]\s*"
                   r"([0-9]+(?:\.[0-9]+)?)\s*[】\]]")


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _read_payload(store: Any, asset: Any) -> Any:
    if asset is None:
        return None
    try:
        return store.read_json(asset.id) if hasattr(asset, "id") else asset
    except Exception:  # noqa: BLE001
        return None


def _as_list(raw: Any, *keys: str) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        for k in keys:
            if isinstance(raw.get(k), list):
                return [x for x in raw[k] if isinstance(x, dict)]
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return []


def _clip_from_marks(seg: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """先从结构化字段取，再退回文本标记。"""
    ost = seg.get("ost")
    if isinstance(ost, dict):
        a, b = _num(ost.get("start"), -1), _num(ost.get("end"), -1)
        if b > a >= 0:
            return a, b
    if isinstance(ost, (list, tuple)) and len(ost) >= 2:
        a, b = _num(ost[0], -1), _num(ost[1], -1)
        if b > a >= 0:
            return a, b
    m = _MARK.search(str(seg.get("text") or ""))
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if b > a:
            return a, b
    return None


def _merge(clips: List[Tuple[float, float]], gap: float) -> List[Tuple[float, float]]:
    if not clips:
        return []
    clips = sorted(clips)
    out = [list(clips[0])]
    for a, b in clips[1:]:
        if a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _coverage(ranges: List[Tuple[float, float]], total: float) -> float:
    if total <= 0:
        return 0.0
    sec = sum(max(0.0, b - a) for a, b in ranges)
    return round(sec / total, 4)


class OstPlanNode(NodeBase):
    """type_id: core/ost_plan"""

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress")

        narration = _as_list(_read_payload(store, inputs.get("narration")),
                             "narration", "items", "segments")
        descs = _as_list(_read_payload(store, inputs.get("descriptions")),
                         "descriptions", "items", "scenes")
        timeline = _as_list(_read_payload(store, inputs.get("timeline")),
                            "timeline", "items", "segments")
        if not narration and not descs:
            raise RuntimeError("既没有解说词也没有场景描述：本节点至少要一个输入才能判断"
                               "哪里留原声")

        try:
            target_ratio = float(params.get("ost_ratio", 0.15) or 0.0)
        except (TypeError, ValueError):
            target_ratio = 0.15
        target_ratio = min(max(target_ratio, 0.0), 0.6)
        try:
            pad = max(float(params.get("padding", 0.2) or 0.0), 0.0)
        except (TypeError, ValueError):
            pad = 0.2
        try:
            min_clip = max(float(params.get("min_clip", 1.5) or 0.0), 0.0)
        except (TypeError, ValueError):
            min_clip = 1.5
        try:
            max_clip = max(float(params.get("max_clip", 20.0) or 0.0), min_clip)
        except (TypeError, ValueError):
            max_clip = 20.0
        try:
            merge_gap = max(float(params.get("merge_gap", 0.3) or 0.0), 0.0)
        except (TypeError, ValueError):
            merge_gap = 0.3
        source = str(params.get("source", "mixed") or "mixed").strip().lower()
        if source not in ("marker", "dialogue", "mixed"):
            source = "mixed"

        warnings: List[str] = []
        raw_clips: List[Tuple[float, float, str, Optional[int]]] = []

        # ---- 1) 标记优先
        if source in ("marker", "mixed"):
            for seg in narration:
                hit = _clip_from_marks(seg)
                if hit:
                    raw_clips.append((hit[0], hit[1], "marker", None))
            if not raw_clips and source == "marker":
                warnings.append("解说词里没有原片标记：ai_narrate 的 ost_ratio 可能为 0，"
                                "或模型没有输出；本节点将产出空 layout")

        # ---- 2) 台词密度补位（只在不达目标占比时）
        src_total = 0.0
        for seg in timeline:
            src_total = max(src_total, _num(seg.get("end"), 0.0))
        if src_total <= 0:
            for seg in descs:
                src_total = max(src_total, _num(seg.get("end"), 0.0))
        out_total = 0.0
        if timeline:
            out_total = _num((inputs["timeline"].meta or {}).get("total_duration"), 0.0) \
                if getattr(inputs.get("timeline"), "meta", None) else 0.0
        total_for_ratio = out_total or src_total or 0.0

        if source in ("dialogue", "mixed") and descs:
            covered = _coverage([(a, b) for a, b, _, _ in raw_clips], total_for_ratio) \
                if total_for_ratio else 0.0
            if covered < target_ratio:
                cands: List[Tuple[int, float, float, float]] = []
                for seg in descs:
                    lines = seg.get("lines")
                    if not isinstance(lines, list) or not lines:
                        continue
                    chars = sum(len(str(ln.get("text") or "")) for ln in lines
                                if isinstance(ln, dict))
                    if chars <= 0:
                        continue
                    a = min(_num(ln.get("start"), 0.0) for ln in lines if isinstance(ln, dict))
                    b = max(_num(ln.get("end"), 0.0) for ln in lines if isinstance(ln, dict))
                    if b > a:
                        try:
                            sidx = int(seg.get("index"))
                        except (TypeError, ValueError):
                            sidx = None
                        cands.append((chars, a, b, sidx))
                cands.sort(key=lambda x: -x[0])
                for chars, a, b, sidx in cands:
                    if total_for_ratio <= 0:
                        break
                    if _coverage([(x, y) for x, y, _, _ in raw_clips], total_for_ratio) \
                            >= target_ratio:
                        break
                    # 已经和现有区间大面积重叠的就不重复加
                    overlap = sum(max(0.0, min(b, y) - max(a, x))
                                  for x, y, _, _ in raw_clips)
                    if overlap >= (b - a) * 0.6:
                        continue
                    raw_clips.append((a, b, "dialogue", sidx))
        if source in ("dialogue", "mixed") and not descs:
            warnings.append("没有场景描述输入：无法按台词密度补位，只用解说词标记")

        if progress:
            progress(0.5)

        # ---- 3) 加 padding / 限长 / 合并
        padded: List[Tuple[float, float]] = []
        for a, b, _, _ in raw_clips:
            a2 = max(a - pad, 0.0)
            b2 = b + pad
            if b2 - a2 > max_clip:
                b2 = a2 + max_clip
            if b2 - a2 < min_clip:
                b2 = a2 + min_clip
                warnings.append("原片区间 %.2f-%.2f 短于下限 %.2fs，已补长到下限"
                                % (a, b, min_clip))
            padded.append((a2, b2))
        merged = _merge(padded, merge_gap)

        # ---- 4) 夹到时间线覆盖范围（放不出来的先摘掉）
        keep: List[Tuple[float, float]] = []
        dropped_sec = 0.0
        if timeline:
            src_ranges = [(_num(s.get("start"), 0.0), _num(s.get("end"), 0.0))
                          for s in timeline]
            src_ranges = [(a, b) for a, b in src_ranges if b > a]
            for a, b in merged:
                parts = []
                for x, y in src_ranges:
                    lo, hi = max(a, x), min(b, y)
                    if hi - lo > 1e-3:
                        parts.append((lo, hi))
                if parts:
                    for lo, hi in parts:
                        keep.append((lo, hi))
                else:
                    dropped_sec += max(0.0, b - a)
                    warnings.append("原片区间 %.2f-%.2f 不在任何画面片段里，已丢弃"
                                    "（渲染时它本来也放不出来）" % (a, b))
            keep = _merge(keep, 1e-3)
        else:
            keep = merged

        clips: List[Dict[str, Any]] = []
        for i, (a, b) in enumerate(keep):
            reason = "marker"
            sidx = None
            for ra, rb, r_reason, r_idx in raw_clips:
                if ra < b and rb > a:
                    reason, sidx = r_reason, r_idx
                    break
            clips.append({
                "index": i,
                "start": round(a, 3),
                "end": round(b, 3),
                "duration": round(b - a, 3),
                "padding_before": round(pad, 3),
                "padding_after": round(pad, 3),
                "reason": reason,
                "scene_index": sidx,
            })

        total = round(sum(c["duration"] for c in clips), 3)
        ratio = _coverage([(c["start"], c["end"]) for c in clips], total_for_ratio) \
            if total_for_ratio else 0.0
        if target_ratio > 0 and ratio < target_ratio * 0.5 and clips:
            warnings.append("原片实际占比 %.0f%% 不到目标 %.0f%% 的一半：标记给少了，"
                            "或可用画面不够长" % (ratio * 100, target_ratio * 100))
        if dropped_sec > 0:
            warnings.append("共 %.2fs 原声片段落在画面之外被丢弃" % dropped_sec)

        payload = {
            "original_clips": clips,
            "total": total,
            "ratio": ratio,
            "target_ratio": target_ratio,
            "source_mode": source,
            "dropped_seconds": round(dropped_sec, 3),
        }
        out = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,   # v3 约定：layout 用 SEGMENTS 承载
                  meta={"clip_count": len(clips),
                        "total_seconds": total,
                        "ratio": ratio,
                        "target_ratio": target_ratio,
                        "source_mode": source,
                        "dropped_seconds": round(dropped_sec, 3),
                        "params": {"padding": pad, "min_clip": min_clip,
                                   "max_clip": max_clip, "merge_gap": merge_gap},
                        "degraded": bool(warnings),
                        "warnings": list(warnings)}),
            payload=payload)
        if progress:
            progress(1.0)
        return {"layout": out}
