"""基础节点：文件输出（S1-T6）。使用 AssetStore 落盘，避免越权写任意路径。"""
from __future__ import annotations
import json
import os
from typing import Any, Dict
from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

class FileOutputNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(type_id="core/file_output", title="文件输出", category="输出",
                        description="将任意数据保存为平台 JSON 资产文件，离线可用",
                        inputs=[PortSpec(name="data", type=PortType.ANY, label="数据")],
                        outputs=[PortSpec(name="file", type=PortType.JSON, label="文件资产"), PortSpec(name="path", type=PortType.STRING, label="路径", required=False)],
                        params=[PortSpec(name="filename", type=PortType.STRING, label="文件名", default="output.json")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        value = inputs.get("data")
        filename = os.path.basename(str(params.get("filename", "output.json") or "output.json")) or "output.json"
        if not filename.lower().endswith(".json"):
            filename += ".json"
        payload = value if isinstance(value, (dict, list)) else {"value": value}
        asset = ctx["store"].save_asset(Asset(id="", kind=PortType.JSON.value, meta={"filename": filename}), payload=payload)
        return {"file": asset, "path": str(getattr(asset, "path", "") or "")}