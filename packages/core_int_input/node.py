# -*- coding: utf-8 -*-
"""内置基础节点：整数输入（core/int_input，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class IntInputNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/int_input", title="整数输入", category="输入",
                        description="内置基础节点：输入一个整数（离线可用）",
                        inputs=[], outputs=[PortSpec("value", PortType.INT, label="整数", required=True)],
                        params=[PortSpec("value", PortType.INT, label="整数值", default=0, widget="number")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        try:
            v = int(params.get("value", 0) or 0)
        except (TypeError, ValueError):
            v = 0
        asset = store.save_asset(Asset(id="", kind=PortType.INT.value, meta={"value": v}), payload=v)
        return {"value": asset}

