# -*- coding: utf-8 -*-
"""user/jsm_video_enhance —— 视频画质增强（解说猫 AI Tools 画质增强对应能力）。

轻量滤镜版（t2 §12.4 G23 建议）：不依赖 Real-ESRGAN 等模型中心大模型，
纯 ffmpeg 滤镜链提升观感，离线可用、秒级出片：
- clarity：unsharp 锐化（中度），通用清晰度增强；
- hd：Lanczos 放大到 2 倍分辨率（源 <1920 时）+ unsharp；
- vivid：饱和度/对比度增强（eq=saturation:contrast）；
- denoise：hqdn3d 时域降噪 + 轻锐化；
- auto：按源分辨率选择（≥1080p → clarity；<1080p → hd）。

输出契约：video_out（增强后视频）+ stats（JSON：preset/滤镜/尺寸/耗时）。
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from typing import Any, Dict

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_PRESETS = ("auto", "clarity", "hd", "vivid", "denoise")

# 各 preset 的滤镜链片段（按顺序拼到 scale 之后）
_PRESET_FILTERS = {
    "clarity": "unsharp=5:5:0.6:5:5:0.0",
    "hd": "scale=iw*2:ih*2:flags=lanczos,unsharp=5:5:0.8:5:5:0.0",
    "vivid": "eq=saturation=1.25:contrast=1.08:brightness=0.02",
    "denoise": "hqdn3d=3:2:4:3,unsharp=5:5:0.4:5:5:0.0",
}


def _ffmpeg() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    return r["path"] if r.get("ready") else "ffmpeg"


def _ffprobe() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    return r["path"] if r.get("ready") else "ffprobe"


async def _probe_size(ctx, path: str) -> tuple:
    """ffprobe 取宽高（失败回退 1920x1080）。"""
    try:
        import json
        out = await ctx["run_tracked"](
            [_ffprobe(), "-v", "quiet", "-print_format", "json", "-show_streams", path],
            timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(out.stdout or "{}")
        for s in (data.get("streams") or []):
            if s.get("codec_type") == "video" and s.get("width"):
                return int(s["width"]), int(s["height"])
    except Exception:  # noqa: BLE001
        pass
    return 1920, 1080


class JsmVideoEnhanceNode(NodeBase):
    """视频画质增强（轻量滤镜版）。type_id: user/jsm_video_enhance"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_video_enhance",
            title="视频画质增强",
            category="剪辑",
            description="ffmpeg 滤镜画质增强（unsharp/lanczos/eq/hqdn3d），轻量离线版，不依赖模型中心",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="源视频", required=True),
            ],
            outputs=[
                PortSpec(name="video_out", type=PortType.VIDEO, label="增强视频"),
                PortSpec(name="stats", type=PortType.JSON, label="增强报告"),
            ],
            params=[
                PortSpec(name="preset", type=PortType.STRING, label="增强预设", default="auto",
                         widget="select", options=list(_PRESETS),
                         description="clarity=锐化；hd=Lanczos 放大+锐化；vivid=饱和对比增强；denoise=降噪"),
                PortSpec(name="output_name", type=PortType.STRING, label="输出文件名", default="enhanced",
                         widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video = inputs.get("video")
        if video is None or not getattr(video, "path", "") or not os.path.exists(video.path):
            raise RuntimeError("需要源视频输入")
        store = ctx["store"]
        preset = str(params.get("preset", "auto") or "auto").strip()
        if preset not in _PRESETS:
            raise ValueError(f"preset 必须是 {'|'.join(_PRESETS)}，当前: {preset!r}")
        progress = ctx.get("progress") or (lambda p: None)

        src_w, src_h = await _probe_size(ctx, video.path)
        if preset == "auto":
            preset = "clarity" if src_w >= 1920 else "hd"
        filt = _PRESET_FILTERS[preset]

        workdir = os.path.join(ctx.get("workdir", "."), "jsm_video_enhance")
        os.makedirs(workdir, exist_ok=True)
        out_name = str(params.get("output_name", "enhanced") or "enhanced")
        out_path = os.path.join(workdir, f"{out_name}_{uuid.uuid4().hex[:8]}.mp4")
        started = time.time()
        cmd = [_ffmpeg(), "-y", "-i", video.path,
               "-vf", f"scale=trunc(iw/2)*2:trunc(ih/2)*2,{filt},setsar=1",
               "-c:v", "libx264", "-preset", "medium", "-crf", "19",
               "-pix_fmt", "yuv420p", "-map", "0:a?", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", out_path]
        progress(0.2)
        proc = await ctx["run_tracked"](cmd, timeout=1800,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise RuntimeError("画质增强失败: %s" % str(proc.stderr or "")[-800:])
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("增强输出为空")
        progress(0.9)

        asset = store.save_asset(
            Asset(id="", kind=PortType.VIDEO.value, path=out_path,
                  meta={"preset": preset, "src_size": [src_w, src_h]}))
        stats = {"preset": preset, "filter": filt, "src": [src_w, src_h],
                 "output": out_path, "size": int(os.path.getsize(out_path)),
                 "rt_s": round(time.time() - started, 1)}
        progress(1.0)
        return {"video_out": asset, "stats": stats}
