# -*- coding: utf-8 -*-
"""内置基础节点：列表长度（core/list_length，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class ListLengthNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/list_length", title="列表长度", category="语义",
                        description="内置基础节点：统计列表长度（离线可用）",
                        inputs=[PortSpec("list", PortType.JSON, label="列表", required=False)],
                        outputs=[PortSpec("length", PortType.INT, label="长度", required=True)],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        data = store.read_json(inputs["list"].id) if inputs.get("list") else []
        n = len(data) if isinstance(data, list) else 0
        asset = store.save_asset(Asset(id="", kind=PortType.INT.value, meta={"length": n}), payload=n)
        return {"length": asset}

