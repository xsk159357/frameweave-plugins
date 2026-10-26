# -*- coding: utf-8 -*-
"""内置基础节点：条件选择（core/select，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class SelectNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/select", title="条件选择", category="控制",
                        description="内置基础节点：按条件选择 a 或 b（离线可用）",
                        inputs=[PortSpec("a", PortType.ANY, label="条件真值", required=False),
                                PortSpec("b", PortType.ANY, label="条件假值", required=False)],
                        outputs=[PortSpec("out", PortType.ANY, label="选中值", required=False)],
                        params=[PortSpec("condition", PortType.BOOL, label="条件", default=True, widget="toggle")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        cond = bool(params.get("condition", True))
        return {"out": inputs.get("a") if cond else inputs.get("b")}

