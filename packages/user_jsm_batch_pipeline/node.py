# -*- coding: utf-8 -*-
"""多视频批量编排（user/jsm_batch_pipeline，t24 交付）。

对一批视频（标准批次信封 item.input={video_id|path|name}）逐个执行完整出片流水线：
场景分析 → AI文案 → 画面匹配 → 字幕 → 配音 → 渲染。具备：
- batchlib 信封：条目级错误隔离（一条失败不影响其它条）、并发上限、checkpoint 断点续跑、
  失败保留（含可重试分类）、进度回调、汇总报告。
- 内部逐条调用 user/jsm_pipeline（app.registry 正式节点类型解析，不 import 其它插件私有模块），
  每条目独立输出名（避免并发写同名成品互相覆盖）。
- 输出 results（批次信封 + 每项成品/中间资产 id）、report（汇总 + 全量条目）、first_video（首条成功成品）。

断点续跑语义（与 t18 batchlib 一致）：batch_id 由条目内容派生；重跑同一批输入时，
checkpoint 中已成功的条目直接复用结果（不重新执行），失败/未跑的条目继续执行。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

# 与 user/jsm_pipeline 相同的流水线参数表（本节点透传给单条流水线）
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
    ("output_name", "STRING", "输出文件名前缀", "batch_out", "text", None, True, "每条目自动追加序号"),
    ("concurrency", "INT", "并发数", 2, "number", None, True, "条目并发上限"),
    ("resume", "BOOL", "断点续跑", True, "toggle", None, True, "成功条目跨运行复用（checkpoint 落盘）"),
    ("retries", "INT", "可重试次数", 1, "number", None, True, "网络/超时/429 类错误的指数退避重试次数"),
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


class BatchPipelineNode(NodeBase):
    """多视频批量编排。type_id: user/jsm_batch_pipeline"""

    @classmethod
    def spec(cls):
        return NodeSpec(
            type_id="user/jsm_batch_pipeline",
            title="多视频批量编排",
            category="控制",
            description="对一批视频逐条执行完整出片流水线（分析→文案→匹配→字幕→配音→渲染）；并发、断点续跑、逐条隔离、首项成品输出",
            inputs=[
                PortSpec("items", PortType.JSON, label="批次条目", required=True,
                         description="标准批次信封；item.input 含 video_id 或 path"),
                PortSpec("audio_in", PortType.AUDIO, label="人工配音(manual 用)", required=False),
                PortSpec("bgm", PortType.AUDIO, label="背景音乐", required=False),
            ],
            outputs=[
                PortSpec("results", PortType.JSON, label="批次结果", required=True),
                PortSpec("report", PortType.JSON, label="编排报告", required=True),
                PortSpec("first_video", PortType.VIDEO, label="首条成品", required=False),
            ],
            params=param_specs(),
            version="1.0.0",
        )

    @staticmethod
    def _resolve_video(ctx: dict, payload: Dict[str, Any]):
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

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        from app import batchlib
        from app.registry import get_class
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda x: None)
        items_asset = inputs.get("items")
        if items_asset is None:
            raise RuntimeError("invalid: 需要 items 批次信封输入")
        items_data = store.read_json(items_asset.id)
        batch = batchlib.normalize_batch(items_data)
        if not batch["items"]:
            raise RuntimeError("invalid: 批次为空")

        concurrency = max(1, int(params.get("concurrency", 2) or 2))
        resume = bool(params.get("resume", True))
        retries = max(0, int(params.get("retries", 1) or 0))
        base_out = str(params.get("output_name", "batch_out"))
        cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                          f"batchpipe_{ctx.get('node_id', 'batch_pipeline')}.json")
        try:
            pipe_cls = get_class("user/jsm_pipeline")
        except KeyError as e:
            raise RuntimeError(f"一键出片流水线插件未注册: {e}") from e

        first_holder: Dict[str, Any] = {"asset": None}
        started = time.time()

        async def worker(item: dict) -> dict:
            payload = item.get("input")
            if not isinstance(payload, dict):
                raise RuntimeError("invalid: 条目输入必须是对象（含 video_id 或 path）")
            asset = self._resolve_video(ctx, payload)
            if asset is None:
                raise RuntimeError("invalid: 条目缺少有效 video_id 或 path")
            idx = int(item.get("index", 0))
            pp = dict(params)
            pp["output_name"] = f"{base_out}_{idx}"
            sub_ctx = dict(ctx, node_id=f"{ctx.get('node_id', 'batch_pipeline')}_{idx}")
            out = await pipe_cls().run(sub_ctx, {
                "video": asset, "audio_in": inputs.get("audio_in"), "bgm": inputs.get("bgm"),
            }, pp)
            rendered = out.get("rendered")
            report_asset = out.get("report")
            rec = {
                "video_id": asset.id if getattr(asset, "id", "") else "",
                "path": getattr(asset, "path", ""),
                "rendered_id": getattr(rendered, "id", ""),
                "rendered_path": getattr(rendered, "path", ""),
                "report_id": getattr(report_asset, "id", ""),
                "scenes_id": getattr(out.get("scenes"), "id", ""),
                "script_id": getattr(out.get("script"), "id", ""),
                "audio_id": getattr(out.get("audio"), "id", ""),
                "timeline_id": getattr(out.get("timeline"), "id", ""),
                "styled_id": getattr(out.get("styled_subtitle"), "id", ""),
            }
            if rendered is not None and first_holder["asset"] is None:
                first_holder["asset"] = rendered
            return rec

        batch = await batchlib.run_items(batch, worker, concurrency=concurrency,
                                         checkpoint_path=cp, progress=progress,
                                         retries=retries, resume=resume)
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
                    "video_id", "path", "rendered_id", "rendered_path", "report_id",
                    "scenes_id", "script_id", "audio_id", "timeline_id", "styled_id")})
            clean.append(rec)
        report["mode"] = "batch"
        report["elapsed_s"] = round(time.time() - started, 2)
        report["items"] = clean

        results_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(clean), "ok": report.get("succeeded", 0)}),
            payload={"batch_id": batch.get("batch_id"), "items": clean})
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": "batch"}), payload=report)
        return {"results": results_asset, "report": report_asset,
                "first_video": first_holder["asset"]}