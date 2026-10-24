"""基础节点：图片输入（S1-T6，stdlib-only）。"""
from __future__ import annotations

import os
from typing import Any, Dict

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


class LoadImageNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/load_image", title="图片输入", category="输入",
            description="选择本地图片文件作为 IMAGE 资产输入，离线可用",
            inputs=[],
            outputs=[PortSpec(name="image", type=PortType.IMAGE, label="图片")],
            params=[PortSpec(name="path", type=PortType.STRING, label="图片路径", required=True,
                             default="", widget="file")],
            version="1.0.0")

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        path = str(params.get("path", "") or "").strip()
        if not path or not os.path.isfile(path):
            raise RuntimeError(f"load_image: 图片文件不存在: {path or '(空)'}")
        ext = os.path.splitext(path)[1].lower()
        if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
            raise RuntimeError(f"load_image: 不支持的图片扩展名: {ext or '(无)'}")
        asset = Asset(id="", kind=PortType.IMAGE.value, path=path,
                      meta={"ext": ext, "size": os.path.getsize(path)})
        return {"image": asset}