"""核心插件：智能分块（增强版 v2，对齐解说猫 r1 §3.4 实测语义）。

- 以场景边界为切点，按目标窗长聚合（target/min/max 参数化），
  末块时长 < min（默认 <120s）自动并入前一块（tail_policy=merge_back）。
- 每块保留原视频时间偏移 video_time_offset 与 scene_indices，供下游分块分析复用。
- 纯确定性算法（无 LLM），输入 scenes 必须先按 start 升序且无重叠（违反则报告，
  按 start 重排并给出 warning）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, PortType


class SmartChunkNode(NodeBase):
    """智能分块。type_id: core/smart_chunk"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/smart_chunk",
            title="智能分块",
            category="分析",
            description="按场景边界聚合成时长目标窗的分块（180s 窗/120-240s 界；末块过短并入前块）",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=True),
                PortSpec("scenes", PortType.SEGMENTS, label="场景片段", required=True),
            ],
            outputs=[
                PortSpec("chunks", PortType.JSON, label="分块结果", required=True),
                PortSpec("report", PortType.JSON, label="分块报告", required=False),
            ],
            params=[
                PortSpec("target_duration", PortType.FLOAT, label="目标窗长(秒)", default=180.0, widget="number"),
                PortSpec("min_duration", PortType.FLOAT, label="最小窗长(秒)", default=120.0, widget="number"),
                PortSpec("max_duration", PortType.FLOAT, label="最大窗长(秒)", default=240.0, widget="number"),
                PortSpec("tail_policy", PortType.STRING, label="末块策略", default="merge_back",
                         widget="select", options=["merge_back", "keep"]),
            ],
            version="2.0.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        scenes_asset = inputs.get("scenes")
        if video_asset is None or not getattr(video_asset, "path", ""):
            raise RuntimeError("invalid: 需要视频输入")
        if scenes_asset is None:
            raise RuntimeError("invalid: 需要场景片段输入")
        store = ctx["store"]
        try:
            scenes = store.read_json(scenes_asset.id) or []
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"invalid: 场景资产读取失败: {e}") from e
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError("invalid: 场景列表为空")

        target = max(1.0, float(params.get("target_duration", 180.0) or 180.0))
        min_d = max(1.0, float(params.get("min_duration", 120.0) or 120.0))
        max_d = max(target, float(params.get("max_duration", 240.0) or 240.0))
        tail_policy = str(params.get("tail_policy", "merge_back") or "merge_back")

        duration = 0.0
        try:
            duration = float((video_asset.meta or {}).get("container", {}).get("duration")
                             or video_asset.meta.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0

        warnings: List[str] = []
        rows: List[Dict[str, Any]] = []
        for i, sc in enumerate(scenes):
            if not isinstance(sc, dict):
                warnings.append(f"第 {i} 行非对象，跳过")
                continue
            try:
                s = float(sc.get("start") or 0)
                e = float(sc.get("end") or s)
            except (TypeError, ValueError):
                warnings.append(f"第 {i} 行时间非法，跳过")
                continue
            rows.append({"start": s, "end": max(e, s), "raw": sc})
        rows.sort(key=lambda r: r["start"])
        for i in range(1, len(rows)):
            if rows[i]["start"] < rows[i - 1]["end"] - 1e-6:
                warnings.append(f"场景 {i-1} 与 {i} 时间重叠（{rows[i-1]['end']:.3f} > {rows[i]['start']:.3f}），已按 start 重排")
                break
        if not rows:
            raise RuntimeError("invalid: 没有可用场景行")

        chunks: List[Dict[str, Any]] = []
        cur: List[Dict[str, Any]] = []
        cur_d = 0.0

        def close_chunk() -> None:
            nonlocal cur, cur_d
            if not cur:
                return
            s = cur[0]["start"]
            e = cur[-1]["end"]
            chunks.append({
                "chunk_index": len(chunks),
                "start": round(s, 3),
                "end": round(e, 3),
                "duration": round(e - s, 3),
                "scene_indices": [r["raw"].get("index", i) for i, r in enumerate(cur)],
                "scene_count": len(cur),
                "video_time_offset": round(s, 3),
            })
            cur = []
            cur_d = 0.0

        for r in rows:
            d = r["end"] - r["start"]
            cand = cur_d + d
            if cur and (cand > max_d or (cand > target and cur_d >= min_d)):
                close_chunk()
            cur.append(r)
            cur_d += d

        # 关闭剩余累积（最后一块）
        if cur:
            s = cur[0]["start"]
            e = cur[-1]["end"]
            chunks.append({
                "chunk_index": len(chunks),
                "start": round(s, 3),
                "end": round(e, 3),
                "duration": round(e - s, 3),
                "scene_indices": [r["raw"].get("index", i) for i, r in enumerate(cur)],
                "scene_count": len(cur),
                "video_time_offset": round(s, 3),
            })
            cur = []

        # 末块过短：merge_back → 并入前一块；keep → 保留独立块
        if len(chunks) > 1 and chunks[-1]["duration"] < min_d and tail_policy == "merge_back":
            last = chunks.pop()
            prev = chunks[-1]
            prev["end"] = last["end"]
            prev["duration"] = round(prev["end"] - prev["start"], 3)
            prev["scene_indices"] = prev["scene_indices"] + last["scene_indices"]
            prev["scene_count"] = len(prev["scene_indices"])
            warnings.append(f"最后分块太短({last['duration']:.1f}s < {min_d:g}s)，已合并到前一个")
        if not chunks:
            d = rows[-1]["end"] - rows[0]["start"]
            chunks.append({
                "chunk_index": 0, "start": round(rows[0]["start"], 3),
                "end": round(rows[-1]["end"], 3), "duration": round(d, 3),
                "scene_indices": [r["raw"].get("index", i) for i, r in enumerate(rows)],
                "scene_count": len(rows), "video_time_offset": round(rows[0]["start"], 3),
            })
        for i, c in enumerate(chunks):
            c["chunk_index"] = i

        if not duration and chunks:
            duration = chunks[-1]["end"]
        payload = {
            "schema_version": 1,
            "video_duration": round(duration, 3),
            "params": {"target_duration": target, "min_duration": min_d,
                       "max_duration": max_d, "tail_policy": tail_policy},
            "chunks": chunks,
        }
        chunks_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(chunks), "video_duration": round(duration, 3)}),
            payload=payload)
        report = {
            "ok": True,
            "chunk_count": len(chunks),
            "scene_count": len(rows),
            "video_duration": round(duration, 3),
            "params": payload["params"],
            "warnings": warnings,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"count": len(chunks)}), payload=report)
        return {"chunks": chunks_asset, "report": report_asset}