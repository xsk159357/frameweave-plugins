"""SceneDetect 节点：视频场景/镜头切分。

M0 实现：基于 ffmpeg 抽取关键帧 + 像素差分的轻量算法（不依赖 torch）。
算法：按固定间隔抽帧 -> 计算相邻帧感知哈希差 -> 超过阈值判定为场景切换。
产出 SEGMENTS 资产（片段列表，含时间戳/时长/首帧缩略图）。
"""
from __future__ import annotations
import json
import math
import os
import struct
import subprocess
import tempfile
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


# 无 opencv 时用纯 Python 计算简易亮度差（避免强依赖）
try:
    import numpy as np
    HAS_NP = True
except ImportError:
    HAS_NP = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


def find_ffmpeg() -> str:
    """查找 ffmpeg 可执行：PATH -> imageio_ffmpeg 自带的二进制。"""
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


FFMPEG = find_ffmpeg()


async def _run(cmd: list, ctx: Dict[str, Any] = None):
    """执行外部命令（P3 迁移：优先 ctx.run_tracked 托管；缺省回退 subprocess.run）。"""
    if ctx is not None and ctx.get("run_tracked"):
        return await ctx["run_tracked"](cmd, timeout=900, text=True)
    import subprocess
    return subprocess.run(cmd, capture_output=True, timeout=900)


async def _probe_duration(video_path: str, ctx: Dict[str, Any] = None) -> float:
    if not FFMPEG:
        return 0.0
    exe = find_ffprobe()
    try:
        out = await _run([exe, "-v", "quiet", "-show_entries", "format=duration", "-of", "json", video_path], ctx)
        data = json.loads(out.stdout or "{}")
        return float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def find_ffprobe() -> str:
    import shutil
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    # imageio-ffmpeg 只带 ffmpeg；ffprobe 缺失时用 ffmpeg -i 解析
    return "ffprobe"


async def _extract_frames(video_path: str, fps: float, workdir: str,
                          ctx: Dict[str, Any] = None) -> List[str]:
    """抽取 fps 帧率的 jpg 帧到 workdir，返回帧文件列表（按序）。"""
    out_dir = os.path.join(workdir, "frames")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        try: os.remove(os.path.join(out_dir, f))
        except OSError: pass
    try:
        await _run([FFMPEG, "-v", "quiet", "-i", video_path, "-vf",
                    "fps={}".format(fps), os.path.join(out_dir, "frame_%05d.jpg")], ctx)
    except (OSError, subprocess.TimeoutExpired, TimeoutError):
        return []
    frames = sorted(os.listdir(out_dir))
    return [os.path.join(out_dir, f) for f in frames]


def _frame_diff_simple(path_a: str, path_b: str) -> float:
    """两个 jpg 的感知差（0-1）。优先 opencv 像素差；降级纯 Python。"""
    if HAS_CV2:
        try:
            import numpy as _np
            def _read_gray(p):
                # imdecode 支持中文路径（imread 在中文路径下返回 None）
                data = _np.fromfile(p, dtype=_np.uint8)
                return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            a = _read_gray(path_a)
            b = _read_gray(path_b)
            if a is not None and b is not None and a.shape == b.shape:
                # 灰度直方图差（感知差异，对场景切换敏感）
                ha = cv2.calcHist([a], [0], None, [16], [0, 256])
                hb = cv2.calcHist([b], [0], None, [16], [0, 256])
                cv2.normalize(ha, ha)
                cv2.normalize(hb, hb)
                return float(cv2.compareHist(ha, hb, cv2.HISTCMP_BHATTACHARYYA))
            if a is not None and b is not None:
                # 尺寸不同：缩放到小图再比
                small = lambda im: cv2.resize(im, (32, 18))
                a2, b2 = small(a), small(b)
                diff = cv2.absdiff(a2, b2)
                return float(diff.mean() / 255.0)
        except Exception:
            pass
    # 纯 Python 兜底
    try:
        sz_a, sz_b = os.path.getsize(path_a), os.path.getsize(path_b)
        if sz_a == 0 or sz_b == 0:
            return 0.0
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            ha = fa.read(4096) + fa.read()[-1024:]
            hb = fb.read(4096) + fb.read()[-1024:]
        diff = 0
        n = min(len(ha), len(hb))
        if n == 0:
            return 0.0
        for i in range(n):
            diff += abs(ha[i] - hb[i]) / 255.0
        return diff / n
    except OSError:
        return 0.0

class SceneDetectNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/scene_detect",
            title="场景检测",
            category="分析",
            description="检测镜头/场景切换，输出片段列表（时间戳/时长/缩略图）",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="视频", required=True),
            ],
            outputs=[
                PortSpec(name="segments", type=PortType.SEGMENTS, label="片段列表"),
                PortSpec(name="count", type=PortType.INT, label="片段数"),
            ],
            params=[
                PortSpec(name="sample_fps", type=PortType.FLOAT, label="抽样帧率",
                         default=1.0, widget="number"),
                PortSpec(name="threshold", type=PortType.FLOAT, label="切换阈值",
                         default=0.12, widget="number"),
                PortSpec(name="min_duration", type=PortType.FLOAT, label="最小片段时长(秒)",
                         default=0.6, widget="number"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        if video_asset is None or not video_asset.path or not os.path.exists(video_asset.path):
            raise RuntimeError("缺少有效的视频输入资产")

        sample_fps = float(params.get("sample_fps", 1.0) or 1.0)
        threshold = float(params.get("threshold", 0.12) or 0.12)
        min_duration = float(params.get("min_duration", 0.6) or 0.6)

        duration = video_asset.meta.get("duration") or await _probe_duration(video_asset.path, ctx)
        workdir = ctx["workdir"]

        frames = await _extract_frames(video_asset.path, sample_fps, workdir, ctx)
        if not frames:
            # 无 ffmpeg 时生成伪片段（整段为一个片段）
            segments = [{
                "index": 0, "start": 0.0, "end": duration,
                "duration": duration, "thumbnail": "",
            }]
        else:
            boundaries: List[int] = [0]
            for i in range(1, len(frames)):
                d = _frame_diff_simple(frames[i - 1], frames[i])
                if d >= threshold:
                    boundaries.append(i)
            boundaries.append(len(frames))

            segments = []
            for k in range(len(boundaries) - 1):
                s = boundaries[k] / sample_fps
                e = boundaries[k + 1] / sample_fps
                seg_dur = e - s
                if seg_dur < min_duration:
                    continue
                segments.append({
                    "index": len(segments),
                    "start": round(s, 3),
                    "end": round(e, 3),
                    "duration": round(seg_dur, 3),
                    "thumbnail": frames[boundaries[k]],
                })

        store = ctx["store"]
        asset = Asset(id="", kind=PortType.SEGMENTS.value,
                      meta={"count": len(segments), "video_duration": duration,
                            "algorithm": "framediff-m0"})
        asset = store.save_asset(asset, payload=segments)
        return {"segments": asset, "count": len(segments)}
