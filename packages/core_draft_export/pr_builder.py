# -*- coding: utf-8 -*-
"""Premiere Pro XML（xmeml v4）与 SRT 生成器（对齐 r4 §3 实测结构）。

修复原版缺陷：
- B8：序列尺寸按源视频分辨率生成（原版固定 1920×1080）；
- B9：同源文件复用 <file id>（原版每个 clipitem 重复内联完整 <file>）；
- 帧换算：round(sec × fps)；pathurl：file://localhost/ + urllib.quote(safe='/')。
"""
from __future__ import annotations
import os
import urllib.parse
import xml.sax.saxutils as sax
from typing import Any, Dict, List, Optional


def _esc(t: Any) -> str:
    return sax.escape(str(t))


def sec_to_frames(sec: float, fps: float) -> int:
    return int(round(float(sec) * float(fps)))


def path_url(path: str) -> str:
    p = path.replace("\\", "/")
    return "file://localhost/" + urllib.parse.quote(p, safe="/")


def format_srt_time(t: float) -> str:
    t = max(float(t), 0.0)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(rows: List[Dict[str, Any]]) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        st = float(r.get("start", 0.0))
        en = float(r.get("end", st + 0.5))
        out.append(str(i))
        out.append(f"{format_srt_time(st)} --> {format_srt_time(en)}")
        out.append(str(r.get("text", "")).strip())
        out.append("")
    return "\n".join(out)


def _sample_characteristics(width: int, height: int, fps: float) -> str:
    return (
        "<rate><timebase>%g</timebase><ntsc>FALSE</ntsc></rate>" % fps +
        f"<width>{int(width)}</width><height>{int(height)}</height>"
        "<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>"
        "<fielddominance>none</fielddominance>"
    )


def normalize_entries(segments: List[Dict[str, Any]], video_path: str,
                      voice_map: Optional[Dict[int, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """把片段输入归一化为 xmeml entries（秒→微秒，配音目标时长优先取 tts）。"""
    US = 1_000_000
    entries: List[Dict[str, Any]] = []
    tgt_cursor = 0.0
    for raw in segments:
        seg = dict(raw)
        idx = int(seg.get("segment_index", seg.get("index", len(entries))))
        is_ost = bool(seg.get("OST") or seg.get("ost") or seg.get("is_ost"))
        src_start = float(seg.get("src_start", seg.get("start", 0.0)))
        src_end = float(seg.get("src_end", seg.get("end", src_start + 1.0)))
        src_dur = float(seg.get("clip_duration", src_end - src_start))
        if src_dur <= 0:
            src_dur = max(src_end - src_start, 1.0)
        tts_dur = float(seg.get("tts_duration", 0.0) or 0.0)
        if is_ost:
            target_dur = src_dur
        elif voice_map and idx in voice_map:
            target_dur = float(voice_map[idx].get("duration", 0.0) or 0.0)
        elif tts_dur > 0:
            target_dur = tts_dur
        elif seg.get("duration") is not None:
            target_dur = float(seg.get("duration"))
        else:
            target_dur = max(src_dur, 1.0)
        if target_dur <= 0:
            target_dur = max(src_dur, 1.0)
        entries.append({
            "segment_index": idx,
            "text": str(seg.get("text") or seg.get("roast_text") or ""),
            "is_ost": is_ost,
            "src_start_us": int(src_start * US),
            "src_dur_us": int(src_dur * US),
            "tgt_start_us": int(tgt_cursor * US),
            "tgt_dur_us": int(target_dur * US),
            "video_path": str(seg.get("video") or seg.get("video_path") or video_path),
        })
        tgt_cursor += target_dur
    return entries


def split_subtitles_from_entries(entries: List[Dict[str, Any]],
                                 min_dur: float = 0.6, max_dur: float = 4.0) -> List[Dict[str, Any]]:
    """按解说段拆句生成绝对时间字幕（与剪映草稿同一中文标点拆句规则）。"""
    out: List[Dict[str, Any]] = []
    for e in entries:
        text = (e.get("text") or "").strip()
        if not text or e.get("is_ost"):
            continue
        tgt_sec = e.get("tgt_dur_us", 0) / 1_000_000.0
        tgt_start = e.get("tgt_start_us", 0) / 1_000_000.0
        if tgt_sec <= 0:
            continue
        parts: List[str] = []
        buf = ""
        for ch in text:
            buf += ch
            if ch in "。！？；":
                parts.append(buf.strip())
                buf = ""
        if buf.strip():
            parts.append(buf.strip())
        refined: List[str] = []
        for p in parts:
            if len(p) <= 12:
                refined.append(p)
                continue
            sub = ""
            for ch in p:
                sub += ch
                if ch in "，、：" and len(sub) >= 4:
                    refined.append(sub.strip())
                    sub = ""
            if sub.strip():
                refined.append(sub.strip())
        if not refined:
            refined = [text]
        chars = [len(r) for r in refined]
        total_chars = max(sum(chars), 1)
        acc = 0.0
        for i, (part, c) in enumerate(zip(refined, chars)):
            d = tgt_sec * c / total_chars
            if i == len(refined) - 1:
                d = max(tgt_sec - acc, 0.05)
            d = max(min(d, max_dur), 0.2)
            out.append({"start": round(tgt_start + acc, 3),
                        "end": round(tgt_start + min(acc + d, tgt_sec), 3),
                        "text": part})
            acc += d
    return out


def build_xmeml(entries: List[Dict[str, Any]], *, video_path: str, fps: float,
                width: int, height: int, total_duration: float,
                audio_path: Optional[str] = None,
                sequence_name: Optional[str] = None,
                subtitle_rows: Optional[List[Dict[str, Any]]] = None,
                srt_output: Optional[str] = None) -> Dict[str, str]:
    """构造 xmeml v4 XML + 可选 SRT。返回 {'xml': str, 'srt': str}。

    entries 字段：{video_path, src_start_us, src_dur_us, tgt_start_us, tgt_dur_us}。
    同源文件复用 file id（B9 修复）；序列尺寸用源分辨率（B8 修复）。
    """
    fps = float(fps) or 25.0
    total_frames = max(sec_to_frames(total_duration, fps), 1)
    name = sequence_name or os.path.basename(video_path)
    vid_path = video_path.replace("\\", "/")
    vname = os.path.basename(vid_path)

    # 文件引用去重（B9）
    file_ids: Dict[str, str] = {}
    next_fid = [1]

    def _file_id(p: str) -> str:
        pn = p.replace("\\", "/")
        if pn not in file_ids:
            file_ids[pn] = f"file-{next_fid[0]}"
            next_fid[0] += 1
        return file_ids[pn]

    clip_parts: List[str] = []
    for i, e in enumerate(entries):
        vp = (e.get("video_path") or video_path).replace("\\", "/")
        src_start = float(e.get("src_start_us", 0)) / 1_000_000.0
        src_dur = float(e.get("src_dur_us", 0)) / 1_000_000.0
        tgt_start = float(e.get("tgt_start_us", 0)) / 1_000_000.0
        tgt_dur = float(e.get("tgt_dur_us", src_dur)) / 1_000_000.0
        if tgt_dur <= 0:
            continue
        src_frames = sec_to_frames(src_dur, fps)
        start_f = sec_to_frames(tgt_start, fps)
        end_f = start_f + sec_to_frames(tgt_dur, fps)
        in_f = sec_to_frames(src_start, fps)
        out_f = in_f + src_frames
        fid = _file_id(vp)
        clip_parts.append(
            f'<clipitem id="clipitem-{i + 1}">'
            f"<name>{_esc(os.path.basename(vp))}</name><enabled>TRUE</enabled>"
            f"<duration>{src_frames}</duration>"
            f"<rate><timebase>{fps:g}</timebase><ntsc>FALSE</ntsc></rate>"
            f"<start>{start_f}</start><end>{end_f}</end>"
            f"<in>{in_f}</in><out>{out_f}</out>"
            f'<file id="{fid}"><name>{_esc(os.path.basename(vp))}</name>'
            f"<pathurl>{path_url(vp)}</pathurl>"
            f"<rate><timebase>{fps:g}</timebase><ntsc>FALSE</ntsc></rate>"
            f"<duration>{src_frames}</duration><media><video><samplecharacteristics>"
            f"{_sample_characteristics(width, height, fps)}"
            f"</samplecharacteristics></video>"
            f"<audio><samplecharacteristics><depth>16</depth><samplerate>44100</samplerate>"
            f"</samplecharacteristics><channelcount>2</channelcount></audio>"
            f"</media></file>"
            f'<sourcetrack><mediatype>video</mediatype><trackindex>1</trackindex></sourcetrack>'
            f"</clipitem>"
        )

    # 音频轨（可选配音）
    audio_parts: List[str] = []
    if audio_path and os.path.exists(audio_path):
        ap = audio_path.replace("\\", "/")
        adur = max(sec_to_frames(total_duration, fps), 1)
        audio_parts.append(
            f'<clipitem id="audio-1"><name>{_esc(os.path.basename(ap))}</name><enabled>TRUE</enabled>'
            f"<duration>{adur}</duration>"
            f"<rate><timebase>{fps:g}</timebase><ntsc>FALSE</ntsc></rate>"
            f"<start>0</start><end>{adur}</end><in>0</in><out>{adur}</out>"
            f'<file id="audio-file-1"><name>{_esc(os.path.basename(ap))}</name>'
            f"<pathurl>{path_url(ap)}</pathurl>"
            f"<rate><timebase>{fps:g}</timebase><ntsc>FALSE</ntsc></rate>"
            f"<duration>{adur}</duration><media><audio><samplecharacteristics>"
            f"<depth>16</depth><samplerate>44100</samplerate></samplecharacteristics>"
            f"<channelcount>2</channelcount></audio></media></file>"
            f'<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>'
            f"</clipitem>"
        )

    # 字幕 SRT（独立文件，不在 XML 中烧录）
    srt = ""
    if subtitle_rows is not None:
        srt = build_srt(subtitle_rows)

    doc = [
        "<?xml version='1.0' encoding='utf-8'?>",
        '<xmeml version="4">',
        '<sequence id="sequence-1">',
        f"<name>{_esc(name)}</name>",
        f"<duration>{total_frames}</duration>",
        f"<rate><timebase>{fps:g}</timebase><ntsc>FALSE</ntsc></rate>",
        "<timecode>"
        f"<rate><timebase>{fps:g}</timebase><ntsc>FALSE</ntsc></rate>"
        "<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat>"
        "</timecode>",
        "<media><video><format><samplecharacteristics>",
        _sample_characteristics(width, height, fps),
        "</samplecharacteristics></format>",
        "<track><enabled>TRUE</enabled><locked>FALSE</locked>",
        "".join(clip_parts),
        "</track></video>",
    ]
    if audio_parts:
        doc.append("<audio><track><enabled>TRUE</enabled><locked>FALSE</locked>")
        doc.append("".join(audio_parts))
        doc.append("</track></audio>")
    doc += ["</media>", "</sequence>", "</xmeml>", ""]

    return {"xml": "\n".join(doc), "srt": srt, "srt_path": srt_output or ""}
