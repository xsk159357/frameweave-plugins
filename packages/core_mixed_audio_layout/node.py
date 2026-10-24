"""核心插件：双轨混音布局（B7，对齐解说猫 r2 §5 generate_mixed_audio_layout 语义）。

数据流：RichScene(keep 标记) + 解说时间线 → MixedAudioLayout（互斥静音）。
- 原声裁剪（calculate_all_original_clips）：入点=对话 start - padding_before（默认 0.05），
  出点=end + padding_after（默认 0.1）；fade_in/fade_out=0.1（OriginalAudioClip 契约）。
- 互斥静音规则（r2 §5.2 实测）：原声播→解说轨静音（narration_mute_ranges=原声时间区间）；
  解说播→原声轨静音（original_mute_ranges=解说时间区间）。
- 斜坡：每静音区间 4 帧 ±ramp_ms（默认 50ms），与 core_draft_export jy_builder._volume_keyframes
  完全一致（r4 §2.13：t-50ms→1.0, t→0.0, t_end→0.0, t_end+50ms→1.0）。
- 纯确定性：无 LLM；输出 layout 供 draft_export（layout 输入）/ 渲染层消费。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def merge_ranges(ranges: List[Tuple[float, float]], gap: float = 0.0) -> List[List[float]]:
    """区间合并（升序、重叠/邻接合并）。输入 [[s,e],...] 秒。"""
    if not ranges:
        return []
    rs = sorted((float(s), float(e)) for s, e in ranges if float(e) > float(s))
    out: List[List[float]] = []
    cur_s, cur_e = rs[0]
    for s, e in rs[1:]:
        if s <= cur_e + gap:
            cur_e = max(cur_e, e)
        else:
            out.append([round(cur_s, 3), round(cur_e, 3)])
            cur_s, cur_e = s, e
    out.append([round(cur_s, 3), round(cur_e, 3)])
    return out


def calculate_original_clips(rich_scenes: Any, padding_before: float = 0.05,
                             padding_after: float = 0.1, fade: float = 0.1) -> List[Dict[str, Any]]:
    """从 keep 标记对话计算原声裁剪片段（r2 §5.3 calculate_all_original_clips）。"""
    clips: List[Dict[str, Any]] = []
    scenes = rich_scenes.get("scenes") if isinstance(rich_scenes, dict) else None
    if not isinstance(scenes, list):
        raise ValueError("rich_scenes 缺少 scenes 列表（请先接 B5/B6）")
    for sc in scenes:
        if not isinstance(sc, dict):
            continue
        si = sc.get("index", 0)
        for d in sc.get("dialogues") or []:
            if not d.get("keep"):
                continue
            st = round(max(_num(d.get("start")) - padding_before, 0.0), 3)
            en = round(_num(d.get("end")) + padding_after, 3)
            clips.append({
                "dialogue_id": d.get("id", 0),
                "text": str(d.get("text") or ""),
                "start": st,
                "end": en,
                "src_start": st,
                "src_end": en,
                "original_start": round(_num(d.get("start")), 3),
                "original_end": round(_num(d.get("end")), 3),
                "scene_index": si,
                "fade_in": round(fade, 3),
                "fade_out": round(fade, 3),
            })
    clips.sort(key=lambda c: (c["start"], c["end"]))
    return clips


def normalize_narration_items(items: Any) -> List[Dict[str, Any]]:
    """归一化解说时间线：[{segment_index, text, start, end}] → narration_track 条目。"""
    out: List[Dict[str, Any]] = []
    if items is None:
        return out
    if isinstance(items, dict):
        items = items.get("segments") or items.get("items") or []
    if not isinstance(items, list):
        raise ValueError("narration_items 必须是列表或 {segments:[...]}")
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ValueError(f"narration_items[{i}] 必须是对象")
        st, en = _num(raw.get("start")), _num(raw.get("end"))
        if en <= st:
            continue
        out.append({
            "type": "narration",
            "segment_index": int(raw.get("segment_index", raw.get("index", i))),
            "text": str(raw.get("text") or ""),
            "start": round(st, 3),
            "end": round(en, 3),
            "duration": round(en - st, 3),
        })
    out.sort(key=lambda x: (x["start"], x["end"]))
    return out


def build_layout(rich_scenes: Any, narration_items: Any, padding_before: float,
                 padding_after: float, ramp_ms: int, fade: float) -> Dict[str, Any]:
    """构造 MixedAudioLayout（r2 §5.2/§5.3 契约 + ramp_ms）。"""
    clips = calculate_original_clips(rich_scenes, padding_before, padding_after, fade)
    narration = normalize_narration_items(narration_items)
    original_track = [{
        "type": "original",
        "start": c["start"], "end": c["end"],
        "src_start": c["src_start"], "src_end": c["src_end"],
        "scene_index": c["scene_index"],
        "dialogue_id": c["dialogue_id"],
        "dialogue_text": c["text"],
        "fade_in": c["fade_in"], "fade_out": c["fade_out"],
    } for c in clips]
    narration_mute_ranges = merge_ranges([(c["start"], c["end"]) for c in clips])
    original_mute_ranges = merge_ranges([(n["start"], n["end"]) for n in narration])
    stats = {
        "narration_count": len(narration),
        "original_count": len(clips),
        "total_narration_duration": round(sum(n["duration"] for n in narration), 3),
        "total_original_duration": round(sum(c["end"] - c["start"] for c in clips), 3),
        "narration_mute_ranges": len(narration_mute_ranges),
        "original_mute_ranges": len(original_mute_ranges),
    }
    return {
        "schema_version": 1,
        "ramp_ms": max(1, int(ramp_ms)),
        "padding_before": padding_before,
        "padding_after": padding_after,
        "narration_track": narration,
        "original_track": original_track,
        "original_clips": clips,
        # 互斥静音：原声播→解说轨静音；解说播→原声轨静音
        "narration_mute_ranges": narration_mute_ranges,
        "original_mute_ranges": original_mute_ranges,
        "stats": stats,
    }


class MixedAudioLayoutNode(NodeBase):
    """B7 双轨混音布局。type_id: core/mixed_audio_layout"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/mixed_audio_layout",
            title="双轨混音布局",
            category="剪辑",
            description="B7：解说轨+原声轨互斥静音布局（±50ms 斜坡），可导入剪映/渲染",
            inputs=[
                PortSpec(name="rich_scenes", type=PortType.JSON, label="富场景(已决策)", required=True),
                PortSpec(name="narration_items", type=PortType.JSON, label="解说片段", required=True),
            ],
            outputs=[
                PortSpec(name="layout", type=PortType.JSON, label="混音布局", required=True),
                PortSpec(name="report", type=PortType.JSON, label="布局报告", required=False),
            ],
            params=[
                PortSpec(name="padding_before", type=PortType.FLOAT, label="原声入点padding(秒)",
                         default=0.05, widget="number"),
                PortSpec(name="padding_after", type=PortType.FLOAT, label="原声出点padding(秒)",
                         default=0.1, widget="number"),
                PortSpec(name="ramp_ms", type=PortType.INT, label="静音斜坡(ms)", default=50,
                         widget="number"),
                PortSpec(name="fade", type=PortType.FLOAT, label="原声淡入淡出(秒)", default=0.1,
                         widget="number"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress")
        rs_asset = inputs.get("rich_scenes")
        narr_asset = inputs.get("narration_items")
        if rs_asset is None or narr_asset is None:
            raise RuntimeError("需要连接 富场景(已决策) 与 解说片段 输入")
        rich = store.read_json(rs_asset.id) or {}
        narr = store.read_json(narr_asset.id)

        if progress:
            progress(0.2)
        try:
            padding_before = float(params.get("padding_before", 0.05) or 0.05)
            padding_after = float(params.get("padding_after", 0.1) or 0.1)
            ramp_ms = int(params.get("ramp_ms", 50) or 50)
            fade = float(params.get("fade", 0.1) or 0.1)
        except (TypeError, ValueError):
            padding_before, padding_after, ramp_ms, fade = 0.05, 0.1, 50, 0.1
        if progress:
            progress(0.5)
        layout = build_layout(rich, narr, padding_before, padding_after, ramp_ms, fade)
        layout_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"narration": layout["stats"]["narration_count"],
                        "original": layout["stats"]["original_count"]}),
            payload=layout)
        report = {
            "schema_version": 1,
            "ramp_ms": layout["ramp_ms"],
            "padding": {"before": padding_before, "after": padding_after},
            "stats": layout["stats"],
            "note": "互斥静音：原声播→解说轨静音(narration_mute_ranges)；解说播→原声轨静音(original_mute_ranges)；"
                    "斜坡 ±%dms 与 jy_builder 语义一致" % layout["ramp_ms"],
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"status": "ok"}), payload=report)
        if progress:
            progress(1.0)
        return {"layout": layout_asset, "report": report_asset}


def _run_selftest() -> int:
    """纯 stdlib 自测：互斥区间/原声裁剪/斜坡契约。"""
    rich = {"schema_version": 1, "scenes": [
        {"index": 0, "dialogues": [
            {"id": 0, "text": "你终于来了。", "start": 1.0, "end": 3.0, "keep": True},
            {"id": 1, "text": "我等了很久。", "start": 5.0, "end": 7.5, "keep": False},
        ]},
        {"index": 1, "dialogues": [
            {"id": 0, "text": "别过来！", "start": 11.0, "end": 12.0, "keep": True},
        ]},
    ]}
    narr = [
        {"segment_index": 0, "text": "解说一", "start": 0.0, "end": 0.8},
        {"segment_index": 1, "text": "解说二", "start": 7.6, "end": 10.9},
    ]
    layout = build_layout(rich, narr, 0.05, 0.1, 50, 0.1)
    # 原声裁剪：1.0-3.0 +0.05/0.1 → 0.95-3.1；11.0-12.0 → 10.95-12.1
    assert layout["original_clips"][0]["start"] == 0.95 and layout["original_clips"][0]["end"] == 3.1
    assert layout["original_clips"][1]["start"] == 10.95 and layout["original_clips"][1]["end"] == 12.1
    # 互斥：narration_mute_ranges = 原声区间；original_mute_ranges = 解说区间
    assert layout["narration_mute_ranges"] == [[0.95, 3.1], [10.95, 12.1]], layout["narration_mute_ranges"]
    assert layout["original_mute_ranges"] == [[0.0, 0.8], [7.6, 10.9]], layout["original_mute_ranges"]
    assert layout["ramp_ms"] == 50
    assert layout["stats"]["original_count"] == 2 and layout["stats"]["narration_count"] == 2
    # 无 keep 场景：不产生原声/静音解说
    rich2 = {"schema_version": 1, "scenes": [
        {"index": 0, "dialogues": [{"id": 0, "text": "x", "start": 1.0, "end": 2.0, "keep": False}]}]}
    l2 = build_layout(rich2, narr, 0.05, 0.1, 50, 0.1)
    assert l2["original_track"] == [] and l2["narration_mute_ranges"] == []
    print("[B7 selftest] OK: 原声裁剪 padding/互斥区间/斜坡字段/无 keep 语义 均通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_selftest())
