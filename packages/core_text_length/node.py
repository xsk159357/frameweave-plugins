# -*- coding: utf-8 -*-
"""内置基础节点：文本长度（core/text_length，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class TextLengthNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/text_length", title="文本长度", category="控制",
                        description="内置基础节点：统计文本长度（离线可用）",
                        inputs=[PortSpec("text", PortType.STRING, label="文本", required=False)],
                        outputs=[PortSpec("length", PortType.INT, label="长度", required=True)],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        asset = inputs.get("text")
        text = ""
        if asset is not None:
            try:
                p = store.read_json(asset.id)
                text = str(p.get("text", "") or "") if isinstance(p, dict) else str(p or "")
            except Exception:
                text = ""
        out = store.save_asset(Asset(id="", kind=PortType.INT.value, meta={"length": len(text)}),
                               payload=len(text))
        return {"length": out}

