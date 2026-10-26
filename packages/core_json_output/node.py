# -*- coding: utf-8 -*-
"""内置基础节点：JSON 输出（core/json_output，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class JsonOutputNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/json_output", title="JSON 输出", category="输出",
                        description="内置基础节点：把任意值包成 JSON 资产（离线可用；多别名输入适配平台同名连线）",
                        inputs=[PortSpec("data", PortType.ANY, label="输入", required=False),
                                PortSpec("text", PortType.ANY, label="文本", required=False),
                                PortSpec("value", PortType.ANY, label="值", required=False),
                                PortSpec("length", PortType.ANY, label="长度", required=False)],
                        outputs=[PortSpec("json", PortType.JSON, label="JSON", required=True)],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        data = None
        for key in ("data", "text", "value", "length"):
            if inputs.get(key) is not None:
                data = inputs.get(key)
                break
        payload = None
        if data is not None:
            try:
                payload = store.read_json(data.id)
            except Exception:
                payload = getattr(data, "path", "") or str(data)
        asset = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload={"value": payload})
        return {"json": asset}

