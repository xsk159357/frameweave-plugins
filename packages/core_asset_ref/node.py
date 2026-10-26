# -*- coding: utf-8 -*-
"""内置基础节点：资产引用（core/asset_ref，C1 平台内置包，离线可用）。

子图输入种子节点：按参数 asset_id 从素材库取回既有资产（同一 id，不复制）。
引擎在构造嵌套子图内层工作流时，用本节点把外层输入资产注入内层端口；
节点也可被用户直接使用（引用素材库既有资产参与连线/计算）。
"""
from __future__ import annotations

from typing import Any, Dict

from app.nodespec import NodeBase, PortType


class AssetRefNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/asset_ref", title="资产引用", category="控制",
                        description="内置基础节点：按资产 ID 引用素材库资产（离线可用）",
                        inputs=[],
                        outputs=[PortSpec("out", PortType.ANY, label="资产", required=False)],
                        params=[PortSpec("asset_id", PortType.STRING, label="资产ID",
                                         default="", required=False, widget="text")],
                        version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        aid = str(params.get("asset_id", "") or "")
        if not aid:
            return {"out": None}
        asset = store.get(aid)
        if asset is None:
            raise RuntimeError(f"资产不存在: {aid}")
        return {"out": asset}