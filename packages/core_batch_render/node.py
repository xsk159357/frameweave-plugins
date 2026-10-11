# -*- coding: utf-8 -*-
"""批量 Loop 节点：矩阵批量出片。

输入：一个源视频 + 片段列表 + 文案矩阵（JSON 或 manual_items 文本）。
行为：对每个文案条目依次执行 edge-TTS 配音 + 字幕 + MP4 渲染，输出多个成品。
条目格式（manual_items 每行）：名称|文案
"""
from __future__ import annotations
import os
import re
from typing import Any, Dict, List

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")


def _render_template(text: str, vars_map: Dict[str, str]) -> str:
    """把 {{key}} 替换为 vars_map 中的值；未定义的 key 原样保留。"""
    def _sub(m):
        key = m.group(1)
        return str(vars_map.get(key, m.group(0)))
    return _TEMPLATE_RE.sub(_sub, text)

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app.registry import register, get_class, autodiscover


class BatchRenderNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/batch_render",
            title="批量出片",
            category="控制",
            description="矩阵批量出片：一个视频 x 多组文案，自动配音+字幕+渲染",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="源视频", required=True),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="片段列表", required=True),
                PortSpec(name="items", type=PortType.JSON, label="矩阵数据", required=False,
                         description="[{name, script}]"),
            ],
            outputs=[
                PortSpec(name="results", type=PortType.JSON, label="批量结果"),
                PortSpec(name="video", type=PortType.VIDEO, label="第一批成品"),
            ],
            params=[
                PortSpec(name="manual_items", type=PortType.STRING, label="矩阵条目",
                         default="", widget="text",
                         description="每行一个：名称|文案；可用 {{变量}} 模板，如：第{{n}}集|欢迎观看{{title}}"),
                PortSpec(name="variables", type=PortType.STRING, label="模板变量",
                         default="", widget="text",
                         description="每行一个：key=value；文案/名称/文件名中的 {{key}} 会被替换"),
                PortSpec(name="voice", type=PortType.STRING, label="音色", default="zh-CN-YunxiNeural",
                         widget="select", options=["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural",
                                                   "zh-CN-YunjianNeural", "zh-CN-XiaoyiNeural"]),
                PortSpec(name="aspect", type=PortType.STRING, label="画幅", default="16:9",
                         widget="select", options=["16:9", "9:16", "1:1"]),
                PortSpec(name="resolution", type=PortType.STRING, label="分辨率", default="1080p",
                         widget="select", options=["1080p", "720p", "4k"]),
                PortSpec(name="fps", type=PortType.INT, label="帧率", default=30, widget="number"),
                PortSpec(name="transition", type=PortType.STRING, label="转场", default="cut",
                         widget="select", options=["cut", "fade", "fadeblack"]),
                PortSpec(name="output_prefix", type=PortType.STRING, label="输出前缀",
                         default="batch_", widget="text"),
                PortSpec(name="max_concurrent", type=PortType.INT, label="最大并发数", default=2,
                         widget="number", description="0=不限制"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        seg_asset = inputs.get("segments")
        if video_asset is None or not video_asset.path or not os.path.exists(video_asset.path):
            raise RuntimeError("需要源视频输入")
        if seg_asset is None:
            raise RuntimeError("需要片段列表输入")
        store = ctx["store"]
        segs = store.read_json(seg_asset.id) or []
        if not segs:
            raise RuntimeError("片段列表为空")

        # ---- 模板变量（全局 variables + 条目级 vars 覆盖） ----
        global_vars: Dict[str, str] = {}
        vars_text = str(params.get("variables", "") or "").strip()
        if vars_text:
            for line in vars_text.split("\n"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                global_vars[k.strip()] = v.strip()
        # JSON items 可带全局变量对象（items 不含 name/script 时作全局）
        items_asset0 = inputs.get("items")
        if items_asset0 is not None:
            _extra0 = store.read_json(items_asset0.id) or []
            if isinstance(_extra0, dict) and "variables" in _extra0:
                for k, v in (_extra0.get("variables") or {}).items():
                    global_vars[str(k)] = str(v)

        # 解析矩阵条目
        items: List[Dict[str, Any]] = []
        manual = str(params.get("manual_items", "") or "").strip()
        if manual:
            for line in manual.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "|" in line:
                    name, script = line.split("|", 1)
                else:
                    name, script = "item_" + str(len(items)), line
                items.append({"name": name.strip(), "script": script.strip()})
        items_asset = inputs.get("items")
        if items_asset is not None:
            extra = store.read_json(items_asset.id) or []
            if isinstance(extra, list):
                items.extend(extra)
        if not items:
            raise RuntimeError("矩阵条目为空（填写矩阵条目或连接矩阵数据）")

        # 应用模板：条目级 vars 覆盖全局，替换 name/script
        for it in items:
            merged = dict(global_vars)
            it_vars = it.get("vars") or {}
            if isinstance(it_vars, dict):
                for k, v in it_vars.items():
                    merged[str(k)] = str(v)
            it["_vars"] = merged
            it["name"] = _render_template(str(it.get("name", "")), merged)
            it["script"] = _render_template(str(it.get("script", "")), merged)

        voice = params.get("voice", "zh-CN-YunxiNeural")

        autodiscover()
        tts_cls = get_class("core/tts")
        render_cls = get_class("core/video_render")

        results: List[Dict[str, Any]] = []
        first_out = None
        import asyncio

        max_conc = int(params.get("max_concurrent", 2) or 2)
        sem = asyncio.Semaphore(max_conc if max_conc > 0 else len(items) or 1)
        progress = ctx.get("progress")
        _done = [0]

        async def _process(idx: int, item: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                try:
                    return await self._process_item(ctx, store, tts_cls, render_cls, params,
                                                    video_asset, seg_asset, item, idx, voice,
                                                    global_vars, first_holder)
                except Exception as e:
                    return {"index": idx, "name": str(item.get("name", "item_%d" % idx)),
                            "ok": False, "error": str(e)[:200]}
                finally:
                    _done[0] += 1
                    if progress:
                        try:
                            progress(_done[0] / max(len(items), 1))
                        except Exception:
                            pass

        first_holder = {"video": None}
        tasks = [asyncio.create_task(_process(idx, item)) for idx, item in enumerate(items)]
        batch_results = await asyncio.gather(*tasks)
        for r in batch_results:
            results.append(r)
        # first_out 取第一个成功条目的 Asset
        for r in batch_results:
            if r.get("ok") and first_holder["video"] is None:
                first_holder["video"] = r.get("_video_asset")
                first_out = first_holder["video"]
        # 保持结果按条目顺序
        results.sort(key=lambda r: r.get("index", 0))
        # 剥离内部字段（_video_asset 仅引擎内部使用，不进结果 payload）
        clean = []
        for r in results:
            rr = dict(r)
            rr.pop("_video_asset", None)
            clean.append(rr)
        res_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(clean), "ok": sum(1 for r in clean if r["ok"])}),
            payload={"items": clean})
        return {"results": res_asset, "video": first_out}

    async def _process_item(self, ctx, store, tts_cls, render_cls, params,
                             video_asset, seg_asset, item, idx, voice, global_vars, first_holder):
        """单条目：TTS 配音 + 字幕 + MP4 渲染。"""
        import asyncio
        name = str(item.get("name", "item_" + str(idx)))
        script = str(item.get("script", ""))
        if not script.strip():
            return {"index": idx, "name": name, "ok": False, "error": "文案为空"}

        # TTS（edge_tts 异步）
        script_asset = store.save_asset(
            Asset(id="", kind=PortType.SCRIPT.value), payload={"text": script})
        tts_out = await tts_cls().run(
            ctx, {"script": script_asset},
            {"engine": "edge", "voice": voice, "rate": "+0%", "pitch": "+0Hz",
             "output_format": "mp3"})
        tts_asset = tts_out["audio"]

        # 字幕
        subs = [{"start": 0.1, "end": max(len(script) * 0.3, 2.0), "text": script[:60],
                 "style": {"font": "Microsoft YaHei", "size": 44, "color": "#FFFFFF",
                           "stroke": "#000000", "bold": 1}}]
        sub_asset = store.save_asset(Asset(id="", kind=PortType.SUBTITLE.value),
                                     payload=subs)

        # 渲染（subprocess 阻塞 -> to_thread）；输出名追加条目序号避免同名条目互相覆盖（M1）
        out_name = (_render_template(str(params.get("output_prefix", "batch_")) + name,
                                     item.get("_vars") or global_vars) + "_" + str(idx))
        out = await asyncio.to_thread(
            self._render_sync, render_cls, ctx, video_asset, seg_asset,
            tts_asset, sub_asset, params, out_name)
        sz = os.path.getsize(out["video"].path) if os.path.exists(out["video"].path) else 0
        return {"index": idx, "name": name, "ok": True,
                "path": out["video"].path, "size": sz,
                "video_id": out["video"].id,
                "_video_asset": out["video"]}

    def _render_sync(self, render_cls, ctx, video_asset, seg_asset, tts_asset, sub_asset,
                     params, out_name):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(render_cls().run(
                ctx, {"video": video_asset, "segments": seg_asset,
                      "audio": tts_asset, "styled_subtitle": sub_asset},
                {"aspect": params.get("aspect", "16:9"),
                 "resolution": params.get("resolution", "1080p"),
                 "fps": int(params.get("fps", 30) or 30),
                 "burn_subtitle": True,
                 "transition": params.get("transition", "cut"),
                 "output_name": out_name}))
        finally:
            loop.close()
