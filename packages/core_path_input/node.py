# -*- coding: utf-8 -*-
"""内置基础节点：路径输入（core/path_input，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class PathInputNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/path_input", title="路径输入", category="输入",
                        description="内置基础节点：选择一个本地路径（离线可用）",
                        inputs=[], outputs=[PortSpec("path", PortType.STRING, label="路径", required=True)],
                        params=[PortSpec("path", PortType.STRING, label="路径", default="", widget="file")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        path = str(params.get("path", "") or "")
        asset = store.save_asset(Asset(id="", kind=PortType.STRING.value,
                                      meta={"exists": os.path.isfile(path)}),
                                 payload={"text": path})
        return {"path": asset}

