# -*- coding: utf-8 -*-
"""MP4 渲染节点：自动混剪（片段拼接）+ 配音混入 + 字幕烧录（ASS 花字样式）。

依赖 imageio-ffmpeg 自带的 ffmpeg（已验证支持 concat / ass / libx264 / aac）。
实现要点：拼接(或转场)在 filter_complex 中完成，字幕烧录也并入同一
filter_complex（同一输出流不能同时用 -filter_complex 与 -vf）。
"""
from __future__ import annotations
import os
import subprocess
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app.registry import register

RESOLUTIONS = {
    "1080p": {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)},
    "720p": {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720)},
    "4k": {"16:9": (3840, 2160), "9:16": (2160, 3840), "1:1": (2160, 2160)},
}


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _start(segs, i):
    return float(segs[i].get("start", 0))


def _end(segs, i):
    return float(segs[i].get("end", segs[i].get("start", 0)))


def _srt_to_ass(subs: List[Dict], style: Dict[str, Any]) -> str:
    size = int(style.get("size", 48))
    color = str(style.get("color", "#FFFFFF")).lstrip("#") or "FFFFFF"
    stroke = str(style.get("stroke", "#000000")).lstrip("#") or "000000"
    bold = "1" if style.get("bold") else "0"
    font = str(style.get("font", "Microsoft YaHei"))
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: FWS," + font + "," + str(int(size * 2)) + ",&H00" + color + ",&H00FFFFFF,&H00" + stroke + ",&H64000000," + bold + ",0,0,0,100,100,0,0,1," + str(max(2, size // 12)) + ",0,2,60,60,60,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def ts(t: float) -> str:
        h = int(t // 3600)
        m = int(t % 3600 // 60)
        sec = t % 60
        return str(h) + ":" + ("%02d" % m) + ":" + ("%05.2f" % sec)

    for s in subs:
        start = ts(float(s.get("start", 0)))
        end = ts(float(s.get("end", s.get("start", 0)) + 0.1))
        text = str(s.get("text", "")).replace("\\", "\\N").replace("{", "(").replace("}", ")")
        header.append("Dialogue: 0," + start + "," + end + ",FWS,,0,0,0,," + text)
    return "\n".join(header)


def _esc_ass_path(path: str) -> str:
    """ffmpeg filtergraph 中转义 ASS 文件路径。"""
    p = path.replace("\\", "/")
    return p.replace(":", "\\:").replace("'", "\\'")



_XFADE_MAP = {
    "cut": None,
    "fade": "fade",
    "fadeblack": "fadeblack",
    "dissolve": "dissolve",
    "slide": "slideleft",
    "wipe": "wipeleft",
    "zoom": "zoomin",
}

class VideoRenderNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/video_render",
            title="MP4 渲染",
            category="输出",
            description="自动混剪出片：片段拼接 + 配音混入 + 字幕烧录（花字样式）",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="源视频", required=True),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="片段列表", required=True),
                PortSpec(name="audio", type=PortType.AUDIO, label="配音音频", required=False),
                PortSpec(name="styled_subtitle", type=PortType.SUBTITLE, label="样式字幕", required=False),
            ],
            outputs=[
                PortSpec(name="rendered", type=PortType.VIDEO, label="成品 MP4"),
            ],
            params=[
                PortSpec(name="aspect", type=PortType.STRING, label="画幅", default="16:9",
                         widget="select", options=["16:9", "9:16", "1:1"]),
                PortSpec(name="resolution", type=PortType.STRING, label="分辨率", default="1080p",
                         widget="select", options=["1080p", "720p", "4k"]),
                PortSpec(name="fps", type=PortType.INT, label="帧率", default=30, widget="number"),
                PortSpec(name="burn_subtitle", type=PortType.BOOL, label="烧录字幕", default=True),
                PortSpec(name="transition", type=PortType.STRING, label="转场", default="cut",
                         widget="select", options=["cut", "fade", "fadeblack", "dissolve", "slide", "wipe", "zoom"]),
                PortSpec(name="output_name", type=PortType.STRING, label="输出文件名",
                         default="chengpian", widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        seg_asset = inputs.get("segments")
        if video_asset is None or not video_asset.path or not os.path.exists(video_asset.path):
            raise RuntimeError("需要源视频输入")
        if seg_asset is None:
            raise RuntimeError("需要片段列表输入")

        store = ctx["store"]
        segs = store.read_json(seg_asset.id) or []
        if not segs:
            raise RuntimeError("片段列表为空")
        audio_asset = inputs.get("audio")
        sub_asset = inputs.get("styled_subtitle")
        subs = store.read_json(sub_asset.id) if sub_asset else []

        ff = _ffmpeg()
        aspect = params.get("aspect", "16:9")
        res = params.get("resolution", "1080p")
        fps = int(params.get("fps", 30) or 30)
        burn = params.get("burn_subtitle", True)
        transition = params.get("transition", "cut")
        out_name = str(params.get("output_name", "chengpian")) or "chengpian"

        try:
            w, h = RESOLUTIONS[res][aspect]
        except KeyError:
            w, h = 1920, 1080

        workdir = ctx["workdir"]
        os.makedirs(workdir, exist_ok=True)
        n = len(segs)

        # ---------- 1) 拼接 filter_complex ----------
        parts = []
        for i in range(n):
            parts.append(
                "[" + str(i) + ":v]trim=start=" + ("%.3f" % _start(segs, i)) +
                ":end=" + ("%.3f" % _end(segs, i)) +
                ",setpts=PTS-STARTPTS,fps=" + str(fps) + ",scale=" + str(w) + ":" + str(h) +
                ":force_original_aspect_ratio=decrease,pad=" + str(w) + ":" + str(h) +
                ":(ow-iw)/2:(oh-ih)/2,setsar=1[v" + str(i) + "]"
            )

        if transition == "cut" or n <= 1:
            concat_in = "".join("[v" + str(i) + "]" for i in range(n))
            filter_complex = ";".join(parts) + ";" + concat_in + "concat=n=" + str(n) + ":v=1:a=0[vcat]"
            final_label = "vcat"
        else:
            # xfade 链：v0+v1->x1, x1+v2->x2, ... x(n-1)->vcat
            # 每个 xfade 输入顺序 [已拼接结果][下一段]，offset = 当前总长 - 0.3（重叠）
            expr = ";".join(parts)
            prev = "v0"
            total = _end(segs, 0) - _start(segs, 0)
            for i in range(1, n):
                offset = max(total - 0.3, 0)
                out_label = "vcat" if i == n - 1 else ("x" + str(i))
                _t = _XFADE_MAP.get(transition, "fade")
                if _t:
                    expr += ";[" + prev + "][v" + str(i) + "]xfade=transition=" + _t + ":duration=0.3:offset=" + ("%.3f" % offset) + "[" + out_label + "]"
                total = total + (_end(segs, i) - _start(segs, i)) - 0.3
                prev = out_label
            filter_complex = expr
            final_label = "vcat"

        # ---------- 2) 字幕烧录（并入同一 filter_complex） ----------
        if burn and subs:
            ass_file = os.path.join(workdir, "subs.ass")
            style = subs[0].get("style", {}) if isinstance(subs[0], dict) else {}
            with open(ass_file, "w", encoding="utf-8") as f:
                f.write(_srt_to_ass(subs, style))
            esc = _esc_ass_path(ass_file)
            filter_complex = filter_complex + ";[" + final_label + "]ass=filename='" + esc + "'[voutf]"
            final_label = "voutf"

        # ---------- 3) 组装命令 ----------
        cmd = [ff, "-y"]
        for _ in range(n):
            cmd += ["-i", video_asset.path]
        has_audio = audio_asset is not None and audio_asset.path and os.path.exists(audio_asset.path)
        if has_audio:
            cmd += ["-i", audio_asset.path]
        cmd += ["-filter_complex", filter_complex, "-map", "[" + final_label + "]"]
        if has_audio:
            cmd += ["-map", str(n) + ":a"]
        cmd += ["-r", str(fps), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]

        out_mp4 = os.path.join(workdir, out_name + ".mp4")
        cmd += [out_mp4]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, errors="replace")
        if proc.returncode != 0:
            # 写错误日志便于排查
            try:
                with open(os.path.join(workdir, "render_err.log"), "w", encoding="utf-8") as f:
                    f.write(proc.stderr or "")
            except OSError:
                pass
            raise RuntimeError("渲染失败: " + (proc.stderr[-1200:] if proc.stderr else "未知错误"))

        asset = store.save_asset(Asset(id="", kind=PortType.VIDEO.value, path=out_mp4,
                                       meta={"width": w, "height": h, "aspect": aspect,
                                             "resolution": res, "segments": n}))
        return {"rendered": asset}
