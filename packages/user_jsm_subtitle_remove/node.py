# -*- coding: utf-8 -*-
"""user/jsm_subtitle_remove —— AI 无痕去字幕（解说猫 AI Tools 去字幕对应能力，轻量版）。

t2 §12.4 G24 建议：轻量版 = 画面底部字幕区域裁剪后放大回填（"demux 画面裁切"语义），
不依赖 inpainting 模型中心（模型版可作为后续增强）。
- region=bottom：裁剪底部 region_ratio（默认 0.20）区域 → 上移裁切并放大到原尺寸；
- region=top：裁剪顶部字幕区；
- region=both：上下各裁 region_ratio/2。
输出契约：video_out（去字幕视频）+ stats（JSON：region/比例/耗时）。
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from typing import Any, Dict, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


def _ffmpeg() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    return r["path"] if r.get("ready") else "ffmpeg"


def _ffprobe() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    return r["path"] if r.get("ready") else "ffprobe"


async def _probe_wh(ctx, path: str) -> Tuple[int, int]:
    import json
    try:
        out = await ctx["run_tracked"](
            [_ffprobe(), "-v", "quiet", "-print_format", "json", "-show_streams", path],
            timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for s in (json.loads(out.stdout or "{}").get("streams") or []):
            if s.get("codec_type") == "video" and s.get("width"):
                return int(s["width"]), int(s["height"])
    except Exception:  # noqa: BLE001
        pass
    return 1920, 1080


class JsmSubtitleRemoveNode(NodeBase):
    """AI 无痕去字幕（轻量版）。type_id: user/jsm_subtitle_remove"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_subtitle_remove",
            title="去字幕(轻量)",
            category="剪辑",
            description="画面字幕区域裁剪放大回填去字幕（bottom/top/both，轻量滤镜版）",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="源视频", required=True),
            ],
            outputs=[
                PortSpec(name="video_out", type=PortType.VIDEO, label="去字幕视频"),
                PortSpec(name="stats", type=PortType.JSON, label="处理报告"),
            ],
            params=[
                PortSpec(name="region", type=PortType.STRING, label="字幕区域", default="bottom",
                         widget="select", options=["bottom", "top", "both"],
                         description="bottom=底部字幕；top=顶部；both=上下各裁一半"),
                PortSpec(name="region_ratio", type=PortType.FLOAT, label="区域比例(0-0.5)", default=0.20,
                         widget="number", description="裁剪区域占画面高的比例（默认 0.20=20%）"),
                PortSpec(name="output_name", type=PortType.STRING, label="输出文件名", default="no_sub",
                         widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video = inputs.get("video")
        if video is None or not getattr(video, "path", "") or not os.path.exists(video.path):
            raise RuntimeError("需要源视频输入")
        store = ctx["store"]
        region = str(params.get("region", "bottom") or "bottom").strip()
        if region not in ("bottom", "top", "both"):
            raise ValueError(f"region 必须是 bottom|top|both，当前: {region!r}")
        ratio = max(0.0, min(0.5, float(params.get("region_ratio", 0.20) or 0.20)))
        if ratio <= 0:
            ratio = 0.20
        progress = ctx.get("progress") or (lambda p: None)

        w, h = await _probe_wh(ctx, video.path)
        # 裁剪后放大回原尺寸（无痕：裁掉字幕区并把保留区向上/向下平移填充）
        if region == "bottom":
            crop = f"crop={w}:{int(h * (1 - ratio))}:0:0,scale={w}:{h}"
        elif region == "top":
            crop = f"crop={w}:{int(h * (1 - ratio))}:0:{int(h * ratio)},scale={w}:{h}"
        else:  # both
            keep = max(1, int(h * (1 - ratio)))
            crop = f"crop={w}:{keep}:0:{(h - keep) // 2},scale={w}:{h}"

        workdir = os.path.join(ctx.get("workdir", "."), "jsm_subtitle_remove")
        os.makedirs(workdir, exist_ok=True)
        out_name = str(params.get("output_name", "no_sub") or "no_sub")
        out_path = os.path.join(workdir, f"{out_name}_{uuid.uuid4().hex[:8]}.mp4")
        started = time.time()
        cmd = [_ffmpeg(), "-y", "-i", video.path,
               "-vf", f"{crop},setsar=1",
               "-c:v", "libx264", "-preset", "medium", "-crf", "19",
               "-pix_fmt", "yuv420p", "-map", "0:a?", "-c:a", "copy",
               "-movflags", "+faststart", out_path]
        progress(0.2)
        proc = await ctx["run_tracked"](cmd, timeout=1800,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise RuntimeError("去字幕处理失败: %s" % str(proc.stderr or "")[-800:])
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("去字幕输出为空")
        progress(0.9)

        asset = store.save_asset(
            Asset(id="", kind=PortType.VIDEO.value, path=out_path,
                  meta={"region": region, "ratio": ratio}))
        stats = {"region": region, "ratio": ratio, "src": [w, h],
                 "output": out_path, "size": int(os.path.getsize(out_path)),
                 "rt_s": round(time.time() - started, 1)}
        progress(1.0)
        return {"video_out": asset, "stats": stats}