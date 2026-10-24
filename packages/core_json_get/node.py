"""基础节点：JSON 点路径取值（S1-T6）。"""
from __future__ import annotations
import re
from typing import Any, Dict
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_TOKEN = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")

def _tokens(path: str):
    return [a if a is not None else int(b) for a, b in _TOKEN.findall(path or "")]

class JsonGetNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(type_id="core/json_get", title="JSON 取值", category="JSON",
                        description="按点路径读取 JSON 值，离线可用",
                        inputs=[PortSpec(name="json", type=PortType.JSON, label="JSON")],
                        outputs=[PortSpec(name="value", type=PortType.ANY, label="值")],
                        params=[PortSpec(name="path", type=PortType.STRING, label="路径", default=""),
                                PortSpec(name="default", type=PortType.STRING, label="缺省值", required=False, default="")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        cur: Any = inputs.get("json")
        # JSON 资产在运行时可能仍是 Asset；优先从 store 读取
        if hasattr(cur, "id"):
            cur = ctx["store"].read_json(cur.id)
        found = True
        for token in _tokens(str(params.get("path", "") or "")):
            try:
                cur = cur[token]
            except (KeyError, IndexError, TypeError):
                found = False
                break
        if not found:
            cur = str(params.get("default", "") or "")
        return {"value": cur}