# -*- coding: utf-8 -*-
"""内置基础节点：JSON 数组（core/json_array，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class JsonArrayNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/json_array", title="JSON 数组", category="语义",
                        description="内置基础节点：把若干输入收集为数组（离线可用）",
                        inputs=[PortSpec("v1", PortType.ANY, label="元素1", required=False),
                                PortSpec("v2", PortType.ANY, label="元素2", required=False),
                                PortSpec("v3", PortType.ANY, label="元素3", required=False),
                                PortSpec("v4", PortType.ANY, label="元素4", required=False)],
                        outputs=[PortSpec("array", PortType.JSON, label="数组", required=True)],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        arr = []
        for k in ("v1", "v2", "v3", "v4"):
            asset = inputs.get(k)
            if asset is None:
                continue
            try:
                arr.append(store.read_json(asset.id))
            except Exception:
                arr.append(getattr(asset, "path", "") or str(asset))
        asset = store.save_asset(Asset(id="", kind=PortType.JSON.value, meta={"count": len(arr)}),
                                 payload=arr)
        return {"array": asset}

