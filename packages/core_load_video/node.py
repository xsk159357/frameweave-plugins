"""官方插件：导入视频节点。"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from typing import Any, Dict
from app.assets import Asset, file_fingerprint
from app.nodespec import PortType


def _parse_fps(rate: str) -> float:
    try:
        if "/" in rate:
            n, d = rate.split("/")
            return round(float(n) / float(d), 3)
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


def ffprobe_meta(video_path: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"path": video_path, "exists": os.path.exists(video_path)}
    if not meta["exists"]:
        return meta
    meta["size"] = os.path.getsize(video_path)
    meta["fingerprint"] = file_fingerprint(video_path)
    for probe in ("ffprobe", "ffprobe.exe"):
        try:
            out = subprocess.run([probe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", video_path], capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout:
                data = json.loads(out.stdout)
                fmt = data.get("format", {})
                meta["duration"] = float(fmt.get("duration", 0) or 0)
                meta["bitrate"] = fmt.get("bit_rate", "")
                meta["format_name"] = fmt.get("format_name", "")
                for stream in data.get("streams", []):
                    if stream.get("codec_type") == "video":
                        meta["width"] = int(stream.get("width", 0) or 0)
                        meta["height"] = int(stream.get("height", 0) or 0)
                        meta["fps"] = _parse_fps(stream.get("avg_frame_rate", ""))
                        meta["video_codec"] = stream.get("codec_name", "")
                    if stream.get("codec_type") == "audio":
                        meta["audio_codec"] = stream.get("codec_name", "")
            break
        except (OSError, subprocess.TimeoutExpired):
            continue
    return meta


class LoadVideoNode:
    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        path = params.get("path", "")
        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"视频文件不存在: {path}")
        meta = ffprobe_meta(path)
        store = ctx["store"]
        dest_dir = os.path.join(store.files_dir, "videos")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(path))
        if os.path.abspath(dest) != os.path.abspath(path):
            shutil.copy2(path, dest)
        asset = Asset(id="", kind=PortType.VIDEO.value, path=dest, meta=meta, fingerprint=meta.get("fingerprint", ""), node_id="")
        store.save_asset(asset)
        return {"video": asset, "meta": meta}
