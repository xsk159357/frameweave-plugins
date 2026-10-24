"""人声分离节点：Demucs htdemucs 人声/伴奏分离。

设计：
- 用 tracked 子进程运行包内 separate_worker.py（隔离 CUDA/torch 崩溃，任务取消可杀进程树）。
- GPU 崩溃/不可用自动以 CPU 重试一次（device=auto 时）。
- demucs 未安装时报明确错误并给出安装方式，绝不伪造分离结果。
- 解释器（FWA-003）：demucs 只能跑在**外部** Python 里。安装版内置的是 frameweave-backend.exe，
  拿它当 python 会再拉起一个后端（端口冲突/自检失败）。所以 python_exe 留空时按
  参数→FRAMEWEAVE_DEMUCS_PYTHON→FRAMEWEAVE_PYTHON→(源码态)sys.executable→PATH python 解析，
  冻结态解不到就报 binary_missing，并在跑 worker 之前先探一次 `import demucs`。
- 输入二选一：audio（直接）/ video（先 ffmpeg 提取音轨）。
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "separate_worker.py")

# demucs 侧车解释器的环境变量（按优先级）。FRAMEWEAVE_PYTHON 作为通用回落，
# 这样用户已有的「外置 python」约定在多个节点间一致。
_PY_ENV_KEYS = ("FRAMEWEAVE_DEMUCS_PYTHON", "FRAMEWEAVE_PYTHON")


def _resolve_ffmpeg(ffmpeg_path: str = "") -> str:
    """统一依赖解析层。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", requested=ffmpeg_path, env=os.environ,
                            params={"ffmpeg_path": ffmpeg_path})
    return r["path"] if r["ready"] else "ffmpeg"


# ---------------------------------------------------------------- demucs 解释器解析（FWA-003）
# 改前：python_exe 参数留空 → sys.executable。源码态没问题（后端本身就是 python），
# 但**安装版**里 sys.executable 是 frameweave-backend.exe：拿它执行
# [exe, "-u", separate_worker.py, json] 等于再拉起一个后端进程（占 8788 / 自检失败），
# 而报错落在「人声分离失败」上，用户完全看不出根因。
# 改后：冻结态只认真外部解释器，解析不到就给 binary_missing（点名怎么装、怎么配）。
def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _probe_demucs(python_exe: str, timeout: float = 90.0) -> Any:
    """探测解释器能否 import demucs。返回 (ok, 摘要)；摘要成功时是 Python 版本号。"""
    try:
        p = subprocess.run(
            [python_exe, "-c", "import sys, demucs; print(sys.version.split()[0])"],
            capture_output=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    if p.returncode == 0 and out:
        return True, out.splitlines()[-1]
    tail = [ln for ln in (err or out).splitlines() if ln.strip()]
    return False, (tail[-1] if tail else f"exit={p.returncode}")


def resolve_demucs_python(param_value: str = "") -> Dict[str, Any]:
    """解析「装有 demucs 的外部解释器」。返回 {path, source, candidates, frozen}。

    顺序：节点参数 python_exe → FRAMEWEAVE_DEMUCS_PYTHON → FRAMEWEAVE_PYTHON
          → 非冻结进程的 sys.executable（源码态后端本身就是 python）
          → PATH 里的 python / python3
    冻结态**不**回落 sys.executable —— 那是 frameweave-backend.exe，不是解释器。
    """
    cands = []
    src = ""
    p = str(param_value or "").strip()
    if p:
        cands.append(("param", p))
    for k in _PY_ENV_KEYS:
        v = str(os.environ.get(k, "") or "").strip()
        if v:
            cands.append((f"env:{k}", v))
    if not _is_frozen():
        cands.append(("sys.executable", sys.executable))
    for name in ("python", "python3"):
        w = shutil.which(name)
        if w:
            cands.append((f"path:{name}", w))

    tried = [{"source": s, "path": v} for s, v in cands]
    for s, v in cands:
        if v and os.path.exists(v):
            return {"path": v, "source": s, "candidates": tried, "frozen": _is_frozen()}
    return {"path": "", "source": "", "candidates": tried, "frozen": _is_frozen()}


def hint_python_missing(py: Dict[str, Any]) -> str:
    """解不出解释器时的可读提示（测试直接调用它断言文案，避免文案与代码漂移）。"""
    return ("人声分离需要外部 Python 解释器：安装版内置的 frameweave-backend.exe 不是 python，"
            "不能拿来跑 demucs。二选一：① 在装有 demucs 的 Python 里执行 pip install demucs，"
            "再把该解释器绝对路径填到节点参数 python_exe（如 C:\\Python311\\python.exe）；"
            "② 设置环境变量 FRAMEWEAVE_DEMUCS_PYTHON 指向它，重启应用后生效。"
            "源码态（后端本身是 Python）会自动使用当前解释器。"
            f"已尝试：{py.get('candidates')}")


def hint_demucs_missing(python_exe: str, source: str, detail: str) -> str:
    return (f"解释器 {python_exe}（来源 {source}）无法导入 demucs：{detail}。"
            "请在该解释器里 pip install demucs（会一并装 torch），"
            "或用节点参数 python_exe / 环境变量 FRAMEWEAVE_DEMUCS_PYTHON "
            "指向已装好 demucs 的解释器——注意安装版内置的后端 exe 不能当解释器。")



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
                         widget="text",
                         description="装有 demucs 的外部 Python 绝对路径（如 C:\\Python311\\python.exe）。"
                                     "留空则依次尝试环境变量 FRAMEWEAVE_DEMUCS_PYTHON / FRAMEWEAVE_PYTHON、"
                                     "PATH 里的 python；安装版内置的 frameweave-backend.exe 不能当解释器，"
                                     "解析不到会报 binary_missing 并提示怎么装"),
                PortSpec(name="format", type=PortType.STRING, label="输出格式", default="wav",
                         widget="select", options=["wav", "flac", "mp3(320k)"]),
                PortSpec(name="ffmpeg_path", type=PortType.STRING, label="ffmpeg 路径", default="",
                         widget="text"),
            ],
            version="1.1.1",
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
        # G20：mp3(320k) → worker 出 wav，节点层 ffmpeg 转 mp3 320kbps
        worker_fmt = "wav" if out_fmt == "mp3(320k)" else out_fmt
        out_fmt = "mp3" if out_fmt == "mp3(320k)" else out_fmt
        python_exe = str(params.get("python_exe", "") or "").strip()
        # FWA-003：绝不把 sys.executable 当默认解释器（安装版那是 frameweave-backend.exe）。
        py = resolve_demucs_python(python_exe)
        if not py["path"]:
            from app.plugin import deps
            raise RuntimeError(deps.coded_message(
                "binary_missing", "python", hint=hint_python_missing(py)))
        python_exe = py["path"]
        py_ok, py_detail = _probe_demucs(python_exe)
        if not py_ok:
            from app.plugin import deps
            raise RuntimeError(deps.coded_message(
                "dependency_missing", "demucs",
                hint=hint_demucs_missing(python_exe, py["source"], py_detail)))

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
                    "stems": stems, "device": dev, "format": worker_fmt}
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

        vocals_path = os.path.join(out_dir, "vocals." + worker_fmt)
        accomp_path = os.path.join(out_dir, "accompaniment." + worker_fmt)
        missing = [p for p in (vocals_path, accomp_path) if not os.path.exists(p)]
        if missing:
            raise RuntimeError(f"分离产物缺失: {missing}（stems={stems} 时按模型源轨命名）")

        # G20：worker 出 wav → 转 mp3 320k（ffmpeg 二合一）
        if out_fmt == "mp3":
            for src_p in (vocals_path, accomp_path):
                dst_p = os.path.splitext(src_p)[0] + ".mp3"
                ff = _resolve_ffmpeg(ffmpeg_path)
                out = await ctx["run_tracked"](
                    [ff, "-y", "-i", src_p, "-codec:a", "libmp3lame", "-b:a", "320k", dst_p],
                    timeout=600)
                if out.returncode != 0 or not os.path.exists(dst_p):
                    raise RuntimeError("mp3(320k) 转码失败: %s" %
                                      (out.stderr or b"").decode("utf-8", "replace")[-300:])
            vocals_path = os.path.splitext(vocals_path)[0] + ".mp3"
            accomp_path = os.path.splitext(accomp_path)[0] + ".mp3"

        vocals_asset = Asset(id="", kind=PortType.AUDIO.value, path=vocals_path,
                             meta={"model": model, "device": used_device, "stems": stems})
        accomp_asset = Asset(id="", kind=PortType.AUDIO.value, path=accomp_path,
                             meta={"model": model, "device": used_device, "stems": stems})
        store.save_asset(vocals_asset)
        store.save_asset(accomp_asset)
        progress(1.0)
        report = {"model": model, "stems": stems, "device": used_device,
                  "format": out_fmt,
                  "python_exe": python_exe, "source": src_kind,
                  # FWA-003：解释器来源与版本进报告（旧工作流只读 model/device/rt_s，新增字段向后兼容）
                  "python_source": py["source"], "python_version": py_detail,
                  "dependency": {
                      "python": {"ready": True, "path": python_exe, "source": py["source"],
                                 "version": py_detail},
                      "demucs": {"ready": py_ok, "python_exe": python_exe},
                  },
                  "rt_s": round(time.time() - started, 1),
                  "warnings": [] if len(attempt_devices) == 1 or used_device == attempt_devices[0]
                             else [f"GPU 失败已降级 CPU: {last_err[:200]}"]}
        return {"vocals": vocals_asset, "accompaniment": accomp_asset, "report": report}
