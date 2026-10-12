# -*- coding: utf-8 -*-
"""一键出片流水线（user/jsm_pipeline，t24 交付）。

把「场景检测 →(可选)智能分块 →(可选)ASR 字幕 →AI 文案 →文案-画面匹配 →
字幕装配 →TTS 配音 →MP4 渲染」编排为单个节点：

- 单视频模式：video 输入 → rendered/report/scenes/script/audio/timeline/styled_subtitle 输出，
  供主链直接接线或人工复核。
- 批量模式：items（标准批次信封，item.input = {video_id|path|name}）→ results 信封 + report；
  逐条错误隔离、checkpoint 断点续跑、并发上限参数化；首条成功项同时输出成品与中间资产。

内部通过 app.registry 正式节点类型解析（不 import 其它插件私有模块），阶段参数透传；
任一处失败按 stage=xxx 标记到条目错误，不吞错、不影响其它条目。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


# ---------------------------------------------------------------- 参数表（spec 与 manifest 的单一来源）
# (name, type, label, default, widget, options, required, description)
_PARAM_DEFS: List[tuple] = [
    ("scene_strategy", "STRING", "场景策略", "auto", "select",
     ["auto", "framediff", "transnetv2", "fixed_interval"], True, "auto=优先 TransNetV2，不可用降级帧差"),
    ("scene_sample_fps", "FLOAT", "场景抽样帧率", 1.0, "number", None, True, ""),
    ("scene_diff_threshold", "FLOAT", "场景帧差阈值", 0.10, "number", None, True, ""),
    ("scene_min_duration", "FLOAT", "最小片段(秒)", 1.0, "number", None, True, ""),
    ("scene_max_duration", "FLOAT", "最大片段(秒)", 30.0, "number", None, True, ""),
    ("chunk_enabled", "BOOL", "智能分块", False, "toggle", None, True, "开启后对长视频做 180s 目标窗分块"),
    ("chunk_target_duration", "FLOAT", "分块目标窗(秒)", 180.0, "number", None, True, ""),
    ("chunk_min_duration", "FLOAT", "分块最小窗(秒)", 120.0, "number", None, True, ""),
    ("chunk_max_duration", "FLOAT", "分块最大窗(秒)", 240.0, "number", None, True, ""),
    ("asr_mode", "STRING", "ASR 字幕", "off", "select",
     ["off", "auto", "local", "remote"], True, "off=不转字幕（从文案段落生成字幕）；auto=本地优先"),
    ("asr_model", "STRING", "ASR 本地模型", "small", "select",
     ["tiny", "base", "small", "medium", "large-v3"], True, ""),
    ("asr_language", "STRING", "ASR 语言", "zh", "select",
     ["zh", "en", "ja", "ko", "auto"], True, ""),
    ("asr_api_key", "STRING", "ASR API Key", "", "password", None, False, "远程 ASR 用；支持 @secret:名称"),
    ("asr_base_url", "STRING", "ASR 中转地址", "https://api.openai.com/v1", "text", None, False, ""),
    ("narrate_mode", "STRING", "文案模式", "上帝视角", "select",
     ["上帝视角", "扩写", "配画面", "仿写"], True, ""),
    ("narrate_style", "STRING", "文案风格", "客观平实", "select",
     ["悬疑解说", "搞笑解说", "知识科普", "情感讲述", "客观平实", "自定义"], True, ""),
    ("narrate_language", "STRING", "文案语言", "中文", "select",
     ["中文", "英语", "日语", "韩语", "泰语", "越南语", "自动"], True, ""),
    ("provider", "STRING", "LLM Provider", "auto", "select",
     ["auto", "openai", "gemini"], True, ""),
    ("api_key", "STRING", "LLM API Key", "", "password", None, False, "支持 @secret:名称 引用保险箱"),
    ("base_url", "STRING", "LLM API 地址", "", "text", None, False, ""),
    ("model", "STRING", "LLM 模型", "gpt-4o", "text", None, True, ""),
    ("temperature", "FLOAT", "温度", 0.7, "number", None, True, ""),
    ("target_duration", "FLOAT", "目标时长(秒,0=自动)", 0.0, "number", None, True, ""),
    ("target_chars", "INT", "目标字数(0=自动)", 0, "number", None, True, ""),
    ("json_mode", "BOOL", "结构化输出", True, "toggle", None, True, ""),
    ("local_match", "BOOL", "本地匹配兜底", True, "toggle", None, True, ""),
    ("match_embedder", "STRING", "匹配嵌入器", "text", "select",
     ["auto", "clip", "text"], True, "text=本地 n-gram 嵌入，离线可用"),
    ("match_min_similarity", "FLOAT", "匹配最低相似度", 0.2, "number", None, True, ""),
    ("subtitle_preset", "STRING", "字幕样式预设", "默认", "select",
     ["默认", "悬疑惊悚", "搞笑鬼畜", "知识科普", "情感讲述"], True, ""),
    ("tts_engine", "STRING", "TTS 引擎", "edge", "select",
     ["edge", "manual", "indextts", "cloud"], True, "manual 需连接 audio_in 输入"),
    ("tts_voice", "STRING", "TTS 音色", "zh-CN-XiaoxiaoNeural", "text", None, True, ""),
    ("tts_rate", "STRING", "TTS 语速", "+0%", "text", None, True, ""),
    ("segments_source", "STRING", "渲染片段来源", "scenes", "select",
     ["scenes", "timeline"], True, "scenes=整片；timeline=按匹配时间线剪出"),
    ("render_aspect", "STRING", "画幅", "original", "select",
     ["16:9", "9:16", "1:1", "original"], True, ""),
    ("render_resolution", "STRING", "分辨率", "source", "select",
     ["1080p", "720p", "4k", "source"], True, ""),
    ("render_fps", "INT", "帧率", 30, "number", None, True, ""),
    ("render_transition", "STRING", "转场", "cut", "select",
     ["cut", "fade", "fadeblack", "dissolve"], True, ""),
    ("render_transition_duration", "FLOAT", "转场时长(秒)", 0.5, "number", None, True, ""),
    ("render_audio_mix", "STRING", "混音模式", "narration_only", "select",
     ["narration_only", "original_only", "mix"], True, ""),
    ("render_narration_volume", "FLOAT", "解说音量", 1.0, "number", None, True, ""),
    ("render_original_volume", "FLOAT", "原声音量", 0.35, "number", None, True, ""),
    ("render_burn_subtitle", "BOOL", "烧录字幕", True, "toggle", None, True, ""),
    ("output_name", "STRING", "输出文件名", "pipeline_out", "text", None, True, ""),
    ("concurrency", "INT", "批量并发数", 2, "number", None, True, "批量模式条目并发上限"),
    ("resume", "BOOL", "断点续跑", True, "toggle", None, True, "成功条目跨运行复用（checkpoint 落盘）"),
]


def _pt(t: str) -> PortType:
    return {"STRING": PortType.STRING, "INT": PortType.INT, "FLOAT": PortType.FLOAT,
            "BOOL": PortType.BOOL, "JSON": PortType.JSON, "VIDEO": PortType.VIDEO,
            "AUDIO": PortType.AUDIO, "SCRIPT": PortType.SCRIPT,
            "SEGMENTS": PortType.SEGMENTS, "TIMELINE": PortType.TIMELINE,
            "SUBTITLE": PortType.SUBTITLE, "IMAGE": PortType.IMAGE}.get(t, PortType.STRING)


def param_specs() -> List[PortSpec]:
    out: List[PortSpec] = []
    for name, typ, label, default, widget, options, required, desc in _PARAM_DEFS:
        out.append(PortSpec(
            name=name, type=_pt(typ), label=label, required=required, default=default,
            widget=widget, options=list(options) if options else [], description=desc))
    return out


class PipelineNode(NodeBase):
    """一键出片流水线。type_id: user/jsm_pipeline"""

    @classmethod
    def spec(cls):
        return NodeSpec(
            type_id="user/jsm_pipeline",
            title="一键出片流水线",
            category="控制",
            description="视频→场景分析→AI文案→画面匹配→字幕→配音→渲染 一键编排；支持批次信封批量与断点续跑",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=False),
                PortSpec("items", PortType.JSON, label="批次条目", required=False,
                         description="标准批次信封；item.input 含 video_id 或 path"),
                PortSpec("audio_in", PortType.AUDIO, label="人工配音(manual 用)", required=False),
                PortSpec("bgm", PortType.AUDIO, label="背景音乐", required=False),
            ],
            outputs=[
                PortSpec("rendered", PortType.VIDEO, label="成品 MP4", required=False),
                PortSpec("report", PortType.JSON, label="流水线报告", required=True),
                PortSpec("results", PortType.JSON, label="批次结果", required=False),
                PortSpec("scenes", PortType.SEGMENTS, label="场景片段", required=False),
                PortSpec("script", PortType.SCRIPT, label="解说文案", required=False),
                PortSpec("audio", PortType.AUDIO, label="配音音频", required=False),
                PortSpec("timeline", PortType.TIMELINE, label="匹配时间线", required=False),
                PortSpec("styled_subtitle", PortType.SUBTITLE, label="样式字幕", required=False),
            ],
            params=param_specs(),
            version="1.0.0",
        )

    # ------------------------------------------------------------ 工具
    def _resolve_video(self, ctx: dict, payload: Dict[str, Any]) -> Optional[Asset]:
        store = ctx["store"]
        vid = payload.get("video_id")
        if vid:
            asset = store.get(str(vid))
            if asset is not None:
                return asset
        p = payload.get("path") or payload.get("video_path")
        if p and os.path.isfile(str(p)):
            return Asset(id="", kind=PortType.VIDEO.value, path=str(p),
                         meta={"name": os.path.basename(str(p))})
        return None

    # ------------------------------------------------------------ 单条流水线
    async def _run_one(self, ctx: dict, video_asset: Asset, audio_in, bgm,
                       p: Dict[str, Any], suffix: str) -> Dict[str, Any]:
        from app.registry import get_class
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda x: None)
        base_nid = str(ctx.get("node_id", "pipeline"))
        sub_ctx = dict(ctx, node_id=f"{base_nid}_{suffix}")

        def _sp(a: float, b: float):
            return lambda v: progress(a + (b - a) * min(max(v, 0.0), 1.0))

        warnings: List[str] = []
        ids: Dict[str, str] = {}

        # ---- 阶段 1：场景检测 ----
        try:
            scene_cls = get_class("core/scene_detect")
        except KeyError as e:
            raise RuntimeError(f"stage=scene_detect: 插件未注册: {e}") from e
        scene_out = await scene_cls().run(dict(sub_ctx, progress=_sp(0.02, 0.25)), {"video": video_asset}, {
            "strategy": p.get("scene_strategy", "auto"),
            "sample_fps": p.get("scene_sample_fps", 1.0),
            "diff_threshold": p.get("scene_diff_threshold", 0.10),
            "min_duration": p.get("scene_min_duration", 1.0),
            "max_duration": p.get("scene_max_duration", 30.0),
            "merge_policy": "previous", "fast_mode": False,
        })
        scenes_asset = scene_out.get("scenes")
        if scenes_asset is None:
            raise RuntimeError("stage=scene_detect: 未产出场景资产")
        ids["scenes_id"] = scenes_asset.id

        # ---- 阶段 2：智能分块（可选） ----
        chunks_asset = None
        if bool(p.get("chunk_enabled", False)):
            try:
                chunk_cls = get_class("core/smart_chunk")
            except KeyError as e:
                raise RuntimeError(f"stage=smart_chunk: 插件未注册: {e}") from e
            chunk_out = await chunk_cls().run(dict(sub_ctx, progress=_sp(0.26, 0.32)),
                                              {"video": video_asset, "scenes": scenes_asset}, {
                "target_duration": p.get("chunk_target_duration", 180.0),
                "min_duration": p.get("chunk_min_duration", 120.0),
                "max_duration": p.get("chunk_max_duration", 240.0),
                "tail_policy": "merge_back",
            })
            chunks_asset = chunk_out.get("chunks")

        # ---- 阶段 3：ASR 字幕（可选） ----
        sub_asset = None
        asr_mode = str(p.get("asr_mode", "off") or "off")
        if asr_mode != "off":
            try:
                asr_cls = get_class("core/asr")
            except KeyError as e:
                raise RuntimeError(f"stage=asr: 插件未注册: {e}") from e
            asr_out = await asr_cls().run(dict(sub_ctx, progress=_sp(0.33, 0.40)),
                                          {"video": video_asset, "audio": None}, {
                "engine": asr_mode, "model": p.get("asr_model", "small"),
                "language": p.get("asr_language", "zh"), "device": "auto",
                "compute_type": "int8", "vad": True, "word_timestamps": False,
                "api_key": p.get("asr_api_key", ""), "base_url": p.get("asr_base_url", ""),
                "remote_model": "whisper-1",
            })
            sub_asset = asr_out.get("subtitles")

        # ---- 阶段 4：AI 文案 ----
        try:
            narrate_cls = get_class("core/ai_narrate")
        except KeyError as e:
            raise RuntimeError(f"stage=narrate: 插件未注册: {e}") from e
        narrate_out = await narrate_cls().run(dict(sub_ctx, progress=_sp(0.41, 0.55)), {
            "video": video_asset, "scenes": scenes_asset,
            "descriptions": None, "draft": None,
        }, {
            "mode": p.get("narrate_mode", "上帝视角"),
            "style": p.get("narrate_style", "客观平实"),
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
        narration_asset = narrate_out.get("narration")
        if script_asset is None:
            raise RuntimeError("stage=narrate: 未产出文案资产")
        ids["script_id"] = script_asset.id
        ids["narration_id"] = getattr(narration_asset, "id", "")

        # ---- 阶段 5：文案-画面匹配 ----
        timeline_asset = None
        matches_asset = None
        try:
            match_cls = get_class("user/jsm_match_visual")
        except KeyError as e:
            raise RuntimeError(f"stage=match: 插件未注册: {e}") from e
        match_out = await match_cls().run(dict(sub_ctx, progress=_sp(0.56, 0.68)), {
            "video": video_asset, "script": script_asset, "scenes": scenes_asset,
        }, {
            "embedder": p.get("match_embedder", "text"),
            "min_similarity": p.get("match_min_similarity", 0.2),
            "frame_positions": "0.1,0.5,0.9",
            "avoid_adjacent_repeat": True, "allow_reuse": True,
            "max_concurrent": 1, "retries": 0, "resume": False,
        })
        timeline_asset = match_out.get("timeline")
        matches_asset = match_out.get("matches")
        ids["timeline_id"] = getattr(timeline_asset, "id", "")
        ids["matches_id"] = getattr(matches_asset, "id", "")

        # ---- 阶段 6：字幕装配（ASR 字幕或文案段落字幕） ----
        styled_asset = None
        sub_rows: List[Dict[str, Any]] = []
        if sub_asset is not None:
            sub_rows = store.read_json(sub_asset.id) or []
        if not sub_rows:
            nar = store.read_json(narration_asset.id) or {}
            segs = nar.get("segments") or []
            for s in segs:
                txt = str(s.get("text", "") or "").strip()
                if not txt:
                    continue
                st = float(s.get("start", 0) or 0)
                en = float(s.get("end", st + 1.0) or st + 1.0)
                sub_rows.append({"start": st, "end": max(en, st + 0.5), "text": txt})
        if sub_rows:
            base_sub = store.save_asset(
                Asset(id="", kind=PortType.SUBTITLE.value, meta={"count": len(sub_rows)}),
                payload=sub_rows)
            try:
                comp_cls = get_class("core/subtitle_compose")
            except KeyError as e:
                raise RuntimeError(f"stage=subtitle_compose: 插件未注册: {e}") from e
            comp_out = await comp_cls().run(dict(sub_ctx, progress=_sp(0.70, 0.74)),
                                            {"subtitle": base_sub, "script": script_asset},
                                            {"preset": p.get("subtitle_preset", "默认"),
                                             "karaoke": False})
            styled_asset = comp_out.get("styled_subtitle")
        ids["styled_id"] = getattr(styled_asset, "id", "")

        # ---- 阶段 7：TTS 配音 ----
        tts_engine = str(p.get("tts_engine", "edge") or "edge")
        if tts_engine == "manual" and audio_in is None:
            raise RuntimeError("stage=tts: tts_engine=manual 需要 audio_in 输入")
        try:
            tts_cls = get_class("core/tts")
        except KeyError as e:
            raise RuntimeError(f"stage=tts: 插件未注册: {e}") from e
        tts_inputs: Dict[str, Any] = {"script": script_asset}
        if tts_engine == "manual":
            tts_inputs["audio_in"] = audio_in
        tts_out = await tts_cls().run(dict(sub_ctx, progress=_sp(0.75, 0.85)), tts_inputs, {
            "engine": tts_engine, "voice": p.get("tts_voice", "zh-CN-XiaoxiaoNeural"),
            "rate": p.get("tts_rate", "+0%"), "pitch": "+0Hz", "volume": "+0%",
            "output_format": "mp3", "chunk_chars": 800, "max_concurrent": 2,
            "retries": 0, "merge_audio": True,
        })
        audio_asset = tts_out.get("audio")
        if audio_asset is None:
            raise RuntimeError("stage=tts: 未产出配音音频")
        ids["audio_id"] = audio_asset.id

        # ---- 阶段 8：渲染 ----
        segments_asset = scenes_asset
        if str(p.get("segments_source", "scenes")) == "timeline" and timeline_asset is not None:
            tl = store.read_json(timeline_asset.id) or []
            seg_rows = [{"start": float(r.get("start", 0)), "end": float(
                r.get("end", r.get("start", 0) + 1.0))} for r in tl if isinstance(r, dict)]
            if seg_rows:
                segments_asset = store.save_asset(
                    Asset(id="", kind=PortType.SEGMENTS.value, meta={"count": len(seg_rows)}),
                    payload=seg_rows)
        out_name = str(p.get("output_name", "pipeline_out"))
        try:
            render_cls = get_class("core/video_render")
        except KeyError as e:
            raise RuntimeError(f"stage=render: 插件未注册: {e}") from e
        render_out = await render_cls().run(dict(sub_ctx, progress=_sp(0.86, 0.98)), {
            "video": video_asset, "segments": segments_asset, "audio": audio_asset,
            "styled_subtitle": styled_asset, "bgm": bgm,
        }, {
            "aspect": p.get("render_aspect", "original"),
            "resolution": p.get("render_resolution", "source"),
            "fps": int(p.get("render_fps", 30) or 30),
            "burn_subtitle": bool(p.get("render_burn_subtitle", True)),
            "transition": p.get("render_transition", "cut"),
            "transition_duration": p.get("render_transition_duration", 0.5),
            "scale_mode": "contain",
            "audio_mix": p.get("render_audio_mix", "narration_only"),
            "narration_volume": p.get("render_narration_volume", 1.0),
            "original_volume": p.get("render_original_volume", 0.35),
            "bgm_volume": 0.3, "overlay_alpha": 0.8,
            "output_name": out_name,
        })
        rendered_asset = render_out.get("rendered")
        if rendered_asset is None:
            raise RuntimeError("stage=render: 未产出成品视频")
        ids["rendered_id"] = rendered_asset.id

        progress(1.0)
        return {
            "asset": rendered_asset,
            "ids": ids,
            "rendered_id": rendered_asset.id,
            "scenes_id": scenes_asset.id,
            "chunks_id": getattr(chunks_asset, "id", ""),
            "script_id": script_asset.id,
            "narration_id": ids.get("narration_id", ""),
            "audio_id": audio_asset.id,
            "timeline_id": ids.get("timeline_id", ""),
            "styled_id": ids.get("styled_id", ""),
            "rendered_path": getattr(rendered_asset, "path", ""),
            "scene_count": int((scenes_asset.meta or {}).get("count", 0) or 0),
            "chars": int((script_asset.meta or {}).get("chars", 0) or 0),
            "duration_estimate": (script_asset.meta or {}).get("duration_estimate"),
            "warnings": warnings,
        }

    # ------------------------------------------------------------ run
    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        from app import batchlib
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda x: None)
        video_asset = inputs.get("video")
        items_asset = inputs.get("items")
        audio_in = inputs.get("audio_in")
        bgm = inputs.get("bgm")

        items_data = None
        if items_asset is not None:
            items_data = store.read_json(items_asset.id)

        if items_data is not None:
            # ---- 批量模式 ----
            batch = batchlib.normalize_batch(items_data)
            concurrency = max(1, int(params.get("concurrency", 2) or 2))
            resume = bool(params.get("resume", True))
            cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                              f"pipeline_{ctx.get('node_id', 'pipeline')}.json")
            p = dict(params)

            async def worker(item: dict) -> dict:
                payload = item.get("input")
                if not isinstance(payload, dict):
                    raise RuntimeError("invalid: 条目输入必须是对象（含 video_id 或 path）")
                asset = self._resolve_video(ctx, payload)
                if asset is None:
                    raise RuntimeError("invalid: 条目缺少有效 video_id 或 path")
                idx = int(item.get("index", 0))
                # 每条目独立输出名，避免并发写同名成品互相覆盖
                pp = dict(p)
                pp["output_name"] = f"{p.get('output_name', 'pipeline_out')}_{idx}"
                r = await self._run_one(ctx, asset, audio_in, bgm, pp, f"item{idx}")
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
                        "scenes_id", "chunks_id", "script_id", "narration_id",
                        "audio_id", "timeline_id", "styled_id", "rendered_id",
                        "rendered_path", "scene_count", "chars", "duration_estimate")})
                clean.append(rec)
            results_payload = {"batch_id": batch.get("batch_id"), "items": clean}
            results_asset = store.save_asset(
                Asset(id="", kind=PortType.JSON.value,
                      meta={"count": len(clean), "ok": report.get("succeeded", 0)}),
                payload=results_payload)
            report_asset = store.save_asset(
                Asset(id="", kind=PortType.JSON.value, meta={"mode": "batch"}), payload=report)
            # 首条成功项资产（供下游链式接线）
            first = None
            for it in clean:
                if it.get("status") == "succeeded" and it.get("rendered_id"):
                    first = it
                    break
            first_rendered = first_scenes = first_script = first_audio = first_tl = first_sub = None
            if first is not None:
                first_rendered = store.get(first.get("rendered_id")) if first.get("rendered_id") else None
                first_scenes = store.get(first.get("scenes_id")) if first.get("scenes_id") else None
                first_script = store.get(first.get("script_id")) if first.get("script_id") else None
                first_audio = store.get(first.get("audio_id")) if first.get("audio_id") else None
                first_tl = store.get(first.get("timeline_id")) if first.get("timeline_id") else None
                first_sub = store.get(first.get("styled_id")) if first.get("styled_id") else None
            return {"rendered": first_rendered, "report": report_asset, "results": results_asset,
                    "scenes": first_scenes, "script": first_script, "audio": first_audio,
                    "timeline": first_tl, "styled_subtitle": first_sub}

        # ---- 单视频模式 ----
        if video_asset is None or not getattr(video_asset, "path", ""):
            raise RuntimeError("invalid: 需要 video 输入（单模式）或 items 输入（批量模式）")
        r = await self._run_one(ctx, video_asset, audio_in, bgm, dict(params), "single")
        report = {
            "ok": True, "mode": "single",
            "scenes_id": r["scenes_id"], "chunks_id": r.get("chunks_id", ""),
            "script_id": r["script_id"], "narration_id": r.get("narration_id", ""),
            "audio_id": r["audio_id"], "timeline_id": r.get("timeline_id", ""),
            "styled_id": r.get("styled_id", ""), "rendered_id": r["rendered_id"],
            "rendered_path": r["rendered_path"],
            "scene_count": r["scene_count"], "chars": r["chars"],
            "duration_estimate": r["duration_estimate"],
            "warnings": r["warnings"], "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": "single"}), payload=report)
        return {
            "rendered": store.get(r["rendered_id"]),
            "report": report_asset,
            "results": None,
            "scenes": store.get(r["scenes_id"]),
            "script": store.get(r["script_id"]),
            "audio": store.get(r["audio_id"]),
            "timeline": store.get(r["timeline_id"]) if r.get("timeline_id") else None,
            "styled_subtitle": store.get(r["styled_id"]) if r.get("styled_id") else None,
        }
