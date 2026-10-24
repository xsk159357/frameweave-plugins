# -*- coding: utf-8 -*-
"""core/timeline_builder —— split_cam 规范校验/规范化节点（S3-T11/A7 数据契约）。

历史：该包目录原先只有 schema.py（canonical split_cam schema），无 manifest 无法打包/扫描。
t3 补全为完整节点：输入 split_cam 原始行列表（JSON），经 schema.normalize_segment /
validate_split_cam 规范化输出，供下游 video_render / draft_export 消费标准字段：
{segment_index, speed, src_start, src_end, OST, video}。

能力：
- 输入 {rows:[...]} 或直接数组；逐行补缺省字段（speed=1.0 / OST=0 / video=""）。
- 校验 src_end >= src_start、必需字段存在；非法行报错（不静默吞错）。
- 输出 rows（规范化后）+ report（行数/字段统计）。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_SCHEMA_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_schema_module():
    """加载包内 schema.py（split_cam 规范化/校验）。"""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "fw_timeline_builder_schema", os.path.join(_SCHEMA_DIR, "schema.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fw_timeline_builder_schema"] = mod
    spec.loader.exec_module(mod)
    return mod


class TimelineBuilderNode(NodeBase):
    """split_cam 规范校验/规范化。type_id: core/timeline_builder"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/timeline_builder",
            title="时间线构建(校验)",
            category="分析",
            description="split_cam 规范校验/规范化（S3-T11/A7）：输入原始分镜行，输出规范化 rows 供渲染/导出",
            inputs=[
                PortSpec(name="rows", type=PortType.JSON, label="分镜行", required=True,
                         description="split_cam 原始行数组或 {rows:[...]}"),
            ],
            outputs=[
                PortSpec(name="timeline", type=PortType.TIMELINE, label="规范化时间线"),
                PortSpec(name="report", type=PortType.JSON, label="构建报告", required=False),
            ],
            params=[],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        rows_asset = inputs.get("rows")
        if rows_asset is None:
            raise RuntimeError("需要 rows 输入（split_cam 原始行）")
        raw = store.read_json(rows_asset.id) or []
        if isinstance(raw, dict):
            raw = raw.get("rows") or raw.get("items") or []
        if not isinstance(raw, list):
            raise RuntimeError("rows 输入必须是数组（或 {rows:[...]} 信封）")
        if not raw:
            raise RuntimeError("rows 为空")
        mod = _load_schema_module()
        out = mod.validate_split_cam(raw)
        timeline_asset = store.save_asset(
            Asset(id="", kind=PortType.TIMELINE.value, meta={"count": len(out)}), payload=out)
        report = {
            "rows": len(out),
            "fields": list(mod.REQUIRED_FIELDS),
            "normalized": True,
            "source": "core/timeline_builder (schema.py)",
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"rows": len(out)}), payload=report)
        return {"timeline": timeline_asset, "report": report_asset}