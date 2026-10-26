# -*- coding: utf-8 -*-
"""内置基础节点：布尔取反（core/bool_not，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class BoolNotNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/bool_not", title="布尔取反", category="控制",
                        description="内置基础节点：布尔取反（离线可用）",
                        inputs=[PortSpec("value", PortType.BOOL, label="输入", required=False)],
                        outputs=[PortSpec("out", PortType.BOOL, label="取反结果", required=True)],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        asset = inputs.get("value")
        v = False
        if asset is not None:
            try:
                v = bool(store.read_json(asset.id))
            except Exception:
                v = False
        out = store.save_asset(Asset(id="", kind=PortType.BOOL.value, meta={"value": not v}),
                               payload=not v)
        return {"out": out}

