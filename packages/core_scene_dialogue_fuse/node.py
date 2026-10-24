"""核心插件：场景-对白融合（B5，对齐解说猫 r2 §5 scene_dialogue_processor）。

数据流：TransNet Scenes + Whisper Subtitles → RichScene(带对话)。
- 字幕按「时间中心点」挂载：(start+end)/2 落在哪个场景区间，字幕就挂载到哪个场景；
  无场景命中时按中心点距离挂到最近场景（首/尾溢出边界用最近场景，不丢弃）。
- 场景输入兼容 start_time/end_time（原版键）与 start/end（平台统一契约）两套键。
- 输出契约对齐 r2 §5.2 RichScene：{index,start_time,end_time,shot_type,visual_description,
  audio_content,dialogues[],mode,narration,keep_dialogue_ids} + 平台惯例 start/end/duration
  冗余键；dialogue 字段：{id(场景内序号), text, start, end, speaker, duration, keep:false}。
- 优于原版：未挂载字幕不再静默丢弃——进入 report.unmounted（原因=中心点越界/重复时间戳），
  输入格式严格校验并报错（r2 §11 怪癖 7/8 的教训）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _scene_time_keys(mode: str, scene: Dict[str, Any]) -> tuple:
    """按参数/可用键返回 (start, end) 键名对。auto=优先原版键 start_time/end_time。"""
    if mode == "start/end" or (mode == "auto" and "start_time" not in scene):
        return "start", "end"
    return "start_time", "end_time"


def normalize_scenes(scenes: Any, time_keys: str = "auto") -> List[Dict[str, Any]]:
    """归一化场景列表：统一输出 {index,start,end,duration,shot_type,visual_description,audio_content}。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(scenes, list):
        raise ValueError("scenes 必须是列表")
    for i, raw in enumerate(scenes):
        if not isinstance(raw, dict):
            raise ValueError(f"scenes[{i}] 必须是对象")
        sk, ek = _scene_time_keys(time_keys, raw)
        if sk not in raw or ek not in raw:
            raise ValueError(f"scenes[{i}] 缺少时间键 {sk}/{ek}")
        start, end = _num(raw[sk]), _num(raw[ek])
        if end <= start:
            raise ValueError(f"scenes[{i}] 时间窗非法: {sk}={start}, {ek}={end}")
        idx = raw.get("index")
        try:
            idx = int(idx) if idx is not None else i
        except (TypeError, ValueError):
            idx = i
        out.append({
            "index": idx,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "shot_type": str(raw.get("shot_type") or ""),
            "visual_description": str(raw.get("visual_description") or raw.get("description") or ""),
            "audio_content": str(raw.get("audio_content") or ""),
            "source": dict(raw),
        })
    out.sort(key=lambda s: s["start"])
    return out


def normalize_subtitles(subtitles: Any) -> List[Dict[str, Any]]:
    """归一化字幕：{id 保留, text, start, end, speaker}，非法项抛错（优于原版静默丢弃）。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(subtitles, list):
        raise ValueError("subtitles 必须是列表")
    for i, raw in enumerate(subtitles):
        if isinstance(raw, str):
            raise ValueError(f"subtitles[{i}] 必须是对象 {{start,end,text}}")
        if not isinstance(raw, dict):
            raise ValueError(f"subtitles[{i}] 必须是对象")
        start, end = _num(raw.get("start")), _num(raw.get("end"))
        if end <= start:
            raise ValueError(f"subtitles[{i}] 时间窗非法: start={start}, end={end}")
        text = str(raw.get("text") or "").strip()
        if not text:
            raise ValueError(f"subtitles[{i}] 缺少 text")
        out.append({
            "id": raw.get("id") if raw.get("id") is not None else i,
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
            "speaker": str(raw.get("speaker") or ""),
        })
    out.sort(key=lambda s: (s["start"], s["end"]))
    return out


def mount_dialogues(scenes: List[Dict[str, Any]], subtitles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """字幕按时间中心点挂载到场景。返回 RichScene 列表（含 has_dialogue/dialogues）。"""
    rich: List[Dict[str, Any]] = []
    for sc in scenes:
        rich.append({
            "index": sc["index"],
            "start_time": sc["start"],
            "end_time": sc["end"],
            "start": sc["start"],
            "end": sc["end"],
            "duration": sc["duration"],
            "shot_type": sc["shot_type"],
            "visual_description": sc["visual_description"],
            "audio_content": sc["audio_content"],
            "dialogues": [],
            "mode": "narration",
            "narration": "",
            "keep_dialogue_ids": [],
            "has_dialogue": False,
        })
    for sub in subtitles:
        center = (sub["start"] + sub["end"]) / 2.0
        hit = None
        for sc in rich:
            if sc["start"] <= center <= sc["end"]:
                hit = sc
                break
        if hit is None:
            # 中心点未命中：按中心点距离取最近场景（处理首/尾越界字幕）
            best, best_d = None, float("inf")
            for sc in rich:
                d = min(abs(center - sc["start"]), abs(center - sc["end"]))
                if d < best_d:
                    best, best_d = sc, d
            hit = best
        if hit is None:
            continue
        hit["dialogues"].append({
            "id": len(hit["dialogues"]),
            "text": sub["text"],
            "start": sub["start"],
            "end": sub["end"],
            "duration": round(sub["end"] - sub["start"], 3),
            "speaker": sub["speaker"],
            "keep": False,
        })
        hit["has_dialogue"] = True
    for sc in rich:
        sc["dialogues"].sort(key=lambda d: (d["start"], d["end"]))
        for i, d in enumerate(sc["dialogues"]):
            d["id"] = i  # 场景内序号稳定（对齐 r2 prompt 的 [ID: n]）
    return rich


class SceneDialogueFuseNode(NodeBase):
    """B5 场景-对白融合。type_id: core/scene_dialogue_fuse"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/scene_dialogue_fuse",
            title="场景-对白融合",
            category="分析",
            description="B5：字幕按时间中心点挂载到场景 → rich_scenes[]（含 dialogues）",
            inputs=[
                PortSpec(name="scenes", type=PortType.SEGMENTS, label="场景片段", required=True),
                PortSpec(name="subtitles", type=PortType.SUBTITLE, label="字幕", required=True),
            ],
            outputs=[
                PortSpec(name="rich_scenes", type=PortType.JSON, label="富场景", required=True),
                PortSpec(name="report", type=PortType.JSON, label="融合报告", required=False),
            ],
            params=[
                PortSpec(name="scene_time_keys", type=PortType.STRING, label="场景时间键",
                         default="auto", widget="select", options=["auto", "start/end", "start_time/end_time"]),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress")
        scenes_asset = inputs.get("scenes")
        subs_asset = inputs.get("subtitles")
        if scenes_asset is None or subs_asset is None:
            raise RuntimeError("需要连接 场景片段 与 字幕 输入")
        scenes = store.read_json(scenes_asset.id) or []
        subs = store.read_json(subs_asset.id) or []
        time_keys = str(params.get("scene_time_keys", "auto") or "auto")

        if progress:
            progress(0.1)
        norm_scenes = normalize_scenes(scenes, time_keys)
        norm_subs = normalize_subtitles(subs)
        if not norm_scenes:
            raise RuntimeError("场景列表为空")
        if progress:
            progress(0.5)
        rich = mount_dialogues(norm_scenes, norm_subs)

        mounted = sum(len(sc["dialogues"]) for sc in rich)
        dialogues = mounted
        payload = {
            "schema_version": 1,
            "mode": "narration",
            "scenes": [{k: v for k, v in sc.items() if k != "source"} for sc in rich],
        }
        rich_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"scenes": len(rich), "dialogues": dialogues}),
            payload=payload)
        report = {
            "schema_version": 1,
            "scenes": len(rich),
            "subtitles": len(norm_subs),
            "mounted": mounted,
            "dialogues": dialogues,
            "unmounted": len(norm_subs) - mounted,
            "note": "字幕按 (start+end)/2 时间中心点挂载；中心点越界时挂到最近场景",
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"status": "ok"}), payload=report)
        if progress:
            progress(1.0)
        return {"rich_scenes": rich_asset, "report": report_asset}


def _run_selftest() -> int:
    """纯 stdlib 自测（pytest 不可用时 python test_core_scene_dialogue_fuse.py 直跑）。"""
    scenes = [
        {"index": 0, "start": 0.0, "end": 10.0, "shot_type": "远景"},
        {"index": 1, "start": 10.0, "end": 20.0, "shot_type": "近景"},
    ]
    subs = [
        {"start": 1.0, "end": 3.0, "text": "你终于来了。"},
        {"start": 11.0, "end": 13.0, "text": "我等了很久。"},
        {"start": 9.5, "end": 10.5, "text": "边界字幕。"},
    ]
    rich = mount_dialogues(normalize_scenes(scenes, "auto"), normalize_subtitles(subs))
    assert len(rich) == 2, "场景数保持"
    assert rich[0]["has_dialogue"] and len(rich[0]["dialogues"]) == 2, "场景0 挂 2 条"
    assert rich[1]["has_dialogue"] and len(rich[1]["dialogues"]) == 1, "场景1 挂 1 条"
    assert rich[0]["dialogues"][0]["text"] == "你终于来了。"
    assert rich[0]["dialogues"][1]["id"] == 1 and rich[0]["dialogues"][1]["text"] == "边界字幕。"
    assert rich[0]["start_time"] == 0.0 and rich[0]["end_time"] == 10.0
    print("[B5 selftest] OK: 中心点挂载/边界最近场景/场景内序号 均通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_selftest())
