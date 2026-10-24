"""基础节点：常量输入（S1-T6 内置基础节点集·输入类，离线可用）。

按 type 强转：STRING/INT/FLOAT/BOOL/JSON；JSON 经 json.loads 解析后原样输出。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


class ValueInputNode(NodeBase):
    """type_id: core/value_input"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/value_input",
            title="常量输入",
            category="输入",
            description="手动输入一个常量值（字符串/数字/布尔/JSON），离线可用",
            inputs=[],
            outputs=[PortSpec(name="value", type=PortType.ANY, label="值", required=True)],
            params=[
                PortSpec(name="type", type=PortType.STRING, label="类型", default="STRING",
                         widget="select", options=["STRING", "INT", "FLOAT", "BOOL", "JSON"]),
                PortSpec(name="value", type=PortType.STRING, label="值", required=True,
                         default="", widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        vtype = str(params.get("type", "STRING") or "STRING")
        raw = str(params.get("value", "") or "")
        if vtype == "INT":
            try:
                return {"value": int(raw.strip())}
            except (TypeError, ValueError) as e:
                raise RuntimeError(f"value_input: 无法转为 INT: {raw!r}") from e
        if vtype == "FLOAT":
            try:
                return {"value": float(raw.strip())}
            except (TypeError, ValueError) as e:
                raise RuntimeError(f"value_input: 无法转为 FLOAT: {raw!r}") from e
        if vtype == "BOOL":
            t = raw.strip().lower()
            if t in ("1", "true", "yes", "y", "on"):
                return {"value": True}
            if t in ("0", "false", "no", "n", "off", ""):
                return {"value": False}
            raise RuntimeError(f"value_input: 无法转为 BOOL: {raw!r}")
        if vtype == "JSON":
            try:
                return {"value": json.loads(raw)}
            except json.JSONDecodeError as e:
                raise RuntimeError(f"value_input: JSON 解析失败: {e}") from e
        return {"value": raw}