"""镜头切分节点：把视频按画面突变切成场景段。

降级链（产品可用性的一部分，不是防御性代码——没装 ffmpeg、没有独显的机器也要能跑）：
  1. ffmpeg scene 检测：画面差异超过阈值处切断，最准
  2. ffprobe 取时长 + 固定间隔切分：ffmpeg 不可用或整片没有可识别切点时
  3. 两者都不可用 ⇒ 明确报错

第 3 条是刻意的：不返回「空场景」假装成功。返回空列表时下游会产出空时间线，
用户看到的是「导出成功但没画面」，比直接报错难查十倍。

**切分与描述必须解耦**（jieshuomao 踩过的坑）：本节点只输出时间边界，不生成任何
内容描述。下游 scene.describe 拿到这些边界后只能读，不许改——它一旦能改边界，
时间轴和画面就会对不上，而且错得很隐蔽。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

_FFMPEG_TIMEOUT = 900     # 长片切分可能跑很久；超时视为不可用并降级
_PROBE_TIMEOUT = 30
_PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")


def _tool(name: str) -> str:
    return shutil.which(name) or ""


def _run(cmd: List[str], timeout: int) -> Optional[subprocess.CompletedProcess]:
    """跑外部命令；任何执行期异常都折叠成 None（由调用方决定降级）。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None


def _duration(path: str) -> float:
    exe = _tool("ffprobe")
    if not exe:
        return 0.0
    p = _run([exe, "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", path], _PROBE_TIMEOUT)
    if p is None or p.returncode != 0:
        return 0.0
    try:
        v = float((p.stdout or "").strip() or 0)
    except ValueError:
        return 0.0
    return v if v > 0 else 0.0


def _cut_long(start: float, end: float, max_sec: float) -> List[Tuple[float, float]]:
    """把一段强切到不超过 max_sec。等分而不是从前往后切，避免末段特别短。"""
    span = end - start
    if span <= max_sec or max_sec <= 0:
        return [(start, end)]
    n = int(span // max_sec) + (1 if span % max_sec > 1e-9 else 0)
    step = span / n
    return [(start + k * step, start + (k + 1) * step) for k in range(n)]


def _build(cuts: List[float], total: float, min_sec: float,
           max_sec: float) -> List[Dict[str, Any]]:
    """切点 → 合法段：丢弃过近切点、补齐片尾、强切过长。"""
    bounds: List[float] = [0.0]
    for c in sorted(set(cuts)):
        if c <= 0:
            continue
        if total > 0 and c >= total - 0.05:
            continue                      # 片尾切点没有意义，由下面的 total 兜住
        if c - bounds[-1] < min_sec:
            continue                      # 太近：并进前一段
        bounds.append(c)
    bounds.append(total if total > 0 else bounds[-1] + max_sec)

    segs: List[Dict[str, Any]] = []
    for i in range(len(bounds) - 1):
        for s, e in _cut_long(bounds[i], bounds[i + 1], max_sec):
            if e - s <= 1e-6:
                continue
            segs.append({"index": len(segs), "start": round(s, 3),
                         "end": round(e, 3), "duration": round(e - s, 3)})
    return segs


def _detect(path: str, threshold: float, min_sec: float,
            max_sec: float) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """ffmpeg 场景检测。返回 (段列表 或 None, 原因)。"""
    exe = _tool("ffmpeg")
    if not exe:
        return None, "未找到 ffmpeg"
    p = _run([exe, "-hide_banner", "-nostats", "-i", path,
              "-filter:v", "select='gt(scene,%g)',showinfo" % threshold,
              "-an", "-f", "null", "-"], _FFMPEG_TIMEOUT)
    if p is None:
        return None, "ffmpeg 执行失败或超时"
    if p.returncode != 0:
        return None, "ffmpeg 返回码 %d（视频可能无法解码）" % p.returncode
    times = [float(x) for x in _PTS_RE.findall(p.stderr or "")]
    if not times:
        return None, "整片没有超过阈值 %g 的画面变化" % threshold
    total = _duration(path)
    segs = _build(times, total, min_sec, max_sec)
    if not segs:
        return None, "切点全部被最短镜头约束合并"
    return segs, "ffmpeg"


def _fixed(total: float, interval: float, max_sec: float) -> List[Dict[str, Any]]:
    """固定间隔切分：完全不看画面，同一输入每次结果一致（可复现）。"""
    interval = max(interval, 0.5)
    segs: List[Dict[str, Any]] = []
    t = 0.0
    while t < total - 1e-6:
        e = min(total, t + interval)
        for s, ee in _cut_long(t, e, max_sec):
            if ee - s <= 1e-6:
                continue
            segs.append({"index": len(segs), "start": round(s, 3),
                         "end": round(ee, 3), "duration": round(ee - s, 3)})
        t = e
    return segs


class SceneDetectNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video = inputs.get("video")
        path = str(getattr(video, "path", "") or "") if video is not None else ""
        if not path:
            raise RuntimeError("缺少视频输入：video 端口未连接，或资产没有文件路径")
        if not os.path.isfile(path):
            raise RuntimeError("视频文件不存在: %s" % path)

        engine = str(params.get("engine", "auto") or "auto").strip().lower()
        if engine not in ("auto", "ffmpeg", "fixed"):
            engine = "auto"
        try:
            threshold = float(params.get("threshold", 0.3))
        except (TypeError, ValueError):
            threshold = 0.3
        threshold = min(max(threshold, 0.05), 0.95)
        try:
            min_sec = max(float(params.get("min_scene_sec", 1.0) or 1.0), 0.0)
        except (TypeError, ValueError):
            min_sec = 1.0
        try:
            max_sec = float(params.get("max_scene_sec", 12.0) or 12.0)
        except (TypeError, ValueError):
            max_sec = 12.0
        max_sec = max(max_sec, max(min_sec, 0.5))
        try:
            interval = float(params.get("fallback_interval", 5.0) or 5.0)
        except (TypeError, ValueError):
            interval = 5.0

        warnings: List[str] = []
        segs: Optional[List[Dict[str, Any]]] = None
        used = ""

        if engine in ("auto", "ffmpeg"):
            segs, reason = await asyncio.to_thread(
                _detect, path, threshold, min_sec, max_sec)
            if segs:
                used = "ffmpeg"
            else:
                warnings.append("ffmpeg 场景检测不可用：%s" % reason)

        if not segs and engine in ("auto", "fixed"):
            total = await asyncio.to_thread(_duration, path)
            if total > 0:
                segs = _fixed(total, interval, max_sec)
                used = "fixed"
                warnings.append("已降级为固定间隔切分（每 %.1fs 一刀，不看画面）" % interval)
            else:
                warnings.append("取不到视频时长（ffprobe 不可用或文件不可解析）")

        if not segs:
            raise RuntimeError(
                "镜头切分失败：ffmpeg 检测与固定间隔切分都不可用。原因：%s"
                % ("；".join(warnings) or "未知"))
        store = ctx["store"]
        out = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"count": len(segs), "engine": used, "source": path,
                        "degraded": used != "ffmpeg",
                        "warnings": list(warnings)}),
            payload=segs)
        return {"scenes": out}
