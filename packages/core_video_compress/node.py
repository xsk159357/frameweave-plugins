# -*- coding: utf-8 -*-
"""视频压缩转码节点：对齐逆向还原的 video_compressor 语义（r4 §5）并修复其缺陷。

与原版相比：
- 修复 B11：硬件编码失败（驱动/NVENC 初始化错误）自动回退 libx265 软编并写 warning，
  不再静默失败；原版固定 `-hwaccel cuda` 无重试。
- 修复弱码率源：maxrate=原码率×0.7 与 bufsize=2× 始终生效（防越压越大）。
- 进度：ffmpeg -progress 轮询上报 0→1；输出 ffprobe 复核宽高/码率/时长。
- 固定序列：-y、-fflags +genpts+igndts、-map_metadata -1（参数可关）。
- HEVC 系（libx265/hevc_nvenc）CRF 自动 +5 偏移（与 r4 实测一致）。
- 编码器探测缓存于模块级，避免每节点重复跑 `ffmpeg -encoders`。
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


def _which_ffmpeg() -> str:
    """统一依赖解析层。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    if r.get("ready"):
        return r["path"]
    raise RuntimeError(deps.coded_message("binary_missing", "ffmpeg", r.get("repair_hint")))


def _which_ffprobe() -> str:
    """统一依赖解析层。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    if r.get("ready"):
        return r["path"]
    raise RuntimeError(deps.coded_message("binary_missing", "ffprobe", r.get("repair_hint")))


_AVAILABLE_ENCODERS: Optional[List[str]] = None
_ENCODER_DETECT_LOCK = asyncio.Lock()


async def probe_media(ctx, path: str) -> Dict[str, Any]:
    """ffprobe 取视频流/封装元数据（包内自包含实现）。失败返回部分字段 + warning。"""
    cmd = [_which_ffprobe(), "-v", "error", "-show_entries",
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
            "video_duration": float(v.get("duration") or 0.0),
            "video_bit_rate": float(v.get("bit_rate") or 0.0),
        })
    out["duration"] = float(fmt.get("duration") or v.get("duration") or 0.0)
    out["bit_rate"] = float(fmt.get("bit_rate") or 0.0)
    out["size"] = float(fmt.get("size") or 0.0)
    return out


async def _detect_encoders(ctx) -> List[str]:
    """探测可用编码器（hevc_nvenc > h264_qsv > h264_amf > libx265 …）。

    ffmpeg -encoders 输出可达数百 KB，管道会写满死锁（engine.run_tracked
    先等退出再读管道），因此 stdout/stderr 重定向到临时文件后读取。
    """
    global _AVAILABLE_ENCODERS
    if _AVAILABLE_ENCODERS is not None:
        return _AVAILABLE_ENCODERS
    async with _ENCODER_DETECT_LOCK:
        if _AVAILABLE_ENCODERS is not None:
            return _AVAILABLE_ENCODERS
        import tempfile
        with tempfile.TemporaryDirectory(prefix="fw_enc_") as td:
            out_f, err_f = os.path.join(td, "o.log"), os.path.join(td, "e.log")
            with open(out_f, "wb") as fo, open(err_f, "wb") as fe:
                proc = await ctx["run_tracked"]([_which_ffmpeg(), "-hide_banner", "-encoders"],
                                                 timeout=60, stdout=fo, stderr=fe)
            text = ""
            for p in (out_f, err_f):
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        text += f.read()
                except OSError:
                    pass
        names = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] and ("nvenc" in parts[1] or "qsv" in parts[1] or
                                                 "amf" in parts[1] or parts[1].startswith("libx") or
                                                 parts[1].startswith("libsvt")):
                names.append(parts[1])
        _AVAILABLE_ENCODERS = names
        return names


_HEVC_FAMILY = {"libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf"}
_HW_PREF = ["hevc_nvenc", "h264_nvenc", "h264_qsv", "h264_amf", "av1_nvenc", "av1_qsv"]


def _effective_crf(encoder: str, crf: int) -> int:
    """HEVC 同 CRF 比 H.264 输出更大，偏移 +5（r4 §5.4 实测）。"""
    if encoder in _HEVC_FAMILY:
        return crf + 5
    return crf


class VideoCompressNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/video_compress",
            title="视频压缩转码",
            category="输出",
            description="HEVC/H.264 压缩转码：硬件加速探测与自动软编回退、码率上限=原码率×0.7、HEVC CRF 偏移+5、进度与统计报告。",
            inputs=[PortSpec(name="video", type=PortType.VIDEO, label="源视频", required=True)],
            outputs=[PortSpec(name="compressed", type=PortType.VIDEO, label="压缩后视频"),
                     PortSpec(name="stats", type=PortType.JSON, label="压缩统计", required=False)],
            params=[
                PortSpec(name="encoder", type=PortType.STRING, label="编码器", default="auto",
                         widget="select", options=["auto", "hevc_nvenc", "h264_nvenc", "libx265", "libx264", "libsvtav1"]),
                PortSpec(name="crf", type=PortType.INT, label="CRF 质量", default=23, widget="number"),
                PortSpec(name="preset", type=PortType.STRING, label="预设", default="medium", widget="text"),
                PortSpec(name="max_width", type=PortType.INT, label="最大宽度", default=0, widget="number"),
                PortSpec(name="max_height", type=PortType.INT, label="最大高度", default=0, widget="number"),
                PortSpec(name="fps", type=PortType.INT, label="输出帧率", default=0, widget="number"),
                PortSpec(name="audio_bitrate", type=PortType.STRING, label="音频码率", default="192k", widget="text"),
                PortSpec(name="hardware_accel", type=PortType.BOOL, label="硬件加速优先", default=True, widget="toggle"),
                PortSpec(name="remove_metadata", type=PortType.BOOL, label="去除元数据", default=True, widget="toggle"),
                PortSpec(name="output_name", type=PortType.STRING, label="输出文件名", default="compressed", widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video = inputs.get("video")
        if video is None or not getattr(video, "path", "") or not os.path.exists(video.path):
            raise RuntimeError("需要源视频输入")
        store = ctx["store"]
        progress = ctx.get("progress")

        if progress:
            progress(0.03)
        probe = await probe_media(ctx, video.path)
        orig_bitrate = float(probe.get("bit_rate") or 0.0)
        orig_size = int(probe.get("size") or os.path.getsize(video.path))
        if progress:
            progress(0.08)

        encoder_sel = str(params.get("encoder", "auto") or "auto")
        crf = int(params.get("crf", 23) or 23)
        preset = str(params.get("preset", "medium") or "medium")
        max_width = int(params.get("max_width", 0) or 0)
        max_height = int(params.get("max_height", 0) or 0)
        fps = int(params.get("fps", 0) or 0)
        audio_bitrate = str(params.get("audio_bitrate", "192k") or "192k")
        hardware = bool(params.get("hardware_accel", True))
        remove_meta = bool(params.get("remove_metadata", True))
        out_name = str(params.get("output_name", "compressed")) or "compressed"
        out_name = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in out_name)

        # ---- 编码器选择 ----
        avail = await _detect_encoders(ctx)
        encoder = encoder_sel
        if encoder_sel == "auto":
            encoder = "libx265"
            if hardware:
                for cand in _HW_PREF:
                    if cand in avail:
                        encoder = cand
                        break
        elif encoder_sel.startswith("h264") and encoder_sel not in avail and "libx264" in avail:
            pass  # 保持用户选择，构建失败时统一回退 libx265
        elif encoder_sel.startswith("hevc_nvenc") and encoder_sel not in avail and "libx265" in avail:
            pass
        if encoder not in avail:
            fallback = "libx265" if "libx265" in avail else ("libx264" if "libx264" in avail else encoder)
            if fallback != encoder:
                encoder = fallback

        # ---- CRF / 码率 ----
        eff_crf = _effective_crf(encoder, crf)
        maxrate = int(orig_bitrate * 0.7) if orig_bitrate > 0 else 0
        bufsize = maxrate * 2

        vf = None
        if max_width and max_height:
            vf = f"scale={max_width}:{max_height}"
        elif max_width:
            vf = f"scale={max_width}:-2"
        elif max_height:
            vf = f"scale=-2:{max_height}"
        if fps > 0:
            vf = (vf + "," if vf else "") + f"fps={fps}"

        is_hw = any(h in encoder for h in ("nvenc", "qsv", "amf"))
        cmd = [_which_ffmpeg(), "-y", "-fflags", "+genpts+igndts"]
        if is_hw:
            cmd += ["-hwaccel", "cuda"]
        cmd += ["-i", video.path]
        cmd += ["-c:v", encoder]
        if is_hw:
            cmd += ["-rc", "vbr", "-cq", str(eff_crf), "-preset", str(preset or "p4")]
        else:
            cmd += ["-crf", str(eff_crf), "-preset", str(preset or "medium")]
        if maxrate > 0:
            cmd += ["-maxrate", str(maxrate), "-bufsize", str(bufsize)]
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
        if remove_meta:
            cmd += ["-map_metadata", "-1"]

        workdir = ctx["workdir"]
        os.makedirs(workdir, exist_ok=True)
        out_mp4 = os.path.join(workdir, out_name + ".mp4")
        cmd += [out_mp4]

        warnings: List[str] = []
        final_encoder = encoder
        rc, err = await self._run_encode(ctx, cmd, workdir, progress, probe)
        if rc != 0 and is_hw:
            # B11 修复：硬件失败自动回退软编
            if "libx265" in avail or "libx264" in avail:
                sw_enc = "libx265" if "libx265" in avail else "libx264"
                sw_crf = _effective_crf(sw_enc, crf)
                sw_cmd = [_which_ffmpeg(), "-y", "-fflags", "+genpts+igndts", "-i", video.path,
                          "-c:v", sw_enc, "-crf", str(sw_crf), "-preset", str(preset or "medium")]
                if maxrate > 0:
                    sw_cmd += ["-maxrate", str(maxrate), "-bufsize", str(bufsize)]
                if vf:
                    sw_cmd += ["-vf", vf]
                sw_cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
                if remove_meta:
                    sw_cmd += ["-map_metadata", "-1"]
                if os.path.exists(out_mp4):
                    os.remove(out_mp4)
                sw_cmd += [out_mp4]
                warnings.append(f"硬件编码 {encoder} 失败，已回退软编 {sw_enc}: {err[-300:]}")
                rc2, err2 = await self._run_encode(ctx, sw_cmd, workdir, progress, probe)
                rc, err, final_encoder = rc2, err2, sw_enc
        if rc != 0:
            raise RuntimeError(f"压缩失败: {(err or '')[-1200:]}")

        if progress:
            progress(0.95)
        verify = await probe_media(ctx, out_mp4)
        new_size = int(os.path.getsize(out_mp4)) if os.path.exists(out_mp4) else 0
        ratio = (1.0 - new_size / orig_size) if orig_size > 0 else 0.0
        stats = {
            "status": "ok",
            "output": out_mp4,
            "original_size": orig_size,
            "compressed_size": new_size,
            "compression_ratio": round(ratio, 4),
            "saved_bytes": max(orig_size - new_size, 0),
            "encoder": final_encoder,
            "crf": eff_crf,
            "maxrate": maxrate,
            "bufsize": bufsize,
            "vf": vf,
            "out_width": int(verify.get("width") or 0),
            "out_height": int(verify.get("height") or 0),
            "out_fps": verify.get("fps"),
            "out_duration": round(float(verify.get("duration") or 0), 3),
            "out_bitrate": int(float(verify.get("bit_rate") or 0)),
            "warnings": warnings,
            "command_summary": {
                "encoder": final_encoder, "preset": preset, "crf": eff_crf,
                "maxrate_kbps": round(maxrate / 1000.0, 1),
                "hardware": is_hw,
            },
        }
        asset = store.save_asset(
            Asset(id="", kind=PortType.VIDEO.value, path=out_mp4,
                  meta={"width": int(verify.get("width") or 0),
                        "height": int(verify.get("height") or 0),
                        "encoder": final_encoder, "compression_ratio": round(ratio, 4),
                        "compressed_size": new_size, "original_size": orig_size}))
        stats_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"status": "ok"}), payload=stats)
        if progress:
            progress(1.0)
        return {"compressed": asset, "stats": stats_asset}

    async def _run_encode(self, ctx, cmd, workdir, progress, probe) -> tuple:
        """执行 ffmpeg 并轮询 -progress 上报进度。返回 (returncode, stderr tail)。"""
        log_err = os.path.join(workdir, "compress_err.log")
        prog_file = os.path.join(workdir, "compress.progress")
        for p in (log_err, prog_file):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        total_us = max(float(probe.get("duration") or 1.0) * 1_000_000.0, 1.0)
        with open(log_err, "wb") as fe:
            proc = ctx["spawn_tracked"](cmd + ["-progress", prog_file],
                                        stdout=open(os.devnull, "wb"), stderr=fe)
        last = 0.15
        while proc.poll() is None:
            if progress:
                p = self._read_progress(prog_file, total_us)
                if p > last:
                    last = min(p, 0.9)
                    progress(last)
            await asyncio.sleep(0.4)
        err = ""
        try:
            with open(log_err, "r", encoding="utf-8", errors="replace") as f:
                err = f.read()
        except OSError:
            pass
        return proc.returncode, err

    @staticmethod
    def _read_progress(prog_file: str, total_us: float) -> float:
        try:
            with open(prog_file, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return 0.0
        out_us = 0.0
        for line in text.splitlines():
            if line.startswith("out_time_us="):
                try:
                    out_us = float(line.split("=", 1)[1])
                except ValueError:
                    pass
        if "progress=end" in text:
            return 1.0
        return min(out_us / total_us, 1.0)
