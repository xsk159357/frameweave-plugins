# -*- coding: utf-8 -*-
"""MP4 渲染节点 v1.1（拼接/转场/字幕烧录/多轨/混音/进度/成品验证）。

相比原版（及上一版简化实现）的增强：
- 转场：xfade 链（fade/fadeblack/dissolve/slide/wipe/zoom/smooth/circle 等），
  转场时长参数化，不再固定 0.3s；
- 字幕烧录：ASS 生成按输出画布缩放字号、支持描边/加粗/字体，合并进同一
  filter_complex（避免 -vf 与 -filter_complex 冲突）；
- 多轨：可选覆盖视频轨（overlay，透明度参数化）；音频三模式混音
  （narration_only / original_only / mix = 解说+原声+背景乐）；
- 进度：ffmpeg -progress 轮询，节点进度 0→1 分阶段上报；
- 成品验证：ffprobe 复核宽高/帧率/时长写入 report 与资产 meta；
- 画幅：16:9/9:16/1:1/original + 1080p/720p/4k/source；scale_mode=contain/fill。
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType
from app import ai_label


def _apply_ai_label(expr: str, final_label: str):
    """把 AI 生成合成内容**显式标识**接到 filter_complex 末尾。

    返回 (expr, final_label)；无中文字体时 drawtext_filter() 为空串，
    原样返回（仅靠 metadata 隐式标识），不抛异常。
    """
    frag = ai_label.drawtext_filter()
    if frag:
        expr = expr + ";" + "[" + final_label + "]" + frag + "[vlabel]"
        final_label = "vlabel"
    return expr, final_label

# ---------------------------------------------------------------- 工具

_RESOLUTIONS = {
    "1080p": {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)},
    "720p": {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720)},
    "4k": {"16:9": (3840, 2160), "9:16": (2160, 3840), "1:1": (2160, 2160)},
}

_XFADE_MAP = {
    "cut": None,
    "fade": "fade",
    "fadeblack": "fadeblack",
    "dissolve": "dissolve",
    "slideleft": "slideleft",
    "slideup": "slideup",
    "wiperight": "wiperight",
    "zoomin": "zoomin",
    "smoothleft": "smoothleft",
    "circleopen": "circleopen",
    "pixelize": "pixelize",
}

# G25：一键调色预设 → ffmpeg 滤镜链（叠加在 scale 之后）
_COLOR_GRADE_FILTERS = {
    "warm": "colorbalance=rs=0.06:gs=0.01:bs=-0.05:rm=0.02:bm=-0.02",
    "cool": "colorbalance=rs=-0.05:gs=0.0:bs=0.07:rm=-0.02:bm=0.02",
    "vintage": "eq=saturation=0.82:contrast=1.06,colorbalance=rs=0.05:rm=0.04:bm=-0.04",
    "cinematic": "eq=contrast=1.1:saturation=1.12:brightness=-0.01,curves=preset=linear_contrast",
    "high_contrast": "eq=contrast=1.25:saturation=1.15",
    "bright": "eq=brightness=0.06:saturation=1.05",
}


def _which(candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    p = shutil.which(candidates[-1] if candidates else "")
    if p:
        return p
    return None


def ffmpeg_path() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    if r.get("ready"):
        return r["path"]
    raise RuntimeError(deps.coded_message("binary_missing", "ffmpeg", r.get("repair_hint")))


def ffprobe_path() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    if r.get("ready"):
        return r["path"]
    raise RuntimeError(deps.coded_message("binary_missing", "ffprobe", r.get("repair_hint")))


def _dep(id: str) -> Dict[str, Any]:
    """报告 dependency 字段。"""
    from app.plugin import deps
    r = deps.resolve_binary(id, env=os.environ)
    return {"ready": bool(r.get("ready")), "source": r.get("source", ""),
            "path": r.get("path", ""), "version": r.get("version"),
            "warnings": list(r.get("warnings") or [])}


def _safe_name(s: str) -> str:
    out = []
    for ch in str(s):
        out.append(ch if (ch.isalnum() or ch in "._- ") else "_")
    return "".join(out)


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def probe_media(ctx, path: str) -> Dict[str, Any]:
    """ffprobe 取视频流/封装元数据。失败返回部分字段 + warning。"""
    cmd = [ffprobe_path(), "-v", "error", "-show_entries",
           "stream=index,codec_type,width,height,r_frame_rate,duration,bit_rate",
           "-show_entries", "format=duration,bit_rate,size",
           "-of", "json", path]
    proc = await ctx["run_tracked"](cmd, timeout=60, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
    out = {}
    if proc.returncode != 0:
        return {"warning": "ffprobe 失败: " + ((proc.stderr or "")[-300:])}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"warning": "ffprobe 输出无法解析"}
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v:
        try:
            num, den = str(v.get("r_frame_rate", "30/1")).split("/")
            fps = round(float(num) / max(float(den), 1.0), 3)
        except (ValueError, ZeroDivisionError):
            fps = 30.0
        out.update({
            "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0),
            "fps": fps,
            "video_duration": _num(v.get("duration")),
            "video_bit_rate": _num(v.get("bit_rate")),
        })
    if a:
        out["has_audio"] = True
    # FW-DEV-55：`_num(a, b)` 的第二个实参**先求值**——纯音频（无视频流 ⇒ v=None）时
    # `v.get("duration")` 在 _num 被调用之前就抛 AttributeError，即使 fmt.duration 有效。
    out["duration"] = _num(fmt.get("duration"), _num(v.get("duration")) if v else None)
    out["bit_rate"] = _num(fmt.get("bit_rate"))
    out["size"] = _num(fmt.get("size"))
    return out


def _sec2ass(t: float) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _scale_subs_for_tempo(subs: Any, tempo: float, total_sec: float
                          ) -> tuple:
    """音频按 atempo 压缩/拉伸后，字幕轴必须同步除以 tempo（FW-DEV-67）。

    为什么：成片 t 时刻显示的应当是配音轴 t*tempo 处的字幕。不缩放的话，
    成片 t 时刻显示的是配音轴 t 处的字幕，而音频实际已到 t*tempo ⇒ 错位随 t 线性增长
    （tempo=1.0508 时 30 s 处已差 1.5 s，用户看到"字幕越到后面越早"）。

    tempo == 1.0 ⇒ **原样返回**（逐位不变，align_mode=none / 等长场景不受影响）。
    tempo != 1.0 ⇒ 每条 cue 的 start/end 都除以 tempo；若 max(end) 超过成片总时长，
    截断到总时长并返回一条 warning（不静默）。

    返回 (subs, warnings)。
    """
    warnings: List[str] = []
    if not subs or not isinstance(subs, list) or tempo == 1.0:
        return subs, warnings
    try:
        t = float(tempo)
    except (TypeError, ValueError):
        return subs, warnings
    if t <= 0 or t == 1.0:
        return subs, warnings
    scaled: List[Any] = []
    for c in subs:
        if not isinstance(c, dict):
            scaled.append(c)
            continue
        try:
            st = float(c.get("start", 0.0) or 0.0) / t
            en = float(c.get("end", 0.0) or 0.0) / t
        except (TypeError, ValueError):
            scaled.append(c)
            continue
        scaled.append({**c, "start": round(st, 3), "end": round(en, 3)})
    ends = [float(c.get("end", 0.0) or 0.0) for c in scaled if isinstance(c, dict)]
    max_end = max(ends) if ends else 0.0
    limit = float(total_sec or 0.0)
    if limit > 0 and max_end > limit + 1e-6:
        warnings.append(
            "字幕轴按 tempo=%.4f 缩放后超出成片时长（max(end)=%.3f > total=%.3f），"
            "已截断到成片末尾" % (t, max_end, limit))
        for c in scaled:
            if isinstance(c, dict) and float(c.get("end", 0.0) or 0.0) > limit:
                c["end"] = round(limit, 3)
    return scaled, warnings


def _layout_clip_ranges(layout: Any) -> List[tuple]:
    """取 mixed_audio_layout.original_clips 的**源时间轴**保留区间（该放原声的台词段）。

    优先 start/end（已含 padding_before/after），退回 original_start/original_end。
    坏数据不抛异常（一段坏 layout 不该让整片渲染失败），跳过并留给调用方计数。
    """
    if not isinstance(layout, dict):
        return []
    clips = layout.get("original_clips")
    if not isinstance(clips, list):
        return []
    out: List[tuple] = []
    for c in clips:
        if not isinstance(c, dict):
            continue
        try:
            a = float(c.get("start", c.get("original_start")))
            b = float(c.get("end", c.get("original_end")))
        except (TypeError, ValueError):
            continue
        if b > a:
            out.append((max(a, 0.0), b))
    out.sort()
    return out


def _map_clips_to_output(clips: List[tuple], segs: List[Dict[str, Any]],
                         seg_durs: List[float], trans_dur: float,
                         total_sec: float) -> tuple:
    """源时间轴区间 → 成片（拼接后）时间轴区间。

    成片时间轴上第 i 段起点：off_0 = 0；off_i = off_{i-1} + dur_{i-1} - trans_dur
    —— 与 xfade 的 offset 递推一致（本文件 xfade 循环里 offset=acc-trans_dur），
    所以开转场时也是按**实际重叠后**的位置算，不是按累加和硬减。

    返回 (ranges, warnings, dropped_seconds)：落在所有段之外的时长被丢弃并计入
    dropped_seconds（不静默 —— 调用方写进 report/warnings）。
    """
    n = len(segs)
    offsets: List[float] = []
    acc = 0.0
    for i in range(n):
        offsets.append(max(acc, 0.0))
        acc = acc + seg_durs[i] - (trans_dur if i < n - 1 else 0.0)
    bounds: List[tuple] = []
    for i in range(n):
        st = _num(segs[i].get("start", 0))
        bounds.append((st, st + seg_durs[i]))
    limit = float(total_sec or 0.0)
    out: List[tuple] = []
    warnings: List[str] = []
    dropped = 0.0
    for a, b in clips:
        hit = False
        for i in range(n):
            s0, s1 = bounds[i]
            lo = max(a, s0)
            hi = min(b, s1)
            if hi <= lo:
                continue
            hit = True
            o_lo = offsets[i] + (lo - s0)
            o_hi = offsets[i] + (hi - s0)
            if limit > 0:
                o_hi = min(o_hi, limit)
            if o_hi > o_lo:
                out.append((round(o_lo, 3), round(o_hi, 3)))
        if not hit:
            dropped += (b - a)
    out.sort()
    merged: List[tuple] = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    if dropped > 0.05:
        warnings.append("原声保留区间有 %.3fs 落在所有片段之外（已丢弃，不静默）" % dropped)
    return merged, warnings, dropped


def _volume_gate_expr(ranges: List[tuple]) -> str:
    """'between(t,a,b)+between(t,c,d)' —— 供 volume 的 if() 逐帧求值（1=可闻，0=静音）。"""
    return "+".join("between(t,%.3f,%.3f)" % (a, b) for a, b in ranges)


def _build_ass(subs: List[Dict[str, Any]], canvas: tuple, style: Dict[str, Any]) -> str:
    """生成 ASS 字幕（PlayRes 对齐画布，字号随画布高度缩放）。"""
    w, h = canvas
    size = _num(style.get("size", 48), 48) * (h / 1080.0)
    color = str(style.get("color", "#FFFFFF")).lstrip("#") or "FFFFFF"
    stroke = str(style.get("stroke", "#000000")).lstrip("#") or "000000"
    bold = "1" if style.get("bold") else "0"
    font = str(style.get("font", "Microsoft YaHei"))
    outline = max(2, int(size / 12))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: FWS,{font},{size:.1f},&H00{color},&H00FFFFFF,&H00{stroke},&H64000000,{bold},0,0,0,100,100,0,0,1,{outline},0,2,60,60,40,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for s in subs:
        st = _sec2ass(_num(s.get("start", 0)))
        en = _sec2ass(_num(s.get("end", _num(s.get("start", 0)) + 0.5)))
        text = str(s.get("text", "")).replace("\\", "\\N").replace("{", "(").replace("}", ")")
        if text:
            lines.append(f"Dialogue: 0,{st},{en},FWS,,0,0,0,,{text}")
    return "\n".join(lines)


def _esc_filter_path(path: str) -> str:
    p = path.replace("\\", "/")
    return p.replace(":", "\\:").replace("'", "\\'")


# ---------------------------------------------------------------- 节点

class VideoRenderNode(NodeBase):
    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video = inputs.get("video")
        seg_asset = inputs.get("segments")
        if video is None or not getattr(video, "path", "") or not os.path.exists(video.path):
            raise RuntimeError("需要源视频输入")
        if seg_asset is None:
            raise RuntimeError("需要片段列表输入")
        store = ctx["store"]
        segs = store.read_json(seg_asset.id) or []
        if not isinstance(segs, list) or not segs:
            raise RuntimeError("片段列表为空")

        audio = inputs.get("audio")
        sub_asset = inputs.get("styled_subtitle")
        subs = store.read_json(sub_asset.id) if sub_asset is not None else None
        layout_asset = inputs.get("layout")
        # v3 端口体系已移除 JSON 类型，layout 用 SEGMENTS 承载（同样是 store 里的结构化资产）。
        # read_json 对两者一视同仁：都是 store 里那份 JSON 的持久引用。
        layout_obj = store.read_json(layout_asset.id) if layout_asset is not None else None
        overlay = inputs.get("overlay")
        bgm = inputs.get("bgm")

        progress = ctx.get("progress")
        if progress:
            progress(0.02)
        probe = await probe_media(ctx, video.path)
        src_w = int(probe.get("width") or 0)
        src_h = int(probe.get("height") or 0)
        src_fps = float(probe.get("fps") or 30.0)
        src_has_audio = bool(probe.get("has_audio"))
        if progress:
            progress(0.06)

        aspect = str(params.get("aspect", "16:9") or "16:9")
        res = str(params.get("resolution", "1080p") or "1080p")
        fps = int(params.get("fps", 30) or 30)
        burn = bool(params.get("burn_subtitle", True))
        transition = str(params.get("transition", "fade") or "cut")
        trans_dur = max(_num(params.get("transition_duration", 0.5), 0.5), 0.05)
        scale_mode = str(params.get("scale_mode", "contain") or "contain")
        audio_mix = str(params.get("audio_mix", "narration_only") or "narration_only")
        narration_vol = max(_num(params.get("narration_volume", 1.0), 1.0), 0.0)
        original_vol = max(_num(params.get("original_volume", 0.35), 0.35), 0.0)
        bgm_vol = max(_num(params.get("bgm_volume", 0.3), 0.3), 0.0)
        overlay_alpha = min(max(_num(params.get("overlay_alpha", 0.8), 0.8), 0.0), 1.0)
        # T3（1.2.4）：混音布局 —— 只在保留的台词区间放出原声（区间内 1.0、区间外静音）
        layout_clips = _layout_clip_ranges(layout_obj)
        layout_note: List[str] = []
        if layout_asset is not None and not layout_clips:
            layout_note.append("layout 输入为空或没有 original_clips（按无 layout 处理）")
        if layout_clips and audio_mix != "mix":
            layout_note.append(
                "layout 已给但 audio_mix=%s（仅 mix 模式生效，本次忽略 layout）" % audio_mix)
        out_name = _safe_name(str(params.get("output_name", "chengpian")) or "chengpian")
        # G4：音画双向微调模式；G25：调色
        align_mode = str(params.get("align_mode", "none") or "none")
        color_grade = str(params.get("color_grade", "none") or "none")
        align_used = False
        if align_mode not in ("none", "audio_to_video", "video_to_audio", "bidirectional"):
            align_mode = "none"
        if color_grade not in ("warm", "cool", "vintage", "cinematic", "high_contrast", "bright"):
            color_grade = "none" if str(color_grade or "").strip() == "" else color_grade

        # ---- 画布 ----
        # 1.2.4：resolution 额外接受 "WxH" 自定义画布（如 1280x544 = 2.35:1 不补黑边）；
        # 不匹配时仍走原预设表，既有取值逐位不变（回归线）。
        custom_canvas = re.match(r"^(\d{2,5})[xX×](\d{2,5})$", str(res or "").strip())
        if custom_canvas:
            w, h = int(custom_canvas.group(1)), int(custom_canvas.group(2))
        elif aspect == "original" or res == "source":
            if src_w and src_h:
                w, h = src_w, src_h
            else:
                w, h = (1920, 1080) if aspect != "9:16" else (1080, 1920)
        else:
            try:
                w, h = _RESOLUTIONS[res][aspect]
            except KeyError:
                w, h = 1920, 1080
        canvas = (w, h)

        workdir = ctx["workdir"]
        os.makedirs(workdir, exist_ok=True)
        n = len(segs)

        # ---- 片段时长（进度基准） ----
        seg_durs: List[float] = []
        for s in segs:
            st = _num(s.get("start", 0))
            en = _num(s.get("end", _num(s.get("start", 0)) + 1.0))
            if en <= st:
                en = st + 1.0
            seg_durs.append(en - st)
        total_sec = sum(seg_durs)
        if transition != "cut" and n > 1:
            total_sec = max(sum(seg_durs) - trans_dur * (n - 1), 0.1)

        # ---- G4 音画双向微调：配音时长 vs 画面时长自动对齐 ----
        align_used = None
        align_tempo = 1.0
        if align_mode != "none":
            narration_ok_probe = audio is not None and getattr(audio, "path", "") and os.path.exists(audio.path)
            if narration_ok_probe:
                aprobe = await probe_media(ctx, audio.path)
                audio_dur = _num(aprobe.get("duration"))
                if audio_dur > 0.5 and total_sec > 0.5:
                    ratio = audio_dur / total_sec
                    if align_mode in ("audio_to_video", "bidirectional"):
                        # 配音偏长：压缩音轨节奏到画面时长；偏短：拉伸
                        align_tempo = max(0.5, min(2.0, ratio))
                        align_used = f"audio_to_video(tempo={align_tempo:.3f}, {audio_dur:.1f}s->{total_sec:.1f}s)"
                    if align_mode in ("video_to_audio", "bidirectional"):
                        # 画面偏短：按配音时长扩展末段/整体时长（简化为扩展最后一段）
                        if ratio > 1.02:
                            delta = audio_dur - total_sec
                            segs[-1]["end"] = _num(segs[-1].get("end", 0)) + delta
                            total_sec = audio_dur
                            align_used = f"video_to_audio(+{delta:.2f}s 扩展末段)"
            # 若末段被扩展，重算 seg_durs（供 xfade offset 与进度）
            seg_durs = []
            for s in segs:
                st = _num(s.get("start", 0))
                en = _num(s.get("end", _num(s.get("start", 0)) + 1.0))
                if en <= st:
                    en = st + 1.0
                seg_durs.append(en - st)
            if transition != "cut" and n > 1:
                total_sec = max(sum(seg_durs) - trans_dur * (n - 1), 0.1)

        # FW-DEV-67：tempo≠1 时把字幕轴除回 tempo；tempo==1 时原样不动（回归线）。
        subs, sub_scale_warnings = _scale_subs_for_tempo(subs, align_tempo, total_sec)

        # ---- filtergraph：视频 ----
        scale = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
                 if scale_mode == "fill" else
                 f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
        grade = _COLOR_GRADE_FILTERS.get(color_grade, "")
        parts = []
        for i in range(n):
            st = segs[i].get("start", 0)
            en = segs[i].get("end", _num(segs[i].get("start", 0)) + 1.0)
            vchain = f"trim=start={st:.3f}:end={en:.3f},setpts=PTS-STARTPTS,fps={fps},{scale},setsar=1"
            if grade:
                vchain += f",{grade}"
            parts.append(f"[{i}:v]{vchain}[v{i}]")
        xfade = _XFADE_MAP.get(transition)
        if xfade is None or n <= 1:
            expr = ";".join(parts)
            if n == 1:
                expr += ";[v0]null[vcat]"
            else:
                concat_in = "".join(f"[v{i}]" for i in range(n))
                expr += ";" + concat_in + f"concat=n={n}:v=1:a=0[vcat]"
            final_label = "vcat"
        else:
            expr = ";".join(parts)
            prev, acc = "v0", seg_durs[0]
            for i in range(1, n):
                offset = max(acc - trans_dur, 0.0)
                out_label = "vcat" if i == n - 1 else f"x{i}"
                expr += f";[{prev}][v{i}]xfade=transition={xfade}:duration={trans_dur:.3f}:offset={offset:.3f}[{out_label}]"
                acc = acc + seg_durs[i] - trans_dur
                prev = out_label
            final_label = "vcat"

        # ---- 字幕烧录（并入同一 filter_complex） ----
        has_subs = burn and subs and len(subs) > 0
        if has_subs:
            style = {}
            if isinstance(subs[0], dict):
                style = subs[0].get("style") or {}
            ass = _build_ass(subs, canvas, style)
            ass_file = os.path.join(workdir, "subs.ass")
            with open(ass_file, "w", encoding="utf-8") as f:
                f.write(ass)
            esc = _esc_filter_path(ass_file)
            expr += f";[{final_label}]ass=filename='{esc}'[vsub]"
            final_label = "vsub"

        # ---- 覆盖视频轨 ----
        has_overlay = overlay is not None and getattr(overlay, "path", "") and os.path.exists(overlay.path)
        if has_overlay:
            alpha = f"{overlay_alpha:.3f}"
            ov_label = "ov0"
            ov_idx = n + len(extra_inputs)  # 覆盖轨输入下标（所有额外输入追加之后）
            expr += f";[{ov_idx}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=rgba,colorchannelmixer=aa={alpha}[{ov_label}]"
            expr += f";[{final_label}][{ov_label}]overlay=0:0[vout]"
            final_label = "vout"

        # ---- 音频 ----
        narration_ok = audio is not None and getattr(audio, "path", "") and os.path.exists(audio.path)
        bgm_ok = bgm is not None and getattr(bgm, "path", "") and os.path.exists(bgm.path)
        audio_label = ""
        extra_inputs: List[Any] = []
        gate_ranges: List[tuple] = []   # T3：原声保留区间（成片时间轴）；空 ⇒ 无 layout
        mix_layers: List[str] = []      # 本次实际参与混音的音轨标签（report 用）
        tempo_chain = f",atempo={align_tempo:.4f}" if align_tempo != 1.0 else ""
        if audio_mix == "narration_only" and narration_ok:
            extra_inputs.append(audio)
            expr += f";[{n}:a]atrim=0:{total_sec:.3f},asetpts=PTS-STARTPTS{tempo_chain},volume={narration_vol:.3f}[aN]"
            audio_label = "aN"
            mix_layers = ["aN"]
            if bgm_ok:
                extra_inputs.append(bgm)
                expr += f";[{n + 1}:a]atrim=0:{total_sec:.3f},asetpts=PTS-STARTPTS,volume={bgm_vol:.3f}[aB]"
                expr += f";[aN][aB]amix=inputs=2:duration=first:dropout_transition=0[aout]"
                audio_label = "aout"
                mix_layers = ["aN", "aB"]
        elif audio_mix == "mix":
            # 解说 + 原声（逐段 concat）+ 背景乐
            chain = []
            if not narration_ok:
                # 触发面：某章文案全是〔原声〕、或 narration 合成失败但有兜底 ⇒ 这也会走到
                layout_note.append("audio_mix=mix 但无解说输入 ⇒ 原声/BGM 单路透传（不生成 amix/c=1）")
            if narration_ok:
                extra_inputs.append(audio)
                chain.append(f"[{n}:a]atrim=0:{total_sec:.3f},asetpts=PTS-STARTPTS{tempo_chain},volume={narration_vol:.3f}[mN]")
            orig_labels = []
            gate_warnings: List[str] = []
            orig_mix_label = "[mO]"
            if src_has_audio:
                # 有 layout（且 audio_mix=mix）时：窗口内满音量 1.0，窗口外由 gate 静音；
                # 无 layout 时保持 1.2.3 的 original_volume 语义（逐位一致，回归线）
                orig_gain_tok = "1.000" if (layout_clips and audio_mix == "mix") else f"{original_vol:.3f}"
                for i in range(n):
                    st = _num(segs[i].get("start", 0))
                    en = _num(segs[i].get("end", st + 1.0))
                    chain.append(f"[{i}:a]atrim=start={st:.3f}:end={en:.3f},asetpts=PTS-STARTPTS,volume={orig_gain_tok}[aO{i}]")
                    orig_labels.append(f"[aO{i}]")
                if orig_labels:
                    # n==1 不许 concat=n=1（ffmpeg 直接 -22），单段透传
                    if len(orig_labels) == 1:
                        orig_mix_label = orig_labels[0]
                    else:
                        chain.append("".join(orig_labels) + f"concat=n={len(orig_labels)}:v=0:a=1[mO]")
                        orig_mix_label = "[mO]"
                    if orig_gain_tok == "1.000":
                        gate_ranges, gate_warnings, _dropped = _map_clips_to_output(
                            layout_clips, segs, seg_durs, trans_dur if transition != "cut" else 0.0,
                            total_sec)
                        if gate_ranges:
                            # 用 if() 逐帧求值，不用 enable=：volume 的 enable 语义是
                            # 「过滤器不启用 ⇒ 原样透传」，静音不了窗口外（实测过）。
                            chain.append("[%s]volume=volume='if(%s,1,0)':eval=frame[mOg]"
                                         % (orig_mix_label.strip("[]"), _volume_gate_expr(gate_ranges)))
                            orig_mix_label = "[mOg]"
                            layout_note.extend(gate_warnings)
                        else:
                            layout_note.append("layout 的保留区间全部落在片段之外（原声按静音处理）")
            if bgm_ok:
                extra_inputs.append(bgm)
                chain.append(f"[{n + (1 if narration_ok else 0)}:a]atrim=0:{total_sec:.3f},asetpts=PTS-STARTPTS,volume={bgm_vol:.3f}[mB]")
            mix_in = []
            if narration_ok:
                mix_in.append("[mN]")
            if orig_labels:
                mix_in.append(orig_mix_label)
            if bgm_ok:
                mix_in.append("[mB]")
            if mix_in:
                if len(mix_in) == 1:
                    # 只有一路：不生成 amix（amix 要求 ≥2 个输入，inputs=1 会 -22），直接透传该标签
                    audio_label = mix_in[0].strip("[]")
                    mix_layers = [audio_label]
                else:
                    chain.append("".join(mix_in) + f"amix=inputs={len(mix_in)}:duration=first:dropout_transition=0[aout]")
                    audio_label = "aout"
                    mix_layers = [x.strip("[]") for x in mix_in]
            else:
                layout_note.append("audio_mix=mix 但没有任何可用音轨（无解说/无原声/无 BGM）⇒ 成片无音轨")
            if chain:
                expr += ";" + ";".join(chain)
        elif audio_mix == "original_only" and src_has_audio:
            labels = []
            for i in range(n):
                st = _num(segs[i].get("start", 0))
                en = _num(segs[i].get("end", st + 1.0))
                expr += f";[{i}:a]atrim=start={st:.3f}:end={en:.3f},asetpts=PTS-STARTPTS,volume={original_vol:.3f}[aO{i}]"
                labels.append(f"[aO{i}]")
            if labels:
                if len(labels) == 1:
                    audio_label = labels[0].strip("[]")   # n==1：不许 concat=n=1
                else:
                    expr += ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]"
                    audio_label = "aout"

        # ---- 组装命令 ----
        # AI 生成合成内容：显式标识追加到 filter_complex 末尾（同一次编码，不额外转码）
        expr, final_label = _apply_ai_label(expr, final_label)
        cmd = [ffmpeg_path(), "-y", "-fflags", "+genpts+igndts"]
        for i in range(n):
            cmd += ["-i", video.path]
        for extra in extra_inputs:
            cmd += ["-i", extra.path]
        if has_overlay:
            cmd += ["-i", overlay.path]
        # Windows 命令行 8191 字符上限（c-final13 实测 WinError 206：31 段 × 长 filter_complex
        # 超限 → spawn FileNotFoundError → 退出码 -1）。filter_complex 走脚本文件规避。
        if len(expr) > 6000:
            fc_script = os.path.join(workdir, "filter_complex.txt")
            with open(fc_script, "w", encoding="utf-8") as _fcf:
                _fcf.write(expr)
            cmd += ["-filter_complex_script", fc_script]
        else:
            cmd += ["-filter_complex", expr]
        cmd += ["-map", f"[{final_label}]"]
        if audio_label:
            cmd += ["-map", f"[{audio_label}]"]
        cmd += ["-r", str(fps), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p"]
        if audio_label:
            cmd += ["-c:a", "aac", "-b:a", "192k"]

        # AI 生成合成内容：隐式标识写进容器 metadata（必须在输出文件之前）
        cmd += ai_label.metadata_args()

        out_mp4 = os.path.join(workdir, out_name + ".mp4")
        cmd += [out_mp4]

        # ---- 执行（进度轮询） ----
        if progress:
            progress(0.12)
        log_err = os.path.join(workdir, "render_err.log")
        with open(log_err, "wb") as fe:
            proc = ctx["spawn_tracked"](cmd, stdout=open(os.devnull, "wb"), stderr=fe)
        last = 0.12
        while proc.poll() is None:
            await asyncio.sleep(0.4)
            if progress:
                last = min(0.9, last + 0.03)
                progress(last)
        if proc.returncode != 0:
            err = ""
            try:
                with open(log_err, "r", encoding="utf-8", errors="replace") as f:
                    err = f.read()
            except OSError:
                pass
            raise RuntimeError("渲染失败: " + (err[-1500:] if err else f"ffmpeg 退出码 {proc.returncode}"))

        # ---- 成品验证 ----
        if progress:
            progress(0.95)
        verify = await probe_media(ctx, out_mp4)
        if verify.get("width") and verify.get("height"):
            vw, vh = int(verify["width"]), int(verify["height"])
        else:
            vw, vh = w, h
        report = {
            "status": "ok",
            "output": out_mp4,
            "width": vw, "height": vh,
            "fps": int(verify.get("fps") or fps),
            "duration": round(_num(verify.get("duration")), 3),
            "size": int(os.path.getsize(out_mp4)) if os.path.exists(out_mp4) else 0,
            "segments": n,
            "transition": transition if transition != "cut" else "cut",
            "transition_duration": trans_dur if transition != "cut" and n > 1 else 0.0,
            "subtitle_count": len(subs) if has_subs else 0,
            "audio_mix": audio_mix,
            "audio_layers": list(mix_layers),
            "audio_track_present": bool(audio_label),
            "overlay": has_overlay,
            "align_mode": align_mode,
            "align_used": align_used,
            "align_tempo": round(align_tempo, 4) if align_tempo != 1.0 else None,
            # FW-DEV-67：字幕轴是否按 tempo 缩放过（tempo==1 时为 applied=false，轴逐位不变）
            "subtitle_shift": {
                "applied": bool(align_tempo != 1.0 and subs),
                "tempo": round(align_tempo, 4),
                "max_end": round(max([float(c.get("end", 0.0) or 0.0)
                                      for c in (subs or []) if isinstance(c, dict)], default=0.0), 3),
                "total_seconds": round(float(total_sec or 0.0), 3),
            },
            "color_grade": color_grade if grade else None,
            # T3（1.2.4）：原声门控（无 layout 时 mode=off，与 1.2.3 行为相同）
            "original_gate": {
                "mode": "layout_clips" if gate_ranges else "off",
                "gain_inside": 1.0 if gate_ranges else None,
                "gain_outside": 0.0 if gate_ranges else None,
                "base_volume": None if gate_ranges else round(original_vol, 3),
                "ranges": [[a, b] for a, b in gate_ranges],
                "kept_seconds": round(sum(b - a for a, b in gate_ranges), 3),
                "layout_clips": len(layout_clips),
            },
            "source_probe": {k: probe.get(k) for k in ("width", "height", "fps", "duration", "bit_rate")},
            "warnings": ([probe.get("warning")] if probe.get("warning") else [])
                        + list(sub_scale_warnings) + list(layout_note),
            "dependency": {"ffmpeg": _dep("ffmpeg"), "ffprobe": _dep("ffprobe")},
        }
        # v3 没有 report 端口（裁定：不为不存在的需求开接口）。诊断与统计一律落
        # asset.meta —— 下游与界面都从这里读，degraded/warnings 是统一约定。
        meta = {
            "width": vw, "height": vh,
            "fps": int(verify.get("fps") or fps),
            "duration": round(_num(verify.get("duration")), 3),
            "size": report["size"],
            "segments": n,
            "transition": report["transition"],
            "subtitle_count": report["subtitle_count"],
            "audio_mix": audio_mix,
            "audio_layers": list(mix_layers),
            "audio_track_present": bool(audio_label),
            "overlay": has_overlay,
            "align_mode": align_mode,
            "align_used": align_used,
            "align_tempo": report["align_tempo"],
            "color_grade": report["color_grade"],
            "original_gate": report["original_gate"],
            "source_probe": report["source_probe"],
            "output": out_mp4,
            "stats": report,
            "degraded": bool(report["warnings"]),
            "warnings": list(report["warnings"]),
        }
        if not audio_label:
            meta["warnings"].append(
                "成片没有音轨（audio_mix=%s 但没有任何可用音轨输入）" % audio_mix)
            meta["degraded"] = True
        asset = store.save_asset(
            Asset(id="", kind=PortType.VIDEO.value, path=out_mp4, meta=meta))
        if progress:
            progress(1.0)
        return {"rendered": asset}
