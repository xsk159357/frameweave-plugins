# -*- coding: utf-8 -*-
"""内置基础节点：视频信息（core/video_info，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class VideoInfoNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/video_info", title="视频信息", category="分析",
                        description="内置基础节点：读取视频资产元数据（离线可用）",
                        inputs=[PortSpec("video", PortType.VIDEO, label="视频", required=True)],
                        outputs=[PortSpec("info", PortType.JSON, label="信息", required=True)],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        asset = inputs.get("video")
        if asset is None:
            raise RuntimeError("invalid: 需要 视频 输入")
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

