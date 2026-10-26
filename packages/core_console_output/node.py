# -*- coding: utf-8 -*-
"""内置基础节点：控制台输出（core/console_output，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class ConsoleOutputNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/console_output", title="控制台输出", category="输出",
                        description="内置基础节点：查看任意端口值（离线可用；多别名输入适配平台同名连线）",
                        inputs=[PortSpec("data", PortType.ANY, label="输入", required=False),
                                PortSpec("text", PortType.ANY, label="文本", required=False),
                                PortSpec("value", PortType.ANY, label="值", required=False),
                                PortSpec("length", PortType.ANY, label="长度", required=False),
                                PortSpec("json", PortType.ANY, label="JSON", required=False)],
                        outputs=[PortSpec("result", PortType.JSON, label="结果", required=True)],
                        params=[PortSpec("note", PortType.STRING, label="备注", required=False, default="", widget="text")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        data = None
        for key in ("data", "text", "value", "length", "json"):
            if inputs.get(key) is not None:
                data = inputs.get(key)
                break
        payload = None
        if data is not None:
            try:
                payload = store.read_json(data.id)
            except Exception:
                payload = getattr(data, "path", "") or str(data)
        result = {"value": payload, "type": type(payload).__name__,
                  "note": str(params.get("note", "") or "")}
        asset = store.save_asset(Asset(id="", kind=PortType.JSON.value,
                                      meta={"note": result["note"]}), payload=result)
        return {"result": asset}

