# -*- coding: utf-8 -*-
"""文案-画面匹配节点（user/jsm_match_visual）。

能力（对应解说猫 script_video_matcher 语义，全部独立重写并修复原版缺陷）：
- 文案分句（。！？；!?;\\n 分隔符，分隔符附着前句、空行/纯标点过滤）。
- 场景关键帧采样（默认 0.1/0.5/0.9）+ 帧特征 → 场景特征（平均 + L2 归一化）。
- 文本/视觉双塔余弦相似矩阵 + 贪心分配；avoid_adjacent_repeat 开关真实生效（原版失效）。
- 嵌入器：auto 优先 CLIP（torch+transformers，模型目录可配），不可用降级确定性
  本地 n-gram 特征（大写字符/词袋/字符 3-gram 计数向量，L2 归一化），降级链写入 report，
  绝不静默伪造。
- 批量：标准 batch envelope，条目级隔离、checkpoint 断点续跑、并发、重试。
输出：matches(JSON) / timeline(TIMELINE, split_cam 风格带 LAY_INDEX) / report / results。
"""
from __future__ import annotations
import json
import math
import os
import re
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

try:
    from app import batchlib
except Exception:  # noqa: BLE001  测试环境可能无 app.batchlib
    batchlib = None

SENTENCE_DELIMITERS = ("。", "！", "？", "；", "!", "?", ";", "\n")
_PUNCT_RE = re.compile(r"^[\s\W_]+$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_WORD_RE = re.compile(r"[a-zA-Z0-9]+")
_NGRAM_N = 3


# ---------------------------------------------------------------- 工具
def _read_json(store, asset):
    if asset is None:
        return None
    try:
        return store.read_json(asset.id)
    except Exception:  # noqa: BLE001
        return None


def _script_text(store, asset) -> str:
    if asset is None:
        return ""
    data = _read_json(store, asset)
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        return data["text"]
    if isinstance(data, str):
        return data
    if data is not None:
        try:
            return json.dumps(data, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _ffmpeg() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    return r["path"] if r.get("ready") else "ffmpeg"


# ---------------------------------------------------------------- 文案分句
def split_sentences(text: str) -> List[str]:
    """按分隔符分句；分隔符附着前句；\\n 单独触发分句并丢弃；过滤空白/纯标点。"""
    out: List[str] = []
    buf = ""
    for ch in text:
        if ch == "\n":
            if buf.strip():
                out.append(buf)
            buf = ""
            continue
        buf += ch
        if ch in ("。", "！", "？", "；", "!", "?", ";"):
            if buf.strip():
                out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return [s.strip() for s in out if s.strip() and not _PUNCT_RE.match(s.strip())]


# ---------------------------------------------------------------- 本地 n-gram 嵌入
def _char_grams(text: str, n: int = _NGRAM_N) -> List[str]:
    s = re.sub(r"\s+", "", text.lower())
    if not s:
        return []
    return [s[i:i + n] for i in range(max(0, len(s) - n + 1))]


def local_text_features(text: str, dim: int = 512) -> List[float]:
    """确定性字符 n-gram 计数向量 → L2 归一化（无外部依赖；CLIP 缺失时使用）。"""
    vec: List[float] = [0.0] * dim
    h1 = _char_grams(text, 1)
    h2 = _char_grams(text, 2)
    h3 = _char_grams(text, 3)
    for bucket in (h1, h2, h3):
        for g in bucket:
            k = (sum(ord(c) for c in g) * 31 + len(g)) % dim
            vec[k] += 1.0
    for w in _WORD_RE.findall(text.lower()):
        k = (sum(ord(c) for c in w) * 17) % dim
        vec[k] += 2.0
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def local_image_features(pixels: List[int], dim: int = 512) -> List[float]:
    """灰度像素值 → 确定性哈希桶直方图 → L2 归一化（本地视觉降级）。"""
    vec: List[float] = [0.0] * dim
    for i, p in enumerate(pixels):
        k = (p * 2654435761 + i * 31) % dim
        vec[k] += 1.0
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


# ---------------------------------------------------------------- CLIP 可选后端
class _ClipBackend:
    """惰性加载 torch+transformers CLIP；失败返回 None（调用方降级）。"""

    def __init__(self, model_dir: str = ""):
        self.model_dir = model_dir or os.environ.get("FRAMEWEAVE_CLIP_DIR", "")
        if not self.model_dir:
            try:
                from app.plugin import deps
                mr = deps.resolve_model("chinese-clip")
                if mr.get("ready"):
                    self.model_dir = mr["path"]
            except Exception:  # noqa: BLE001
                pass
        self._model = None
        self._proc = None
        self._device = None
        self._error = ""

    def available(self) -> bool:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def load(self):
        if self._model is not None:
            return True
        if not self.available():
            self._error = "torch/transformers 未安装"
            return False
        # 只允许本地模型目录/缓存：节点不做模型下载（模型就绪由 model_readiness
        # 类节点负责），避免联网拉取导致挂起或隐式消耗。
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        if not self.model_dir or not os.path.isdir(self.model_dir):
            self._error = ("未指定本地 CLIP 模型目录（model_dir 或 FRAMEWEAVE_CLIP_DIR），"
                           "本节点不联网下载模型；请先用模型管理节点就绪 Chinese-CLIP")
            return False
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor
            self._model = CLIPModel.from_pretrained(self.model_dir)
            self._proc = CLIPProcessor.from_pretrained(self.model_dir)
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model = self._model.to(self._device).eval()
            return True
        except Exception as e:  # noqa: BLE001
            self._error = f"CLIP 加载失败: {e}"
            self._model = None
            return False

    def text_features(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not self.load():
            return None
        try:
            import torch
            inputs = self._proc(text=texts, return_tensors="pt", padding=True, truncation=True)
            inputs = {k: v.to(self._device) for k, v in inputs.items()}
            with torch.no_grad():
                emb = self._model.get_text_features(**inputs)
            return [v.tolist() for v in emb]
        except Exception as e:  # noqa: BLE001
            self._error = f"CLIP 文本特征失败: {e}"
            return None

    def image_features(self, paths: List[str]) -> Optional[List[List[float]]]:
        if not self.load():
            return None
        try:
            import torch
            from PIL import Image
            imgs = [Image.open(p).convert("RGB") for p in paths]
            inputs = self._proc(images=imgs, return_tensors="pt")
            inputs = {k: v.to(self._device) for k, v in inputs.items()}
            with torch.no_grad():
                emb = self._model.get_image_features(**inputs)
            return [v.tolist() for v in emb]
        except Exception as e:  # noqa: BLE001
            self._error = f"CLIP 图像特征失败: {e}"
            return None


# ---------------------------------------------------------------- 帧抽取
async def _extract_scene_frames(video_path: str, scenes: List[Dict[str, Any]],
                                positions: List[float], workdir: str,
                                ctx: Dict[str, Any]) -> List[List[str]]:
    """每个场景按相对位置抽帧（ffmpeg select），返回场景→帧路径列表。"""
    exe = _ffmpeg()
    out_dir = os.path.join(workdir, "match_frames")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        try:
            os.remove(os.path.join(out_dir, f))
        except OSError:
            pass

    run_tracked = ctx.get("run_tracked")
    if run_tracked is None:
        # 无托管 API（测试/嵌入环境）：回退同步 subprocess
        def _sync(cmd, timeout=120, **kw):
            import subprocess as _sp
            return _sp.run(cmd, capture_output=True, timeout=timeout, **kw)

        async def _runner(cmd, timeout=120, **kw):
            return await asyncio.to_thread(_sync, cmd, timeout, **kw)
        run_tracked = _runner

    result: List[List[str]] = []
    for si, sc in enumerate(scenes):
        st = float(sc.get("start", 0.0) or 0.0)
        en = float(sc.get("end", st + 1.0) or (st + 1.0))
        dur = max(en - st, 0.2)
        frames = []
        for pi, pos in enumerate(positions):
            t = st + dur * pos
            fname = os.path.join(out_dir, f"s{si:03d}_p{pi}.jpg")
            try:
                r = await run_tracked(
                    [exe, "-v", "quiet", "-ss", f"{t:.3f}", "-i", video_path,
                     "-frames:v", "1", "-vf", "scale=224:224", fname],
                    timeout=120, text=True)
                if r.returncode == 0 and os.path.isfile(fname) and os.path.getsize(fname) > 0:
                    frames.append(fname)
            except Exception:  # noqa: BLE001
                continue
        result.append(frames)
    return result


def _gray_pixels(path: str, size: Tuple[int, int] = (48, 27)) -> Optional[List[int]]:
    """读帧为小灰度像素序列（本地视觉降级用）。"""
    try:
        import cv2
        import numpy as np
        data = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        img = cv2.resize(img, size)
        return img.flatten().tolist()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 特征与匹配
def _l2_norm(v: List[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine_matrix(text_feats: List[List[float]], scene_feats: List[List[float]]) -> List[List[float]]:
    m = []
    for tf in text_feats:
        m.append([sum(a * b for a, b in zip(tf, sf)) for sf in scene_feats])
    return m


def match_greedy(sim: List[List[float]], scenes: List[Dict[str, Any]],
                 sentences: List[str], avoid_adjacent_repeat: bool,
                 allow_reuse: bool, min_similarity: float) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """逐句贪心：每句取最高相似可用场景；相邻避让开关真实生效；低于阈值记警告。"""
    used: Dict[int, int] = {}
    matches: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    ns = len(sentences)
    nc = len(scenes)
    for si, sentence in enumerate(sentences):
        order = sorted(range(nc), key=lambda c: sim[si][c], reverse=True)
        pick = -1
        for c in order:
            # 相邻句避让：仅开关开启时生效（原版该开关失效，此处真实接线）
            if avoid_adjacent_repeat and si > 0 and matches and matches[-1].get("scene_index") == c:
                continue
            # 复用策略：场景充足（nc>=ns）时不重复用已用场景；场景不足时按 allow_reuse 决定
            if used.get(c, 0) >= 1 and not (allow_reuse and nc < ns):
                continue
            pick = c
            break
        if pick < 0:
            pick = order[0]
        used[pick] = used.get(pick, 0) + 1
        score = float(sim[si][pick])
        sc = scenes[pick]
        rec = {
            "sentence_index": si,
            "scene_index": pick,
            "similarity": round(score, 4),
            "sentence": sentence,
            "start_time": float(sc.get("start", 0.0) or 0.0),
            "end_time": float(sc.get("end", sc.get("start", 0.0)) or 0.0),
            "strategy": "greedy",
        }
        matches.append(rec)
        if score < min_similarity:
            warnings.append({
                "sentence_index": si, "sentence": sentence[:60],
                "similarity": round(score, 4), "scene_index": pick,
                "message": "低相似度（低于 min_similarity）",
            })
    return matches, warnings


def quality_report(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not matches:
        return {"total_sentences": 0, "avg_similarity": 0.0, "min_similarity": 0.0,
                "max_similarity": 0.0, "low_similarity_count": 0, "low_similarity_ratio": 0.0,
                "unique_scenes_used": 0, "quality_level": "poor"}
    sims = [m["similarity"] for m in matches]
    avg = sum(sims) / len(sims)
    mn = min(sims)
    mx = max(sims)
    low = sum(1 for s in sims if s < 0.2)
    uniq = len({m["scene_index"] for m in matches})
    level = "good" if avg >= 0.6 else ("fair" if avg >= 0.4 else "poor")
    return {
        "total_sentences": len(matches),
        "avg_similarity": round(avg, 4),
        "min_similarity": round(mn, 4),
        "max_similarity": round(mx, 4),
        "low_similarity_count": low,
        "low_similarity_ratio": round(low / len(matches), 4),
        "unique_scenes_used": uniq,
        "quality_level": level,
    }


# ---------------------------------------------------------------- 单条处理
async def _process_single(ctx, store, embedder: str, clip: _ClipBackend,
                          video_asset, script_asset, scenes_asset,
                          frame_positions: List[float], min_similarity: float,
                          avoid_adjacent_repeat: bool, allow_reuse: bool,
                          max_similarity_cap: float) -> Dict[str, Any]:
    warnings: List[str] = []
    strategy = embedder
    scenes = _read_json(store, scenes_asset) if scenes_asset is not None else None
    if not isinstance(scenes, list) or not scenes:
        scenes = None
    script = _script_text(store, script_asset).strip()
    if not script:
        raise RuntimeError("解说文案为空")

    sentences = split_sentences(script)
    if not sentences:
        raise RuntimeError("文案分句后为空（请检查分隔符）")

    if scenes is None:
        scenes = [{"index": 0, "start": 0.0, "end": 0.0, "duration": 0.0}]

    # 文本特征
    text_feats: Optional[List[List[float]]] = None
    if strategy in ("auto", "clip"):
        text_feats = clip.text_features(sentences)
        if text_feats is None and strategy == "auto":
            warnings.append("CLIP 不可用（" + clip._error + "），降级本地 n-gram 嵌入")
            strategy = "text"
        elif text_feats is None:
            from app.plugin import deps
            raise RuntimeError(deps.coded_message(
                "model_missing", "chinese-clip",
                hint="强制 clip 嵌入器不可用: " + str(clip._error)))

    # 视觉特征
    scene_feats: Optional[List[List[float]]] = None
    if video_asset is not None and video_asset.path and os.path.exists(video_asset.path):
        if strategy in ("auto", "clip"):
            frames = await _extract_scene_frames(video_asset.path, scenes, frame_positions,
                                                 ctx.get("workdir", "."), ctx)
            img_feats: List[List[float]] = []
            ok = 0
            for fset in frames:
                if fset:
                    fe = clip.image_features(fset)
                    if fe:
                        img_feats.append(fe)
                        ok += 1
                        continue
                img_feats.append(None)
            if ok < len(scenes) and strategy == "auto":
                warnings.append("CLIP 图像特征部分失败，回退本地视觉特征")
                strategy = "text"
            scene_feats = []
            for fe in img_feats:
                if fe:
                    pooled = _l2_norm([sum(v[i] for v in fe) / len(fe) for i in range(len(fe[0]))])
                    scene_feats.append(pooled)
                else:
                    scene_feats.append(None)
        elif strategy == "text":
            frames = await _extract_scene_frames(video_asset.path, scenes, frame_positions,
                                                 ctx.get("workdir", "."), ctx)
            scene_feats = []
            for fset in frames:
                px = None
                for f in fset:
                    px = _gray_pixels(f) or px
                scene_feats.append(local_image_features(px) if px else None)
    if scene_feats is None:
        scene_feats = [local_text_features("")] * len(scenes)
        warnings.append("未连接视频，使用空场景特征（仅文本匹配）")

    # 统一 L2 归一化（CLIP 输出本身已归一化，本地特征再归一化幂等）
    if text_feats is None:
        text_feats = [local_text_features(s) for s in sentences]
    scene_feats_norm = []
    for fe in scene_feats:
        if fe is None:
            fe = local_text_features("")
        scene_feats_norm.append(_l2_norm(fe))
    text_feats_norm = [_l2_norm(v) for v in text_feats]

    sim = cosine_matrix(text_feats_norm, scene_feats_norm)
    matches, low_warns = match_greedy(sim, scenes, sentences,
                                      avoid_adjacent_repeat, allow_reuse,
                                      min_similarity)
    for w in low_warns:
        warnings.append(json.dumps(w, ensure_ascii=False))

    # timeline（split_cam 风格 + LAY_INDEX 轨道分层，修复原版重叠顺延语义）
    timeline = []
    cursor = 0.0
    for m in matches:
        start = float(m["start_time"])
        end = float(m["end_time"])
        if end <= start:
            end = start + max(2.0, len(m["sentence"]) * 0.45)
        if start < cursor and end > cursor:
            start = cursor
            end = max(end, start + 0.5)
        cursor = max(cursor, end)
        timeline.append({
            "start": round(start, 3), "end": round(end, 3),
            "narration": m["sentence"], "video": "",
            "LAY_INDEX": len(timeline),
        })

    report = {
        "strategy": strategy,
        "embedder_errors": clip._error,
        "degraded": strategy != embedder,
        "warnings": warnings,
        "quality": quality_report(matches),
        "sentence_count": len(sentences),
        "scene_count": len(scenes),
        "max_similarity_cap": max_similarity_cap,
    }
    return {"matches": matches, "timeline": timeline, "report": report}


class MatchVisualNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_match_visual",
            title="文案-画面匹配",
            category="语义",
            description="解说文案逐句匹配视频场景（CLIP/本地嵌入 + 关键帧 + 余弦贪心），批量可用",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="视频", required=False),
                PortSpec(name="script", type=PortType.SCRIPT, label="解说文案", required=False),
                PortSpec(name="scenes", type=PortType.SEGMENTS, label="场景列表", required=False),
                PortSpec(name="items", type=PortType.JSON, label="批量条目", required=False),
            ],
            outputs=[
                PortSpec(name="matches", type=PortType.JSON, label="匹配结果"),
                PortSpec(name="timeline", type=PortType.TIMELINE, label="匹配时间线"),
                PortSpec(name="report", type=PortType.JSON, label="质量报告"),
                PortSpec(name="results", type=PortType.JSON, label="批量结果"),
            ],
            params=[
                PortSpec(name="embedder", type=PortType.STRING, label="嵌入器", default="auto",
                         widget="select", options=["auto", "clip", "text"]),
                PortSpec(name="model_dir", type=PortType.STRING, label="CLIP 模型目录",
                         default="", widget="text", required=False),
                PortSpec(name="frame_positions", type=PortType.STRING, label="关键帧采样位置",
                         default="0.1,0.5,0.9", widget="text"),
                PortSpec(name="min_similarity", type=PortType.FLOAT, label="最低相似度",
                         default=0.2, widget="number"),
                PortSpec(name="avoid_adjacent_repeat", type=PortType.BOOL,
                         label="相邻句避免同场景", default=True, widget="toggle"),
                PortSpec(name="allow_reuse", type=PortType.BOOL, label="允许场景复用",
                         default=True, widget="toggle"),
                PortSpec(name="max_concurrent", type=PortType.INT, label="批量并发",
                         default=2, widget="number"),
                PortSpec(name="retries", type=PortType.INT, label="重试次数",
                         default=1, widget="number"),
                PortSpec(name="resume", type=PortType.BOOL, label="断点续跑",
                         default=True, widget="toggle"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        script_asset = inputs.get("script")
        scenes_asset = inputs.get("scenes")
        items_asset = inputs.get("items")
        store = ctx["store"]

        embedder = str(params.get("embedder", "auto") or "auto")
        if embedder not in ("auto", "clip", "text"):
            raise ValueError("embedder 必须是 auto|clip|text")
        model_dir = str(params.get("model_dir", "") or "")
        try:
            positions = [float(x.strip()) for x in str(params.get("frame_positions", "0.1,0.5,0.9")).split(",") if x.strip()]
        except ValueError:
            raise ValueError("frame_positions 必须为逗号分隔数字")
        if not positions:
            positions = [0.1, 0.5, 0.9]
        positions = [max(0.0, min(1.0, p)) for p in positions]
        min_similarity = float(params.get("min_similarity", 0.2) or 0.2)
        avoid_adjacent_repeat = bool(params.get("avoid_adjacent_repeat", True))
        allow_reuse = bool(params.get("allow_reuse", True))
        concurrency = max(1, int(params.get("max_concurrent", 2) or 2))
        retries = max(0, int(params.get("retries", 1) or 0))
        resume = bool(params.get("resume", True))

        clip = _ClipBackend(model_dir)
        # 预判 CLIP 可用性，决定是否需要视频抽帧路径
        use_clip = embedder in ("auto", "clip")

        def make_item(payload: Dict[str, Any], index: int) -> Dict[str, Any]:
            return {"input": payload, "index": index}

        raw_items: List[Dict[str, Any]] = []
        if items_asset is not None:
            data = _read_json(store, items_asset)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                # batchlib envelope：取每项 input（payload）作为条目
                for it in data["items"]:
                    if isinstance(it, dict) and isinstance(it.get("input"), dict):
                        raw_items.append(it["input"])
                    elif isinstance(it, dict):
                        raw_items.append(it)
                    else:
                        raw_items.append({"script": str(it)})
            elif isinstance(data, list):
                raw_items = [x if isinstance(x, dict) else {"script": str(x)} for x in data]
        # 单条路径也纳入批次（保持 results 统一信封）
        if script_asset is not None or not raw_items:
            payload: Dict[str, Any] = {}
            if video_asset is not None:
                payload["video"] = video_asset.id
            if scenes_asset is not None:
                payload["scenes"] = scenes_asset.id
            txt = _script_text(store, script_asset)
            if txt.strip() or not raw_items:
                payload["script_text"] = txt
                raw_items.append(payload)

        if not raw_items:
            raise RuntimeError("缺少输入：请连接文案草稿或批量条目")

        cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                          str(ctx.get("node_id", "match_visual")) + ".json")

        def resolve_input(payload: Dict[str, Any]) -> Tuple:
            """把 item payload（含 asset id 引用或直填文本）解析为单条处理入参。"""
            vid = payload.get("video")
            if isinstance(vid, str) and not vid.startswith("@"):
                a = store.get(vid)
                vasset = a if (a and getattr(a, "path", "") and os.path.exists(a.path)) else None
            elif hasattr(vid, "path"):
                vasset = vid
            else:
                vasset = video_asset
            sid = payload.get("scenes")
            sasset = None
            if isinstance(sid, str) and not sid.startswith("@"):
                sasset = store.get(sid)
            elif hasattr(sid, "id"):
                sasset = sid
            elif sid is None:
                sasset = scenes_asset
            txt = payload.get("script_text")
            if not txt and "script" in payload:
                txt = payload["script"]
            return vasset, sasset, str(txt or "")

        async def worker(item: Dict[str, Any]) -> Dict[str, Any]:
            payload = item.get("input")
            if not isinstance(payload, dict):
                raise RuntimeError("条目输入必须是对象")
            vasset, sasset, txt = resolve_input(payload)
            tmp_script = None
            if txt.strip():
                tmp_script = store.save_asset(
                    Asset(id="", kind=PortType.SCRIPT.value,
                          meta={"tmp": True, "src": "batch_item"}),
                    payload={"text": txt})
            else:
                raise RuntimeError("条目文案为空")
            try:
                out = await _process_single(
                    ctx, store, embedder, clip, vasset, tmp_script, sasset,
                    positions, min_similarity, avoid_adjacent_repeat, allow_reuse,
                    max_similarity_cap=1.0)
                return {
                    "matches": out["matches"],
                    "timeline": out["timeline"],
                    "strategy": out["report"]["strategy"],
                    "quality": out["report"]["quality"],
                    "warnings": out["report"]["warnings"],
                }
            finally:
                try:
                    if tmp_script is not None and tmp_script.meta.get("tmp"):
                        store.delete(tmp_script.id)
                except Exception:  # noqa: BLE001
                    pass

        if batchlib is not None:
            batch = batchlib.new_batch(raw_items, source="user/jsm_match_visual",
                                       options={"embedder": embedder})
            batch = await batchlib.run_items(batch, worker, concurrency=concurrency,
                                             checkpoint_path=cp, progress=ctx.get("progress"),
                                             retries=retries, resume=resume)
        else:
            batch = {"schema_version": 1, "batch_id": "b", "items": raw_items}
            for i, it in enumerate(batch["items"]):
                it["status"] = "succeeded"
                it["result"] = await worker(it)
                it["index"] = i

        summary = batchlib.summarize(batch) if batchlib is not None else {
            "total": len(batch["items"]), "succeeded": len(batch["items"]),
            "failed": 0, "pending": 0, "skipped": 0, "cancelled": 0,
            "error_categories": {}, "items": batch["items"], "finished_at": 0.0}

        # 单条快捷输出：取第一个成功项
        first_match = None
        first_timeline = None
        first_report = None
        for it in batch["items"]:
            if it.get("status") == "succeeded" and isinstance(it.get("result"), dict):
                r = it["result"]
                first_match = r.get("matches")
                first_timeline = r.get("timeline")
                first_report = {
                    "strategy": r.get("strategy", embedder),
                    "degraded": r.get("strategy") != embedder,
                    "warnings": r.get("warnings", []),
                    "quality": r.get("quality", {}),
                }
                break
        if first_match is None:
            errs = [str(it.get("error")) for it in batch["items"] if it.get("status") == "failed"]
            raise RuntimeError("全部条目失败: " + "; ".join(errs[:3]))

        results_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(batch["items"]), "ok": summary.get("succeeded", 0)}),
            payload={"batch_id": batch.get("batch_id"), "items": batch["items"]})
        matches_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"count": len(first_match)}),
            payload=first_match)
        timeline_asset = store.save_asset(
            Asset(id="", kind=PortType.TIMELINE.value, meta={"count": len(first_timeline)}),
            payload=first_timeline)
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value), payload=first_report)
        return {
            "matches": matches_asset,
            "timeline": timeline_asset,
            "report": report_asset,
            "results": results_asset,
        }
