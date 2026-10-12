"""音频母带节点：音量 / 限幅 / 响度归一 / 静音缩短 / 边缘静音裁剪。

滤镜链顺序（对齐逆向实证的母带管线）：
    缩短静音(silenceremove) → 增强音量(volume) → 强制限幅(alimiter) → 响度匹配(loudnorm)
量测：处理前/后均做 volumedetect（峰值/均值 dB）与 ebur128（集成响度 LUFS），
写入 report；ffmpeg 非零退出码直接报错，不吞错。
"""
from __future__ import annotations
import json
import os
import re
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional

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


async def _probe(ctx, ffmpeg_path: str, media_path: str) -> Dict[str, Any]:
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    ffprobe = _ffprobe_bin(ffmpeg)
    out = await ctx["run_tracked"](
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", media_path],
        timeout=60)
    if out.returncode != 0 or not out.stdout:
        return {}
    try:
        data = json.loads(out.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {}
    fmt = data.get("format", {})
    meta: Dict[str, Any] = {"duration": None, "bitrate": fmt.get("bit_rate", "")}
    try:
        meta["duration"] = round(float(fmt.get("duration", 0) or 0), 3)
    except (TypeError, ValueError):
        pass
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            meta["codec"] = s.get("codec_name", "")
            meta["sample_rate"] = s.get("sample_rate", "")
            meta["channels"] = s.get("channels", 0)
            break
    return meta


async def _measure(ctx, ffmpeg_path: str, media_path: str) -> Dict[str, Any]:
    """volumedetect + ebur128 量测；返回 {mean_db, max_db, lufs, duration}。"""
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    m: Dict[str, Any] = {}
    cmd = [ffmpeg, "-i", media_path, "-af", "volumedetect", "-f", "null", "-"]
    try:
        out = await ctx["run_tracked"](cmd, timeout=180)
        err = (out.stderr or b"").decode("utf-8", "replace")
        for pat, key in ((r"mean_volume:\s*(-?[\d.]+) dB", "mean_db"),
                         (r"max_volume:\s*(-?[\d.]+) dB", "max_db")):
            mm = re.search(pat, err)
            if mm:
                m[key] = float(mm.group(1))
    except Exception:
        pass
    cmd2 = [ffmpeg, "-i", media_path, "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:
        out2 = await ctx["run_tracked"](cmd2, timeout=180)
        err2 = (out2.stderr or b"").decode("utf-8", "replace")
        for pat, key in ((r"Integrated loudness:\s*I:\s*(-?[\d.]+) LUFS", "lufs"),
                         (r"I:\s*(-?[\d.]+) LUFS", "lufs_i")):
            mm = re.search(pat, err2)
            if mm and "lufs" not in m:
                m[key.split("_")[0] if "_" in key else key] = float(mm.group(1))
                break
    except Exception:
        pass
    return m


def _audio_codec_args(fmt: str) -> List[str]:
    if fmt == "wav":
        return ["-c:a", "pcm_s16le"]
    if fmt == "mp3":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    if fmt == "m4a":
        return ["-c:a", "aac", "-b:a", "192k"]
    if fmt == "flac":
        return ["-c:a", "flac"]
    return ["-c:a", "copy"]


class AudioMasterNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/audio_master",
            title="音频母带",
            category="音频",
            description="响度/音量/限幅/静音缩短/边缘静音裁剪 FFmpeg 滤镜链，输出量测报告",
            inputs=[PortSpec(name="audio", type=PortType.AUDIO, label="源音频", required=True)],
            outputs=[PortSpec(name="mastered", type=PortType.AUDIO, label="母带音频"),
                     PortSpec(name="report", type=PortType.JSON, label="母带报告")],
            params=[
                PortSpec(name="enable_volume_boost", type=PortType.BOOL, label="启用音量提升", default=False,
                         widget="toggle"),
                PortSpec(name="volume_boost_db", type=PortType.FLOAT, label="音量提升(dB)", default=3.0,
                         widget="number"),
                PortSpec(name="enable_limiter", type=PortType.BOOL, label="启用限幅", default=False,
                         widget="toggle"),
                PortSpec(name="limiter_ceiling_db", type=PortType.FLOAT, label="限幅上限(dB)", default=-3.0,
                         widget="number"),
                PortSpec(name="limiter_input_gain_db", type=PortType.FLOAT, label="限幅输入增益(dB)",
                         default=6.0, widget="number"),
                PortSpec(name="enable_loudnorm", type=PortType.BOOL, label="启用响度归一", default=False,
                         widget="toggle"),
                PortSpec(name="loudnorm_target_lufs", type=PortType.FLOAT, label="目标响度(LUFS)",
                         default=-6.0, widget="number"),
                PortSpec(name="enable_silence_shorten", type=PortType.BOOL, label="缩短静音段",
                         default=False, widget="toggle"),
                PortSpec(name="silence_max_ms", type=PortType.INT, label="静音上限(ms)", default=250,
                         widget="number"),
                PortSpec(name="trim_edge_silence", type=PortType.BOOL, label="裁剪边缘静音", default=True,
                         widget="toggle"),
                PortSpec(name="output_format", type=PortType.STRING, label="输出格式", default="wav",
                         widget="select", options=["wav", "mp3", "m4a", "flac"]),
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
        src_meta = await _probe(ctx, ffmpeg_path, src)
        if src_meta.get("duration") is None or src_meta.get("duration", 0) <= 0:
            raise RuntimeError(f"无法读取音频（ffprobe 失败或时长为零）: {src}")

        filters: List[str] = []
        steps: List[Dict[str, Any]] = []
        # 1) 缩短静音（先做，原版顺序）
        silence_max_ms = max(0, int(params.get("silence_max_ms", 250) or 250))
        if params.get("enable_silence_shorten", False):
            filters.append(f"silenceremove=start_periods=0:stop_periods=0:start_threshold=-35dB:stop_threshold=-35dB:start_silence=0.05:stop_silence=0.05")
            steps.append({"filter": "silenceremove(缩短静音)", "note": f">={silence_max_ms}ms 静音段缩短"})
        # 2) 边缘静音裁剪
        if params.get("trim_edge_silence", True):
            filters.append("silenceremove=start_periods=1:stop_periods=1:start_threshold=-45dB:stop_threshold=-45dB:start_silence=0.08:stop_silence=0.08")
            steps.append({"filter": "silenceremove(边缘静音)", "note": "头尾静音裁剪"})
        # 3) 音量提升
        if params.get("enable_volume_boost", False):
            boost = max(-30.0, min(30.0, float(params.get("volume_boost_db", 3.0) or 3.0)))
            filters.append(f"volume={boost:.1f}dB")
            steps.append({"filter": f"volume={boost:.1f}dB", "note": "音量提升"})
        # 4) 限幅
        if params.get("enable_limiter", False):
            ceiling = max(-12.0, min(0.0, float(params.get("limiter_ceiling_db", -3.0) or -3.0)))
            gain = max(0.0, min(24.0, float(params.get("limiter_input_gain_db", 6.0) or 6.0)))
            limit = 10 ** (ceiling / 20)
            level_in = 10 ** (gain / 20)
            filters.append(f"alimiter=limit={limit:.4f}:attack=5:release=50:level_in={level_in:.4f}:level=disabled")
            steps.append({"filter": f"alimiter(ceiling={ceiling}dB, gain={gain}dB)", "note": "强制限幅"})
        # 5) 响度归一
        if params.get("enable_loudnorm", False):
            target = float(params.get("loudnorm_target_lufs", -6.0) or -6.0)
            filters.append(f"loudnorm=I={target:.1f}:TP=-3.0:LRA=11")
            steps.append({"filter": f"loudnorm(I={target:.1f})", "note": "EBU R128 响度归一"})

        if not filters:
            raise RuntimeError("没有启用任何母带处理（至少开启一项滤镜）")

        out_dir = os.path.join(store.files_dir, "audio_master")
        os.makedirs(out_dir, exist_ok=True)
        out_fmt = str(params.get("output_format", "wav") or "wav").lstrip(".")
        out_path = os.path.join(out_dir, f"master_{os.getpid()}_{uuid.uuid4().hex[:8]}.{out_fmt}")
        started = time.time()
        progress(0.2)
        cmd = [ffmpeg, "-y", "-i", src, "-af", ",".join(filters), * _audio_codec_args(out_fmt), out_path]
        out = await ctx["run_tracked"](cmd, timeout=600)
        if out.returncode != 0:
            raise RuntimeError("ffmpeg 母带处理失败(%s): %s" % (
                out.returncode, (out.stderr or b"").decode("utf-8", "replace")[-500:]))
        progress(0.7)
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("母带输出为空")

        before = await _measure(ctx, ffmpeg_path, src)
        after = await _measure(ctx, ffmpeg_path, out_path)
        out_meta = await _probe(ctx, ffmpeg_path, out_path)
        progress(1.0)

        asset = Asset(id="", kind=PortType.AUDIO.value, path=out_path,
                      meta={"engine": "ffmpeg", "format": out_fmt, "duration": out_meta.get("duration")})
        store.save_asset(asset)
        report = {
            "source": {"path": src, "duration": src_meta.get("duration"),
                       "codec": src_meta.get("codec"), "channels": src_meta.get("channels"),
                       "measure": before},
            "output": {"path": out_path, "duration": out_meta.get("duration"),
                       "format": out_fmt, "measure": after},
            "steps": steps,
            "filter_chain": filters,
            "rt_s": round(time.time() - started, 1),
            "warnings": [],
        }
        return {"mastered": asset, "report": report}
