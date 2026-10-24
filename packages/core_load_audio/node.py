"""基础节点：音频输入（S1-T6，stdlib-only）。"""
from __future__ import annotations

import os
from typing import Any, Dict

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


class LoadAudioNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/load_audio", title="音频输入", category="输入",
            description="选择本地音频文件作为 AUDIO 资产输入，离线可用",
            inputs=[],
            outputs=[PortSpec(name="audio", type=PortType.AUDIO, label="音频")],
            params=[PortSpec(name="path", type=PortType.STRING, label="音频路径", required=True,
                             default="", widget="file")],
            version="1.0.0")

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        path = str(params.get("path", "") or "").strip()
        if not path or not os.path.isfile(path):
            raise RuntimeError(f"load_audio: 音频文件不存在: {path or '(空)'}")
        ext = os.path.splitext(path)[1].lower()
        if ext not in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}:
            raise RuntimeError(f"load_audio: 不支持的音频扩展名: {ext or '(无)'}")
        asset = Asset(id="", kind=PortType.AUDIO.value, path=path,
                      meta={"ext": ext, "size": os.path.getsize(path)})
        return {"audio": asset}