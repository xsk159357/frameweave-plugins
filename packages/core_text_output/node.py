"""基础节点：文本输出（S1-T6，离线可用）。"""
from __future__ import annotations
import json
from typing import Any, Dict
from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

class TextOutputNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(type_id="core/text_output", title="文本输出", category="输出",
                        description="将任意值整理为可查看的文本统计 JSON，离线可用",
                        inputs=[PortSpec(name="text", type=PortType.ANY, label="输入")],
                        outputs=[PortSpec(name="result", type=PortType.JSON, label="输出")],
                        params=[PortSpec(name="note", type=PortType.STRING, label="备注", required=False, default="")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        value = inputs.get("text")
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value if value is not None else "")
        payload = {"schema_version": 1, "text": text, "chars": len(text),
                   "lines": len(text.splitlines()) if text else 0,
                   "note": str(params.get("note", "") or "")}
        asset = ctx["store"].save_asset(Asset(id="", kind=PortType.JSON.value, meta={"chars": len(text)}), payload=payload)
        return {"result": asset}