"""核心插件：双轨混音布局（B7，对齐解说猫 r2 §5 generate_mixed_audio_layout 语义）。

数据流：RichScene(keep 标记) + 解说时间线 → MixedAudioLayout（互斥静音）。
- 原声裁剪（calculate_all_original_clips）：入点=对话 start - padding_before（默认 0.05），
  出点=end + padding_after（默认 0.1）；fade_in/fade_out=0.1（OriginalAudioClip 契约）。
- 互斥静音规则（r2 §5.2 实测）：原声播→解说轨静音（narration_mute_ranges=原声时间区间）；
  解说播→原声轨静音（original_mute_ranges=**旁白发声区间**，不是旁白窗口）。
  发声区间 = [start, start + tts_duration)；只有拿不到 tts_duration 时才回退窗口 end-start。
  为什么：窗口是"这一段属于哪个叙事单元"，不是"这段一直在说话"——300 s 实测
  segment 0 窗口 137.291 s 只发声 6.936 s，用窗口会让 56.900 s 原声被静掉 53.750 s（94.46%）。
- 斜坡：每静音区间 4 帧 ±ramp_ms（默认 50ms），与 core_draft_export jy_builder._volume_keyframes
  完全一致（r4 §2.13：t-50ms→1.0, t→0.0, t_end→0.0, t_end+50ms→1.0）。
- 纯确定性：无 LLM；输出 layout 供 draft_export（layout 输入）/ 渲染层消费。
- 发声时长来源优先级（1.0.3）：narration_marks（TTS marks 实测，按窗口重叠匹配）>
  条目自带 tts_duration > duration > 窗口回退。marks 口新增解决工作流层
  "同端口双来源靠边序"的语义债（规范 §3.3），n7.narration 与 n9.marks 现在走两个端口。
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


# 发声时长的字段优先级：TTS 实测 > 通用 duration > 回退窗口长度
_VOICED_KEYS = ("tts_duration", "voiced_duration", "duration")


def _voiced_duration(raw: Dict[str, Any], st: float, en: float) -> Tuple[float, str]:
    """取这一段旁白**真正发声**的时长，返回 (时长, 来源字段名)。

    why：narration 的 ``start``/``end`` 是**叙事窗口**，不是"这段一直在说话"。
    优先 ``tts_duration``（core/ai_narrate 逐条产出，或 TTS ``marks.segments[i].duration``
    实测值），其次 ``duration``；两者都没有才回退窗口长度 ``en - st``（旧语义，逐位不变）。
    """
    for key in _VOICED_KEYS:
        if raw.get(key) is None:
            continue
        v = _num(raw.get(key), -1.0)
        if v > 0:
            return round(v, 3), key
    return round(max(en - st, 0.0), 3), "window"


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


def normalize_narration_items(items: Any, marks: Any = None,
                              fusion: Dict[str, int] | None = None) -> List[Dict[str, Any]]:
    """归一化解说时间线：[{segment_index, text, start, end, tts_duration?}] → narration_track 条目。

    ``start``/``end`` 保留为**窗口**，``duration`` 是**发声时长**（优先 tts_duration），
    ``window_duration`` 另存窗口长度供对账。两者不一致时以 duration 为准去静音。

    2026-10-27：可选 ``marks``（core/tts 的 marks 输出，{segments:[{start,duration,...}]}）。
    匹配规则：tts segment 与 narration 条目窗口重叠比例最大者，取其 ``duration`` 覆写发声时长
    （duration_source="marks"）。marks 里任何段最多消费一次，防多 narration 命中同段。
    """
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
        voiced, source = _voiced_duration(raw, st, en)
        out.append({
            "type": "narration",
            "segment_index": int(raw.get("segment_index", raw.get("index", i))),
            "text": str(raw.get("text") or ""),
            "start": round(st, 3),
            "end": round(en, 3),
            # 发声时长（旧版这里是 en-st，会把 tts_duration 吞掉）
            "duration": voiced,
            "duration_source": source,
            "window_duration": round(en - st, 3),
        })
    if marks:
        out = _apply_marks_durations(out, marks, fusion)
    out.sort(key=lambda x: (x["start"], x["end"]))
    return out


def _apply_marks_durations(narration: List[Dict[str, Any]], marks: Any,
                           fusion: Dict[str, int] | None = None) -> List[Dict[str, Any]]:
    """用 TTS marks 的实测 duration 覆写 narration 条目发声时长。

    匹配：窗口重叠比例（overlap / narration 窗口长）最大的 marks 段；比例阈值 0.3，
    低于视为不相关（纯噪声段防误配）。marks 段最多消费一次。
    """
    segs = marks.get("segments") if isinstance(marks, dict) else None
    if not isinstance(segs, list):
        return narration
    used: set = set()
    fused = 0
    for n in narration:
        ns, ne = n["start"], n["end"]
        win = max(ne - ns, 1e-6)
        best_i, best_r = -1, 0.0
        for mi, ms in enumerate(segs):
            if not isinstance(ms, dict) or mi in used:
                continue
            ms_s, ms_d = _num(ms.get("start")), _num(ms.get("duration"))
            if ms_d <= 0:
                continue
            ms_e = ms_s + ms_d
            ov = max(0.0, min(ne, ms_e) - max(ns, ms_s))
            r = ov / win
            if r > best_r:
                best_r, best_i = r, mi
        if best_i >= 0 and best_r >= 0.3:
            ms = segs[best_i]
            n["duration"] = round(_num(ms.get("duration")), 3)
            n["duration_source"] = "marks"
            used.add(best_i)
            fused += 1
    if fusion is not None:
        fusion["fused"] = fused
    return narration


def build_layout(rich_scenes: Any, narration_items: Any, padding_before: float,
                 padding_after: float, ramp_ms: int, fade: float,
                 narration_marks: Any = None) -> Dict[str, Any]:
    """构造 MixedAudioLayout（r2 §5.2/§5.3 契约 + ramp_ms）。"""
    clips = calculate_original_clips(rich_scenes, padding_before, padding_after, fade)
    _fusion: Dict[str, int] = {}
    narration = normalize_narration_items(narration_items, narration_marks, _fusion)
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
    # 原声轨静音 = 旁白**发声**区间 [start, start+duration)，不是旁白窗口 [start, end)。
    # 旧版用 (n["start"], n["end"]) ⇒ 窗口内没在说话的时段也把原声静掉（300 s 实测误静 100%）。
    original_mute_ranges = merge_ranges(
        [(n["start"], round(n["start"] + n["duration"], 3)) for n in narration])
    # 统计口径（B6）：clips 之间会因 padding 自重叠，**原始相加 ≠ 真实原声长度**。
    # 任何"被静音比例"的分母必须是 merged，否则比例系统性偏小
    # （300 s 实测：raw 56.900 / merged 53.750，同一件事算出 94.46% 与 100% 两个数）。
    _clip_ranges = [(c["start"], c["end"]) for c in clips]
    _merged_clip_ranges = merge_ranges(_clip_ranges)
    _raw_original = round(sum(e - s for s, e in _clip_ranges), 3)
    _merged_original = round(sum(e - s for s, e in _merged_clip_ranges), 3)
    stats = {
        "narration_count": len(narration),
        "original_count": len(clips),
        # 发声总长（旧版此处 = 窗口总长，因为 duration 被重算过）
        "total_narration_duration": round(sum(n["duration"] for n in narration), 3),
        "total_narration_window_duration": round(
            sum(n.get("window_duration", n["end"] - n["start"]) for n in narration), 3),
        # 旧字段保持 raw 语义（向后兼容），比例请用 total_original_duration_merged
        "total_original_duration": _raw_original,
        "total_original_duration_raw": _raw_original,
        "total_original_duration_merged": _merged_original,
        "original_self_overlap": round(_raw_original - _merged_original, 3),
        "narration_mute_ranges": len(narration_mute_ranges),
        "original_mute_ranges": len(original_mute_ranges),
        "marks_fused": _fusion.get("fused", 0),
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
                # 2026-10-27：新增可选口 marks（TTS 实测时长），替代"同端口双来源靠边序"的旧语义
                PortSpec(name="narration_marks", type=PortType.JSON, label="TTS 时间戳(marks)",
                         required=False,
                         description="core/tts marks 输出 {segments:[{start,duration,...}]}；"
                                     "提供时其 per-segment duration 优先于 narration_items 的窗口/估算值"),
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
            version="1.0.3",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress")
        rs_asset = inputs.get("rich_scenes")
        narr_asset = inputs.get("narration_items")
        marks_asset = inputs.get("narration_marks")
        if rs_asset is None or narr_asset is None:
            raise RuntimeError("需要连接 富场景(已决策) 与 解说片段 输入")
        rich = store.read_json(rs_asset.id) or {}
        narr = store.read_json(narr_asset.id)
        marks = store.read_json(marks_asset.id) if marks_asset is not None else None

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
        layout = build_layout(rich, narr, padding_before, padding_after, ramp_ms, fade,
                              narration_marks=marks)
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
            "note": "互斥静音：原声播→解说轨静音(narration_mute_ranges)；解说播→原声轨静音"
                    "(original_mute_ranges=旁白**发声**区间 [start, start+duration))；"
                    "斜坡 ±%dms（jy_builder._volume_keyframes 当前硬编码 50 ms，"
                    "ramp_ms≠50 时草稿侧不生效，见立项②）" % layout["ramp_ms"],
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
    # 发声口径（1.0.1）：window=137.291 / tts_duration=6.936 时，静音只覆盖 [2.167, 9.103)
    narration_long = [
        {"segment_index": 0, "text": "长窗口", "start": 2.167, "end": 139.458,
         "tts_duration": 6.936},
        {"segment_index": 1, "text": "无 tts，回退窗口", "start": 140.0, "end": 141.5},
    ]
    l3 = build_layout({"scenes": []}, narration_long, 0.05, 0.1, 50, 0.1)
    assert l3["original_mute_ranges"] == [[2.167, 9.103], [140.0, 141.5]], l3["original_mute_ranges"]
    assert l3["narration_track"][0]["duration"] == 6.936
    assert l3["narration_track"][0]["duration_source"] == "tts_duration"
    assert l3["narration_track"][0]["window_duration"] == 137.291
    assert l3["narration_track"][1]["duration_source"] == "window"
    assert l3["stats"]["total_narration_duration"] == 8.436, l3["stats"]
    assert l3["stats"]["total_narration_window_duration"] == 138.791, l3["stats"]
    # 统计口径（B6）：clips 自重叠时 raw ≠ merged，比例必须用 merged
    rich_ov = {"schema_version": 1, "scenes": [{"index": 0, "dialogues": [
        {"id": 0, "text": "a", "start": 1.0, "end": 2.0, "keep": True},
        {"id": 1, "text": "b", "start": 1.5, "end": 2.5, "keep": True},
    ]}]}
    l4 = build_layout(rich_ov, [], 0.05, 0.1, 50, 0.1)
    # clips = [0.95,2.1] 与 [1.45,2.6] → raw 2.300 / merged 1.650 / 自重叠 0.650
    assert l4["stats"]["total_original_duration_raw"] == 2.3, l4["stats"]
    assert l4["stats"]["total_original_duration_merged"] == 1.65, l4["stats"]
    assert l4["stats"]["original_self_overlap"] == 0.65, l4["stats"]
    assert l4["stats"]["total_original_duration"] == 2.3, l4["stats"]  # 旧字段保持 raw
    # marks 融合（1.0.3）：marks 段 duration 覆写窗口回退值
    marks = {"segments": [
        {"index": 0, "text": "解说一", "start": 0.0, "end": 0.5, "duration": 0.5},
        {"index": 1, "text": "解说二", "start": 7.6, "end": 9.1, "duration": 1.5},
    ], "total_duration": 2.0}
    l5 = build_layout(rich, narr, 0.05, 0.1, 50, 0.1, narration_marks=marks)
    assert l5["narration_track"][0]["duration"] == 0.5, l5["narration_track"][0]
    assert l5["narration_track"][0]["duration_source"] == "marks"
    assert l5["narration_track"][1]["duration"] == 1.5
    assert l5["stats"]["marks_fused"] == 2, l5["stats"]
    # marks 为 None → 行为与 1.0.2 完全一致（向后兼容）
    l6 = build_layout(rich, narr, 0.05, 0.1, 50, 0.1, narration_marks=None)
    assert l6["narration_track"][0]["duration"] == 0.8
    assert l6["narration_track"][0]["duration_source"] == "window"
    assert l6["stats"]["marks_fused"] == 0
    # marks 段与 narration 窗口重叠 <0.3 → 不匹配，保持回退
    marks_noise = {"segments": [{"index": 0, "text": "x", "start": 50.0, "duration": 0.1}]}
    l7 = build_layout(rich, narr, 0.05, 0.1, 50, 0.1, narration_marks=marks_noise)
    assert l7["narration_track"][0]["duration_source"] == "window"
    assert l7["stats"]["marks_fused"] == 0
    # 同一 marks 段不被两个 narration 条目重复消费
    narr_dup = [
        {"segment_index": 0, "text": "a", "start": 0.0, "end": 0.8},
        {"segment_index": 1, "text": "b", "start": 0.1, "end": 0.9},
    ]
    marks_one = {"segments": [{"index": 0, "start": 0.0, "duration": 0.4}]}
    l8 = build_layout({"scenes": []}, narr_dup, 0.05, 0.1, 50, 0.1, narration_marks=marks_one)
    fused_n = [n for n in l8["narration_track"] if n["duration_source"] == "marks"]
    assert len(fused_n) == 1, l8["narration_track"]
    print("[B7 selftest] OK: 原声裁剪 padding/互斥区间/斜坡字段/无 keep 语义/"
          "发声区间(tts_duration 优先, 缺失回退窗口)/统计口径(raw vs merged)/"
          "marks 融合(命中+兼容+噪声拒配+单段单消费) 均通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_selftest())
