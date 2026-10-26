# -*- coding: utf-8 -*-
"""内置基础节点：JSON 设值（core/json_set，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class JsonSetNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/json_set", title="JSON 设值", category="语义",
                        description="内置基础节点：写入一个键值并返回新 JSON（离线可用）",
                        inputs=[PortSpec("json", PortType.JSON, label="JSON", required=False)],
                        outputs=[PortSpec("result", PortType.JSON, label="新 JSON", required=True)],
                        params=[PortSpec("key", PortType.STRING, label="键", default="", widget="text"),
                                PortSpec("value", PortType.STRING, label="值(JSON 解析优先)", required=False, default="", widget="text")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        data = store.read_json(inputs["json"].id) if inputs.get("json") else {}
        if not isinstance(data, dict):
            data = {"value": data}
        key = str(params.get("key", "") or "")
        raw = str(params.get("value", "") or "")
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
        if key:
            data[key] = val
        asset = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload=data)
        return {"result": asset}

