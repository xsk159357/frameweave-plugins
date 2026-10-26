# -*- coding: utf-8 -*-
"""内置基础节点：音频信息（core/audio_info，S1-T6 平台内置包，离线可用）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

class AudioInfoNode(NodeBase):
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(type_id="core/audio_info", title="音频信息", category="分析",
                        description="内置基础节点：读取音频资产元数据（离线可用）",
                        inputs=[PortSpec("audio", PortType.AUDIO, label="音频", required=True)],
                        outputs=[PortSpec("info", PortType.JSON, label="信息", required=True)],
                        params=[], version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"]
        asset = inputs.get("audio")
        if asset is None:
            raise RuntimeError("invalid: 需要 音频 输入")
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

