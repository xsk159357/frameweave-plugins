# -*- coding: utf-8 -*-
"""剪映草稿导出节点（v1.1 重写）：完整还原 r4 §2 剪映草稿结构与 §3 Premiere XML。

产物：
- 剪映草稿目录（draft_content.json 27 键 + draft_meta_info.json + 6 占位文件）；
- Premiere XML（xmeml v4，源分辨率序列 + 文件引用去重）；
- EDL（CMX 3600）与 FCPXML 1.9；
- SRT（与草稿共用中文标点拆句规则）。

输入契约：
- segments:SEGMENTS  —— split_cam 风格片段 [{index,segment_index,text,start,end,
  duration,tts_duration,speed,src_start,src_end,OST,video,...}]
- timeline:TIMELINE(可选) —— {segments:[...], voice_items:[{segment_index,path,duration,start,end}]}
- audio:AUDIO(可选) —— 合并解说音频（未给 voice_items 时整段一轨）
- styled_subtitle:SUBTITLE(可选) —— [{start,end,text,style}]
- bgm:AUDIO(可选) —— 循环铺满的 BGM 轨
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

# 插件包目录引导（宿主按 entrypoint 加载 node.py，不自动把包目录加入 sys.path）
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

from jy_builder import (US, DraftModel, build_voice_items, normalize_timeline_entries,
                        split_text_to_subtitles, write_draft_dir)
from pr_builder import build_xmeml

_RESOLUTIONS = {
    "1080p": {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)},
    "720p": {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720)},
    "4k": {"16:9": (3840, 2160), "9:16": (2160, 3840), "1:1": (2160, 2160)},
}


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def _probe_video(ctx, path: str) -> Dict[str, Any]:
    """ffprobe 宽高/时长/帧率（缺失时返回默认）。"""
    import json as _json
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    probe_exe = r["path"] if r.get("ready") else ""
    if not probe_exe:
        return {"width": 1920, "height": 1080, "duration": 60.0, "fps": 30.0}
    cmd = [probe_exe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=codec_type,width,height,r_frame_rate,duration",
           "-show_entries", "format=duration", "-of", "json", path]
    try:
        proc = await ctx["run_tracked"](cmd, timeout=60, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
        data = _json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
        v = next((s for s in (data.get("streams") or []) if s.get("codec_type") == "video"), None)
        if not v:
            return {"width": 1920, "height": 1080, "duration": 60.0, "fps": 30.0}
        try:
            num_, den_ = str(v.get("r_frame_rate", "30/1")).split("/")
            fps = round(float(num_) / max(float(den_), 1.0), 3)
        except (ValueError, ZeroDivisionError):
            fps = 30.0
        dur = _num(v.get("duration")) or _num((data.get("format") or {}).get("duration")) or 60.0
        return {"width": int(v.get("width") or 1920), "height": int(v.get("height") or 1080),
                "duration": dur, "fps": fps}
    except Exception:
        return {"width": 1920, "height": 1080, "duration": 60.0, "fps": 30.0}


def _build_edl(entries: List[Dict[str, Any]], video_path: str, fps: float) -> str:
    lines = ["TITLE: FrameWeave Export", "FCM: NON-DROP FRAME", ""]

    def tc(frames: float) -> str:
        f = int(frames)
        h, rem = divmod(f, int(3600 * fps))
        m, rem = divmod(rem, int(60 * fps))
        s, fr = divmod(rem, int(fps))
        return f"{h:02d}:{m:02d}:{s:02d}:{fr:02d}"

    for i, e in enumerate(entries, 1):
        st = round(_num(e.get("tgt_start_us")) / US * fps)
        du = round(_num(e.get("tgt_dur_us")) / US * fps)
        src_st = round(_num(e.get("src_start_us")) / US * fps)
        src_du = round(_num(e.get("src_dur_us")) / US * fps)
        lines.append(f"{i:03d}  AX       V     C        {tc(st)} {tc(st + du)} {tc(src_st)} {tc(src_st + src_du)}")
        lines.append(f"* FROM CLIP NAME: {os.path.basename(str(e.get('video_path') or video_path))}")
        lines.append("")
    return "\n".join(lines)


def _build_fcpxml(entries: List[Dict[str, Any]], video_path: str, width: int, height: int,
                  total_duration: float, fps: float = 30.0) -> str:
    import xml.sax.saxutils as sax

    def esc(t):
        return sax.escape(str(t))

    def fr(sec):
        return "%d/%ds" % (round(float(sec) * fps), fps)

    fmt_name = "FFVideoFormat%d" % (height if width >= height else width) + "p%d" % round(fps)
    total_frames = max(round(total_duration * fps), 1)
    asset_id = "r1"
    parts = []
    parts.append(f'    <format id="fmt1" name="{fmt_name}" frameDuration="1/{fps:.0f}s" width="{width}" height="{height}"/>')
    parts.append(f'    <asset id="{asset_id}" name="{esc(os.path.basename(video_path))}" uid="{asset_id}" '
                 f'start="0s" duration="{fr(total_duration)}" hasVideo="1" hasAudio="1" format="fmt1"/>')
    spine = []
    offset = 0
    for i, e in enumerate(entries):
        du = _num(e.get("tgt_dur_us")) / US
        if du <= 0:
            continue
        src_st = _num(e.get("src_start_us")) / US
        spine.append(
            f'      <asset-clip name="{esc(os.path.basename(str(e.get("video_path") or video_path)))}" '
            f'ref="{asset_id}" lane="1" offset="{fr(offset)}" start="{fr(src_st)}" '
            f'duration="{fr(du)}" tcFormat="NDF"/>')
        offset += du
    doc = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!DOCTYPE fcpxml>", "", '<fcpxml version="1.9">', "  <resources>",
    ]
    doc.extend(parts)
    doc += ["  </resources>", "  <library>", '    <event name="FrameWeave">',
            f'      <project name="{esc("FrameWeave项目")}">',
            f'        <sequence format="fmt1" duration="{fr(max(offset, total_duration))}" tcStart="0s" tcFormat="NDF">',
            "    <spine>"]
    doc.extend(spine if spine else [f'      <gap name="空" duration="{fr(total_duration)}"/>'])
    doc += ["    </spine>", "        </sequence>", "      </project>", "    </event>",
            "  </library>", "</fcpxml>", ""]
    return "\n".join(doc)


class DraftExportNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/draft_export",
            title="剪映草稿导出",
            category="输出",
            description="生成剪映草稿目录（完整 27 键结构）+ Premiere XML/EDL/FCPXML + SRT；字幕样式、画幅、导出目标可选。",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="视频", required=True),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="片段列表", required=True),
                PortSpec(name="audio", type=PortType.AUDIO, label="配音音频", required=False),
                PortSpec(name="styled_subtitle", type=PortType.SUBTITLE, label="样式字幕", required=False),
                PortSpec(name="timeline", type=PortType.TIMELINE, label="时间线", required=False),
                PortSpec(name="bgm", type=PortType.AUDIO, label="背景音乐", required=False),
            ],
            outputs=[
                PortSpec(name="draft", type=PortType.JSON, label="剪映草稿信息"),
                PortSpec(name="xml", type=PortType.STRING, label="Pr XML", required=False),
                PortSpec(name="edl", type=PortType.STRING, label="EDL", required=False),
                PortSpec(name="fcpxml", type=PortType.STRING, label="FCPXML", required=False),
                PortSpec(name="report", type=PortType.JSON, label="导出报告", required=False),
            ],
            params=[
                PortSpec(name="format", type=PortType.STRING, label="导出格式", default="jianying",
                         widget="select", options=["jianying", "prxml", "edl", "fcpxml", "all"]),
                PortSpec(name="aspect", type=PortType.STRING, label="画幅", default="16:9",
                         widget="select", options=["16:9", "9:16", "1:1"]),
                PortSpec(name="resolution", type=PortType.STRING, label="分辨率", default="1080p",
                         widget="select", options=["1080p", "720p", "4k"]),
                PortSpec(name="canvas_ratio", type=PortType.STRING, label="画布比例", default="original",
                         widget="select", options=["original", "16:9", "9:16", "1:1"],
                         description="original=跟随源视频尺寸（r4 默认）"),
                PortSpec(name="draft_name", type=PortType.STRING, label="草稿名",
                         default="FrameWeave_导出", widget="text"),
                PortSpec(name="export_target", type=PortType.STRING, label="导出目标", default="jianying",
                         widget="select", options=["jianying", "capcut"]),
                PortSpec(name="subtitle_style", type=PortType.STRING, label="字幕样式", default="经典-白字黑边",
                         widget="select", options=["经典-白字黑边", "黑底白字", "黄字红边", "默认"]),
                PortSpec(name="font_size", type=PortType.FLOAT, label="字号", default=0.0,
                         widget="number", description="0=横版6/竖版9"),
                PortSpec(name="enable_subtitle", type=PortType.BOOL, label="生成字幕", default=True,
                         widget="toggle"),
                PortSpec(name="fps", type=PortType.INT, label="工程帧率", default=30, widget="number"),
                PortSpec(name="output_dir", type=PortType.STRING, label="输出目录",
                         default="", widget="text", description="留空输出到资产目录"),
            ],
            version="1.1.0",
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

        audio = inputs.get("audio")
        sub_asset = inputs.get("styled_subtitle")
        timeline_asset = inputs.get("timeline")
        bgm = inputs.get("bgm")

        fmt = str(params.get("format", "jianying") or "jianying")
        draft_name = str(params.get("draft_name", "FrameWeave_导出")) or "FrameWeave_导出"
        export_target = str(params.get("export_target", "jianying") or "jianying")
        style_name = str(params.get("subtitle_style", "经典-白字黑边") or "经典-白字黑边")
        font_size = _num(params.get("font_size", 0.0))
        enable_sub = bool(params.get("enable_subtitle", True))
        fps = int(params.get("fps", 30) or 30)
        canvas_ratio = str(params.get("canvas_ratio", "original") or "original")
        aspect = str(params.get("aspect", "16:9") or "16:9")
        res = str(params.get("resolution", "1080p") or "1080p")

        probe = await _probe_video(ctx, video.path)
        sw, sh = int(probe.get("width") or 1920), int(probe.get("height") or 1080)
        vdur = _num(probe.get("duration"), 60.0)
        if progress:
            progress(0.12)

        # ---- 画布 ----
        if canvas_ratio == "original":
            canvas = {"width": sw, "height": sh, "ratio": "original"}
        elif canvas_ratio == "1:1":
            canvas = {"width": 1080, "height": 1080, "ratio": "1:1"}
        else:
            try:
                w, h = _RESOLUTIONS[res][aspect]
            except KeyError:
                w, h = (1920, 1080)
            canvas = {"width": w, "height": h, "ratio": aspect}

        # ---- 时间线 & 配音 ----
        timeline_data = store.read_json(timeline_asset.id) if timeline_asset is not None else None
        tl_segments = segs
        voice_map: Dict[int, Dict[str, Any]] = {}
        if isinstance(timeline_data, dict):
            if isinstance(timeline_data.get("segments"), list) and timeline_data["segments"]:
                # 允许时间线携带自己的条目（含视频源与区间）
                tl_segments = timeline_data["segments"]
            for key in ("voice_items", "audio_items", "tts_items"):
                vis = timeline_data.get(key)
                if isinstance(vis, list):
                    for v in vis:
                        if isinstance(v, dict):
                            si = int(v.get("segment_index", v.get("index", -1)))
                            p = str(v.get("audio_path") or v.get("path") or "")
                            if si >= 0 and p and os.path.exists(p):
                                voice_map[si] = {
                                    "path": p, "name": os.path.basename(p),
                                    "duration": _num(v.get("duration"), 0.0),
                                }

        merged_audio_path = ""
        merged_audio_dur = 0.0
        if audio is not None and getattr(audio, "path", "") and os.path.exists(audio.path):
            merged_audio_path = audio.path
            merged_audio_dur = _num(audio.meta.get("duration"), 0.0)

        # ---- 归一化时间线条目 ----
        entries = normalize_timeline_entries(
            tl_segments, video.path,
            voice_map=voice_map if voice_map else ({} if merged_audio_path else None),
            total_dur=vdur if not (voice_map or merged_audio_path) else None)
        if not entries:
            raise RuntimeError("时间线条目为空（无法生成草稿）")

        model = DraftModel(
            video_path=video.path, video_name=os.path.basename(video.path),
            video_width=sw, video_height=sh, video_duration=vdur,
            canvas=canvas, fps=_num(probe.get("fps"), 30.0),
            export_target=export_target, draft_name=draft_name,
            draft_dir=os.path.join(os.path.dirname(video.path), "Drafts", draft_name),
            subtitle_style_name=style_name,
            font_size=(font_size if font_size > 0 else None),
            enable_subtitle=enable_sub,
        )
        model.entries = entries

        # ---- 配音项 ----
        if voice_map:
            model.voice_items = build_voice_items(voice_map, entries)
        elif merged_audio_path:
            # 合并解说一轨：目标起点 0，时长取探测值或最后解说段结束
            total_tgt = max((e["tgt_start_us"] + e["tgt_dur_us"]) / US for e in entries if not e["is_ost"]) \
                if any(not e["is_ost"] for e in entries) else vdur
            if merged_audio_dur <= 0:
                merged_audio_dur = total_tgt
            model.voice_items = [{
                "audio_path": merged_audio_path, "name": os.path.basename(merged_audio_path),
                "duration_us": int(merged_audio_dur * US), "tgt_start_us": 0,
            }]

        # BGM
        if bgm is not None and getattr(bgm, "path", "") and os.path.exists(bgm.path):
            total_us = int(vdur * US)
            model.bgm_items = [{
                "path": bgm.path, "name": os.path.basename(bgm.path),
                "duration_us": total_us, "volume": 0.38,
            }]

        # ---- 字幕 ----
        subtitles: List[Dict[str, Any]] = []
        if enable_sub:
            if sub_asset is not None:
                raw_subs = store.read_json(sub_asset.id) or []
                if isinstance(raw_subs, list):
                    subtitles = [dict(s) for s in raw_subs if isinstance(s, dict) and s.get("text")]
            else:
                # 从解说段按拆句规则生成（与 SRT 共用规则）
                for e in entries:
                    if not e["text"] or e["is_ost"]:
                        continue
                    tgt_sec = e["tgt_dur_us"] / US
                    tgt_start_sec = e["tgt_start_us"] / US
                    for row in split_text_to_subtitles(e["text"], tgt_sec):
                        subtitles.append({
                            "start": round(tgt_start_sec + row["start"], 3),
                            "end": round(tgt_start_sec + row["end"], 3),
                            "text": row["text"],
                        })
        model.subtitles = subtitles

        # ---- 落盘 ----
        if progress:
            progress(0.2)
        out_dir = params.get("output_dir") or os.path.join(store.files_dir, "drafts")
        os.makedirs(out_dir, exist_ok=True)
        results: Dict[str, Any] = {}
        warnings: List[str] = []

        if fmt in ("jianying", "all"):
            model.draft_dir = os.path.join(out_dir, draft_name)
            write_draft_dir(model)
            results["draft_dir"] = model.draft_dir
            results["draft_content"] = os.path.join(model.draft_dir, "draft_content.json")
            results["draft_meta_info"] = os.path.join(model.draft_dir, "draft_meta_info.json")
        if progress:
            progress(0.5)

        # ---- Pr XML + SRT ----
        xml_str = srt_str = ""
        xml_path = srt_path = ""
        if fmt in ("prxml", "all"):
            names = [os.path.basename(e.get("video_path") or video.path) for e in entries]
            xml_out = os.path.join(out_dir, draft_name + ".xmeml.xml")
            srt_out = os.path.join(out_dir, draft_name + "_subtitles.srt")
            built = build_xmeml(
                entries, video_path=video.path, fps=fps,
                width=canvas["width"], height=canvas["height"],
                total_duration=max(vdur, (entries[-1]["tgt_start_us"] + entries[-1]["tgt_dur_us"]) / US),
                audio_path=(merged_audio_path or (voice_map and list(voice_map.values())[0].get("path"))),
                sequence_name=draft_name,
                subtitle_rows=subtitles if enable_sub else None,
                srt_output=srt_out)
            xml_str, srt_str = built["xml"], built["srt"]
            xml_path, srt_path = xml_out, srt_out
            with open(xml_out, "w", encoding="utf-8") as f:
                f.write(xml_str)
            if srt_str:
                with open(srt_out, "w", encoding="utf-8") as f:
                    f.write(srt_str)
            results["xml_path"] = xml_path
            results["srt_path"] = srt_path
        if progress:
            progress(0.7)

        # ---- EDL / FCPXML ----
        edl_str = fcpxml_str = ""
        if fmt in ("edl", "all"):
            edl_str = _build_edl(entries, video.path, fps)
            ep = os.path.join(out_dir, draft_name + ".edl")
            with open(ep, "w", encoding="utf-8") as f:
                f.write(edl_str)
            results["edl_path"] = ep
        if fmt in ("fcpxml", "all"):
            fcpxml_str = _build_fcpxml(entries, video.path, int(canvas["width"]),
                                       int(canvas["height"]), vdur, fps)
            fp = os.path.join(out_dir, draft_name + ".fcpxml")
            with open(fp, "w", encoding="utf-8") as f:
                f.write(fcpxml_str)
            results["fcpxml_path"] = fp
        if progress:
            progress(0.9)

        # ---- 输出资产 ----
        report = {
            "status": "ok",
            "draft_name": draft_name,
            "export_target": export_target,
            "canvas": canvas,
            "entries": len(entries),
            "voice_items": len(model.voice_items),
            "subtitles": len(subtitles),
            "src_width": sw, "src_height": sh, "duration": round(vdur, 3),
            "results": results,
            "warnings": warnings,
        }
        draft_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"draft_name": draft_name, **report}),
            payload={"draft_name": draft_name, "draft_path": model.draft_dir,
                     "entries": len(entries), "voice_items": len(model.voice_items),
                     "subtitles": len(subtitles), "canvas": canvas,
                     "results": {k: v for k, v in results.items()}})
        xml_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "prxml"}), payload={"text": xml_str})
        edl_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "edl"}), payload={"text": edl_str})
        fcpxml_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "fcpxml"}),
            payload={"text": fcpxml_str})
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"status": "ok"}), payload=report)
        if progress:
            progress(1.0)
        return {"draft": draft_asset, "xml": xml_asset, "edl": edl_asset,
                "fcpxml": fcpxml_asset, "report": report_asset}