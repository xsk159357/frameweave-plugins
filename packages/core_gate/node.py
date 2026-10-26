# -*- coding: utf-8 -*-
"""内置基础节点：门开关（core/gate，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class GateNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/gate", title="门开关", category="控制",
                        description="内置基础节点：enabled 为真时透传输入，否则输出空（离线可用）",
                        inputs=[PortSpec("data", PortType.ANY, label="输入", required=True)],
                        outputs=[PortSpec("out", PortType.ANY, label="输出", required=False)],
                        params=[PortSpec("enabled", PortType.BOOL, label="启用", default=True, widget="toggle")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        enabled = bool(params.get("enabled", True))
        data = inputs.get("data")
        if not enabled or data is None:
            return {"out": None}
        # 透传上游资产（引用同一 asset）
        return {"out": data}

