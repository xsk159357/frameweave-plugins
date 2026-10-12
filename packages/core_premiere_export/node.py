# -*- coding: utf-8 -*-
"""Premiere Pro XML/SRT 导出节点：xmeml v4（源分辨率序列、文件引用去重）+ SRT。

输入：video + segments（或 timeline）；可选 subtitles/audio。
修复原版 B8（固定 1920×1080）与 B9（同源文件重复内联）——见 pr_builder。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

# 插件包目录引导（宿主按 entrypoint 加载 node.py，不自动把包目录加入 sys.path）
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

from pr_builder import build_srt, build_xmeml, normalize_entries


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def _probe_video(ctx, path: str) -> Dict[str, Any]:
    import json as _json
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    probe_exe = r["path"] if r.get("ready") else ""
    if not probe_exe:
        return {"width": 1920, "height": 1080, "duration": 60.0}
    cmd = [probe_exe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=codec_type,width,height,duration",
           "-show_entries", "format=duration", "-of", "json", path]
    try:
        proc = await ctx["run_tracked"](cmd, timeout=60, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
        data = _json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
        v = next((s for s in (data.get("streams") or []) if s.get("codec_type") == "video"), None)
        if not v:
            return {"width": 1920, "height": 1080, "duration": 60.0}
        dur = _num(v.get("duration")) or _num((data.get("format") or {}).get("duration")) or 60.0
        return {"width": int(v.get("width") or 1920), "height": int(v.get("height") or 1080),
                "duration": dur}
    except Exception:
        return {"width": 1920, "height": 1080, "duration": 60.0}


class PremiereExportNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/premiere_export",
            title="Premiere XML/SRT 导出",
            category="输出",
            description="导出 Premiere Pro 工程（xmeml v4，源分辨率序列 + 文件去重）与配套 SRT 字幕。",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="视频", required=True),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="片段列表", required=True),
                PortSpec(name="styled_subtitle", type=PortType.SUBTITLE, label="样式字幕", required=False),
                PortSpec(name="subtitles", type=PortType.SUBTITLE, label="原始字幕", required=False),
                PortSpec(name="audio", type=PortType.AUDIO, label="配音音频", required=False),
                PortSpec(name="timeline", type=PortType.TIMELINE, label="时间线", required=False),
            ],
            outputs=[
                PortSpec(name="xml", type=PortType.STRING, label="Pr XML"),
                PortSpec(name="srt", type=PortType.STRING, label="SRT", required=False),
                PortSpec(name="report", type=PortType.JSON, label="导出报告", required=False),
            ],
            params=[
                PortSpec(name="fps", type=PortType.INT, label="帧率", default=30, widget="number"),
                PortSpec(name="sequence_width", type=PortType.INT, label="序列宽度", default=0,
                         widget="number", description="0=跟随源视频"),
                PortSpec(name="sequence_height", type=PortType.INT, label="序列高度", default=0,
                         widget="number", description="0=跟随源视频"),
                PortSpec(name="enable_subtitle", type=PortType.BOOL, label="生成 SRT 字幕", default=True,
                         widget="toggle"),
                PortSpec(name="file_base", type=PortType.STRING, label="输出文件名", default="output",
                         widget="text"),
                PortSpec(name="output_dir", type=PortType.STRING, label="输出目录", default="",
                         widget="text", description="留空输出到资产目录"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video = inputs.get("video")
        seg_asset = inputs.get("segments")
        if video is None or not getattr(video, "path", "") or not os.path.exists(video.path):
            raise RuntimeError("需要视频输入")
        if seg_asset is None:
            raise RuntimeError("需要片段列表输入")
        store = ctx["store"]
        progress = ctx.get("progress")
        if progress:
            progress(0.05)

        segs = store.read_json(seg_asset.id) or []
        if not isinstance(segs, list) or not segs:
            raise RuntimeError("片段列表为空")

        timeline_asset = inputs.get("timeline")
        tl = store.read_json(timeline_asset.id) if timeline_asset is not None else None
        voice_map: Dict[int, Dict[str, Any]] = {}
        if isinstance(tl, dict):
            if isinstance(tl.get("segments"), list) and tl["segments"]:
                segs = tl["segments"]
            for key in ("voice_items", "audio_items"):
                for v in (tl.get(key) or []):
                    if isinstance(v, dict):
                        si = int(v.get("segment_index", v.get("index", -1)))
                        p = str(v.get("audio_path") or v.get("path") or "")
                        if si >= 0 and p and os.path.exists(p):
                            voice_map[si] = {"path": p, "name": os.path.basename(p),
                                             "duration": _num(v.get("duration"))}

        fps = int(params.get("fps", 30) or 30)
        sw = int(params.get("sequence_width", 0) or 0)
        sh = int(params.get("sequence_height", 0) or 0)
        enable_sub = bool(params.get("enable_subtitle", True))
        file_base = str(params.get("file_base", "output")) or "output"
        out_dir = params.get("output_dir") or os.path.join(store.files_dir, "premiere")
        os.makedirs(out_dir, exist_ok=True)
        if progress:
            progress(0.1)

        probe = await _probe_video(ctx, video.path)
        width = sw or int(probe.get("width") or 1920)
        height = sh or int(probe.get("height") or 1080)
        vdur = _num(probe.get("duration"), 60.0)
        if progress:
            progress(0.15)

        entries = normalize_entries(segs, video.path, voice_map=voice_map or None)
        if not entries:
            raise RuntimeError("时间线条目为空")
        total_dur = max(vdur, (entries[-1]["tgt_start_us"] + entries[-1]["tgt_dur_us"]) / 1_000_000.0)

        # 字幕：styled 优先，其次原始字幕，最后按解说段自动拆句
        subtitles: List[Dict[str, Any]] = []
        if enable_sub:
            sub_asset = inputs.get("styled_subtitle") or inputs.get("subtitles")
            if sub_asset is not None:
                raw = store.read_json(sub_asset.id) or []
                if isinstance(raw, list):
                    subtitles = [dict(s) for s in raw if isinstance(s, dict) and s.get("text")]
            else:
                from pr_builder import split_subtitles_from_entries
                subtitles = split_subtitles_from_entries(entries)

        audio_asset = inputs.get("audio")
        audio_path = ""
        if audio_asset is not None and getattr(audio_asset, "path", "") and os.path.exists(audio_asset.path):
            audio_path = audio_asset.path
        elif voice_map:
            first = next(iter(voice_map.values()))
            audio_path = first.get("path", "")
        if progress:
            progress(0.2)

        built = build_xmeml(entries, video_path=video.path, fps=fps,
                            width=width, height=height, total_duration=total_dur,
                            audio_path=audio_path or None,
                            sequence_name=file_base,
                            subtitle_rows=subtitles if enable_sub else None)
        xml_path = os.path.join(out_dir, file_base + ".xmeml.xml")
        srt_path = os.path.join(out_dir, file_base + "_subtitles.srt")
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(built["xml"])
        if built["srt"]:
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(built["srt"])
        if progress:
            progress(0.8)

        report = {
            "status": "ok",
            "xml_path": xml_path,
            "srt_path": srt_path if built["srt"] else "",
            "fps": fps, "width": width, "height": height,
            "entries": len(entries),
            "duration": round(total_dur, 3),
            "subtitle_count": len(subtitles),
            "source_width": int(probe.get("width") or 0),
            "source_height": int(probe.get("height") or 0),
        }
        xml_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "prxml", "path": xml_path}),
            payload={"text": built["xml"], "path": xml_path})
        srt_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "srt", "path": srt_path}),
            payload={"text": built["srt"], "path": srt_path})
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"status": "ok"}), payload=report)
        if progress:
            progress(1.0)
        return {"xml": xml_asset, "srt": srt_asset, "report": report_asset}