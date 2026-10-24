"""基础节点：媒体信息（S1-T6，离线 stdlib 路径元数据）。"""
from __future__ import annotations
import os
from typing import Any, Dict
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

class ProbeMediaNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(type_id="core/probe_media", title="媒体信息", category="媒体",
                        description="读取媒体路径、扩展名、大小等基础信息，离线可用",
                        inputs=[PortSpec(name="media", type=PortType.ANY, label="媒体")],
                        outputs=[PortSpec(name="info", type=PortType.JSON, label="媒体信息")],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        media = inputs.get("media")
        path = str(getattr(media, "path", "") or (media.get("path", "") if isinstance(media, dict) else ""))
        meta = getattr(media, "meta", None) if media is not None else None
        info = {"path": path, "exists": bool(path and os.path.isfile(path)),
                "size": os.path.getsize(path) if path and os.path.isfile(path) else 0,
                "ext": os.path.splitext(path)[1].lower() if path else "",
                "meta": dict(meta or {})}
        return {"info": info}