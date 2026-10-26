# -*- coding: utf-8 -*-
"""内置基础节点：图片信息（core/image_info，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class ImageInfoNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/image_info", title="图片信息", category="分析",
                        description="内置基础节点：读取图片资产元数据（离线可用）",
                        inputs=[PortSpec("image", PortType.IMAGE, label="图片", required=True)],
                        outputs=[PortSpec("info", PortType.JSON, label="信息", required=True)],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        asset = inputs.get("image")
        if asset is None:
            raise RuntimeError("invalid: 需要 图片 输入")
        meta = dict(getattr(asset, "meta", {}) or {})
        info = {
            "path": getattr(asset, "path", ""),
            "exists": os.path.isfile(getattr(asset, "path", "") or ""),
            "kind": getattr(asset, "kind", ""),
            "size": getattr(asset, "size", 0),
            "meta": meta,
            "container": meta.get("container") or {},
            "duration": (meta.get("container") or {}).get("duration"),
        }
        out = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload=info)
        return {"info": out}

