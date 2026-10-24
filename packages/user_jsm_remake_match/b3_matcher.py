# -*- coding: utf-8 -*-
"""B3 精选复刻匹配算法 —— 干净重实现 curated_remake_matcher（纯算法层）。

语义（依据 docs/reports/jieshuomao解说工坊/r2-match-analysis.md §3）：
  解说片画面 = 若干原片连续片段按 ~1x 速率拼接 → 原片时刻 - 解说时刻 = 偏移在段内近似恒定。
  全本地、无 LLM：灰度缩略图粗筛 + DINOv2/Chinese-CLIP 语义精排 + 全局时序一致性 DP
  （偏移分段恒定、换段有代价）+ log-mel 音频最终裁决。

本模块为纯算法层：FFmpeg 特征抽取经注入的 async runner（节点层接 ctx.run_tracked），
纯函数部分（预设/DP/灰度候选/音频相关/段治理）可独立单测。

39 参数档位化：fast（零模型）/ balanced / precise 预设；advanced JSON 可覆盖全部参数键。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 39 参数全集与预设
# 键名与 r2 侦察 §3.2 构造签名 / §3.3 流水线对齐；值 = 干净重实现默认（与原版一致）。
DEFAULT_PARAMS: Dict[str, Any] = {
    # 解码/裁剪
    "fps": 4, "thumb_w": 48, "thumb_h": 27, "subtitle_crop": 0.8,
    "bar_detect": True, "content_detect": True, "center_crop": 0.6,
    # 灰度粗筛
    "gray_suspect": 0.45, "suspect_score": 0.18,
    # 语义精排（DINOv2 优先，回退 Chinese-CLIP）
    "semantic_model": "auto", "clip_fps": 2.0, "clip_topk": 6,
    "clip_suspect": 0.8, "clip_strong": 0.9,
    "clip_cost_thresh": 0.18, "clip_margin": 0.01,
    "dino_suspect": 0.55, "dino_strong": 0.75,
    # 全局 DP
    "within_shot_jump_pen": 0.6, "boundary_jump_pen": 0.1,
    "drift_scale": 0.03, "drift_cap": 40.0, "cross_file_pen": 0.35,
    "back_jump_extra": 0.1, "far_ctx_sec": 120.0, "refine_prior": 0.03,
    "refine_max_win": 8.0, "jump_dist_scale": 0.0005, "jump_dist_cap": 0.25,
    # 段治理
    "min_seg_dur": 1.0, "anchor_min_dur": 4.0, "merge_tol": 0.55,
    "overlap_snap": 0.4, "max_shift": 2.0,
    # 精修
    "fine_fps": 12, "fine_win": 1.2, "fine_qsec": 2.0, "refine_win": 12.0,
    "refine_query": 6.0,
    # 音频裁决
    "audio_refine": True, "audio_win": 18.0, "audio_query": 6.0,
    "audio_suspect": 0.12,
    # 扩展（原版非构造参数但管线需要）
    "audio_refine_scope": "suspect",   # suspect=仅存疑段；all=全段
    "clips_topk": 8, "n_sub": 5, "gray_step": 2.0,
}

PRESETS: Dict[str, Dict[str, Any]] = {
    "fast": {
        "semantic_model": "none", "audio_refine": False, "audio_refine_scope": "none",
        "fps": 4, "fine_fps": 4, "clip_fps": 0.0,
        "gray_suspect": 0.5, "dino_suspect": 0.6, "dino_strong": 0.8,
    },
    "balanced": {
        "semantic_model": "auto", "audio_refine": True, "audio_refine_scope": "suspect",
        "fps": 4, "fine_fps": 8, "clip_fps": 2.0,
        "dino_suspect": 0.55, "dino_strong": 0.75,
    },
    "precise": {
        "semantic_model": "dino", "audio_refine": True, "audio_refine_scope": "all",
        "fps": 6, "fine_fps": 12, "clip_fps": 2.0,
        "dino_suspect": 0.6, "dino_strong": 0.8,
        "clips_topk": 8, "gray_suspect": 0.4,
    },
}

PRESET_DOC = {
    "fast": "零模型：灰度缩略图多子帧投票粗筛 + 全局 DP；适合快速批量预览",
    "balanced": "默认：DINOv2-Small（缺→Chinese-CLIP→灰度）语义精排 + 存疑段音频裁决 + 8fps 细修",
    "precise": "精准：DINOv2-Base（缺→Small→CLIP→灰度）+ 全段音频裁决 + 12fps 帧级细修；最准但最慢",
}

_ADVANCED_KEYS = (
    set(DEFAULT_PARAMS.keys())
    | {"fps", "thumb_w", "thumb_h", "subtitle_crop", "bar_detect", "content_detect",
       "center_crop", "gray_suspect", "suspect_score", "semantic_model", "clip_fps",
       "clip_topk", "clip_suspect", "clip_strong", "clip_cost_thresh", "clip_margin",
       "dino_suspect", "dino_strong", "within_shot_jump_pen", "boundary_jump_pen",
       "drift_scale", "drift_cap", "cross_file_pen", "back_jump_extra", "far_ctx_sec",
       "refine_prior", "refine_max_win", "jump_dist_scale", "jump_dist_cap",
       "min_seg_dur", "anchor_min_dur", "merge_tol", "overlap_snap", "max_shift",
       "fine_fps", "fine_win", "fine_qsec", "refine_win", "refine_query",
       "audio_refine", "audio_win", "audio_query", "audio_suspect",
       "audio_refine_scope", "clips_topk", "n_sub", "gray_step", "model_dir",
       "use_cache", "cache_dir"}
)


def merged_params(preset: str = "balanced", overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """档位预设 + 高级参数合并 → 完整参数表（advanced 覆盖预设，预设覆盖默认）。"""
    if preset not in PRESETS:
        preset = "balanced"
    p = dict(DEFAULT_PARAMS)
    p.update(PRESETS[preset])
    if overrides:
        for k, v in overrides.items():
            if k in _ADVANCED_KEYS:
                p[k] = v
    return p


def preset_summary(p: Dict[str, Any]) -> Dict[str, Any]:
    sm = str(p.get("semantic_model", "auto"))
    return {
        "semantic_model": sm,
        "semantic_model_label": {
            "none": "灰度（无语义模型）",
            "auto": "自动回退链(dino_base→dino_small→chinese_clip→灰度)",
            "dino": "DINOv2（base→small 回退）",
            "dino_base": "DINOv2-Base",
            "dino_small": "DINOv2-Small",
            "chinese_clip": "Chinese-CLIP",
        }.get(sm, sm),
        "audio_refine": bool(p.get("audio_refine")),
        "audio_scope": str(p.get("audio_refine_scope", "suspect")),
        "fps": p.get("fps"), "fine_fps": p.get("fine_fps"),
        "clip_fps": p.get("clip_fps"), "gray_suspect": p.get("gray_suspect"),
        "dino_suspect": p.get("dino_suspect"), "audio_suspect": p.get("audio_suspect"),
        "advanced_overrides": {k: v for k, v in p.items()
                               if k not in DEFAULT_PARAMS and not k.startswith("_")},
    }


# ---------------------------------------------------------------- 工具
def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def path_hash(path: str) -> str:
    """路径内容指纹（前 1MB）+ 路径哈希混合：文件内容变化 → 缓存键失效。"""
    ap = os.path.abspath(path or "")
    content = ""
    if os.path.isfile(ap):
        try:
            with open(ap, "rb") as f:
                content = f.read(1 << 20).hex()
        except OSError:
            pass
    return sha256_hex(f"{ap}|{content}")[:20]


def l2norm_rows(m: Any) -> Any:
    import numpy as np
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def cosine_matrix(a: Any, b: Any) -> Any:
    import numpy as np
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    chunk = max(1, min(2048, int(64_000_000 / max(b.shape[0], 1))))
    out = np.zeros((a.shape[0], b.shape[0]), dtype=np.float32)
    for i in range(0, a.shape[0], chunk):
        out[i:i + chunk] = a[i:i + chunk] @ b.T
    return out


# ---------------------------------------------------------------- 特征缓存（磁盘）
def _param_key(p: Dict[str, Any], kind: str) -> str:
    keys = {
        "gray": ("fps", "thumb_w", "thumb_h", "subtitle_crop", "bar_detect", "center_crop"),
        "semantic": ("clip_fps", "semantic_model", "model_dir", "subtitle_crop"),
        "audio": ("audio_win", "audio_query"),
    }
    sel = keys.get(kind, ())
    d = {k: p.get(k) for k in sel if k in p}
    return sha256_hex(json.dumps(d, sort_keys=True, ensure_ascii=False))[:12]


class FeatureCache:
    """路径内容哈希 + 特征参数哈希 → 磁盘缓存（.npz）。"""

    def __init__(self, root: str, enabled: bool = True):
        self.root = root
        self.enabled = enabled

    def _fp(self, kind: str, path: str, param_key: str) -> str:
        d = os.path.join(self.root, kind)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{path_hash(path)}_{param_key}.npz")

    def get(self, kind: str, path: str, param_key: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        fp = self._fp(kind, path, param_key)
        if not os.path.isfile(fp):
            return None
        try:
            import numpy as np
            with np.load(fp, allow_pickle=False) as z:
                out = {k: z[k] for k in z.files}
            out["cached"] = True
            return out
        except Exception:  # noqa: BLE001
            return None

    def put(self, kind: str, path: str, param_key: str, data: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        import numpy as np
        try:
            np.savez_compressed(self._fp(kind, path, param_key),
                                **{k: v for k, v in data.items() if v is not None})
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> Dict[str, Any]:
        if not self.enabled or not os.path.isdir(self.root):
            return {"dir": self.root or "", "entries": 0, "enabled": bool(self.enabled)}
        n = sum(len(fs) for _, _, fs in os.walk(self.root))
        return {"dir": self.root, "entries": n, "enabled": True}


# ---------------------------------------------------------------- FFmpeg runner
FFmpegRunner = Any  # async callable (cmd, timeout) -> CompletedProcess


async def default_ffmpeg(ctx: Dict[str, Any], cmd: List[str], timeout: float) -> Any:
    """默认 runner：优先 ctx.run_tracked（平台托管/超时/取消），回退 to_thread subprocess。

    输出保持二进制 bytes（raw video/audio 管道），调用方按需 decode(errors=replace)。
    """
    import asyncio
    import subprocess as sp
    rt = ctx.get("run_tracked") if isinstance(ctx, dict) else None
    if rt is not None:
        try:
            return await rt(cmd, timeout=min(float(timeout), 120.0),
                            stdout=sp.PIPE, stderr=sp.PIPE)
        except Exception:  # noqa: BLE001
            pass
    return await asyncio.to_thread(
        lambda: sp.run(cmd, capture_output=True, timeout=timeout))


# ---------------------------------------------------------------- 特征抽取（经 runner）
async def extract_gray(path: str, p: Dict[str, Any], runner: FFmpegRunner,
                       ffmpeg_cmd: List[str], cache: Optional[FeatureCache]) -> Dict[str, Any]:
    """整片灰度缩略图索引：ffmpeg fps+scale → raw gray → np (N, w*h) 行 L2 归一化。"""
    import numpy as np
    w, h = int(p.get("thumb_w", 48)), int(p.get("thumb_h", 27))
    key = _param_key(p, "gray")
    if cache is not None:
        hit = cache.get("gray", path, key)
        if hit is not None and "frames" in hit:
            hit["path"] = path
            hit["_hit"] = True
            return hit
    crops = ""
    if float(p.get("subtitle_crop", 0.8)) < 1.0:
        keep = float(p["subtitle_crop"])
        crops = f",crop=iw:ih*{keep}:0:ih*(1-{keep})"
    vf = f"fps={float(p.get('fps', 4))},scale={w}:{h}:flags=area{crops}"
    out = await runner(ffmpeg_cmd + ["-v", "error", "-i", path, "-an",
                                     "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"], 600)
    raw = out.stdout if hasattr(out, "stdout") else out
    if not raw or len(raw) < w * h:
        return {"frames": None, "timestamps": [], "path": path, "duration": 0.0, "_hit": False}
    n = len(raw) // (w * h)
    frames = np.frombuffer(raw[: n * w * h], dtype=np.uint8).reshape(n, w * h).astype(np.float32) / 255.0
    frames = l2norm_rows(frames)
    fps = float(p.get("fps", 4))
    ts = [i / fps for i in range(n)]
    d = {"frames": frames, "timestamps": ts, "duration": 0.0, "path": path, "_hit": False}
    if cache is not None:
        cache.put("gray", path, key, {"frames": frames, "timestamps": ts, "duration": 0.0})
    return d


async def extract_audio(path: str, p: Dict[str, Any], runner: FFmpegRunner,
                        ffmpeg_cmd: List[str], cache: Optional[FeatureCache]) -> Dict[str, Any]:
    """log-mel 特征（band z-norm）：ffmpeg 解码 mono 16k PCM → STFT → 三角 mel 滤波。"""
    import numpy as np
    key = _param_key(p, "audio")
    if cache is not None:
        hit = cache.get("audio", path, key)
        if hit is not None and "mel" in hit:
            hit["_hit"] = True
            return hit
    out = await runner(ffmpeg_cmd + ["-v", "error", "-i", path, "-ac", "1", "-ar", "16000",
                                     "-f", "s16le", "-"], 600)
    raw = out.stdout if hasattr(out, "stdout") else out
    if not raw or len(raw) < 1600:
        return {"mel": None, "_hit": False}
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    win, hop = 1024, 256
    n = (len(pcm) - win) // hop
    if n <= 0:
        return {"mel": None, "_hit": False}
    frames = np.lib.stride_tricks.sliding_window_view(pcm[: win + (n - 1) * hop], win)[::hop]
    frames = frames * np.hanning(win)
    spec = np.abs(np.fft.rfft(frames, axis=1))
    fft_freqs = np.fft.rfftfreq(win, 1 / 16000)
    mel = spec @ _mel_filterbank(64, fft_freqs).T
    mel = np.log(mel + 1e-6)
    mu = mel.mean(axis=0, keepdims=True)
    sd = mel.std(axis=0, keepdims=True)
    sd[sd < 1e-6] = 1.0
    mel = (mel - mu) / sd
    d = {"mel": mel.astype(np.float32), "t": np.arange(n) * (hop / 16000.0), "_hit": False}
    if cache is not None:
        cache.put("audio", path, key, {"mel": d["mel"], "t": d["t"]})
    return d


def _mel_filterbank(n_mels: int, freqs: Any) -> Any:
    import numpy as np
    fmin, fmax = 20.0, 8000.0
    n_fft = (len(freqs) - 1) * 2
    mel_min = 2595.0 * math.log10(1 + fmin / 700.0)
    mel_max = 2595.0 * math.log10(1 + fmax / 700.0)
    mel_pts = np.linspace(mel_min, mel_max, n_mels + 2)
    hz = 700.0 * (10 ** (mel_pts / 2595.0) - 1)
    bins = np.floor((n_fft + 1) * hz / 16000.0).astype(int)
    fb = np.zeros((n_mels, len(freqs)), dtype=np.float32)
    for i in range(1, n_mels + 1):
        lo, mid, hi = bins[i - 1], bins[i], bins[i + 1]
        if mid > lo:
            fb[i - 1, lo:mid] = np.linspace(0, 1, mid - lo, dtype=np.float32)
        if hi > mid:
            fb[i - 1, mid:hi] = np.linspace(1, 0, hi - mid, dtype=np.float32)
    return fb


# ---------------------------------------------------------------- 镜头切分（解说片）
def detect_shots(gray: Dict[str, Any], min_dur: float = 1.0) -> List[Tuple[float, float]]:
    """基于灰度帧差均值的轻量镜头切分（未接 TSV 时的内置回退）。"""
    import numpy as np
    frames = gray.get("frames")
    raw_ts = gray.get("timestamps")
    ts = list(raw_ts) if raw_ts is not None and len(raw_ts) else []
    if frames is None or frames.shape[0] < 2:
        return [(0.0, gray.get("duration", 0.0) or 1.0)]
    step = float(ts[1] - ts[0]) if len(ts) > 1 else 0.25
    diffs = np.mean(np.abs(frames[1:] - frames[:-1]), axis=1)
    thr = max(float(np.percentile(diffs, 70)), 0.02)
    cuts = [0]
    for i, d in enumerate(diffs):
        if d > thr and (ts[i + 1] - ts[cuts[-1]]) >= min_dur:
            cuts.append(i + 1)
    cuts.append(len(frames) - 1)
    total = gray.get("duration", 0.0) or (ts[-1] + step if ts else 1.0)
    out: List[Tuple[float, float]] = []
    for i in range(len(cuts) - 1):
        s = ts[cuts[i]]
        e = ts[cuts[i + 1] + 1] if cuts[i + 1] + 1 < len(ts) else total
        if e - s >= 0.3:
            out.append((s, e))
    return out or [(0.0, total)]


# ---------------------------------------------------------------- 灰度粗筛候选
def gray_candidates(q_gray: Dict[str, Any], seg: Tuple[float, float],
                    r_gray: Dict[str, Any], topk: int = 6, n_sub: int = 5,
                    step: float = 2.0) -> List[Dict[str, Any]]:
    """解说段 [s,e] 在原片灰度索引中的候选窗口（多子帧投票 + 滑窗精排，按相似度降序）。"""
    import numpy as np
    qf = q_gray.get("frames")
    rf = r_gray.get("frames")
    raw_rts = r_gray.get("timestamps")
    rts = list(raw_rts) if raw_rts is not None and len(raw_rts) else []
    if qf is None or rf is None or not rts or qf.shape[0] < 1 or rf.shape[0] < 1:
        return []
    fps = 1.0 / ((rts[1] - rts[0]) if len(rts) > 1 else 0.25)
    qs_int = max(0, int(seg[0] * fps))
    qe_int = min(qf.shape[0], int(seg[1] * fps) + 1)
    if qe_int <= qs_int:
        return []
    q = qf[qs_int:qe_int]
    ql = q.shape[0]
    n, d = rf.shape
    qq = q.mean(axis=0, keepdims=True)
    gscores = (qq @ rf.T)[0]
    # 多子帧投票
    votes = np.zeros(n, dtype=np.float32)
    idx = np.linspace(0, ql - 1, min(n_sub, ql)).astype(int)
    for i in idx:
        sims = q[i] @ rf.T
        k = min(12, n)
        top = np.argpartition(sims, -k)[-k:]
        votes[top] += sims[top]
    votes = votes + 0.5 * gscores
    win = max(2, min(int(round(ql * 1.2)), n))
    srt = np.argsort(-votes)
    cand: List[Dict[str, Any]] = []
    used: List[Tuple[int, int]] = []
    for j in srt:
        c = int(j)
        if any(c >= u0 - win and c <= u1 + win for u0, u1 in used):
            continue
        lo, hi = max(0, c - win // 2), min(n - 1, c + win // 2)
        if hi - lo < 1:
            continue
        best = (float((rf[lo:hi] @ qq.T).mean()), lo)
        # ±step 秒内细滑窗找最佳对齐
        offs = range(-int(step * fps), int(step * fps) + 1, max(1, int(fps / 2)))
        for off in offs:
            l2, h2 = max(0, lo + off), min(n, hi + off)
            if h2 - l2 < 1:
                continue
            s2 = float((rf[l2:h2] @ qq.T).mean())
            if s2 > best[0]:
                best = (s2, l2)
        cand.append({
            "ref_start": round(rts[max(0, best[1])], 3),
            "ref_end": round(rts[min(n - 1, best[1] + (hi - lo))], 3),
            "gray_sim": round(best[0], 4),
            "votes": float(votes[c]),
            "ref_frame": (best[1], min(n, best[1] + (hi - lo))),
        })
        used.append((lo, hi))
        if len(cand) >= topk:
            break
    cand.sort(key=lambda x: x["gray_sim"], reverse=True)
    return cand


# ---------------------------------------------------------------- 全局时序一致性 DP
@dataclass
class MatchSeg:
    segment_index: int = 0
    start: float = 0.0
    end: float = 0.0
    ref_source: str = ""
    ref_name: str = ""
    ref_path: str = ""
    ref_start: float = 0.0
    ref_end: float = 0.0
    similarity: float = 0.0
    gray_sim: float = 0.0
    sem_sim: float = 0.0
    speed: float = 1.0
    audio_score: float = 0.0
    audio_evidence: str = ""
    suspect: bool = False
    strategy: str = "gray"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment_index": self.segment_index, "start": round(self.start, 3),
            "end": round(self.end, 3), "ref_source": self.ref_source,
            "ref_name": self.ref_name, "ref_path": self.ref_path,
            "ref_start": round(self.ref_start, 3), "ref_end": round(self.ref_end, 3),
            "similarity": round(self.similarity, 4), "gray_sim": round(self.gray_sim, 4),
            "sem_sim": round(self.sem_sim, 4), "speed": round(self.speed, 4),
            "audio_score": round(self.audio_score, 4), "audio_evidence": self.audio_evidence,
            "suspect": bool(self.suspect), "strategy": self.strategy,
        }


def _off_center(c: Dict[str, Any]) -> float:
    return (c.get("ref_start", 0.0) + c.get("ref_end", 0.0)) / 2.0


def global_dp(segments: List[Tuple[float, float]],
              candidates: List[List[Dict[str, Any]]], p: Dict[str, Any]) -> List[Dict[str, Any]]:
    """全局时序一致性 DP：每段选一候选，最大化 相似度 - 换段惩罚。

    惩罚项（r2 §3.3-6）：within_shot_jump_pen（同文件偏移突变，按漂移度缩放）、
    cross_file_pen（换原片文件）、drift_scale（段间偏移漂移率）、back_jump_extra（回跳）。
    保底：每段至少保留 top1 候选。
    """
    n = len(segments)
    if n == 0:
        return []
    topk = max(1, int(p.get("clips_topk", 8)))
    clips: List[List[Dict[str, Any]]] = []
    for i in range(n):
        cs = (candidates[i] if i < len(candidates) else [])[:topk]
        if not cs:
            cs = [{"ref_start": 0.0, "ref_end": 0.0, "gray_sim": 0.0, "sem_sim": 0.0,
                   "ref_source": "", "ref_name": "", "ref_path": "", "_file_idx": 0}]
        clips.append(cs)
    neg = -1e9
    best: List[List[float]] = []
    back: List[List[int]] = []
    for i in range(n):
        cs = clips[i]
        sims = [max(c.get("gray_sim", 0.0), c.get("sem_sim", 0.0) or 0.0) for c in cs]
        base = max(sims) if sims else 0.0
        row: List[float] = []
        brow: List[int] = []
        for k in range(len(cs)):
            sc = sims[k] - 0.45 * base
            if i == 0:
                row.append(sc)
                brow.append(-1)
                continue
            # 同段 top 候选的文件引用
            fi = int(cs[k].get("_file_idx", 0) or 0)
            o_now = _off_center(cs[k])
            prev = best[i - 1]
            best_v, best_k = neg, -1
            for kk in range(len(prev)):
                pc = clips[i - 1][kk]
                pfi = int(pc.get("_file_idx", 0) or 0)
                o_prev = _off_center(pc)
                diff = abs(o_now - o_prev)
                pen = 0.0
                if fi != pfi:
                    pen += float(p.get("cross_file_pen", 0.35)) + diff * float(p.get("drift_scale", 0.03))
                    if o_now < o_prev:
                        pen += float(p.get("back_jump_extra", 0.1))
                else:
                    cap = max(float(p.get("drift_cap", 40.0)), 1.0)
                    pen += float(p.get("within_shot_jump_pen", 0.6)) * min(1.0, diff / cap)
                    pen += diff * float(p.get("drift_scale", 0.03))
                    if o_now < o_prev:
                        pen += float(p.get("back_jump_extra", 0.1))
                v = prev[kk] - pen
                if v > best_v:
                    best_v, best_k = v, kk
            row.append(sc + (best_v if best_v > neg else neg))
            brow.append(best_k)
        best.append(row)
        back.append(brow)
    # 回溯
    path: List[int] = []
    if not best:
        return []
    last = max(range(len(best[-1])), key=lambda k: best[-1][k])
    path.append(last)
    for i in range(n - 1, 0, -1):
        last = back[i][last]
        path.append(last)
    path.reverse()
    return [{"segment": segments[i], "cand": clips[i][path[i]]} for i in range(n)]


# ---------------------------------------------------------------- 段治理
def refine_matches(chain: List[Dict[str, Any]], p: Dict[str, Any]) -> List[MatchSeg]:
    segs: List[MatchSeg] = []
    for i, it in enumerate(chain):
        (s, e), c = it["segment"], it["cand"]
        rs, re_ = float(c.get("ref_start", 0.0)), float(c.get("ref_end", s and c.get("ref_end", s) or s))
        if re_ < rs:
            re_, rs = rs, re_
        ms = MatchSeg(
            segment_index=i, start=s, end=e,
            ref_source=c.get("ref_source", ""), ref_name=c.get("ref_name", ""),
            ref_path=c.get("ref_path", ""), ref_start=rs, ref_end=re_,
            gray_sim=float(c.get("gray_sim", 0.0)),
            sem_sim=float(c.get("sem_sim", 0.0) or 0.0),
            similarity=max(float(c.get("gray_sim", 0.0)), float(c.get("sem_sim", 0.0) or 0.0)),
            strategy=c.get("strategy", "gray"),
        )
        segs.append(ms)
    # 合并相邻可衔接段（同一原片、同播放速率、解说时间轴连续）
    merged: List[MatchSeg] = []
    for ms in segs:
        if merged:
            last = merged[-1]
            same = (last.ref_path == ms.ref_path and last.ref_source == ms.ref_source)
            gap = ms.start - last.end
            if same and gap <= float(p.get("merge_tol", 0.55)) and ms.start >= last.end - 0.02:
                # 同速率判定：两段 ref 速率（原片时长/解说时长）一致（允许 ±30% 相对差）
                def _rate(x: MatchSeg) -> float:
                    d = x.end - x.start
                    rd = x.ref_end - x.ref_start
                    return (rd / d) if d > 0 else 0.0
                r1, r2 = _rate(last), _rate(ms)
                rmax = max(abs(r1), abs(r2), 1.0)
                if abs(r1 - r2) < 0.3 * rmax:
                    last.end = ms.end
                    last.ref_end = ms.ref_end
                    last.similarity = (last.similarity + ms.similarity) / 2
                    last.gray_sim = (last.gray_sim + ms.gray_sim) / 2
                    last.sem_sim = (last.sem_sim + ms.sem_sim) / 2
                    continue
        merged.append(ms)
    segs = merged
    # 吸收短段
    min_dur = float(p.get("min_seg_dur", 1.0))
    out: List[MatchSeg] = []
    for i, ms in enumerate(segs):
        if (ms.end - ms.start) < min_dur and out:
            out[-1].end = ms.end
            out[-1].ref_end = ms.ref_end
            continue
        out.append(ms)
    # speed + suspect + 相似度
    gsus = float(p.get("gray_suspect", 0.45))
    dsus = float(p.get("dino_suspect", 0.55))
    for i, ms in enumerate(out):
        dur = ms.end - ms.start
        ref_dur = ms.ref_end - ms.ref_start
        ms.speed = (ref_dur / dur) if dur > 0 else 1.0
        ms.suspect = ms.similarity < gsus or (ms.sem_sim > 0 and ms.sem_sim < dsus)
        ms.segment_index = i
        ms.similarity = round(ms.similarity, 4)
        ms.gray_sim = round(ms.gray_sim, 4)
        ms.sem_sim = round(ms.sem_sim, 4)
    return out


def audio_align_score(q_mel: Any, r_mel: Any, q_start: int, q_len: int,
                      center: int, win: int) -> Tuple[float, int]:
    """log-mel 互相关：q 查询窗 vs r 中心 ±win → (得分, 最优偏移帧)。"""
    import numpy as np
    if q_mel is None or r_mel is None:
        return 0.0, 0
    q = q_mel[q_start:q_start + q_len]
    if q.shape[0] < 1 or r_mel.shape[0] < q.shape[0]:
        return 0.0, 0
    lo = max(0, center - win)
    hi = min(r_mel.shape[0] - q.shape[0], center + win)
    if hi < lo:
        return 0.0, 0
    best_s, best_o = -1.0, center
    for o in range(lo, hi + 1):
        w = r_mel[o:o + q.shape[0]]
        s = float(np.mean(np.sum(q * w, axis=1)))
        if s > best_s:
            best_s, best_o = s, o
    return best_s, best_o


# ---------------------------------------------------------------- 语义精排（DINOv2/CLIP）
class SemanticExtractor:
    """惰性加载 DINOv2（HF transformers）/ Chinese-CLIP；不可用 → available=False。

    模型目录解析：显式 model_dir → env FRAMEWEAVE_DINO_DIR / FRAMEWEAVE_CLIP_DIR →
    deps.resolve_model（DATA_DIR/models 约定 + HF 缓存）。
    回退链：auto → dino_base → dino_small → chinese_clip → None（灰度兜底）。
    """

    def __init__(self, model_dir: str = "", prefer: str = "auto",
                 resolver: Optional[Callable[[str], Optional[str]]] = None):
        self.model_dir = (model_dir or "").strip()
        self.prefer = prefer
        self.resolver = resolver or _default_resolver
        self._model = None
        self._proc = None
        self._name = ""
        self._error = "未初始化"
        self._device = "cpu"

    def _load(self) -> None:
        try:
            import torch
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # noqa: BLE001
            self._device = "cpu"
        for name, d in self._candidates():
            if not d or not os.path.isdir(d):
                continue
            try:
                if name.startswith("dino"):
                    # 无 torchvision：仅加载 AutoModel，预处理走 PIL/numpy 手动管线
                    from transformers import AutoModel
                    model = AutoModel.from_pretrained(d)
                else:
                    from transformers import ChineseCLIPProcessor, ChineseCLIPModel
                    proc = ChineseCLIPProcessor.from_pretrained(d)
                    model = ChineseCLIPModel.from_pretrained(d)
                    self._proc = proc
                model = model.to(self._device).eval()
                self._model, self._name = model, name
                return
            except Exception as e:  # noqa: BLE001
                self._error = f"{name}: {type(e).__name__}: {e}"
                self._model = None
        self._model = None

    def _candidates(self) -> List[Tuple[str, Optional[str]]]:
        if self.model_dir:
            return [("dino", self.model_dir)]
        env_d = os.environ.get("FRAMEWEAVE_DINO_DIR", "").strip()
        env_c = os.environ.get("FRAMEWEAVE_CLIP_DIR", "").strip()
        pref = self.prefer
        out: List[Tuple[str, Optional[str]]] = []
        if pref in ("auto", "dino", "dino_base", "dino_small"):
            order = ["dino_base", "dino_small"] if pref != "dino_small" else ["dino_small"]
            for key, nm in (("dinov2-base", "dino_base"), ("dinov2-small", "dino_small")):
                if nm in order:
                    d = self.resolver(key) or env_d or None
                    out.append((nm, d))
        if pref in ("auto", "chinese_clip"):
            d = self.resolver("chinese-clip") or env_c or None
            out.append(("chinese_clip", d))
        if pref == "dino_base":
            out = [("dino_base", self.resolver("dinov2-base") or env_d or None)]
        return [(n, d) for n, d in out if d]

    def available(self) -> bool:
        if self._model is None:
            self._load()
        return self._model is not None

    def name(self) -> str:
        return self._name or "unavailable"

    def error(self) -> str:
        return self._error

    async def frame_features(self, paths: Sequence[str]) -> Optional[List[Any]]:
        if self._model is None:
            self._load()
        if self._model is None:
            return None
        import torch
        import numpy as np
        try:
            feats = []
            batch = 8
            for i in range(0, len(paths), batch):
                chunk_paths = list(paths[i:i + batch])
                if self._name.startswith("dino"):
                    tensors = [_dino_image_tensor(p) for p in chunk_paths]
                    inp = {"pixel_values": torch.stack(tensors).to(self._device)}
                    with torch.no_grad():
                        out = self._model(**inp)
                    feats.append(out.last_hidden_state[:, 0, :].detach().cpu().numpy())
                else:
                    inp = self._proc(images=chunk_paths, return_tensors="pt")
                    inp = {k: v.to(self._device) for k, v in inp.items()}
                    with torch.no_grad():
                        out = self._model.get_image_features(**inp)
                    feats.append(out.detach().cpu().numpy())
            f = np.concatenate(feats, axis=0).astype(np.float32)
            f = l2norm_rows(f)
            return list(f)
        except Exception as e:  # noqa: BLE001
            self._error = f"frame_features: {type(e).__name__}: {e}"
            return None


def _dino_image_tensor(path: str) -> "Any":
    """DINOv2 图像预处理（无 torchvision）：PIL resize(短边256) → center-crop 224 → 归一化。"""
    import numpy as np
    import torch
    from PIL import Image
    img = Image.open(path).convert("RGB")
    w, h = img.size
    short = min(w, h)
    scale = 256.0 / short
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    img = img.resize((nw, nh), Image.BICUBIC)
    cr, cc = 224, 224
    left = max(0, (nw - cc) // 2)
    top = max(0, (nh - cr) // 2)
    img = img.crop((left, top, left + cc, top + cr))
    arr = np.asarray(img, dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    # BHWC -> BCHW，通道搬移后转 tensor（不复制视图）
    t = torch.from_numpy(arr.transpose(2, 0, 1).copy()).float()
    return t


def _default_resolver(key: str) -> Optional[str]:
    try:
        from app.plugin import deps
        r = deps.resolve_model(key)
        return r["path"] if r.get("ready") else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 语义融合
async def fuse_semantic(segments, candidates, q_gray, file_grays, sem, p,
                        runner: FFmpegRunner, ffmpeg_cmd: List[str],
                        commentary_path: str = "") -> Dict[str, Any]:
    """语义精排：解说侧中心帧 + 原片侧候选帧 → DINOv2/CLIP 特征 → 余弦覆盖灰度 top。

    仅对每段灰度 top3 候选取样帧（clip_fps>0 且模型可用时）；失败静默保留灰度
    （report.semantic.degraded 记录）。
    """
    import numpy as np
    import tempfile
    stats = {"attempted": 0, "ok": 0, "error": ""}
    if sem is None or not sem.available():
        return stats
    if float(p.get("clip_fps", 0)) <= 0:
        return stats
    try:
        for si, (s, e) in enumerate(segments):
            # 解说侧中心帧特征（每段 1 帧，成本可控）
            q_fe = None
            if commentary_path and os.path.isfile(commentary_path):
                center = (s + e) / 2.0
                out = await runner(ffmpeg_cmd + ["-v", "error", "-ss", str(max(0.0, center)),
                                                 "-i", commentary_path, "-frames:v", "1",
                                                 "-vf", "scale=224:224", "-f", "image2", "-"], 60)
                raw = out.stdout if hasattr(out, "stdout") else out
                if raw:
                    fd, tmp = tempfile.mkstemp(suffix=".jpg")
                    try:
                        with os.fdopen(fd, "wb") as f:
                            f.write(raw)
                        qf = await sem.frame_features([tmp])
                        if qf and len(qf) == 1:
                            q_fe = np.asarray(qf[0], dtype=np.float32)
                    finally:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
            if q_fe is None:
                continue
            for c in (candidates[si] if si < len(candidates) else [])[:3]:
                rp = c.get("ref_path", "")
                if not rp or not os.path.isfile(rp):
                    continue
                out = await runner(ffmpeg_cmd + ["-v", "error", "-ss", str(max(0.0, float(c.get("ref_start", 0)) + 0.05)),
                                                 "-i", rp, "-frames:v", "1",
                                                 "-vf", "scale=224:224", "-f", "image2", "-"], 60)
                raw = out.stdout if hasattr(out, "stdout") else out
                if not raw:
                    continue
                fd, tmp = tempfile.mkstemp(suffix=".jpg")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(raw)
                    fe = await sem.frame_features([tmp])
                    if fe and len(fe) == 1:
                        stats["attempted"] += 1
                        ref_fe = np.asarray(fe[0], dtype=np.float32)
                        if ref_fe.shape == q_fe.shape:
                            sim = float(q_fe @ ref_fe)
                            c["sem_sim"] = round(sim, 4)
                            c["strategy"] = "semantic"
                            stats["ok"] += 1
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
    except Exception as e:  # noqa: BLE001
        stats["error"] = f"{type(e).__name__}: {e}"
    return stats


# ---------------------------------------------------------------- 顶层编排
async def run_match(commentary_path: str, originals: List[Dict[str, Any]],
                    preset: str = "balanced", overrides: Optional[Dict[str, Any]] = None,
                    ctx: Optional[Dict[str, Any]] = None,
                    runner: Optional[FFmpegRunner] = None,
                    ffmpeg_cmd: Optional[List[str]] = None,
                    ffprobe_cmd: Optional[List[str]] = None,
                    cache: Optional[FeatureCache] = None,
                    semantic: Optional[SemanticExtractor] = None) -> Dict[str, Any]:
    """B3 匹配主流程。originals: [{path,name,source}]。返回 {matches, report}。"""
    t0 = time.time()
    p = merged_params(preset, overrides)
    ffmpeg_cmd = list(ffmpeg_cmd or ["ffmpeg"])
    ffprobe_cmd = list(ffprobe_cmd or ["ffprobe"])
    runner_ = runner or (lambda cmd, timeout: default_ffmpeg(ctx or {}, cmd, timeout))
    cache = cache or FeatureCache(os.path.join((ctx or {}).get("workdir", "."), "remake_cache"),
                                  enabled=bool(p.get("use_cache", True)))
    if cache.enabled:
        cache.enabled = bool(p.get("use_cache", True))

    # 进度
    progress = (ctx or {}).get("progress")
    def _prog(x: float) -> None:
        if progress:
            try:
                progress(x)
            except Exception:  # noqa: BLE001
                pass

    _prog(0.05)
    # 1. 特征
    q_gray = await extract_gray(commentary_path, p, runner_, ffmpeg_cmd, cache)
    _prog(0.2)
    file_grays: List[Dict[str, Any]] = []
    for o in originals:
        g = await extract_gray(o["path"], p, runner_, ffmpeg_cmd, cache)
        file_grays.append(g)
    _prog(0.4)

    # 2. 解说镜头切分
    segs = detect_shots(q_gray, min_dur=float(p.get("min_seg_dur", 1.0)))
    if not segs:
        segs = [(0.0, q_gray.get("duration", 0.0) or 1.0)]

    # 3. 语义精排器
    sem = semantic
    sem_init_error = ""
    if sem is None and str(p.get("semantic_model", "auto")) != "none":
        try:
            sem = SemanticExtractor(model_dir=str(p.get("model_dir", "") or ""),
                                    prefer=str(p.get("semantic_model", "auto")))
            sem.available()  # 预加载探测（决定 report.degraded）
        except Exception as e:  # noqa: BLE001 —— 初始化异常不阻断匹配链
            sem_init_error = f"{type(e).__name__}: {e}"
            sem = None
    sem_ok = sem is not None and sem.available()

    # 4. 每段候选（多原片）
    candidates: List[List[Dict[str, Any]]] = []
    for s, e in segs:
        seg_cands: List[Dict[str, Any]] = []
        for fi, g in enumerate(file_grays):
            if g.get("frames") is None:
                continue
            cs = gray_candidates(q_gray, (s, e), g, topk=6,
                                 n_sub=int(p.get("n_sub", 5)), step=float(p.get("gray_step", 2.0)))
            for c in cs:
                c["_file_idx"] = fi
                c["ref_source"] = originals[fi].get("source", "")
                c["ref_name"] = originals[fi].get("name", os.path.basename(originals[fi]["path"]))
                c["ref_path"] = originals[fi]["path"]
            seg_cands.extend(cs)
        if not seg_cands:
            seg_cands = [{"ref_start": s, "ref_end": e, "gray_sim": 0.0,
                          "_file_idx": 0, "ref_source": "", "ref_name": "",
                          "ref_path": originals[0]["path"] if originals else ""}]
        seg_cands.sort(key=lambda x: x.get("gray_sim", 0.0), reverse=True)
        candidates.append(seg_cands)
    _prog(0.55)

    # 5. 语义精排融合
    sem_stats: Dict[str, Any] = {"attempted": 0, "ok": 0, "error": ""}
    if sem_ok:
        sem_stats = await fuse_semantic(segs, candidates, q_gray, file_grays, sem, p,
                                        runner_, ffmpeg_cmd, commentary_path)
    _prog(0.7)

    # 6. 全局 DP → 段治理
    chain = global_dp(segs, candidates, p)
    matches = refine_matches(chain, p)
    matches.sort(key=lambda x: x.start)
    _prog(0.8)

    # 7. 音频裁决
    audio_evidence: List[Dict[str, Any]] = []
    audio_hits = {"cache": 0}
    if p.get("audio_refine") and float(p.get("audio_query", 0)) > 0 and float(p.get("audio_win", 0)) > 0:
        q_audio = await extract_audio(commentary_path, p, runner_, ffmpeg_cmd, cache)
        audio_hits["cache"] = q_audio.get("_hit", False)
        r_audios: List[Dict[str, Any]] = []
        for o in originals:
            ra = await extract_audio(o["path"], p, runner_, ffmpeg_cmd, cache)
            r_audios.append(ra)
            audio_hits["cache"] = audio_hits["cache"] and bool(ra.get("_hit", False))
        scope = str(p.get("audio_refine_scope", "suspect"))
        hop = 256 / 16000.0
        for ms in matches:
            if scope == "none" or (scope == "suspect" and not ms.suspect):
                continue
            fi = next((i for i, o in enumerate(originals) if o["path"] == ms.ref_path), None)
            if fi is None or q_audio.get("mel") is None or r_audios[fi].get("mel") is None:
                continue
            qs = max(0, int(ms.start / hop))
            ql = max(1, int(float(p.get("audio_query", 6.0)) / hop))
            center = max(0, int((ms.ref_start + ms.ref_end) / 2.0 / hop))
            win = max(1, int(float(p.get("audio_win", 18.0)) / hop))
            score, off = audio_align_score(q_audio["mel"], r_audios[fi]["mel"], qs, ql, center, win)
            ms.audio_score = float(score)
            ms.audio_evidence = f"log-mel 互相关 offset={off*hop:.2f}s score={score:.3f}"
            if score < float(p.get("audio_suspect", 0.12)):
                ms.suspect = True
            audio_evidence.append({
                "segment_index": ms.segment_index, "ref": ms.ref_name,
                "audio_score": round(float(score), 4),
                "offset_sec": round(float(off * hop), 3),
                "scope": scope,
                "suspect_after_audio": bool(ms.suspect),
                "full_evidence": ms.audio_evidence,
            })
    _prog(0.95)

    report = {
        "preset": preset,
        "preset_explained": PRESET_DOC.get(preset, ""),
        "params": preset_summary(p),
        "semantic": {
            "requested": str(p.get("semantic_model", "auto")),
            "available": bool(sem_ok),
            "model": sem.name() if sem is not None and sem_ok else "none",
            "error": sem.error() if sem is not None else sem_init_error,
            "fused_segments": sem_stats.get("ok", 0),
            "fuse_error": sem_stats.get("error", ""),
            "degraded": bool(sem_stats) and not (sem_stats.get("ok") or 0),
        },
        "cache": cache.stats(),
        "cache_hits": {"gray_q": q_gray.get("_hit", False),
                       "audio": audio_hits},
        "segments": len(segs),
        "matches": len(matches),
        "suspect_count": sum(1 for m in matches if m.suspect),
        "audio_adjudicated": len(audio_evidence),
        "audio_evidence": audio_evidence,
        "dp_penalties": {k: p.get(k) for k in (
            "within_shot_jump_pen", "boundary_jump_pen", "drift_scale",
            "cross_file_pen", "back_jump_extra")},
        "elapsed_sec": round(time.time() - t0, 3),
    }
    _prog(1.0)
    return {"matches": [m.to_dict() for m in matches], "report": report}