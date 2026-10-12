"""静音裁剪节点：自适应阈值裁剪头尾静音。

算法（对齐逆向实测语义，独立实现）：
1. volumedetect 取 mean_volume → 阈值 = mean - 25dB，夹在 [-45,-25]；失败回退 -35dB。
2. silencedetect(noise=<thr dB> dB, d=<min_silence>) 找出静音区间。
3. 头：若首段静音从 0 开始，净起点 = 首段静音结束 - edge_keep_ms 余量；
   尾：若末段静音到结尾，净终点 = 末段静音开始 + edge_keep_ms 余量。
4. mp3/m4a/ogg 输入先解码为 wav 中间文件再裁剪，输出默认 wav（避免有损重编码累积损耗）。
5. 全静音/过短输入明确报错，不产出伪资产。
"""
from __future__ import annotations
import json
import os
import re
import shutil
import time
import uuid
from typing import Any, Dict, List, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


def _resolve_ffmpeg(ffmpeg_path: str = "") -> str:
    """统一依赖解析层。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", requested=ffmpeg_path, env=os.environ,
                            params={"ffmpeg_path": ffmpeg_path})
    return r["path"] if r["ready"] else "ffmpeg"


def _ffprobe_bin(ffmpeg: str) -> str:
    """统一依赖解析层：ffmpeg 同目录优先。"""
    from app.plugin import deps
    d = os.path.dirname(ffmpeg)
    for cand in (os.path.join(d, "ffprobe.exe"), os.path.join(d, "ffprobe")):
        if cand and os.path.isfile(cand):
            return cand
    r = deps.resolve_binary("ffprobe", env=os.environ)
    return r["path"] if r["ready"] else "ffprobe"


async def _duration(ctx, ffmpeg_path: str, media_path: str) -> Optional[float]:
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    ffprobe = _ffprobe_bin(ffmpeg)
    try:
        out = await ctx["run_tracked"](
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", media_path], timeout=60)
        if out.returncode != 0 or not out.stdout:
            return None
        data = json.loads(out.stdout.decode("utf-8", "replace"))
        d = float((data.get("format") or {}).get("duration", 0) or 0)
        return round(d, 3) if d > 0 else None
    except Exception:
        return None


async def _mean_volume(ctx, ffmpeg_path: str, media_path: str) -> Optional[float]:
    """volumedetect 均值音量 dB；失败返回 None。"""
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    try:
        out = await ctx["run_tracked"]([ffmpeg, "-i", media_path, "-af", "volumedetect", "-f", "null", "-"],
                                       timeout=180)
        err = (out.stderr or b"").decode("utf-8", "replace")
        mm = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
        return float(mm.group(1)) if mm else None
    except Exception:
        return None


async def _silence_ranges(ctx, ffmpeg_path: str, media_path: str, noise_db: float,
                          min_dur: float) -> List[Tuple[float, float]]:
    """silencedetect 输出静音区间列表 [(start, end), ...]。"""
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    ranges: List[Tuple[float, float]] = []
    try:
        out = await ctx["run_tracked"](
            [ffmpeg, "-i", media_path, "-af",
             f"silencedetect=n={noise_db:.1f}dB:d={min_dur:.3f}", "-f", "null", "-"],
            timeout=180)
        err = (out.stderr or b"").decode("utf-8", "replace")
        starts: List[float] = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", err)]
        ends: List[float] = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err)]
    except Exception:
        return ranges
    if len(starts) == len(ends):
        for a, b in zip(starts, ends):
            ranges.append((round(a, 3), round(b, 3)))
    elif starts:
        # silencedetect 只输出 silence_start（直至视频结束都没安静结束）
        for a in starts:
            ranges.append((round(a, 3), None))  # type: ignore[list-item]
    return ranges


def _compute_cut(total: float, ranges: List[Tuple[float, Optional[float]]],
                 edge_keep_s: float) -> Tuple[float, float, List[str]]:
    """由静音区间计算 [new_start, new_end)；返回裁剪区间与说明。"""
    warns: List[str] = []
    new_start, new_end = 0.0, total
    # 头：首段静音从 0 开始
    if ranges and ranges[0][1] is not None and ranges[0][0] <= 0.05:
        head_silence_end = ranges[0][1]
        new_start = max(0.0, head_silence_end - edge_keep_s)
    # 尾：末段静音延伸到结尾（或无结束标记）
    if ranges:
        last_start, last_end = ranges[-1]
        if last_end is None or last_end >= total - 0.05:
            tail_silence_start = last_start
            new_end = min(total, tail_silence_start + edge_keep_s)
    if new_start >= new_end - 0.05:
        warns.append("音频全为静音或过短，裁剪后无效")
        return 0.0, total, warns
    return round(new_start, 3), round(new_end, 3), warns


class AudioTrimNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/audio_silence_trim",
            title="静音裁剪",
            category="音频",
            description="自适应阈值裁剪头尾静音（mp3 先转 wav 再裁，避免反复重编码损耗）",
            inputs=[PortSpec(name="audio", type=PortType.AUDIO, label="源音频", required=True)],
            outputs=[PortSpec(name="trimmed", type=PortType.AUDIO, label="裁剪后音频"),
                     PortSpec(name="trim_report", type=PortType.JSON, label="裁剪报告")],
            params=[
                PortSpec(name="threshold_db", type=PortType.FLOAT, label="静音阈值(dB)", default=0.0,
                         widget="number", description="0=自适应（均值-25dB，夹在[-45,-25]）"),
                PortSpec(name="min_silence_ms", type=PortType.INT, label="最小静音(ms)", default=200,
                         widget="number", description="短于此视为语气停顿保留"),
                PortSpec(name="edge_keep_ms", type=PortType.INT, label="切点保留(ms)", default=80,
                         widget="number"),
                PortSpec(name="output_format", type=PortType.STRING, label="输出格式", default="wav",
                         widget="select", options=["wav", "mp3"]),
                PortSpec(name="ffmpeg_path", type=PortType.STRING, label="ffmpeg 路径", default="",
                         widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        audio = inputs.get("audio")
        if audio is None or not getattr(audio, "path", "") or not os.path.exists(audio.path):
            raise RuntimeError("缺少音频输入")
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        ffmpeg_path = str(params.get("ffmpeg_path", "") or "")
        ffmpeg = _resolve_ffmpeg(ffmpeg_path)
        src = audio.path
        total = await _duration(ctx, ffmpeg_path, src)
        if total is None or total <= 0:
            raise RuntimeError(f"无法读取音频时长: {src}")

        progress(0.1)
        # 自适应阈值
        thr_param = float(params.get("threshold_db", 0.0) or 0.0)
        if thr_param <= -0.01 or thr_param >= 0.01:
            noise_db = thr_param
            threshold_strategy = f"显式 {thr_param}dB"
        else:
            mean = await _mean_volume(ctx, ffmpeg_path, src)
            if mean is None:
                noise_db = -35.0
                threshold_strategy = "回退 -35dB（均值不可测）"
            else:
                noise_db = max(-45.0, min(-25.0, mean - 25.0))
                threshold_strategy = f"自适应 mean({mean}dB)-25dB = {noise_db:.1f}dB"
        min_sil_s = max(0.05, int(params.get("min_silence_ms", 200) or 200) / 1000.0)
        edge_keep_s = max(0.0, int(params.get("edge_keep_ms", 80) or 80) / 1000.0)

        ranges = await _silence_ranges(ctx, ffmpeg_path, src, noise_db, min_sil_s)
        progress(0.5)
        new_start, new_end, warns = _compute_cut(total, ranges, edge_keep_s)
        if (new_start, new_end) == (0.0, total):
            warns.append("未检测到头尾可裁静音，原样直通")

        out_dir = os.path.join(store.files_dir, "audio_trim")
        os.makedirs(out_dir, exist_ok=True)
        out_fmt = str(params.get("output_format", "wav") or "wav").lstrip(".")
        started = time.time()

        # mp3/m4a/ogg → 先解码 wav 中间文件再裁剪（避免有损重编码损耗）
        src_is_wav = src.lower().endswith(".wav")
        work_input = src
        tmp_wav = ""
        if not src_is_wav or out_fmt == "mp3":
            tmp_wav = os.path.join(out_dir, f"trim_tmp_{os.getpid()}_{uuid.uuid4().hex[:8]}.wav")
            out = await ctx["run_tracked"](
                [ffmpeg, "-y", "-i", src, "-af", "aresample=44100", "-c:a", "pcm_s16le", tmp_wav],
                timeout=300)
            if out.returncode != 0:
                raise RuntimeError("音频解码失败: %s" % (out.stderr or b"").decode("utf-8", "replace")[-300:])
            work_input = tmp_wav

        out_path = os.path.join(out_dir, f"trim_{os.getpid()}_{uuid.uuid4().hex[:8]}.{out_fmt}")
        trim_args = []
        if new_start > 0.01:
            trim_args += ["-ss", f"{new_start:.3f}"]
        trim_args += ["-to", f"{new_end:.3f}"]
        if out_fmt == "wav":
            trim_args += ["-c:a", "pcm_s16le"]
        elif out_fmt == "mp3":
            trim_args += ["-c:a", "libmp3lame", "-q:a", "2"]
        cmd = [ffmpeg, "-y", "-i", work_input, *trim_args, out_path]
        out = await ctx["run_tracked"](cmd, timeout=300)
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except OSError:
                pass
        if out.returncode != 0:
            raise RuntimeError("ffmpeg 裁剪失败: %s" % (out.stderr or b"").decode("utf-8", "replace")[-300:])
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("裁剪输出为空")
        trimmed_dur = await _duration(ctx, ffmpeg_path, out_path)
        if trimmed_dur is None or trimmed_dur < 0.05:
            raise RuntimeError("裁剪后音频过短或为空，不产出伪资产")
        progress(1.0)

        asset = Asset(id="", kind=PortType.AUDIO.value, path=out_path,
                      meta={"engine": "ffmpeg", "original_duration": total,
                            "trimmed_duration": trimmed_dur,
                            "new_start": new_start, "new_end": new_end})
        store.save_asset(asset)
        report = {
            "original_duration": total,
            "trimmed_duration": trimmed_dur,
            "head_trim_s": round(new_start, 3),
            "tail_trim_s": round(total - new_end, 3),
            "threshold_db": round(noise_db, 1),
            "threshold_strategy": threshold_strategy,
            "min_silence_s": round(min_sil_s, 3),
            "edge_keep_s": round(edge_keep_s, 3),
            "silence_ranges": [list(r) for r in ranges],
            "warnings": warns,
            "rt_s": round(time.time() - started, 1),
        }
        return {"trimmed": asset, "trim_report": report}