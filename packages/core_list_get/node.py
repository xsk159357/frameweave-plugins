# -*- coding: utf-8 -*-
"""内置基础节点：列表取值（core/list_get，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class ListGetNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/list_get", title="列表取值", category="语义",
                        description="内置基础节点：按索引取列表元素（离线可用）",
                        inputs=[PortSpec("list", PortType.JSON, label="列表", required=True)],
                        outputs=[PortSpec("value", PortType.ANY, label="元素", required=False)],
                        params=[PortSpec("index", PortType.INT, label="索引", default=0, widget="number")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        data = store.read_json(inputs["list"].id) if inputs.get("list") else None
        idx = int(params.get("index", 0) or 0)
        value = data[idx] if isinstance(data, list) and -len(data) <= idx < len(data) else None
        asset = store.save_asset(Asset(id="", kind=PortType.JSON.value, meta={"index": idx}),
                                 payload={"value": value})
        return {"value": asset}

