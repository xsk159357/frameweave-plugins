"""核心插件：场景/镜头切分（增强版 v2）。

策略阶梯（auto 自动降级，逐级写入 report）：
1. transnetv2（可选）——PyTorch 变体（transnetv2_pytorch.py + .pth，经 transnet_model_path 指定），
   或 TF 变体（pip transnetv2 包，需 tensorflow）；输入 27x48 抽帧，阈值二值化 + 0→1/1→0 跳变切分。
2. ffmpeg 帧差（默认主路径）——按 sample_fps 抽帧 + 感知哈希/直方图差分，超阈值判切点。
3. 固定间隔（最后兜底，仅需时长）。

后处理（对齐解说猫实测语义 r1 §2.3）：
- 合并短场景（< min_duration）：并入前一场景（首场景并入后一）；merge_policy 可选 next/drop。
- 拆分长场景（> max_duration）：均匀拆分至每段 ≤ max_duration。

输出契约（s3 §3）：统一 item_id/start/end/duration/source_id/meta + start_frame/end_frame/fps；
candidates=原始切点，scenes=后处理结果；任何降级写入 report.warnings/strategy_fallback。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.assets import Asset
from app.nodespec import NodeBase, PortType

try:
    import cv2
    HAS_CV2 = True
except Exception:  # noqa: BLE001
    HAS_CV2 = False


def find_ffmpeg() -> Optional[str]:
    """统一 resolver 探测 ffmpeg（FRAMEWEAVE_FFMPEG → FFMPEG_BIN → PATH → imageio → 默认位）。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ, params={})
    if r["ready"]:
        return r["path"]
    return None


def find_ffprobe() -> Optional[str]:
    """统一 resolver 探测 ffprobe。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ, params={})
    if r["ready"]:
        return r["path"]
    return None


def _dep_blob(id: str) -> Dict[str, Any]:
    """节点报告 dependency 字段（ready/source/path/version/warnings）。"""
    from app.plugin import deps
    r = deps.resolve_binary(id, env=os.environ, params={})
    return {id: {"ready": bool(r.get("ready")), "source": r.get("source", ""),
                 "path": r.get("path", ""), "version": r.get("version"),
                 "warnings": list(r.get("warnings") or [])}}


async def _run(cmd: List[str], ctx: Any = None, timeout: float = 900.0) -> Any:
    """执行子进程并捕获输出；平台 run_tracked（dict 或 dict 风格 ctx）先短预算试探，失败回退 subprocess.run。

    参数序遵循平台迁移契约 _run(cmd, ctx)（T6.12g 回归校验托管路径）。
    """
    import asyncio
    import subprocess as _sp
    rt = None
    if ctx is not None:
        rt = ctx.get("run_tracked") if hasattr(ctx, "get") else getattr(ctx, "run_tracked", None)
    if rt is not None:
        try:
            return await rt(cmd, timeout=min(float(timeout), 6.0), text=True,
                            stdout=_sp.PIPE, stderr=_sp.PIPE)
        except TypeError:
            # 兼容不接受 stdout/stderr 的 run_tracked 实现（如平台迁移测试 mock）
            try:
                return await rt(cmd, timeout=min(float(timeout), 6.0), text=True)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
    return await asyncio.to_thread(
        lambda: _sp.run(cmd, capture_output=True, timeout=timeout))


async def _probe(video_path: str, ctx: Any) -> Tuple[float, float]:
    """返回 (duration, fps)；探测失败返回 (0.0, 0.0)。"""
    exe = find_ffprobe()
    if exe is None:
        return 0.0, 0.0
    try:
        out = await _run([exe, "-v", "quiet", "-print_format", "json",
                          "-show_entries", "format=duration", "-show_streams", video_path], ctx)
        data = json.loads(out.stdout or "{}")
        dur = 0.0
        try:
            dur = float((data.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        fps = 0.0
        for s in data.get("streams") or []:
            if s.get("codec_type") == "video":
                r = s.get("avg_frame_rate") or s.get("r_frame_rate") or ""
                if "/" in r:
                    n, d = r.split("/", 1)
                    try:
                        if float(d):
                            fps = float(n) / float(d)
                    except (TypeError, ValueError):
                        fps = 0.0
                else:
                    try:
                        fps = float(r)
                    except (TypeError, ValueError):
                        fps = 0.0
                break
        return dur, fps
    except Exception:  # noqa: BLE001
        return 0.0, 0.0


async def _extract_frames(video_path: str, fps: float, workdir: str, ctx: Any,
                          target_width: int = 0, scale_h: int = 0) -> List[str]:
    """按 fps 抽帧到 workdir/frames/，返回排序帧路径列表。ffmpeg 不可用返回 []。"""
    ff = find_ffmpeg()
    if ff is None:
        return []
    out_dir = os.path.join(workdir, "frames")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        try:
            os.remove(os.path.join(out_dir, f))
        except OSError:
            pass
    vf = "fps={}".format(fps)
    if target_width or scale_h:
        if target_width:
            vf += ",scale={}:-2".format(target_width)
        else:
            vf += ",scale=-2:{}".format(scale_h)
    try:
        proc = await _run([ff, "-v", "error", "-i", video_path, "-vf", vf,
                           os.path.join(out_dir, "frame_%06d.jpg")], ctx)
        if proc.returncode != 0:
            return []
    except Exception:  # noqa: BLE001
        return []
    frames = sorted(os.listdir(out_dir))
    return [os.path.join(out_dir, f) for f in frames]


def _frame_diff(path_a: str, path_b: str) -> float:
    """两帧感知差（0-1）。cv2 直方图 Bhattacharyya；无 cv2 时纯 Python 亮度直方图兜底。"""
    if HAS_CV2:
        try:
            def _read_gray(p):
                data = np.fromfile(p, dtype=np.uint8)
                return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            a = _read_gray(path_a)
            b = _read_gray(path_b)
            if a is not None and b is not None and a.shape == b.shape:
                ha = cv2.calcHist([a], [0], None, [16], [0, 256])
                hb = cv2.calcHist([b], [0], None, [16], [0, 256])
                cv2.normalize(ha, ha)
                cv2.normalize(hb, hb)
                return float(cv2.compareHist(ha, hb, cv2.HISTCMP_BHATTACHARYYA))
            if a is not None and b is not None:
                a2 = cv2.resize(a, (32, 18))
                b2 = cv2.resize(b, (32, 18))
                return float(cv2.absdiff(a2, b2).mean() / 255.0)
        except Exception:  # noqa: BLE001
            pass
    # 纯 Python 亮度直方图兜底（解 JPEG 失败时按文件字节差分，弱但可用）
    try:
        a = _read_bytes_hist(path_a)
        b = _read_bytes_hist(path_b)
        if not a or not b:
            return 0.0
        total = sum(a.values())
        if not total:
            return 0.0
        diff = 0.0
        for k in range(16):
            pa = a.get(k, 0) / total
            pb = b.get(k, 0) / total
            diff += abs(pa - pb)
        return float(diff)
    except OSError:
        return 0.0


def _read_bytes_hist(path: str) -> Dict[int, int]:
    """按文件字节分布粗分 16 桶（降级差分的近似亮度直方图）。"""
    hist: Dict[int, int] = {}
    with open(path, "rb") as f:
        data = f.read(256 * 1024)
    step = 256 // 16
    for b in data:
        if b < 256:
            hist[b // step] = hist.get(b // step, 0) + 1
    return hist


def _frames_to_scenes(boundaries: List[int], total_frames: int, fps: float,
                      sample_fps: float, video_duration: float, source_id: str,
                      strategy: str) -> List[Dict[str, Any]]:
    """帧索引切点 → 候选场景（统一契约字段；start_frame/end_frame 为视频帧空间）。"""
    scenes: List[Dict[str, Any]] = []
    for k in range(len(boundaries) - 1):
        sf = boundaries[k]
        ef = boundaries[k + 1]
        if ef <= sf:
            continue
        start = sf / sample_fps
        end = ef / sample_fps
        scenes.append({
            "item_id": "sc_" + uuid.uuid4().hex[:12],
            "index": k,
            "start": round(min(start, video_duration), 3),
            "end": round(min(end, video_duration), 3),
            "duration": round(min(end, video_duration) - min(start, video_duration), 3),
            "start_frame": int(round(min(start, video_duration) * fps)) if fps else int(sf),
            "end_frame": int(round(min(end, video_duration) * fps)) if fps else int(ef),
            "fps": round(fps, 4) if fps else round(sample_fps, 4),
            "source_id": source_id,
            "meta": {"strategy": strategy, "sample_fps": sample_fps},
        })
    return scenes


def merge_short_scenes(scenes: List[Dict[str, Any]], min_duration: float,
                       policy: str = "previous") -> Tuple[List[Dict[str, Any]], int]:
    """合并短场景（r1 §2.3 语义）。policy: previous(并入前一)/next(并入后一)/drop(丢弃)。

    并入时起点取最早、终点取最晚；返回 (新列表, 合并次数)。首场景过短默认并入后一。
    """
    out: List[Dict[str, Any]] = [dict(s) for s in scenes]
    if not out:
        return [], 0
    merged = 0
    i = 0
    while i < len(out):
        sc = out[i]
        if sc["duration"] >= min_duration:
            i += 1
            continue
        # 当前场景过短
        if policy == "drop":
            out.pop(i)
            merged += 1
            continue
        if policy == "next":
            if i + 1 < len(out):
                nxt = out[i + 1]
                nxt["start"] = min(nxt["start"], sc["start"])
                nxt["duration"] = round(nxt["end"] - nxt["start"], 3)
                if nxt.get("start_frame") and sc.get("start_frame"):
                    nxt["start_frame"] = min(nxt["start_frame"], sc["start_frame"])
                out.pop(i)
                merged += 1
                continue
            if i > 0:
                prev = out[i - 1]
                prev["end"] = max(prev["end"], sc["end"])
                prev["duration"] = round(prev["end"] - prev["start"], 3)
                if prev.get("end_frame") and sc.get("end_frame"):
                    prev["end_frame"] = max(prev["end_frame"], sc["end_frame"])
                out.pop(i)
                merged += 1
                continue
            i += 1
            continue
        # previous（默认）
        if i > 0:
            prev = out[i - 1]
            prev["end"] = max(prev["end"], sc["end"])
            prev["duration"] = round(prev["end"] - prev["start"], 3)
            if prev.get("end_frame") and sc.get("end_frame"):
                prev["end_frame"] = max(prev["end_frame"], sc["end_frame"])
            out.pop(i)
            merged += 1
            continue
        if len(out) > 1:
            # 首场景过短 → 并入后一
            nxt = out[1]
            nxt["start"] = min(nxt["start"], sc["start"])
            nxt["duration"] = round(nxt["end"] - nxt["start"], 3)
            if nxt.get("start_frame") and sc.get("start_frame"):
                nxt["start_frame"] = min(nxt["start_frame"], sc["start_frame"])
            out.pop(0)
            merged += 1
            continue
        i += 1
    for i, sc in enumerate(out):
        sc["index"] = i
        sc["item_id"] = "sc_" + uuid.uuid4().hex[:12]
    return out, merged


def split_long_scenes(scenes: List[Dict[str, Any]], max_duration: float) -> Tuple[List[Dict[str, Any]], int]:
    """拆分长场景（r1 §2.3：均匀拆分至每段 ≤ max_duration）。"""
    out: List[Dict[str, Any]] = []
    split = 0
    for sc in scenes:
        d = sc["duration"]
        if d <= max_duration:
            out.append(dict(sc))
            continue
        n = int(np.ceil(d / max_duration))
        seg = d / n
        sf = sc.get("start_frame", 0)
        ef = sc.get("end_frame", 0)
        fstep = (ef - sf) / n if ef > sf else 0
        for i in range(n):
            s = sc["start"] + i * seg
            e = sc["start"] + (i + 1) * seg
            out.append({
                "item_id": "sc_" + uuid.uuid4().hex[:12],
                "index": 0,
                "start": round(s, 3),
                "end": round(e, 3),
                "duration": round(e - s, 3),
                "start_frame": int(sf + i * fstep) if fstep else None,
                "end_frame": int(sf + (i + 1) * fstep) if fstep else None,
                "fps": sc.get("fps"),
                "source_id": sc.get("source_id", ""),
                "meta": dict(sc.get("meta") or {}),
            })
        split += n - 1
    for i, sc in enumerate(out):
        sc["index"] = i
    return out, split


# ---------------------------------------------------------------- TransNetV2 适配
class TransNetAdapter:
    """可选 TransNetV2 适配器：PyTorch 变体（transnetv2_pytorch.py + .pth）优先，
    TF 变体（pip transnetv2 + tensorflow）次之。任何加载/推理失败 → 抛异常由调用方降级。"""

    def __init__(self, model_path: str = ""):
        self.model_path = model_path
        self.backend = ""
        self._model = None

    def load(self) -> None:
        import importlib.util
        import sys as _sys
        if self.model_path:
            p = self.model_path
            module_file = os.path.join(p, "transnetv2_pytorch.py") if os.path.isdir(p) else p
            if os.path.isfile(module_file):
                spec = importlib.util.spec_from_file_location("fw_transnetv2_pytorch", module_file)
                mod = importlib.util.module_from_spec(spec)
                _sys.modules["fw_transnetv2_pytorch"] = mod
                spec.loader.exec_module(mod)
                weights = self._find_weights(os.path.dirname(module_file))
                self._model = mod.TransNetV2(weights)
                self.backend = "pytorch"
                return
        try:
            import tensorflow  # noqa: F401
            from transnetv2 import TransNetV2 as TfTransNetV2
            self._model = TfTransNetV2()
            self.backend = "tensorflow"
        except Exception:  # noqa: BLE001
            from app.plugin import deps
            raise RuntimeError(deps.coded_message(
                "model_missing", "transnetv2",
                hint="TransNetV2 需要本地模型（models_registry: transnetv2）或 tensorflow/transnetv2 包；"
                     "可将 transnet_model_path 参数指向 *.pth 目录，或安装依赖")) from None

    @staticmethod
    def _find_weights(d: str) -> str:
        for name in ("transnetv2-pytorch-weights.pth", "transnetv2_weights.pth", "weights.pth"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        cands = [f for f in os.listdir(d) if f.endswith(".pth")] if os.path.isdir(d) else []
        if cands:
            return os.path.join(d, sorted(cands)[0])
        raise FileNotFoundError(f"未找到 TransNetV2 PyTorch 权重（*.pth）: {d}")

    def predict(self, frames: np.ndarray) -> np.ndarray:
        """frames: uint8 [N,27,48,3] → 每帧切点概率 [N]。"""
        m = self._model
        if self.backend == "tensorflow":
            # predict_frames 内部滑窗（100/50 + pad），返回 (single, all)
            res = m.predict_frames(frames)
        else:
            for method in ("predict_video", "predict_raw_frames", "predict_frames", "predict_raw"):
                fn = getattr(m, method, None)
                if fn is None:
                    continue
                try:
                    res = fn(frames) if method in ("predict_video", "predict_raw_frames", "predict_raw") else fn(frames)
                    break
                except Exception:  # noqa: BLE001
                    res = None
            else:
                raise RuntimeError("TransNetV2 模型无可用的预测方法")
        arr = np.asarray(res)
        if arr.ndim == 2 and arr.shape[1] == 2:
            arr = arr[:, 0]
        if arr.ndim == 2:
            arr = arr[:, 0]
        arr = np.asarray(arr, dtype=float).reshape(-1)
        return arr


class SceneDetectNode(NodeBase):
    """场景/镜头切分。type_id: core/scene_detect"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/scene_detect",
            title="场景检测",
            category="分析",
            description="镜头/场景切分：ffmpeg 帧差（默认）+ 可选 TransNetV2；输出候选与后处理两级片段",
            inputs=[PortSpec("video", PortType.VIDEO, label="视频", required=True)],
            outputs=[
                PortSpec("scenes", PortType.SEGMENTS, label="场景片段", required=True),
                PortSpec("candidates", PortType.SEGMENTS, label="候选切点", required=False),
                PortSpec("count", PortType.INT, label="场景数", required=False),
                PortSpec("report", PortType.JSON, label="检测报告", required=False),
            ],
            params=[
                PortSpec("strategy", PortType.STRING, label="检测策略", default="auto",
                         widget="select", options=["auto", "framediff", "transnetv2", "fixed_interval"]),
                PortSpec("sample_fps", PortType.FLOAT, label="抽样帧率", default=1.0, widget="number"),
                PortSpec("diff_threshold", PortType.FLOAT, label="帧差阈值", default=0.10, widget="number"),
                PortSpec("threshold", PortType.FLOAT, label="模型阈值(TransNet)", default=0.5, widget="number"),
                PortSpec("min_duration", PortType.FLOAT, label="最小片段(秒)", default=1.0, widget="number"),
                PortSpec("max_duration", PortType.FLOAT, label="最大片段(秒)", default=30.0, widget="number"),
                PortSpec("merge_policy", PortType.STRING, label="短片段合并", default="previous",
                         widget="select", options=["previous", "next", "drop"]),
                PortSpec("fast_mode", PortType.BOOL, label="快速模式(15fps)", default=False, widget="toggle"),
                PortSpec("transnet_model_path", PortType.STRING, label="TransNet 模型目录", required=False,
                         default="", widget="text"),
            ],
            version="2.0.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        if video_asset is None or not getattr(video_asset, "path", "") or not os.path.exists(video_asset.path):
            raise RuntimeError("invalid: 缺少有效的视频输入资产")
        video_path = video_asset.path
        source_id = video_asset.id or video_asset.fingerprint or "video"
        strategy = str(params.get("strategy", "auto") or "auto")
        sample_fps = float(params.get("sample_fps", 1.0) or 1.0)
        if sample_fps <= 0:
            sample_fps = 1.0
        diff_threshold = float(params.get("diff_threshold", 0.10) or 0.10)
        model_threshold = float(params.get("threshold", 0.5) or 0.5)
        min_duration = float(params.get("min_duration", 1.0) or 1.0)
        max_duration = float(params.get("max_duration", 30.0) or 30.0)
        merge_policy = str(params.get("merge_policy", "previous") or "previous")
        fast_mode = bool(params.get("fast_mode", False))
        model_path = str(params.get("transnet_model_path", "") or "").strip()

        progress = ctx.get("progress") or (lambda p: None)
        progress(0.02)
        duration, fps = await _probe(video_path, ctx)
        if duration <= 0:
            from app.plugin import deps
            ff = deps.resolve_binary("ffprobe", env=os.environ, params={})
            if not ff.get("ready"):
                raise RuntimeError(deps.coded_message(
                    "binary_missing", "ffprobe", hint=ff.get("repair_hint", "")))
            raise RuntimeError("invalid: 无法探测视频时长（文件可能损坏或不是有效视频）")

        workdir = ctx.get("workdir", os.path.dirname(video_path))
        fallback: List[str] = []
        candidates: List[Dict[str, Any]] = []
        strategy_used = strategy

        def emit_scenes(list_scenes):
            for i, sc in enumerate(list_scenes):
                sc["index"] = i

        if strategy in ("auto", "transnetv2"):
            try:
                adapter = TransNetAdapter(model_path)
                adapter.load()
                progress(0.2)
                frame_fps = 15.0 if fast_mode else (fps or 25.0)
                frames = await _extract_frames(video_path, frame_fps, workdir, ctx,
                                               target_width=48, scale_h=27)
                if not frames:
                    raise RuntimeError("抽帧失败（ffmpeg 不可用）")
                arr = []
                if HAS_CV2:
                    for fp in frames:
                        data = np.fromfile(fp, dtype=np.uint8)
                        im = cv2.imdecode(data, cv2.IMREAD_COLOR)
                        if im is None:
                            raise RuntimeError("帧解码失败")
                        arr.append(cv2.resize(im, (48, 27)))
                else:
                    raise RuntimeError("TransNet 需要 cv2 解码帧")
                progress(0.45)
                pred = adapter.predict(np.stack(arr, 0).astype(np.uint8))
                # 帧序号 → 秒（按抽取帧率换算，保证与视频时间轴一致）
                mapped = []
                for i, p in enumerate(pred):
                    mapped.append((i / frame_fps, p))
                mask = [(t, 1 if p >= model_threshold else 0) for t, p in mapped]
                boundaries = [0.0]
                for i in range(1, len(mask)):
                    if mask[i][1] != mask[i - 1][1]:
                        boundaries.append(mask[i][0])
                boundaries.append(duration)
                candidates = []
                for k in range(len(boundaries) - 1):
                    s, e = boundaries[k], boundaries[k + 1]
                    if e - s <= 0:
                        continue
                    candidates.append({
                        "item_id": "sc_" + uuid.uuid4().hex[:12],
                        "index": k,
                        "start": round(s, 3), "end": round(min(e, duration), 3),
                        "duration": round(min(e, duration) - s, 3),
                        "start_frame": int(round(s * fps)) if fps else None,
                        "end_frame": int(round(min(e, duration) * fps)) if fps else None,
                        "fps": round(fps, 4), "source_id": source_id,
                        "meta": {"strategy": "transnetv2", "model": adapter.backend,
                                 "sample_fps": frame_fps},
                    })
                strategy_used = "transnetv2"
            except Exception as e:  # noqa: BLE001
                fallback.append(f"transnetv2 不可用（{type(e).__name__}: {e}），降级帧差")
                if strategy == "transnetv2":
                    raise RuntimeError(f"invalid: TransNetV2 策略失败且禁止降级: {e}") from e

        if not candidates and strategy in ("auto", "framediff"):
            try:
                progress(0.25)
                frames = await _extract_frames(video_path, sample_fps, workdir, ctx, target_width=320)
                if not frames:
                    from app.plugin import deps
                    ff = deps.resolve_binary("ffmpeg", env=os.environ, params={})
                    if not ff.get("ready"):
                        raise RuntimeError(deps.coded_message(
                            "binary_missing", "ffmpeg", hint=ff.get("repair_hint", "")))
                    raise RuntimeError("invalid: ffmpeg 抽帧失败（ffmpeg 可用但帧差策略失败）")
                boundaries: List[int] = [0]
                n = len(frames)
                for i in range(1, n):
                    progress(0.3 + 0.5 * i / max(n - 1, 1))
                    d = _frame_diff(frames[i - 1], frames[i])
                    if d >= diff_threshold:
                        boundaries.append(i)
                boundaries.append(n)
                candidates = _frames_to_scenes(boundaries, n, fps or sample_fps, sample_fps,
                                               duration, source_id, "framediff")
                strategy_used = "framediff"
            except Exception as e:  # noqa: BLE001
                fallback.append(f"framediff 不可用（{type(e).__name__}: {e}），降级固定间隔")
                if strategy == "framediff":
                    raise RuntimeError(f"invalid: 帧差策略失败且禁止降级: {e}") from e

        if not candidates:
            # 固定间隔兜底（r1 §2.2 #6：每 interval=5s 一刀）
            interval = max(1.0, sample_fps * 5.0)
            strategy_used = "fixed_interval"
            t = 0.0
            idx = 0
            while t < duration:
                e = min(t + interval, duration)
                candidates.append({
                    "item_id": "sc_" + uuid.uuid4().hex[:12],
                    "index": idx, "start": round(t, 3), "end": round(e, 3),
                    "duration": round(e - t, 3),
                    "start_frame": int(round(t * fps)) if fps else None,
                    "end_frame": int(round(e * fps)) if fps else None,
                    "fps": round(fps, 4), "source_id": source_id,
                    "meta": {"strategy": "fixed_interval"},
                })
                idx += 1
                t = e
            if not candidates:
                candidates.append({
                    "item_id": "sc_" + uuid.uuid4().hex[:12], "index": 0,
                    "start": 0.0, "end": round(duration, 3), "duration": round(duration, 3),
                    "start_frame": 0, "end_frame": int(duration * fps) if fps else None,
                    "fps": round(fps, 4), "source_id": source_id,
                    "meta": {"strategy": "fixed_interval"},
                })

        progress(0.85)
        emit_scenes(candidates)
        scenes, merged = merge_short_scenes([dict(s) for s in candidates], min_duration, merge_policy)
        scenes, split = split_long_scenes(scenes, max_duration)
        for i, sc in enumerate(scenes):
            sc["index"] = i

        store = ctx["store"]
        scenes_asset = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"count": len(scenes), "candidate_count": len(candidates),
                        "video_duration": duration, "strategy": strategy_used,
                        "merged_short": merged, "split_long": split}),
            payload=scenes)
        cand_asset = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"count": len(candidates), "video_duration": duration,
                        "strategy": strategy_used}),
            payload=candidates)
        report = {
            "strategy_used": strategy_used,
            "strategy_requested": strategy,
            "strategy_fallback": fallback,
            "video_fingerprint": video_asset.fingerprint,
            "duration": round(duration, 3),
            "fps": round(fps, 4) if fps else None,
            "candidate_count": len(candidates),
            "scene_count": len(scenes),
            "merged_short": merged,
            "split_long": split,
            "min_duration": min_duration,
            "max_duration": max_duration,
            "merge_policy": merge_policy,
            "warnings": fallback,
            "dependency": _dep_blob("ffmpeg"),
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"strategy": strategy_used}), payload=report)
        progress(1.0)
        return {"scenes": scenes_asset, "candidates": cand_asset,
                "count": len(scenes), "report": report_asset}