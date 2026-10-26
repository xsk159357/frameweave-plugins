# -*- coding: utf-8 -*-
"""内置基础节点：文本拼接（core/text_concat，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class TextConcatNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/text_concat", title="文本拼接", category="控制",
                        description="内置基础节点：拼接两段文本（离线可用）",
                        inputs=[PortSpec("a", PortType.STRING, label="文本A", required=False),
                                PortSpec("b", PortType.STRING, label="文本B", required=False)],
                        outputs=[PortSpec("text", PortType.STRING, label="拼接结果", required=True)],
                        params=[PortSpec("sep", PortType.STRING, label="分隔符", default="", required=False, widget="text")],
                        version="1.0.0")

    @staticmethod
    def _read(store, asset):
        if asset is None:
            return ""
        try:
            p = store.read_json(asset.id)
            if isinstance(p, dict):
                return str(p.get("text", "") or "")
            return str(p or "")
        except Exception:
            return ""

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        a = self._read(store, inputs.get("a"))
        b = self._read(store, inputs.get("b"))
        sep = str(params.get("sep", "") or "")
        text = a + sep + b
        asset = store.save_asset(Asset(id="", kind=PortType.STRING.value, meta={"chars": len(text)}),
                                 payload={"text": text})
        return {"text": asset}

