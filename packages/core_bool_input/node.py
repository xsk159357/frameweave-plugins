# -*- coding: utf-8 -*-
"""内置基础节点：布尔输入（core/bool_input，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class BoolInputNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/bool_input", title="布尔输入", category="输入",
                        description="内置基础节点：输入一个布尔值（离线可用）",
                        inputs=[], outputs=[PortSpec("value", PortType.BOOL, label="布尔", required=True)],
                        params=[PortSpec("value", PortType.BOOL, label="布尔值", default=False, widget="toggle")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        v = bool(params.get("value", False))
        asset = store.save_asset(Asset(id="", kind=PortType.BOOL.value, meta={"value": v}), payload=v)
        return {"value": asset}

