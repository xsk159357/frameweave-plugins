"""媒体导入节点：把本地视频接入流程，产出视频 + 音频两路资产。

**不做转码**（默认）。转码是「跑得好不好」，接入是「能不能跑」——把这两件事分开，
否则一个可选能力（本机没 ffmpeg）就会阻断整条流程。代理生成是显式开启的参数，
默认 0 = 关。

资产不复制文件：Asset.path 直接指向原视频，只算一个内容指纹用于增量缓存。
复制一遍等于把 25 GiB 的片子再存一份，没有任何收益。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from app import media_tools
from app.assets import Asset
from app.nodespec import NodeBase, PortType

_PROBE_TIMEOUT = 30
_FFMPEG_TIMEOUT = 1800


def _probe(path: str) -> Optional[Dict[str, Any]]:
    """容器与流信息：ffprobe 优先，没有就用 ffmpeg -i 解析（见 media_tools）。"""
    return media_tools.stream_info(path)


def _pick(probe: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """从 ffprobe 输出里挑出我们真正要用的几个字段。"""
    meta: Dict[str, Any] = {}
    warns: List[str] = []
    fmt = probe.get("format") or {}
    try:
        dur = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur > 0:
        meta["duration"] = round(dur, 3)
    else:
        warns.append("ffprobe 没给出时长，下游估算会退化为按字数推")
    if fmt.get("format_name"):
        meta["format"] = fmt.get("format_name")
    try:
        meta["size"] = int(fmt.get("size") or 0)
    except (TypeError, ValueError):
        pass
    vs = [s for s in (probe.get("streams") or []) if s.get("codec_type") == "video"]
    aus = [s for s in (probe.get("streams") or []) if s.get("codec_type") == "audio"]
    if vs:
        v = vs[0]
        meta["width"] = int(v.get("width") or 0)
        meta["height"] = int(v.get("height") or 0)
        meta["vcodec"] = v.get("codec_name") or ""
        try:
            num, den = (v.get("avg_frame_rate") or "0/1").split("/")
            fps = float(num) / float(den) if float(den) else 0.0
            if fps > 0:
                meta["fps"] = round(fps, 3)
        except (ValueError, ZeroDivisionError):
            pass
    else:
        warns.append("没有视频流：这可能是纯音频文件")
    if aus:
        meta["acodec"] = aus[0].get("codec_name") or ""
        try:
            meta["sample_rate"] = int(aus[0].get("sample_rate") or 0)
        except (TypeError, ValueError):
            pass
    else:
        warns.append("没有音频流：语音转写与配音相关节点拿不到输入")
    return meta, warns


def _make_proxy(path: str, out_dir: str, height: int) -> Tuple[str, str]:
    """生成低分辨率代理。返回 (代理路径 或 "", 失败原因)。"""
    exe = media_tools.ffmpeg_path()
    if not exe:
        return "", "未找到 ffmpeg"
    dst = os.path.join(out_dir, "proxy_%dp.mp4" % height)
    try:
        p = subprocess.run(
            [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", path,
             "-vf", "scale=-2:%d" % height, "-c:v", "libx264", "-crf", "28",
             "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k", dst],
            capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT, errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return "", "ffmpeg 执行失败: %s" % e
    if p.returncode != 0 or not os.path.isfile(dst):
        return "", "ffmpeg 返回码 %d: %s" % (p.returncode, (p.stderr or "")[:160])
    return dst, ""


class MediaImportNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        path = str(params.get("path") or "").strip().strip('"')
        if not path:
            raise RuntimeError("未选择视频文件：请填写「视频文件」参数")
        if not os.path.isfile(path):
            raise RuntimeError("视频文件不存在: %s" % path)
        path = os.path.abspath(path)

        warnings: List[str] = []
        meta: Dict[str, Any] = {}

        if bool(params.get("probe", True)):
            probe = await asyncio.to_thread(_probe, path)
            if probe is None:
                warnings.append("ffprobe 不可用或无法解析该文件，元数据留空（流程继续）")
            else:
                meta, w = _pick(probe)
                warnings.extend(w)

        store = ctx["store"]

        try:
            proxy_h = int(params.get("proxy_height", 0) or 0)
        except (TypeError, ValueError):
            proxy_h = 0
        video_path = path
        if proxy_h > 0:
            out_dir = getattr(store, "root", None) or os.path.dirname(path)
            pp, why = await asyncio.to_thread(_make_proxy, path, out_dir, proxy_h)
            if pp:
                video_path = pp
                meta["proxy_of"] = path
                meta["proxy_height"] = proxy_h
            else:
                warnings.append("代理生成失败（%s），继续用原片" % why)

        vmeta = dict(meta)
        vmeta.update({"source": path, "warnings": list(warnings),
                      "degraded": bool(warnings)})
        video = store.save_asset(Asset(id="", kind=PortType.VIDEO.value,
                                       path=video_path, meta=vmeta))
        ameta = {"source": path, "from_video": True,
                 "duration": meta.get("duration", 0),
                 "acodec": meta.get("acodec", ""),
                 "warnings": [w for w in warnings if "音频" in w]}
        audio = store.save_asset(Asset(id="", kind=PortType.AUDIO.value,
                                       path=path, meta=ameta))
        return {"video": video, "audio": audio}
