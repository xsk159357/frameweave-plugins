# -*- coding: utf-8 -*-
"""AI Narrate node: narration generation (godview/expand/match/style)."""
from __future__ import annotations
import json
import os
import subprocess
from typing import Any, Dict, List

from app.assets import Asset
from app.llm import chat, vision_analyze
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app.registry import register


async def _extract_keyframes(video_path: str, workdir: str, ctx: Dict[str, Any] = None,
                             count: int = 8) -> List[str]:
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    out_dir = os.path.join(workdir, "narrate_frames")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        try:
            os.remove(os.path.join(out_dir, f))
        except OSError:
            pass
    filt = r"select=not(mod(n\," + str(max(1, count)) + ")),scale=320:-1"
    try:
        if ctx is not None and ctx.get("run_tracked"):
            proc = await ctx["run_tracked"](
                [ff, "-v", "quiet", "-i", video_path, "-vf", filt,
                 "-frames:v", str(count), os.path.join(out_dir, "kf_%02d.jpg")],
                timeout=120, text=True)
            if proc.returncode != 0:
                return []
        else:
            import subprocess
            subprocess.run([ff, "-v", "quiet", "-i", video_path, "-vf", filt,
                            "-frames:v", str(count), os.path.join(out_dir, "kf_%02d.jpg")],
                           capture_output=True, timeout=120)
    except Exception:
        return []
    return sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir))


def _read_text(store, asset: Asset) -> str:
    if asset is None:
        return ""
    if asset.path and asset.path.endswith(".json"):
        try:
            data = store.read_json(asset.id)
            if isinstance(data, dict) and "text" in data:
                return str(data["text"])
            return str(data)
        except Exception:
            return ""
    if asset.path:
        try:
            with open(asset.path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
    return ""


class AINarrateNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/ai_narrate",
            title="AI 文案生成",
            category="语义",
            description="生成解说文案：上帝视角自动分析 / 用户文案扩写 / 配画面 / 风格仿写",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="视频", required=False),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="片段列表", required=False),
                PortSpec(name="script", type=PortType.SCRIPT, label="文案草稿", required=False),
            ],
            outputs=[
                PortSpec(name="narration_script", type=PortType.SCRIPT, label="解说文案"),
                PortSpec(name="narration", type=PortType.JSON, label="结构化文案"),
            ],
            params=[
                PortSpec(name="mode", type=PortType.STRING, label="模式", default="上帝视角",
                         widget="select", options=["上帝视角", "扩写", "配画面", "仿写"]),
                PortSpec(name="style", type=PortType.STRING, label="风格", default="悬疑解说",
                         widget="select", options=["悬疑解说", "搞笑解说", "知识科普", "情感讲述", "客观平实"]),
                PortSpec(name="language", type=PortType.STRING, label="语言", default="中文",
                         widget="select", options=["中文", "英语", "日语", "韩语", "缅甸语", "越南语"]),
                PortSpec(name="api_key", type=PortType.STRING, label="API Key",
                         widget="text", description="用户自带 Key（本机加密存储）"),
                PortSpec(name="base_url", type=PortType.STRING, label="中转地址",
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec(name="model", type=PortType.STRING, label="模型", default="gpt-4o", widget="text"),
                PortSpec(name="word_count", type=PortType.INT, label="目标字数", default=800, widget="number"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        api_key = str(params.get("api_key", "") or "").strip()
        if api_key.startswith("@secret:"):
            ref = api_key.split(":", 1)[1]
            # P3 迁移：优先 ctx.resolve_secret（worker 内经白名单 RPC）；缺省回退主进程保险箱
            resolver = ctx.get("resolve_secret") if hasattr(ctx, "get") else None
            if resolver:
                api_key = resolver(ref) or ""
            else:
                from app.secrets import read_secret
                api_key = read_secret(ref) or ""
        if not api_key:
            raise RuntimeError("请填写 API Key，或在密钥保险箱存好后用 @secret:名称 引用")
        base_url = str(params.get("base_url", "https://ameaaos.com/v1")).strip() or "https://ameaaos.com/v1"
        model = str(params.get("model", "gpt-4o")).strip() or "gpt-4o"
        mode = params.get("mode", "上帝视角")
        style = params.get("style", "悬疑解说")
        language = params.get("language", "中文")
        word_count = int(params.get("word_count", 800) or 800)

        video_asset = inputs.get("video")
        segments_asset = inputs.get("segments")
        script_asset = inputs.get("script")
        store = ctx["store"]

        prompt_tmpl = (
            "你是一名资深" + style + "解说博主。请用" + language + "写一篇约" + str(word_count) +
            "字的解说文案。要求：开头抓人、中间有信息增量、结尾引导关注；口语化，避免书面语。"
        )

        result_text = ""
        narration: Dict[str, Any] = {}

        if mode == "上帝视角":
            if video_asset is None or not video_asset.path or not os.path.exists(video_asset.path):
                raise RuntimeError("上帝视角模式需要连接视频输入")
            frames = await _extract_keyframes(video_asset.path, ctx["workdir"], ctx)
            segments_desc = ""
            if segments_asset is not None:
                segs = store.read_json(segments_asset.id) or []
                if segs:
                    seg_slim = [{"start": s.get("start"), "end": s.get("end")} for s in segs[:12]]
                    segments_desc = "\n视频分段信息：" + json.dumps(seg_slim, ensure_ascii=False)
            prompt = (prompt_tmpl +
                      "\n请先分析以下视频关键帧画面，理解视频内容后撰写解说词。" +
                      segments_desc +
                      "\n输出格式：直接输出文案正文，不要额外说明。")
            if frames:
                result_text = vision_analyze(frames, prompt, api_key, base_url, model)
            else:
                result_text = chat([{"role": "user", "content": prompt}], api_key, base_url, model)
        elif mode == "扩写":
            if script_asset is None:
                raise RuntimeError("扩写模式需要连接文案草稿输入")
            draft_text = _read_text(store, script_asset)
            prompt = prompt_tmpl + "\n\n我的文案草稿如下，请扩写润色为完整解说稿：\n" + draft_text
            result_text = chat([{"role": "user", "content": prompt}], api_key, base_url, model)
        elif mode == "配画面":
            if script_asset is None or segments_asset is None:
                raise RuntimeError("配画面模式需要连接文案草稿与片段列表")
            draft_text = _read_text(store, script_asset)
            segs = store.read_json(segments_asset.id) or []
            seg_desc = json.dumps(
                [{"i": s.get("index"), "t": str(s.get("start")) + "-" + str(s.get("end")) + "s"}
                 for s in segs], ensure_ascii=False)
            prompt = (prompt_tmpl + "\n\n文案：" + draft_text +
                      "\n片段列表：" + seg_desc +
                      "\n请为每句文案分配最合适的片段序号，输出 JSON 数组 [{\"sentence\":..., \"segment_index\":...}]")
            result_text = chat([{"role": "user", "content": prompt}], api_key, base_url, model)
            try:
                narration["mapping"] = json.loads(result_text)
            except json.JSONDecodeError:
                narration["mapping"] = result_text
        elif mode == "仿写":
            if script_asset is None:
                raise RuntimeError("仿写模式需要连接示例文案输入")
            example = _read_text(store, script_asset)
            prompt = prompt_tmpl + "\n\n请模仿以下示例的风格撰写新解说词：\n" + example
            result_text = chat([{"role": "user", "content": prompt}], api_key, base_url, model)
        else:
            raise RuntimeError("未知模式: " + str(mode))

        script_payload = {"text": result_text, "mode": mode, "style": style, "language": language}
        asset = store.save_asset(
            Asset(id="", kind=PortType.SCRIPT.value, meta={"mode": mode, "language": language}),
            payload=script_payload)
        narration_payload = {"text": result_text, "mode": mode}
        narration_payload.update(narration)
        n_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": mode}), payload=narration_payload)
        return {"narration_script": asset, "narration": n_asset}
