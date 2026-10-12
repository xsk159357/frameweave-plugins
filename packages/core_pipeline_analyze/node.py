"""核心插件：视频分析复合流水线（增强版 v2）。

把「场景检测 → ASR 字幕 → 文案生成」编排为单个节点：
- 单视频模式：video 输入 → scenes/subtitles/script/report 输出（供主链直接接线）。
- 批量模式：items（标准批次信封，item.input={video_id|path}）→ results 信封 + report；
  逐条错误隔离、checkpoint 断点续跑、并发上限参数化；首条成功项同时输出 scenes/subtitles/script 便于下游链式处理。

内部通过 app.registry 正式节点类型解析（不 import 其它插件私有模块），阶段参数透传；
任一处失败按 stage=xxx 标记到条目错误，不吞错、不影响其它条目。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType


class PipelineAnalyzeNode(NodeBase):
    """视频分析复合节点。type_id: core/pipeline_analyze"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/pipeline_analyze",
            title="视频分析流水线",
            category="分析",
            description="场景检测→ASR 字幕→文案生成 一键编排；支持批次信封批量与断点续跑",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=False),
                PortSpec("items", PortType.JSON, label="批次条目", required=False),
            ],
            outputs=[
                PortSpec("scenes", PortType.SEGMENTS, label="场景片段", required=False),
                PortSpec("subtitles", PortType.SUBTITLE, label="字幕", required=False),
                PortSpec("script", PortType.SCRIPT, label="解说文案", required=False),
                PortSpec("results", PortType.JSON, label="批次结果", required=False),
                PortSpec("report", PortType.JSON, label="流水线报告", required=True),
            ],
            params=[
                PortSpec("scene_strategy", PortType.STRING, label="场景策略", default="auto",
                         widget="select", options=["auto", "framediff", "transnetv2", "fixed_interval"]),
                PortSpec("scene_sample_fps", PortType.FLOAT, label="场景抽样帧率", default=1.0, widget="number"),
                PortSpec("scene_diff_threshold", PortType.FLOAT, label="场景帧差阈值", default=0.10, widget="number"),
                PortSpec("scene_min_duration", PortType.FLOAT, label="最小场景(秒)", default=1.0, widget="number"),
                PortSpec("scene_max_duration", PortType.FLOAT, label="最大场景(秒)", default=30.0, widget="number"),
                PortSpec("asr_engine", PortType.STRING, label="ASR 引擎", default="auto",
                         widget="select", options=["auto", "local", "remote"]),
                PortSpec("asr_model", PortType.STRING, label="ASR 模型", default="small",
                         widget="select", options=["tiny", "base", "small", "medium", "large-v3"]),
                PortSpec("asr_language", PortType.STRING, label="ASR 语言", default="zh",
                         widget="select", options=["zh", "en", "ja", "ko", "auto"]),
                PortSpec("narrate_mode", PortType.STRING, label="文案模式", default="上帝视角",
                         widget="select", options=["上帝视角", "扩写", "配画面", "仿写"]),
                PortSpec("narrate_style", PortType.STRING, label="文案风格", default="悬疑解说",
                         widget="select", options=["悬疑解说", "搞笑解说", "知识科普", "情感讲述", "客观平实", "自定义"]),
                PortSpec("narrate_language", PortType.STRING, label="文案语言", default="中文",
                         widget="select", options=["中文", "英语", "日语", "韩语", "泰语", "越南语", "自动"]),
                PortSpec("provider", PortType.STRING, label="LLM Provider", default="auto",
                         widget="select", options=["auto", "openai", "gemini"]),
                PortSpec("api_key", PortType.STRING, label="API Key", required=False, default="",
                         widget="password", description="支持 @secret:名称 引用保险箱"),
                PortSpec("base_url", PortType.STRING, label="API 地址", required=False, default="", widget="text"),
                PortSpec("model", PortType.STRING, label="LLM 模型", default="gpt-4o", widget="text"),
                PortSpec("target_duration", PortType.FLOAT, label="目标时长(秒,0=自动)", default=0.0, widget="number"),
                PortSpec("target_chars", PortType.INT, label="目标字数(0=自动)", default=0, widget="number"),
                PortSpec("temperature", PortType.FLOAT, label="温度", default=0.7, widget="number"),
                PortSpec("concurrency", PortType.INT, label="并发数", default=2, widget="number"),
                PortSpec("resume", PortType.BOOL, label="断点续跑", default=True, widget="toggle"),
                PortSpec("json_mode", PortType.BOOL, label="结构化输出", default=True, widget="toggle"),
                PortSpec("local_match", PortType.BOOL, label="本地匹配兜底", default=True, widget="toggle"),
            ],
            version="2.0.0",
        )

    def _resolve_video(self, ctx: dict, payload: Dict[str, Any]) -> Optional[Asset]:
        store = ctx["store"]
        vid = payload.get("video_id")
        if vid:
            asset = store.get(str(vid))
            if asset is not None:
                return asset
        p = payload.get("path") or payload.get("video_path")
        if p and os.path.isfile(str(p)):
            meta = {"path": str(p), "name": os.path.basename(str(p))}
            return Asset(id="", kind=PortType.VIDEO.value, path=str(p), meta=meta)
        return None

    async def _analyze_one(self, ctx: dict, video_asset: Asset,
                           p: Dict[str, Any]) -> Dict[str, Any]:
        from app.registry import get_class
        progress = ctx.get("progress") or (lambda x: None)
        store = ctx["store"]

        def _stage_progress(a: float, b: float):
            return lambda v: progress(a + (b - a) * min(max(v, 0.0), 1.0))

        # 阶段 1：场景检测
        scene_ctx = dict(ctx, progress=_stage_progress(0.02, 0.32))
        try:
            scene_cls = get_class("core/scene_detect")
        except KeyError as e:
            raise RuntimeError(f"stage=scene_detect: 插件未注册: {e}") from e
        scene_out = await scene_cls().run(scene_ctx, {"video": video_asset}, {
            "strategy": p.get("scene_strategy", "auto"),
            "sample_fps": p.get("scene_sample_fps", 1.0),
            "diff_threshold": p.get("scene_diff_threshold", 0.10),
            "min_duration": p.get("scene_min_duration", 1.0),
            "max_duration": p.get("scene_max_duration", 30.0),
        })
        scenes_asset = scene_out.get("scenes")
        if scenes_asset is None:
            raise RuntimeError("stage=scene_detect: 未产出场景资产")

        # 阶段 2：ASR 字幕
        asr_ctx = dict(ctx, progress=_stage_progress(0.34, 0.62))
        try:
            asr_cls = get_class("core/asr")
        except KeyError as e:
            raise RuntimeError(f"stage=asr: 插件未注册: {e}") from e
        asr_out = await asr_cls().run(asr_ctx, {"video": video_asset, "audio": None}, {
            "engine": p.get("asr_engine", "auto"),
            "model": p.get("asr_model", "small"),
            "language": p.get("asr_language", "zh"),
            "api_key": p.get("api_key", ""),
            "base_url": p.get("base_url", ""),
            "remote_model": "whisper-1",
        })
        sub_asset = asr_out.get("subtitles")

        # 阶段 3：文案生成
        narrate_ctx = dict(ctx, progress=_stage_progress(0.64, 0.98))
        try:
            narrate_cls = get_class("core/ai_narrate")
        except KeyError as e:
            raise RuntimeError(f"stage=narrate: 插件未注册: {e}") from e
        narrate_out = await narrate_cls().run(narrate_ctx, {
            "video": video_asset, "scenes": scenes_asset,
            "descriptions": None, "draft": None,
        }, {
            "mode": p.get("narrate_mode", "上帝视角"),
            "style": p.get("narrate_style", "悬疑解说"),
            "language": p.get("narrate_language", "中文"),
            "provider": p.get("provider", "auto"),
            "api_key": p.get("api_key", ""),
            "base_url": p.get("base_url", ""),
            "model": p.get("model", "gpt-4o"),
            "target_duration": p.get("target_duration", 0.0),
            "target_chars": p.get("target_chars", 0),
            "temperature": p.get("temperature", 0.7),
            "json_mode": p.get("json_mode", True),
            "local_match": p.get("local_match", True),
        })
        script_asset = narrate_out.get("script")
        if script_asset is None:
            raise RuntimeError("stage=narrate: 未产出文案资产")

        return {
            "scenes_id": scenes_asset.id,
            "subtitles_id": (sub_asset.id if sub_asset else ""),
            "script_id": script_asset.id,
            "narration_id": getattr(narrate_out.get("narration"), "id", ""),
            "scene_count": int((scenes_asset.meta or {}).get("count", 0) or 0),
            "subtitle_count": int((sub_asset.meta or {}).get("count", 0) or 0),
            "chars": int((script_asset.meta or {}).get("chars", 0) or 0),
            "duration_estimate": (script_asset.meta or {}).get("duration_estimate"),
            "scene_report": getattr(scene_out.get("report"), "id", ""),
            "asr_report": getattr(asr_out.get("report"), "id", ""),
            "narrate_report": getattr(narrate_out.get("report"), "id", ""),
        }

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        from app import batchlib
        video_asset = inputs.get("video")
        items_asset = inputs.get("items")
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda x: None)

        items_data = None
        if items_asset is not None:
            items_data = store.read_json(items_asset.id)
        if items_data is not None:
            # ---- 批量模式 ----
            batch = batchlib.normalize_batch(items_data)
            concurrency = max(1, int(params.get("concurrency", 2) or 2))
            resume = bool(params.get("resume", True))
            cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                              f"pipeline_{ctx.get('node_id', 'pipeline_analyze')}.json")
            p = dict(params)

            async def worker(item: dict) -> dict:
                payload = item.get("input")
                if not isinstance(payload, dict):
                    raise RuntimeError("invalid: 条目输入必须是对象（含 video_id 或 path）")
                asset = self._resolve_video(ctx, payload)
                if asset is None:
                    raise RuntimeError("invalid: 条目缺少有效 video_id 或 path")
                r = await self._analyze_one(ctx, asset, p)
                return r

            batch = await batchlib.run_items(batch, worker, concurrency=concurrency,
                                             checkpoint_path=cp, progress=progress,
                                             retries=0, resume=resume)
            report = batchlib.summarize(batch)
            clean = []
            for it in batch["items"]:
                r = it.get("result")
                rec = {
                    "item_id": it.get("item_id"), "index": it.get("index"),
                    "status": it.get("status"), "name": (it.get("input") or {}).get("name"),
                    "error": it.get("error"), "warnings": it.get("warnings"),
                    "attempt": it.get("attempt"),
                }
                if isinstance(r, dict):
                    rec.update({k: r.get(k) for k in (
                        "scenes_id", "subtitles_id", "script_id", "narration_id",
                        "scene_count", "subtitle_count", "chars", "duration_estimate")})
                clean.append(rec)
            results_payload = {"batch_id": batch.get("batch_id"), "items": clean}
            results_asset = store.save_asset(
                Asset(id="", kind=PortType.JSON.value,
                      meta={"count": len(clean), "ok": report.get("succeeded", 0)}),
                payload=results_payload)
            report_asset = store.save_asset(
                Asset(id="", kind=PortType.JSON.value, meta={"mode": "batch"}), payload=report)
            # 首条成功项资产（供下游链式接线）
            first_scenes = first_sub = first_script = None
            for it in clean:
                if it.get("status") == "succeeded" and it.get("scenes_id"):
                    first_scenes = store.get(it["scenes_id"])
                    first_sub = store.get(it["subtitles_id"]) if it.get("subtitles_id") else None
                    first_script = store.get(it["script_id"]) if it.get("script_id") else None
                    break
            return {"scenes": first_scenes, "subtitles": first_sub, "script": first_script,
                    "results": results_asset, "report": report_asset}

        # ---- 单视频模式 ----
        if video_asset is None or not getattr(video_asset, "path", ""):
            raise RuntimeError("invalid: 需要 video 输入（单模式）或 items 输入（批量模式）")
        r = await self._analyze_one(ctx, video_asset, dict(params))
        report = {
            "ok": True, "mode": "single",
            "scenes_id": r["scenes_id"], "subtitles_id": r["subtitles_id"],
            "script_id": r["script_id"], "chars": r["chars"],
            "scene_count": r["scene_count"], "subtitle_count": r["subtitle_count"],
            "duration_estimate": r["duration_estimate"],
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": "single"}), payload=report)
        return {"scenes": store.get(r["scenes_id"]),
                "subtitles": store.get(r["subtitles_id"]) if r["subtitles_id"] else None,
                "script": store.get(r["script_id"]),
                "results": None, "report": report_asset}