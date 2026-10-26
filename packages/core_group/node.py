# -*- coding: utf-8 -*-
"""内置分组节点：节点组（core/group，C1 平台内置包，离线可用）。

core/subgraph 的别名类型——引擎以同一套内联子图执行路径处理（见
Engine._run_nested_subgraph）；本 run() 是防御性收口，正常路径不会被调用。
"""
from __future__ import annotations

from typing import Any, Dict

from app.nodespec import NodeBase, PortType


class GroupNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/group", title="节点组", category="控制",
                        description="内置分组节点：core/subgraph 别名（离线可用）",
                        inputs=[], outputs=[],
                        params=[PortSpec("graph", PortType.JSON, label="子图定义",
                                         required=False, widget="text")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        raise RuntimeError(
            "core/group 由引擎内联执行（Engine._run_nested_subgraph），不能直接调用 run()")