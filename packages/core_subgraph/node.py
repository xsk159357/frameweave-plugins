# -*- coding: utf-8 -*-
"""内置子图节点：子图（core/subgraph，C1 平台内置包，离线可用）。

引擎在单节点执行分发时识别 core/subgraph 类型并**内联执行**（嵌套执行/输入
输出映射/跨层缓存，见 Engine._run_nested_subgraph）——本 run() 是防御性收口，
正常执行路径不会被调用（worker 通道亦短路回主进程内联分发）。
"""
from __future__ import annotations

from typing import Any, Dict

from app.nodespec import NodeBase, PortType


class SubgraphNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/subgraph", title="子图", category="控制",
                        description="内置子图节点：内嵌工作流按图执行（离线可用）",
                        inputs=[], outputs=[],
                        params=[PortSpec("graph", PortType.JSON, label="子图定义",
                                         required=False, widget="text")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        raise RuntimeError(
            "core/subgraph 由引擎内联执行（Engine._run_nested_subgraph），不能直接调用 run()")