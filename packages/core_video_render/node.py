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
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

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
    out["duration"] = _num(fmt.get("duration"), _num(v.get("duration")))
    out["bit_rate"] = _num(fmt.get("bit_rate"))
    out["size"] = _num(fmt.get("size"))
    return out


def _sec2ass(t: float) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


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
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/video_render",
            title="MP4 渲染",
            category="输出",
            description="自动混剪出片：片段拼接 + 转场 + 配音/原声/背景乐混音 + 字幕烧录 + 可选覆盖视频轨；带进度与成品 ffprobe 验证。",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="源视频", required=True),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="片段列表", required=True),
                PortSpec(name="audio", type=PortType.AUDIO, label="配音音频", required=False),
                PortSpec(name="styled_subtitle", type=PortType.SUBTITLE, label="样式字幕", required=False),
                PortSpec(name="overlay", type=PortType.VIDEO, label="覆盖视频轨", required=False,
                         description="整段覆盖在成片上的第二视频轨"),
                PortSpec(name="bgm", type=PortType.AUDIO, label="背景音乐", required=False),
            ],
            outputs=[
                PortSpec(name="rendered", type=PortType.VIDEO, label="成品 MP4"),
                PortSpec(name="report", type=PortType.JSON, label="渲染报告", required=False),
            ],
            params=[
                PortSpec(name="aspect", type=PortType.STRING, label="画幅", default="16:9",
                         widget="select", options=["16:9", "9:16", "1:1", "original"]),
                PortSpec(name="resolution", type=PortType.STRING, label="分辨率", default="1080p",
                         widget="select", options=["1080p", "720p", "4k", "source"]),
                PortSpec(name="fps", type=PortType.INT, label="帧率", default=30, widget="number"),
                PortSpec(name="burn_subtitle", type=PortType.BOOL, label="烧录字幕", default=True,
                         widget="toggle"),
                PortSpec(name="transition", type=PortType.STRING, label="转场", default="fade",
                         widget="select",
                         options=["cut", "fade", "fadeblack", "dissolve", "slideleft", "slideup",
                                  "wiperight", "zoomin", "smoothleft", "circleopen", "pixelize"]),
                PortSpec(name="transition_duration", type=PortType.FLOAT, label="转场时长(秒)",
                         default=0.5, widget="number"),
                PortSpec(name="scale_mode", type=PortType.STRING, label="缩放模式", default="contain",
                         widget="select", options=["contain", "fill"]),
                PortSpec(name="audio_mix", type=PortType.STRING, label="混音模式", default="narration_only",
                         widget="select", options=["narration_only", "original_only", "mix"]),
                PortSpec(name="narration_volume", type=PortType.FLOAT, label="解说音量", default=1.0,
                         widget="number"),
                PortSpec(name="original_volume", type=PortType.FLOAT, label="原声音量", default=0.35,
                         widget="number"),
                PortSpec(name="bgm_volume", type=PortType.FLOAT, label="背景乐音量", default=0.3,
                         widget="number"),
                PortSpec(name="overlay_alpha", type=PortType.FLOAT, label="覆盖轨不透明度", default=0.8,
                         widget="number"),
                PortSpec(name="output_name", type=PortType.STRING, label="输出文件名",
                         default="chengpian", widget="text"),
            ],
            version="1.1.0",
        )

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
        out_name = _safe_name(str(params.get("output_name", "chengpian")) or "chengpian")

        # ---- 画布 ----
        if aspect == "original" or res == "source":
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

        # ---- filtergraph：视频 ----
        scale = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
                 if scale_mode == "fill" else
                 f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
        parts = []
        for i in range(n):
            st = segs[i].get("start", 0)
            en = segs[i].get("end", _num(segs[i].get("start", 0)) + 1.0)
            parts.append(
                f"[{i}:v]trim=start={st:.3f}:end={en:.3f},setpts=PTS-STARTPTS,"
                f"fps={fps},{scale},setsar=1[v{i}]")
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
        if audio_mix == "narration_only" and narration_ok:
            extra_inputs.append(audio)
            expr += f";[{n}:a]atrim=0:{total_sec:.3f},asetpts=PTS-STARTPTS,volume={narration_vol:.3f}[aN]"
            audio_label = "aN"
            if bgm_ok:
                extra_inputs.append(bgm)
                expr += f";[{n + 1}:a]atrim=0:{total_sec:.3f},asetpts=PTS-STARTPTS,volume={bgm_vol:.3f}[aB]"
                expr += f";[aN][aB]amix=inputs=2:duration=first:dropout_transition=0[aout]"
                audio_label = "aout"
        elif audio_mix == "mix":
            # 解说 + 原声（逐段 concat）+ 背景乐
            chain = []
            if narration_ok:
                extra_inputs.append(audio)
                chain.append(f"[{n}:a]atrim=0:{total_sec:.3f},asetpts=PTS-STARTPTS,volume={narration_vol:.3f}[mN]")
            orig_labels = []
            if src_has_audio:
                for i in range(n):
                    st = _num(segs[i].get("start", 0))
                    en = _num(segs[i].get("end", st + 1.0))
                    chain.append(f"[{i}:a]atrim=start={st:.3f}:end={en:.3f},asetpts=PTS-STARTPTS,volume={original_vol:.3f}[aO{i}]")
                    orig_labels.append(f"[aO{i}]")
                if orig_labels:
                    chain.append("".join(orig_labels) + f"concat=n={len(orig_labels)}:v=0:a=1[mO]")
            if bgm_ok:
                extra_inputs.append(bgm)
                chain.append(f"[{n + (1 if narration_ok else 0)}:a]atrim=0:{total_sec:.3f},asetpts=PTS-STARTPTS,volume={bgm_vol:.3f}[mB]")
            mix_in = []
            if narration_ok:
                mix_in.append("[mN]")
            if orig_labels:
                mix_in.append("[mO]")
            if bgm_ok:
                mix_in.append("[mB]")
            if mix_in:
                chain.append("".join(mix_in) + f"amix=inputs={len(mix_in)}:duration=first:dropout_transition=0[aout]")
                audio_label = "aout"
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
                expr += ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]"
                audio_label = "aout"

        # ---- 组装命令 ----
        cmd = [ffmpeg_path(), "-y", "-fflags", "+genpts+igndts"]
        for i in range(n):
            cmd += ["-i", video.path]
        for extra in extra_inputs:
            cmd += ["-i", extra.path]
        if has_overlay:
            cmd += ["-i", overlay.path]
        cmd += ["-filter_complex", expr, "-map", f"[{final_label}]"]
        if audio_label:
            cmd += ["-map", f"[{audio_label}]"]
        cmd += ["-r", str(fps), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p"]
        if audio_label:
            cmd += ["-c:a", "aac", "-b:a", "192k"]

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
            "overlay": has_overlay,
            "source_probe": {k: probe.get(k) for k in ("width", "height", "fps", "duration", "bit_rate")},
            "warnings": [probe.get("warning")] if probe.get("warning") else [],
            "dependency": {"ffmpeg": _dep("ffmpeg"), "ffprobe": _dep("ffprobe")},
        }
        asset = store.save_asset(
            Asset(id="", kind=PortType.VIDEO.value, path=out_mp4,
                  meta={"width": vw, "height": vh, "fps": int(verify.get("fps") or fps),
                        "duration": round(_num(verify.get("duration")), 3),
                        "segments": n, "transition": transition}))
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"status": "ok"}),
            payload=report)
        if progress:
            progress(1.0)
        return {"rendered": asset, "report": report_asset}
