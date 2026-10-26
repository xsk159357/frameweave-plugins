# -*- coding: utf-8 -*-
"""内置基础节点：JSON 输入（core/json_input，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class JsonInputNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/json_input", title="JSON 输入", category="输入",
                        description="内置基础节点：手动输入一段 JSON（离线可用）",
                        inputs=[], outputs=[PortSpec("value", PortType.JSON, label="JSON", required=True)],
                        params=[PortSpec("value", PortType.STRING, label="JSON 文本", default="{}", widget="text")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        raw = str(params.get("value", "{}") or "{}")
        try:
            v = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"invalid: JSON 文本解析失败: {raw[:120]}")
        asset = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload=v)
        return {"value": asset}

