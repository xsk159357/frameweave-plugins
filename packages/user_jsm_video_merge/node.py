# -*- coding: utf-8 -*-
"""user/jsm_video_merge —— 视频/音频合并拼接（解说猫 video_merge_page 对应能力）。

三种模式（mode）：
- concat：多个视频首尾拼接成一段（多视频拼接；自动统一分辨率/帧率后 concat）。
- mix：主视频 + 外部音频 → 输出带新混音音轨的视频（原音轨替换为外部音频，见说明）。
- mux：主视频 + 外部音频 → 流拷贝封装（不重编码，快且无损；要求容器兼容）。

videos 输入契约（JSON）：
- 数组：["<path>", ...] 或 [{"path": "<path>", "name": "..."}, ...]；
- 也接受 {items:[...]} / {"videos":[...]} 信封（逐层解包）。
- 资产引用：字符串为 asset_id 时经 store.get() 解析；其余视为本地路径。
- concat 模式至少需要 2 个视频；mix/mux 需要 1 个视频 + audio 输入。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


def _ffmpeg() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    return r["path"] if r.get("ready") else "ffmpeg"


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _resolve_videos(store: Any, raw: Any) -> List[Dict[str, Any]]:
    """videos JSON 解包为 [{path, name}] 列表（顺序保留，去重）。"""
    items: List[Any] = []
    if isinstance(raw, dict):
        for key in ("items", "videos", "list", "rows"):
            v = raw.get(key)
            if isinstance(v, list):
                items = v
                break
    elif isinstance(raw, list):
        items = raw
    if not items:
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for it in items:
        if isinstance(it, str):
            p = it.strip()
        elif isinstance(it, dict):
            p = str(it.get("path") or it.get("video_id") or it.get("file") or "").strip()
        else:
            continue
        if not p:
            continue
        ap = os.path.abspath(p)
        if ap in seen:
            continue
        if not os.path.isfile(ap):
            # 尝试按资产 id 解析
            try:
                asset = store.get(p)
            except Exception:  # noqa: BLE001
                asset = None
            if asset is not None and getattr(asset, "path", "") and os.path.isfile(asset.path):
                ap = os.path.abspath(asset.path)
            else:
                continue
        seen.add(ap)
        name = ""
        if isinstance(it, dict):
            name = str(it.get("name") or "")
        out.append({"path": ap, "name": name or os.path.basename(ap)})
    return out


class JsmVideoMergeNode(NodeBase):
    """视频/音频合并拼接。type_id: user/jsm_video_merge"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_video_merge",
            title="视频合并/拼接",
            category="剪辑",
            description="多视频拼接（concat）或视频+音频合并（mix/mux）；videos 传 JSON 数组/信封",
            inputs=[
                PortSpec(name="videos", type=PortType.JSON, label="视频列表(JSON)", required=True,
                         description="数组：路径字符串或 {path,name}；或 {items:[...]} 信封；concat 需≥2 条"),
                PortSpec(name="audio", type=PortType.AUDIO, label="外部音频(可选)", required=False,
                         description="mix/mux 模式的合并音轨（concat 忽略）"),
            ],
            outputs=[
                PortSpec(name="video_out", type=PortType.VIDEO, label="合成视频"),
                PortSpec(name="stats", type=PortType.JSON, label="合并报告"),
            ],
            params=[
                PortSpec(name="mode", type=PortType.STRING, label="合并模式", default="concat",
                         widget="select", options=["concat", "mix", "mux"],
                         description="concat=多视频拼接；mix=视频+外部音频重编码混音；mux=流拷贝封装"),
                PortSpec(name="width", type=PortType.INT, label="统一宽度(0=源)", default=0,
                         widget="number", description="concat 统一输出宽度；0=第一个视频宽度"),
                PortSpec(name="fps", type=PortType.INT, label="统一帧率(0=源)", default=0,
                         widget="number", description="concat 统一输出帧率；0=第一个视频帧率"),
                PortSpec(name="output_name", type=PortType.STRING, label="输出文件名", default="merged",
                         widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        raw_videos = store.read_json(inputs["videos"].id) if inputs.get("videos") is not None else None
        videos = _resolve_videos(store, raw_videos)
        if not videos:
            raise RuntimeError("videos 输入为空或无法解析（需要路径/资产 id 数组或 {items:[...]} 信封）")
        audio = inputs.get("audio")
        mode = str(params.get("mode", "concat") or "concat").strip().lower()
        if mode not in ("concat", "mix", "mux"):
            raise ValueError(f"mode 必须是 concat|mix|mux，当前: {mode!r}")
        if mode == "concat" and len(videos) < 2:
            raise RuntimeError("concat 模式至少需要 2 个视频输入")
        if mode in ("mix", "mux") and audio is None:
            raise RuntimeError(f"{mode} 模式需要连接 audio 输入（外部音轨）")
        if mode in ("mix", "mux") and len(videos) > 1:
            raise RuntimeError(f"{mode} 模式仅支持 1 个视频（多视频请用 concat）")

        workdir = os.path.join(ctx.get("workdir", "."), "jsm_video_merge")
        os.makedirs(workdir, exist_ok=True)
        ff = _ffmpeg()
        out_name = str(params.get("output_name", "merged") or "merged")
        out_path = os.path.join(workdir, f"{out_name}_{uuid.uuid4().hex[:8]}.mp4")
        started = time.time()

        if mode == "concat":
            width = int(params.get("width", 0) or 0)
            fps = int(params.get("fps", 0) or 0)
            # 统一 scale（目标内裁切）→ pad 到统一画布 → setsar → fps，再 concat
            w = width or 1920
            h = int(w * 9 / 16)
            vfilt_parts = [f"scale={w}:{h}:force_original_aspect_ratio=decrease",
                           f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
            if fps:
                vfilt_parts.append(f"fps={fps}")
            vfilt = ",".join(vfilt_parts)
            vexpr = "".join(f"[{i}:v]{vfilt}[v{i}];" for i in range(len(videos)))
            vexpr += "".join(f"[v{i}]" for i in range(len(videos))) + f"concat=n={len(videos)}:v=1:a=0[vout]"
            cmd = [ff, "-y"]
            for v in videos:
                cmd += ["-i", v["path"]]
            cmd += ["-filter_complex", vexpr, "-map", "[vout]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-r", str(fps or 30), out_path]
        elif mode == "mux":
            cmd = [ff, "-y", "-i", videos[0]["path"], "-i", audio.path,
                   "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
        else:  # mix
            cmd = [ff, "-y", "-i", videos[0]["path"], "-i", audio.path,
                   "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1",
                   "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]

        progress(0.2)
        proc = await ctx["run_tracked"](cmd, timeout=1800,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            err = str(proc.stderr or "")[-800:]
            raise RuntimeError(f"{mode} 合并失败: {err}")
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("合并输出为空")
        progress(0.9)

        asset = store.save_asset(
            Asset(id="", kind=PortType.VIDEO.value, path=out_path,
                  meta={"mode": mode, "sources": len(videos), "audio_mux": audio is not None}))
        stats = {
            "mode": mode, "sources": [v["path"] for v in videos],
            "audio": getattr(audio, "path", "") if audio is not None else "",
            "output": out_path, "size": int(os.path.getsize(out_path)),
            "rt_s": round(time.time() - started, 1),
        }
        progress(1.0)
        return {"video_out": asset, "stats": stats}
