# -*- coding: utf-8 -*-
"""内置基础节点：文本输入（core/text_input，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class TextInputNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/text_input", title="文本输入", category="输入",
                        description="内置基础节点：输入一段文本（离线可用）",
                        inputs=[], outputs=[PortSpec("text", PortType.STRING, label="文本", required=True)],
                        params=[PortSpec("value", PortType.STRING, label="文本值", default="", widget="text")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        text = str(params.get("value", "") or "")
        asset = store.save_asset(Asset(id="", kind=PortType.STRING.value, meta={"chars": len(text)}),
                                 payload={"text": text})
        return {"text": asset}

