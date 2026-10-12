"""人声分离节点：Demucs htdemucs 人声/伴奏分离。

设计：
- 用 tracked 子进程运行包内 separate_worker.py（隔离 CUDA/torch 崩溃，任务取消可杀进程树）。
- GPU 崩溃/不可用自动以 CPU 重试一次（device=auto 时）。
- demucs 未安装时报明确错误并给出安装方式，绝不伪造分离结果。
- 输入二选一：audio（直接）/ video（先 ffmpeg 提取音轨）。
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import time
import uuid
from typing import Any, Dict, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "separate_worker.py")


def _resolve_ffmpeg(ffmpeg_path: str = "") -> str:
    """统一依赖解析层。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", requested=ffmpeg_path, env=os.environ,
                            params={"ffmpeg_path": ffmpeg_path})
    return r["path"] if r["ready"] else "ffmpeg"


class VocalSeparateNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/vocal_separate",
            title="人声分离",
            category="音频",
            description="Demucs htdemucs 人声/伴奏分离（tracked 子进程，GPU 崩溃自动 CPU 重试）",
            inputs=[
                PortSpec(name="audio", type=PortType.AUDIO, label="音频(可选)", required=False),
                PortSpec(name="video", type=PortType.VIDEO, label="视频(可选)", required=False),
            ],
            outputs=[
                PortSpec(name="vocals", type=PortType.AUDIO, label="人声"),
                PortSpec(name="accompaniment", type=PortType.AUDIO, label="伴奏"),
                PortSpec(name="report", type=PortType.JSON, label="分离报告"),
            ],
            params=[
                PortSpec(name="model", type=PortType.STRING, label="模型", default="htdemucs",
                         widget="select", options=["htdemucs", "htdemucs_ft", "mdx_extra_q"]),
                PortSpec(name="stems", type=PortType.INT, label="分轨数", default=2, widget="number"),
                PortSpec(name="device", type=PortType.STRING, label="设备", default="auto",
                         widget="select", options=["auto", "cuda", "cpu"]),
                PortSpec(name="python_exe", type=PortType.STRING, label="Python 解释器", default="",
                         widget="text", description="装有 demucs 的解释器；留空用当前进程"),
                PortSpec(name="format", type=PortType.STRING, label="输出格式", default="wav",
                         widget="select", options=["wav", "flac"]),
                PortSpec(name="ffmpeg_path", type=PortType.STRING, label="ffmpeg 路径", default="",
                         widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        audio = inputs.get("audio")
        video = inputs.get("video")
        src_path = ""
        src_kind = ""
        if audio is not None and getattr(audio, "path", ""):
            src_path = audio.path
            src_kind = "audio"
        elif video is not None and getattr(video, "path", ""):
            src_path = video.path
            src_kind = "video"
        if not src_path or not os.path.exists(src_path):
            raise RuntimeError("需要音频或视频输入（audio/video 二选一）")
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        ffmpeg_path = str(params.get("ffmpeg_path", "") or "")
        model = str(params.get("model", "htdemucs") or "htdemucs")
        try:
            stems = max(2, min(4, int(params.get("stems", 2) or 2)))
        except (TypeError, ValueError):
            stems = 2
        device = str(params.get("device", "auto") or "auto")
        out_fmt = str(params.get("format", "wav") or "wav").lstrip(".")
        python_exe = str(params.get("python_exe", "") or "").strip()
        if not python_exe:
            python_exe = sys.executable

        work_dir = os.path.join(store.files_dir, "vocal_sep")
        os.makedirs(work_dir, exist_ok=True)
        run_id = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
        out_dir = os.path.join(work_dir, run_id)
        os.makedirs(out_dir, exist_ok=True)

        # 视频输入：先提取音轨
        input_audio = src_path
        if src_kind == "video":
            input_audio = os.path.join(out_dir, "src_audio.wav")
            ffmpeg = _resolve_ffmpeg(ffmpeg_path)
            out = await ctx["run_tracked"](
                [ffmpeg, "-y", "-i", src_path, "-vn", "-ac", "2", "-ar", "44100",
                 "-c:a", "pcm_s16le", input_audio], timeout=600)
            if out.returncode != 0:
                raise RuntimeError("从视频提取音轨失败: %s" %
                                  (out.stderr or b"").decode("utf-8", "replace")[-300:])
        progress(0.1)

        def _worker_args(dev: str) -> list:
            spec = {"input": input_audio, "out_dir": out_dir, "model": model,
                    "stems": stems, "device": dev, "format": out_fmt}
            return [python_exe, "-u", _WORKER, json.dumps(spec, ensure_ascii=False)]

        attempt_devices = ["cuda", "cpu"] if device == "auto" else [device]
        last_err = ""
        started = time.time()
        used_device = ""
        for i, dev in enumerate(attempt_devices):
            try:
                out = await ctx["run_tracked"](_worker_args(dev), timeout=3600)
            except Exception as e:
                last_err = str(e)
                continue
            if out.returncode == 0:
                used_device = dev
                break
            last_err = (out.stderr or b"").decode("utf-8", "replace")[-800:]
            progress(0.2 + 0.3 * (i + 1) / len(attempt_devices))

        if not used_device:
            from app.plugin import deps
            raise RuntimeError(deps.coded_message(
                "dependency_missing", "demucs",
                hint=f"人声分离失败（模型 {model}，设备 {device}）：{last_err}。"
                     "请确认 python_exe 指向装有 demucs 的解释器（pip install demucs），"
                     "模型首次使用会自动下载。"))
        progress(0.85)

        vocals_path = os.path.join(out_dir, "vocals." + out_fmt)
        accomp_path = os.path.join(out_dir, "accompaniment." + out_fmt)
        missing = [p for p in (vocals_path, accomp_path) if not os.path.exists(p)]
        if missing:
            raise RuntimeError(f"分离产物缺失: {missing}（stems={stems} 时按模型源轨命名）")

        vocals_asset = Asset(id="", kind=PortType.AUDIO.value, path=vocals_path,
                             meta={"model": model, "device": used_device, "stems": stems})
        accomp_asset = Asset(id="", kind=PortType.AUDIO.value, path=accomp_path,
                             meta={"model": model, "device": used_device, "stems": stems})
        store.save_asset(vocals_asset)
        store.save_asset(accomp_asset)
        progress(1.0)
        report = {"model": model, "stems": stems, "device": used_device,
                  "python_exe": python_exe, "source": src_kind,
                  "rt_s": round(time.time() - started, 1),
                  "warnings": [] if len(attempt_devices) == 1 or used_device == attempt_devices[0]
                             else [f"GPU 失败已降级 CPU: {last_err[:200]}"]}
        return {"vocals": vocals_asset, "accompaniment": accomp_asset, "report": report}
