"""镜头切分节点：把视频按画面突变切成场景段。

降级链（产品可用性的一部分，不是防御性代码——没装 ffmpeg、没有独显的机器也要能跑）：
  1. ffmpeg scene 检测：画面差异超过阈值处切断，最准
  2. ffprobe 取时长 + 固定间隔切分：ffmpeg 不可用或整片没有可识别切点时
  3. 两者都不可用 ⇒ 明确报错

第 3 条是刻意的：不返回「空场景」假装成功。返回空列表时下游会产出空时间线，
用户看到的是「导出成功但没画面」，比直接报错难查十倍。

**切分与描述必须解耦**（jieshuomao 踩过的坑）：本节点只输出时间边界，不生成任何
内容描述。下游 scene.describe 拿到这些边界后只能读，不许改——它一旦能改边界，
时间轴和画面就会对不上，而且错得很隐蔽。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from app import media_tools
from app.assets import Asset
from app.nodespec import NodeBase, PortType

_FFMPEG_TIMEOUT = 900     # 长片切分可能跑很久；超时视为不可用并降级
_PROBE_TIMEOUT = 30
_PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")


def _run(cmd: List[str], timeout: int) -> Optional[subprocess.CompletedProcess]:
    """跑外部命令；任何执行期异常都折叠成 None（由调用方决定降级）。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None


def _duration(path: str) -> float:
    return media_tools.duration(path)


def _cut_long(start: float, end: float, max_sec: float) -> List[Tuple[float, float]]:
    """把一段强切到不超过 max_sec。等分而不是从前往后切，避免末段特别短。"""
    span = end - start
    if span <= max_sec or max_sec <= 0:
        return [(start, end)]
    n = int(span // max_sec) + (1 if span % max_sec > 1e-9 else 0)
    step = span / n
    return [(start + k * step, start + (k + 1) * step) for k in range(n)]


def _build(cuts: List[float], total: float, min_sec: float,
           max_sec: float) -> List[Dict[str, Any]]:
    """切点 → 合法段：丢弃过近切点、补齐片尾、强切过长。"""
    bounds: List[float] = [0.0]
    for c in sorted(set(cuts)):
        if c <= 0:
            continue
        if total > 0 and c >= total - 0.05:
            continue                      # 片尾切点没有意义，由下面的 total 兜住
        if c - bounds[-1] < min_sec:
            continue                      # 太近：并进前一段
        bounds.append(c)
    bounds.append(total if total > 0 else bounds[-1] + max_sec)

    segs: List[Dict[str, Any]] = []
    for i in range(len(bounds) - 1):
        for s, e in _cut_long(bounds[i], bounds[i + 1], max_sec):
            if e - s <= 1e-6:
                continue
            segs.append({"index": len(segs), "start": round(s, 3),
                         "end": round(e, 3), "duration": round(e - s, 3)})
    return segs


def _detect(path: str, threshold: float, min_sec: float,
            max_sec: float) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """ffmpeg 场景检测。返回 (段列表 或 None, 原因)。"""
    exe = media_tools.ffmpeg_path()
    if not exe:
        return None, "未找到 ffmpeg（内置目录 / PATH / imageio_ffmpeg 都没有）"
    p = _run([exe, "-hide_banner", "-nostats", "-i", path,
              "-filter:v", "select='gt(scene,%g)',showinfo" % threshold,
              "-an", "-f", "null", "-"], _FFMPEG_TIMEOUT)
    if p is None:
        return None, "ffmpeg 执行失败或超时"
    times = [float(x) for x in _PTS_RE.findall(p.stderr or "")]
    if not times:
        # 一帧都没选中有两种完全不同的原因，必须分开说：
        #   ① 视频确实没有超过阈值的画面变化（静态镜头、纯色）—— 正常，降级即可
        #   ② 视频解不开 —— 异常，要让用户知道是文件的问题
        # ffmpeg 对 ① 也返回非 0（stderr 里是 "Output file is empty"）。只看返回码
        # 会把正常情况报成「视频无法解码」：用户拿一段静态镜头来切，被告知文件坏了。
        err = p.stderr or ""
        benign = ("No filtered frames" in err) or ("Output file is empty" in err)
        if p.returncode != 0 and not benign:
            return None, "ffmpeg 返回码 %d（视频可能无法解码）" % p.returncode
        return None, "整片没有超过阈值 %g 的画面变化" % threshold
    total = _duration(path)
    segs = _build(times, total, min_sec, max_sec)
    if not segs:
        return None, "切点全部被最短镜头约束合并"
    return segs, "ffmpeg"


def _fixed(total: float, interval: float, max_sec: float) -> List[Dict[str, Any]]:
    """固定间隔切分：完全不看画面，同一输入每次结果一致（可复现）。"""
    interval = max(interval, 0.5)
    segs: List[Dict[str, Any]] = []
    t = 0.0
    while t < total - 1e-6:
        e = min(total, t + interval)
        for s, ee in _cut_long(t, e, max_sec):
            if ee - s <= 1e-6:
                continue
            segs.append({"index": len(segs), "start": round(s, 3),
                         "end": round(ee, 3), "duration": round(ee - s, 3)})
        t = e
    return segs


# ---------- TransNetV2（外置解释器侧车） ----------
# 为什么走侧车：安装版后端是 PyInstaller 冻结产物，spec 里 excludes 了 torch
# （+2.5 GB 不现实），模型在本进程内**永远** import 不到。这不是模型缺，是能力
# 边界。正确做法交给外置解释器跑：平台负责进程管理与协议，本包提供 worker 脚本。
_WORKER_TRANSNET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "worker_transnet.py")
_SIDECAR_PROBE: Dict[str, Any] = {"checked": False, "ok": False, "why": ""}


def _transnet_weights(d: str) -> str:
    for name in ("transnetv2-pytorch-weights.pth", "transnetv2_weights.pth", "weights.pth"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    try:
        cands = [f for f in os.listdir(d) if f.lower().endswith(".pth")]
    except OSError:
        cands = []
    return os.path.join(d, sorted(cands)[0]) if cands else ""


def transnet_supply(model_path: str = "") -> Dict[str, Any]:
    """TransNetV2「供给 + 可运行性」预检。

    供给（模型在不在）与可运行性（torch 能不能用）分开判断：
    前者用户能自己解决（去模型中心下载），后者是能力边界（需要外置 Python）。
    """
    from app.plugin import deps
    info: Dict[str, Any] = {"ready": False, "usable": False, "path": "",
                            "source": "", "warnings": [], "blockers": []}
    try:
        res = deps.resolve_model("transnetv2", requested=model_path,
                                 env=os.environ, params={}) or {}
    except Exception as e:  # noqa: BLE001 —— 供给探测失败不应让节点崩
        info["warnings"].append("resolve_model 异常: %s: %s" % (type(e).__name__, e))
        res = {}
    path = str(res.get("path") or model_path or "")
    d = os.path.dirname(path) if path and os.path.isfile(path) else path
    info.update({"ready": bool(res.get("ready")), "path": path,
                 "source": str(res.get("source") or ""),
                 "warnings": info["warnings"] + list(res.get("warnings") or [])})
    if not d or not os.path.isdir(d):
        info["blockers"].append("model_missing")
        return info
    if not os.path.exists(os.path.join(d, "transnetv2_pytorch.py")):
        info["blockers"].append("module_missing")
    elif not _transnet_weights(d):
        info["blockers"].append("weights_missing")
    else:
        info["weights"] = _transnet_weights(d)
        ok = _sidecar_ready(d)
        info["torch_sidecar"] = ok
        if not ok:
            info["blockers"].append("needs_external_python")
            info["sidecar_reason"] = _SIDECAR_PROBE.get("why") or ""
        info["model_dir"] = d
    info["usable"] = not info["blockers"]
    return info


def _sidecar_ready(model_dir: str = "") -> bool:
    """外置解释器能不能跑 TransNetV2。只探一次并缓存——起进程加载模型要十几秒，
    每次推理都探会把「auto 降级」变成常态。"""
    if _SIDECAR_PROBE["checked"]:
        return bool(_SIDECAR_PROBE["ok"])
    _SIDECAR_PROBE["checked"] = True
    try:
        from app.plugin import deps
        rt = deps.sidecar_runtime()
        if not rt.get("ok"):
            _SIDECAR_PROBE["why"] = str(rt.get("reason") or "没有可用的外置 Python 运行时")
            return False
        if not os.path.isfile(_WORKER_TRANSNET):
            _SIDECAR_PROBE["why"] = "缺少侧车脚本 %s" % _WORKER_TRANSNET
            return False
        r = deps.sidecar_call(_WORKER_TRANSNET, "ping", {"model_dir": model_dir}, 300)
        _SIDECAR_PROBE["ok"] = bool(r.get("ready"))
        if not _SIDECAR_PROBE["ok"]:
            _SIDECAR_PROBE["why"] = "侧车 ping 未就绪"
        return bool(_SIDECAR_PROBE["ok"])
    except Exception as e:  # noqa: BLE001
        _SIDECAR_PROBE["why"] = "%s: %s" % (type(e).__name__, e)
        return False


def _transnet_blocker_error(supply: Dict[str, Any]) -> RuntimeError:
    """把预检结果翻成节点错误。判定顺序 = 先补供给、再看能力：
    模型缺失用户能自己解决，所以先报 model_missing；模型齐了才轮到 torch。"""
    blockers = list(supply.get("blockers") or [])
    hint = ("去模型中心下载 transnetv2；或把 model_dir 指到已下载的目录"
            if "model_missing" in blockers else "")
    if "needs_external_python" in blockers:
        reason = supply.get("sidecar_reason") or ""
        return RuntimeError(
            "TransNetV2 需要外置 Python 运行时（安装版后端不含 torch）。"
            "请指定一个装好 torch 的 Python（FRAMEWEAVE_PY_EXE 或设置里的模型运行时）。"
            + (" 原因: %s" % reason if reason else ""))
    return RuntimeError("TransNetV2 不可用：%s（%s）" % (", ".join(blockers), hint))


def _extract_transnet_frames(path: str, workdir: str) -> Any:
    """按 27x48 抽帧供 TransNetV2。返回 numpy 数组 [N,27,48,3]，失败返回 None。"""
    try:
        import numpy as np
    except ImportError:
        return None
    exe = media_tools.ffmpeg_path()
    if not exe:
        return None
    out_dir = os.path.join(workdir, "tn_frames")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        try:
            os.remove(os.path.join(out_dir, f))
        except OSError:
            pass
    p = _run([exe, "-v", "error", "-i", path, "-vf", "scale=48:27",
              os.path.join(out_dir, "f_%06d.jpg")], _FFMPEG_TIMEOUT)
    if p is None or p.returncode != 0:
        return None
    files = sorted(os.listdir(out_dir))
    if not files:
        return None
    arr = []
    try:
        from PIL import Image
        for f in files:
            im = Image.open(os.path.join(out_dir, f)).convert("RGB").resize((48, 27))
            arr.append(np.asarray(im, dtype=np.uint8))
    except ImportError:
        # 没有 PIL 就交给 ffmpeg 直接吐 rawvideo
        p2 = _run([exe, "-v", "error", "-i", path, "-vf", "scale=48:27",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], _FFMPEG_TIMEOUT)
        if p2 is None or p2.returncode != 0:
            return None
        return None   # 文本模式捕获不到二进制，走 PIL 路径才是正路
    return np.asarray(arr) if arr else None


def _detect_transnet(path: str, workdir: str, model_dir: str, threshold: float,
                     min_sec: float, max_sec: float) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """TransNetV2 检测。返回 (段列表 或 None, 原因)。"""
    try:
        import numpy as np
    except ImportError:
        return None, "缺少 numpy（TransNetV2 需要）"
    frames = _extract_transnet_frames(path, workdir)
    if frames is None or frames.size == 0:
        return None, "抽帧失败（需要 ffmpeg 与 PIL）"
    fd, npy = tempfile.mkstemp(prefix="fw_tn_", suffix=".npy")
    os.close(fd)
    try:
        np.save(npy, np.ascontiguousarray(frames))
        from app.plugin import deps
        r = deps.sidecar_call(_WORKER_TRANSNET, "predict",
                              {"model_dir": model_dir, "npy": npy}, 1800)
        probs = np.asarray(r.get("probs") or [], dtype=float)
    except Exception as e:  # noqa: BLE001 —— 推理失败按策略降级，不吞成空结果
        return None, "TransNetV2 推理失败: %s: %s" % (type(e).__name__, str(e)[:120])
    finally:
        try:
            os.remove(npy)
        except OSError:
            pass
    if probs.size == 0:
        return None, "TransNetV2 没有返回概率"
    total = _duration(path)
    if total <= 0:
        return None, "探不到视频时长"
    # 逐帧概率 → 切点帧号（概率过阈值且是上升沿）
    cuts: List[float] = []
    fps_est = max(len(probs) / total, 0.001) if total > 0 else 25.0
    for i in range(1, len(probs)):
        if probs[i] >= threshold and probs[i - 1] < threshold:
            cuts.append(i / fps_est)
    if not cuts:
        return None, "TransNetV2 未检出超过阈值 %g 的切点" % threshold
    segs = _build(cuts, total, min_sec, max_sec)
    if not segs:
        return None, "切点全部被最短镜头约束合并"
    return segs, "transnetv2"


class SceneDetectNode(NodeBase):
    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video = inputs.get("video")
        path = str(getattr(video, "path", "") or "") if video is not None else ""
        if not path:
            raise RuntimeError("缺少视频输入：video 端口未连接，或资产没有文件路径")
        if not os.path.isfile(path):
            raise RuntimeError("视频文件不存在: %s" % path)

        engine = str(params.get("engine", "auto") or "auto").strip().lower()
        if engine not in ("auto", "transnetv2", "ffmpeg", "fixed"):
            engine = "auto"
        try:
            threshold = float(params.get("threshold", 0.3))
        except (TypeError, ValueError):
            threshold = 0.3
        threshold = min(max(threshold, 0.05), 0.95)
        try:
            min_sec = max(float(params.get("min_scene_sec", 1.0) or 1.0), 0.0)
        except (TypeError, ValueError):
            min_sec = 1.0
        try:
            max_sec = float(params.get("max_scene_sec", 12.0) or 12.0)
        except (TypeError, ValueError):
            max_sec = 12.0
        max_sec = max(max_sec, max(min_sec, 0.5))
        try:
            interval = float(params.get("fallback_interval", 5.0) or 5.0)
        except (TypeError, ValueError):
            interval = 5.0

        warnings: List[str] = []
        segs: Optional[List[Dict[str, Any]]] = None
        used = ""
        model_path = str(params.get("model_dir", "") or "").strip()

        # TransNetV2 优先：它比帧差准得多，但要多一个外置运行时。
        # 显式选它时不可用就报结构化错误（不静默换引擎）；auto 才降级并写原因。
        if engine in ("auto", "transnetv2"):
            supply = await asyncio.to_thread(transnet_supply, model_path)
            if supply.get("usable"):
                segs, reason = await asyncio.to_thread(
                    _detect_transnet, path, ctx.get("workdir", "."),
                    supply.get("model_dir", ""), threshold, min_sec, max_sec)
                if segs:
                    used = "transnetv2"
                else:
                    warnings.append("TransNetV2 未产出切点：%s" % reason)
            else:
                detail = _transnet_blocker_error(supply)
                if engine == "transnetv2":
                    raise detail
                warnings.append("TransNetV2 不可用，改用 ffmpeg：%s" % detail)

        if not segs and engine in ("auto", "ffmpeg"):
            segs, reason = await asyncio.to_thread(
                _detect, path, threshold, min_sec, max_sec)
            if segs:
                used = "ffmpeg"
            elif engine == "ffmpeg":
                warnings.append("ffmpeg 场景检测不可用：%s" % reason)
            else:
                warnings.append("ffmpeg 场景检测不可用：%s" % reason)

        if not segs and engine in ("auto", "fixed"):
            total = await asyncio.to_thread(_duration, path)
            if total > 0:
                segs = _fixed(total, interval, max_sec)
                used = "fixed"
                warnings.append("已降级为固定间隔切分（每 %.1fs 一刀，不看画面）" % interval)
            else:
                warnings.append("取不到视频时长（ffprobe 不可用或文件不可解析）")

        if not segs:
            raise RuntimeError(
                "镜头切分失败：ffmpeg 检测与固定间隔切分都不可用。原因：%s"
                % ("；".join(warnings) or "未知"))
        store = ctx["store"]
        out = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"count": len(segs), "engine": used, "source": path,
                        "degraded": used != "transnetv2",
                        "warnings": list(warnings)}),
            payload=segs)
        return {"scenes": out}
