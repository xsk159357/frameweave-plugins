# -*- coding: utf-8 -*-
"""user/jsm_video_compress —— 薄封装（P2-12 双实现收敛：委托 core/video_compress）。

背景：core/video_compress（官方）已实现编码器探测 + 硬件加速自动回退软编 +
码率上限=原码率×0.7 + HEVC CRF 偏移+5 + 进度/统计报告（content 对齐 r4 §5 逆向语义）。
本 `user/jsm_video_compress` 曾为独立重复实现（编码逻辑基本同 core），存在双实现漂移风险。
收敛决策（R-工程-3 / P2-12）：**保留 type_id 兼容旧工作流，运行期委托 core/video_compress**，
本包仅做参数词汇映射（h264→libx264 / h265→libx265 / nvenc_h264→h264_nvenc /
nvenc_h265→hevc_nvenc / accel→hardware_accel），输出契约不变（compressed/stats）。
取舍：core/video_compress 为唯一真实现（编码语义/缺陷修复/B11 回退均在其内）；
本包不再携带编码逻辑，避免与官方实现漂移。
"""
from __future__ import annotations

import os
from typing import Any, Dict


def _core_class():
    """经平台注册表解析 core/video_compress（安装布局无关）。"""
    from app import declarative
    cls = declarative.get_class("core/video_compress")
    return cls


_ENCODER_MAP = {
    "auto": "auto",
    "h264": "libx264",
    "h265": "libx265",
    "nvenc_h264": "h264_nvenc",
    "nvenc_h265": "hevc_nvenc",
}


class VideoCompressNode:
    """兼容壳：user/jsm_video_compress → core/video_compress（P2-12 收敛）。"""

    @classmethod
    def spec(cls):
        return _core_class().spec()

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        # 参数词汇映射（保持旧工作流参数名可用）
        enc = str(params.get("encoder", "auto") or "auto")
        mapped = dict(params or {})
        mapped["encoder"] = _ENCODER_MAP.get(enc, enc if enc in (
            "auto", "hevc_nvenc", "h264_nvenc", "libx265", "libx264", "libsvtav1") else "auto")
        mapped.setdefault("max_height", 0)
        mapped.setdefault("hardware_accel", str(params.get("accel", "auto") or "auto") != "none")
        mapped.setdefault("remove_metadata", True)
        return await _core_class()().run(ctx, inputs, mapped)