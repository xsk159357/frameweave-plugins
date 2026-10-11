"""ASR 节点：语音转字幕（faster-whisper，CPU/GPU 均可用）。

模型下载：优先 ModelScope（keepitsimple/faster-whisper-small），失败走 HF。
"""
from __future__ import annotations
import json
import os
from typing import Any, Dict, List

from app.assets import Asset
from app.model_download import download_model
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app.registry import register


class ASRNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/asr",
            title="语音转字幕",
            category="分析",
            description="语音识别转字幕（faster-whisper）",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="视频", required=True),
                PortSpec(name="audio", type=PortType.AUDIO, label="音频(可选)", required=False),
            ],
            outputs=[
                PortSpec(name="subtitle", type=PortType.SUBTITLE, label="字幕"),
                PortSpec(name="srt", type=PortType.STRING, label="SRT 文本"),
            ],
            params=[
                PortSpec(name="model", type=PortType.STRING, label="模型", default="large-v3",
                         widget="select", options=["large-v3"]),
                PortSpec(name="language", type=PortType.STRING, label="语言", default="zh",
                         widget="select", options=["zh", "en", "ja", "ko", "auto"]),
                PortSpec(name="device", type=PortType.STRING, label="设备", default="auto",
                         widget="select", options=["auto", "cpu", "cuda"]),
            ],
            version="1.0.0",
        )

    async def setup(self, ctx) -> None:
        """懒加载：首次运行下载模型。"""
        return

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        audio_asset = inputs.get("audio")
        src_path = audio_asset.path if audio_asset else (video_asset.path if video_asset else None)
        if not src_path or not os.path.exists(src_path):
            raise RuntimeError("缺少视频/音频输入")

        model_key = params.get("model", "large-v3")
        language = params.get("language", "zh")
        device = params.get("device", "auto")

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError("faster-whisper 未安装")

        # 下载模型（ModelScope 优先）
        models_dir = os.path.join(ctx["store"].root, "models")
        try:
            info = download_model(f"faster-whisper-{model_key}", models_dir)
            model_path = info["path"]
            model_dir = os.path.dirname(model_path)
            model = WhisperModel(model_dir, device=device, compute_type="int8")
        except Exception as e:
            raise RuntimeError(f"模型加载失败: {e}")

        segments, info = model.transcribe(
            src_path, language=None if language == "auto" else language,
            vad_filter=True)

        subs: List[Dict[str, Any]] = []
        for seg in segments:
            subs.append({
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": seg.text.strip(),
            })

        # SRT
        srt_lines = []
        for i, s in enumerate(subs, 1):
            def ts(t):
                h = int(t // 3600); m = int(t % 3600 // 60); sec = t % 60
                return f"{h:02d}:{m:02d}:{sec:06.3f}".replace(".", ",")
            srt_lines.append(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{s['text']}\n")
        srt_text = "\n".join(srt_lines)

        store = ctx["store"]
        sub_asset = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value, meta={"count": len(subs), "model": model_key}),
            payload=subs)
        srt_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "srt"}),
            payload={"text": srt_text})
        return {"subtitle": sub_asset, "srt": srt_asset}
