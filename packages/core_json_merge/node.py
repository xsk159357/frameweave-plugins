# -*- coding: utf-8 -*-
"""内置基础节点：JSON 合并（core/json_merge，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class JsonMergeNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/json_merge", title="JSON 合并", category="语义",
                        description="内置基础节点：合并两个 JSON 对象（离线可用）",
                        inputs=[PortSpec("a", PortType.JSON, label="对象A", required=False),
                                PortSpec("b", PortType.JSON, label="对象B", required=False)],
                        outputs=[PortSpec("json", PortType.JSON, label="合并结果", required=True)],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        out = {}
        for k in ("a", "b"):
            asset = inputs.get(k)
            if asset is None:
                continue
            v = store.read_json(asset.id)
            if isinstance(v, dict):
                out.update(v)
            else:
                out[k] = v
        asset = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload=out)
        return {"json": asset}

