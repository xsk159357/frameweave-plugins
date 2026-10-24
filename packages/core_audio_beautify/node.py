# -*- coding: utf-8 -*-
"""参考音频美化节点（core/audio_beautify，C7）。

链（对齐 08 §S3-T7 契约）：
  1. 校验/上限：输入 < min_seconds（默认 3s）→ 明确报错（invalid: 前缀）；
     输入 > max_seconds（默认 30s）→ 从前端截断（-t），输出 ≤30s；
  2. Demucs 去 BGM：经 core/vocal_separate 子进程（separate_worker.py + run_tracked，
     GPU 崩溃自动 CPU 重试——T3 子进程隔离教训），保留人声轨；
  3. 剪气口 + loudnorm：经 core/audio_master（enable_silence_shorten=剪气口 +
     enable_loudnorm）单次调用；
  4. 成品转换：ffmpeg 重采样到 sample_rate(24000)/channels(1) pcm_s16le wav。

错误码（S13 语义）：逐阶段 status + code（STAGE_DEMUCS_FAILED/STAGE_MASTER_FAILED/
STAGE_FINALIZE_FAILED），阶段失败不阻断（demucs 失败降级原音并告警；master 失败降级
上一阶段产物），致命校验错误才 raise invalid:。demucs/ffmpeg 全部子进程执行（run_tracked
+ 超时），崩溃/超时只影响本节点，不波及主进程。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _ffprobe_bin() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    return r["path"] if r.get("ready") else ""


def _ffmpeg_bin() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    return r["path"] if r.get("ready") else ""


async def _run_proc(ctx: dict, argv: List[str], timeout: float = 300.0) -> Tuple[int, bytes, bytes]:
    """子进程执行（run_tracked 优先；隔离：超时/崩溃只影响本节点）。"""
    import subprocess as _sp
    rt = ctx.get("run_tracked")
    if rt is not None:
        try:
            proc = await rt(argv, timeout=min(timeout, 60.0), text=False,
                            stdout=_sp.PIPE, stderr=_sp.PIPE)
            return (int(getattr(proc, "returncode", -1)),
                    getattr(proc, "stdout", b"") or b"",
                    getattr(proc, "stderr", b"") or b"")
        except Exception as e:  # noqa: BLE001
            return -1, b"", str(e).encode("utf-8", "replace")
    try:
        proc = await asyncio.to_thread(
            lambda: _sp.run(argv, capture_output=True, timeout=timeout))
        return int(proc.returncode or 0), proc.stdout, proc.stderr
    except Exception as e:  # noqa: BLE001
        return -1, b"", str(e).encode("utf-8", "replace")


async def probe_duration(ctx: dict, path: str) -> float:
    fp = _ffprobe_bin()
    if not fp or not os.path.isfile(path):
        return 0.0
    code, out, _err = await _run_proc(
        ctx, [fp, "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", path], timeout=30.0)
    if code != 0 or not out.strip():
        return 0.0
    try:
        txt = out.decode("utf-8", "replace").strip()
        return float(txt.splitlines()[-1])
    except (TypeError, ValueError):
        return 0.0


async def probe_audio_format(ctx: dict, path: str) -> Dict[str, Any]:
    """ffprobe 采样率/声道数/时长（成品格式断言用）。"""
    fp = _ffprobe_bin()
    if not fp or not os.path.isfile(path):
        return {}
    code, out, _err = await _run_proc(
        ctx, [fp, "-v", "error", "-select_streams", "a:0",
              "-show_entries", "stream=sample_rate,channels",
              "-show_entries", "format=duration",
              "-of", "json", path], timeout=30.0)
    if code != 0:
        return {}
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return {}
    streams = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    return {
        "sample_rate": int(streams.get("sample_rate") or 0),
        "channels": int(streams.get("channels") or 0),
        "duration": float(fmt.get("duration") or 0),
    }


async def _invoke(type_id: str, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
    """平台注册表取节点类并进程内执行（对齐团队编排模式；demucs 子进程在节点内部）。"""
    from app.registry import get_class
    node = get_class(type_id)()
    return await node.run(ctx, inputs, params)


async def to_final_wav(ctx: dict, src: str, sample_rate: int, channels: int, out: str) -> str:
    """成品转换：重采样到 sample_rate/channels pcm_s16le wav。"""
    ff = _ffmpeg_bin()
    if not ff:
        raise RuntimeError("ffmpeg 不可用")
    code, _out, err = await _run_proc(
        ctx, [ff, "-v", "error", "-y", "-i", src,
              "-ar", str(sample_rate), "-ac", str(channels),
              "-c:a", "pcm_s16le", out], timeout=180.0)
    if code != 0:
        raise RuntimeError(f"成品转换失败(code={code}): {err.decode('utf-8', 'replace')[-300:]}")
    return out


async def trim_cap(ctx: dict, src: str, max_seconds: float, out: str) -> str:
    """30s 上限截断（前端 -t；时长未超则复制）。"""
    ff = _ffmpeg_bin()
    if not ff:
        raise RuntimeError("ffmpeg 不可用")
    code, _out, err = await _run_proc(
        ctx, [ff, "-v", "error", "-y", "-i", src, "-t", f"{max_seconds:.3f}",
              "-c:a", "pcm_s16le", out], timeout=180.0)
    if code != 0:
        raise RuntimeError(f"截断失败(code={code}): {err.decode('utf-8', 'replace')[-300:]}")
    return out


class AudioBeautifyNode(NodeBase):
    """参考音频美化（C7）。type_id: core/audio_beautify"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/audio_beautify",
            title="参考音频美化（C7）",
            category="音频",
            description="Demucs 去 BGM → 剪气口 → loudnorm → 24k 单声道 wav；30s 上限、<3s 明确报错；子进程隔离",
            inputs=[
                PortSpec("audio", PortType.AUDIO, label="参考音频", required=True),
            ],
            outputs=[
                PortSpec("beautified", PortType.AUDIO, label="美化音频", required=True),
                PortSpec("report", PortType.JSON, label="美化报告", required=True),
            ],
            params=[
                PortSpec("max_seconds", PortType.FLOAT, label="时长上限(秒)", default=30.0, widget="number"),
                PortSpec("min_seconds", PortType.FLOAT, label="最短时长(秒)", default=3.0, widget="number"),
                PortSpec("enable_demucs", PortType.BOOL, label="Demucs 去 BGM", default=True, widget="toggle"),
                PortSpec("model", PortType.STRING, label="Demucs 模型", default="htdemucs",
                         widget="select", options=["htdemucs", "htdemucs_ft", "mdx_extra_q"]),
                PortSpec("device", PortType.STRING, label="设备", default="auto",
                         widget="select", options=["auto", "cuda", "cpu"]),
                PortSpec("python_exe", PortType.STRING, label="Python 解释器", required=False, default="", widget="text"),
                PortSpec("silence_max_ms", PortType.INT, label="气口静音上限(ms)", default=250, widget="number"),
                PortSpec("loudnorm_target_lufs", PortType.FLOAT, label="响度目标(LUFS)", default=-14.0, widget="number"),
                PortSpec("sample_rate", PortType.INT, label="输出采样率", default=24000, widget="number"),
                PortSpec("channels", PortType.INT, label="输出声道", default=1, widget="number"),
                PortSpec("ffmpeg_path", PortType.STRING, label="ffmpeg 路径", required=False, default="", widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        audio_asset = inputs.get("audio")
        if audio_asset is None or not getattr(audio_asset, "path", "") or not os.path.exists(audio_asset.path):
            raise RuntimeError("invalid: 需要可读的参考音频输入")
        workdir = os.path.join(ctx.get("workdir", "."), "beautify_work")
        os.makedirs(workdir, exist_ok=True)

        max_seconds = max(1.0, _num(params.get("max_seconds", 30.0), 30.0))
        min_seconds = max(0.5, _num(params.get("min_seconds", 3.0), 3.0))
        sample_rate = max(8000, int(params.get("sample_rate", 24000) or 24000))
        channels = max(1, min(2, int(params.get("channels", 1) or 1)))

        stages: List[Dict[str, Any]] = []
        warnings: List[str] = []

        def _stage(name: str) -> None:
            stages.append({"name": name, "status": "running"})

        def _finish(name: str, code: str, ok: bool, detail: Any = None) -> None:
            for st in stages:
                if st["name"] == name:
                    st["status"] = "succeeded" if ok else "failed"
                    st["code"] = code
                    if detail is not None:
                        st["detail"] = detail
                    return
            stages.append({"name": name, "status": "succeeded" if ok else "failed",
                           "code": code, "detail": detail})

        async def _guard(name: str, code: str, fn_async):
            try:
                r = await fn_async()
                _finish(name, "OK", True)
                return True, r
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:300]
                warnings.append(f"{name} 失败[{code}]: {msg}")
                _finish(name, code, False, {"error": msg})
                return False, None

        # ---- 校验/上限
        _stage("validate")
        src_path = audio_asset.path
        duration = await probe_duration(ctx, src_path)
        capped = False
        capped_seconds = 0.0
        if duration > 0 and duration < min_seconds:
            raise RuntimeError(
                f"invalid: 参考音频过短（{duration:.1f}s < 最短 {min_seconds:g}s），请提供更长的参考音频")
        if duration > max_seconds:
            capped_path = os.path.join(workdir, "capped.wav")
            await _guard("validate", "STAGE_CAP_FAILED", lambda: trim_cap(
                ctx, src_path, max_seconds, capped_path))
            src_path = capped_path if os.path.isfile(capped_path) else src_path
            capped = True
            capped_seconds = round(max_seconds, 2)
            warnings.append(f"输入 {duration:.1f}s 超过上限 {max_seconds:g}s，已截断到前 {max_seconds:g}s")
        _finish("validate", "VALIDATE_OK", True, {"input_duration": round(duration, 2),
                                                  "capped": capped})
        progress(0.15)

        # ---- Demucs 去 BGM（core/vocal_separate 子进程；失败降级原音）
        _stage("demucs")
        base_asset = audio_asset
        if bool(params.get("enable_demucs", True)):
            src_asset = store.save_asset(
                Asset(id="", kind=PortType.AUDIO.value, path=src_path,
                      meta={"duration": round(duration, 2)}), payload=None)
            dem_ok, dem_out = await _guard("demucs", "STAGE_DEMUCS_FAILED", lambda: _invoke(
                "core/vocal_separate", ctx, {"audio": src_asset},
                {"model": str(params.get("model", "htdemucs") or "htdemucs"),
                 "device": str(params.get("device", "auto") or "auto"),
                 "python_exe": str(params.get("python_exe", "") or ""),
                 "stems": 2, "format": "wav"}))
            if dem_ok and dem_out and dem_out.get("vocals") is not None:
                base_asset = dem_out["vocals"]
        else:
            _finish("demucs", "DEMUCS_SKIP", True, {"note": "enable_demucs=关"})
        progress(0.5)

        # ---- 剪气口 + loudnorm（core/audio_master 单次调用）
        _stage("master")
        mastered_asset = base_asset
        mas_ok, mas_out = await _guard("master", "STAGE_MASTER_FAILED", lambda: _invoke(
            "core/audio_master", ctx, {"audio": base_asset},
            {"enable_silence_shorten": True,
             "silence_max_ms": max(50, int(params.get("silence_max_ms", 250) or 250)),
             "trim_edge_silence": True,
             "enable_loudnorm": True,
             "loudnorm_target_lufs": _num(params.get("loudnorm_target_lufs", -14.0), -14.0),
             "enable_volume_boost": False, "enable_limiter": True,
             "limiter_ceiling_db": -1.0, "output_format": "wav"}))
        if mas_ok and mas_out and mas_out.get("mastered") is not None:
            mastered_asset = mas_out["mastered"]
        progress(0.8)

        # ---- 成品转换（24k 单声道 wav）
        _stage("finalize")
        final_path = os.path.join(workdir, "beautified.wav")
        fin_ok, fin_res = await _guard("finalize", "STAGE_FINALIZE_FAILED", lambda: to_final_wav(
            ctx, mastered_asset.path, sample_rate, channels, final_path))
        if not (fin_ok and fin_res and os.path.isfile(fin_res)):
            # 成品转换失败 → 用 mastered/base 原路径兜底（仍可交付音频资产）
            final_path = mastered_asset.path
        out_fmt = await probe_audio_format(ctx, final_path)
        progress(0.95)

        beautified = store.save_asset(
            Asset(id="", kind=PortType.AUDIO.value, path=final_path,
                  meta={"sample_rate": out_fmt.get("sample_rate", sample_rate),
                        "channels": out_fmt.get("channels", channels),
                        "duration": round(out_fmt.get("duration") or 0, 3),
                        "capped": capped}), payload=None)
        report = {
            "ok": True,
            "tool": "core/audio_beautify",
            "stages": stages,
            "input": {"duration": round(duration, 2), "capped": capped,
                      "capped_seconds": capped_seconds},
            "output": out_fmt,
            "contract": {"sample_rate": sample_rate, "channels": channels,
                         "max_seconds": max_seconds, "min_seconds": min_seconds},
            "warnings": warnings,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"stages": len(stages)}), payload=report)
        progress(1.0)
        return {"beautified": beautified, "report": report_asset}