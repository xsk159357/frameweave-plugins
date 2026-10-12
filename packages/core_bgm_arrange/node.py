"""BGM 自动配乐编排节点（确定性加权随机 + 叙事 cue 驱动）。

曲线：内容点（timeline/segments 文本命中 cue，或 roles 输入预映射）
      → 角色/强度驱动选曲权重 → 加权随机（种子可复现）→ 时间线布局
      （内容段 ≥ min_segment、结尾段 ≥ terminal、交叉淡化）。

曲库：内置 33 首剪映曲目（bgm_catalog.json），safe_ranges 控制素材安全剪区间。
实现独立重写，不复用原版源码；种子缺省由内容派生，同一输入多次运行稳定。
"""
from __future__ import annotations
import hashlib
import json
import os
import random
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bgm_catalog.json")


def _load_catalog(catalog_json: str = "") -> Dict[str, Any]:
    if catalog_json and catalog_json.strip():
        data = json.loads(catalog_json)
        if isinstance(data, dict) and data.get("tracks"):
            return _normalize_catalog(data)
        raise ValueError("catalog_json 必须是 {tracks:[...]} 结构")
    with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
        return _normalize_catalog(json.load(f))


def _normalize_catalog(data: Dict[str, Any]) -> Dict[str, Any]:
    tracks = []
    for t in data.get("tracks", []):
        if not isinstance(t, dict) or not t.get("track_id"):
            continue
        safe = t.get("safe_ranges") or [(0.0, 30.0)]
        tracks.append({
            "track_id": str(t["track_id"]),
            "music_id": str(t.get("music_id", "")),
            "role": str(t.get("role", "推进")),
            "intensity": int(t.get("intensity", 3) or 3),
            "default_volume": float(t.get("default_volume", 0.24) or 0.24),
            "safe_ranges": [[float(a), float(b)] for a, b in safe],
        })
    cues = []
    for row in data.get("content_cues", []):
        if isinstance(row, list) and len(row) == 3:
            cues.append((str(row[0]), int(row[1]), [str(k) for k in row[2]]))
    fallbacks = {str(k): [str(x) for x in v] for k, v in (data.get("role_fallbacks") or {}).items()}
    story = {str(k): [str(v[0]), int(v[1])] for k, v in (data.get("story_node_roles") or {}).items()}
    return {
        "tracks": tracks,
        "content_cues": cues,
        "role_fallbacks": fallbacks,
        "story_node_roles": story,
        "content_section_limits": data.get("content_section_limits", [[300, 3], [450, 4], [600, 5]]),
        "crossfade_us": int(data.get("crossfade_us", 800000) or 800000),
        "max_content_sections": int(data.get("max_content_sections", 6) or 6),
        "min_content_segment_us": int(data.get("min_content_segment_us", 15000000) or 15000000),
        "min_terminal_segment_us": int(data.get("min_terminal_segment_us", 30000000) or 30000000),
    }


def _section_limit(catalog: Dict[str, Any], total_s: float) -> int:
    limits = sorted((int(a), int(b)) for a, b in catalog.get("content_section_limits", []))
    cur = 3
    for limit_s, count in limits:
        if total_s >= limit_s / 1e9:
            cur = count
    return min(catalog.get("max_content_sections", 6), max(3, cur))


def _text_fields(seg) -> str:
    if not isinstance(seg, dict):
        return ""
    for k in ("text", "narration", "source_text", "content", "title"):
        v = seg.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _derive_seed(content: str, options: dict) -> str:
    raw = json.dumps({"content": content, "options": options}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _choose_track(rng: random.Random, catalog: Dict[str, Any], role: str, intensity: int,
                  used: set, recent: List[str]) -> Dict[str, Any]:
    """加权随机：角色权重 + 强度匹配 + 去重（used）与近期降权。"""
    tracks = catalog["tracks"]
    fallback_roles = catalog.get("role_fallbacks", {}).get(role, [role])
    weights: List[float] = []
    for t in tracks:
        w = 0.3
        if t["role"] == role:
            w += 1.0
        elif t["role"] in fallback_roles[1:]:
            w += 0.6
        w += max(0, 1.0 - abs(t["intensity"] - intensity) * 0.15)
        if t["track_id"] in used:
            w *= 0.05
        if t["track_id"] in recent:
            w *= 0.5
        weights.append(w)
    total_w = sum(weights)
    pick = rng.random() * total_w
    for t, w in zip(tracks, weights):
        pick -= w
        if pick <= 0:
            return t
    return tracks[0]


def _safe_duration(track: Dict[str, Any], target_s: float) -> Tuple[float, float]:
    """返回 (源起点秒, 素材安全时长秒)：在 safe_ranges 内裁剪。"""
    ranges = track.get("safe_ranges") or [[0.0, 30.0]]
    for a, b in ranges:
        span = float(b) - float(a)
        if span >= target_s:
            return float(a), target_s
    best_a, best_b = ranges[0]
    for a, b in ranges:
        if (float(b) - float(a)) > (float(best_b) - float(best_a)):
            best_a, best_b = a, b
    return float(best_a), float(best_b) - float(best_a)


class BgmArrangeNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/bgm_arrange",
            title="BGM 自动配乐编排",
            category="音频",
            description="叙事 cue→角色强度、曲库加权随机选曲、确定性种子、交叉淡化",
            inputs=[
                PortSpec(name="timeline", type=PortType.TIMELINE, label="台词时间线", required=False),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="分段列表", required=False),
                PortSpec(name="roles", type=PortType.JSON, label="预映射角色", required=False),
                PortSpec(name="duration", type=PortType.FLOAT, label="总时长(秒)", required=False),
            ],
            outputs=[
                PortSpec(name="bgm_timeline", type=PortType.TIMELINE, label="BGM 时间线"),
                PortSpec(name="report", type=PortType.JSON, label="配乐报告"),
            ],
            params=[
                PortSpec(name="seed", type=PortType.STRING, label="随机种子", default="", widget="text"),
                PortSpec(name="fade_ms", type=PortType.INT, label="交叉淡化(ms)", default=800, widget="number"),
                PortSpec(name="min_segment_s", type=PortType.FLOAT, label="内容段最短(秒)", default=15.0,
                         widget="number"),
                PortSpec(name="terminal_segment_s", type=PortType.FLOAT, label="结尾段最短(秒)", default=30.0,
                         widget="number"),
                PortSpec(name="max_sections", type=PortType.INT, label="内容段上限", default=6, widget="number"),
                PortSpec(name="catalog_json", type=PortType.STRING, label="曲库覆盖(JSON)", default="",
                         widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        catalog = _load_catalog(str(params.get("catalog_json", "") or ""))

        # ---- 1. 汇总素材时间线 ----
        timeline_asset = inputs.get("timeline")
        segments_asset = inputs.get("segments")
        roles_asset = inputs.get("roles")
        raw_rows: List[Dict[str, Any]] = []
        if timeline_asset is not None:
            data = store.read_json(timeline_asset.id)
            if isinstance(data, dict):
                raw_rows.extend(data.get("segments") or data.get("items") or [])
            elif isinstance(data, list):
                raw_rows.extend(data)
        if segments_asset is not None:
            data = store.read_json(segments_asset.id)
            if isinstance(data, list):
                raw_rows.extend(data)
        dur_input = inputs.get("duration")
        total_s = float(dur_input) if (isinstance(dur_input, (int, float)) and dur_input > 0) else None
        if total_s is None and raw_rows:
            ends = [float(r.get("end", 0) or 0) for r in raw_rows if isinstance(r, dict)]
            if ends:
                total_s = max(ends)

        # ---- 2. 内容点 ----
        role_points: List[Dict[str, Any]] = []
        if roles_asset is not None:
            rdata = store.read_json(roles_asset.id)
            if isinstance(rdata, list):
                for r in rdata:
                    if isinstance(r, dict) and r.get("role"):
                        role_points.append(r)
        for seg in raw_rows:
            text = _text_fields(seg)
            if not text:
                continue
            for role, intensity, keywords in catalog["content_cues"]:
                for kw in keywords:
                    if kw in text:
                        role_points.append({"start": seg.get("start"), "end": seg.get("end"),
                                            "text": text, "role": role, "intensity": intensity})
                        break
                else:
                    continue
                break
        warnings = []
        used_role_merge = []
        for rp in role_points:
            if rp not in used_role_merge:
                used_role_merge.append(rp)
        role_points = used_role_merge

        # ---- 3. 布局 ----
        if not total_s or total_s <= 0:
            total_s = sum(
                t["safe_ranges"][0][1] - t["safe_ranges"][0][0] for t in catalog["tracks"][:3]) or 120.0
        fade_s = max(0.0, int(params.get("fade_ms", 800) or 800) / 1000.0)
        min_content_s = max(5.0, float(params.get("min_segment_s", 15.0) or 15.0))
        terminal_s = max(10.0, float(params.get("terminal_segment_s", 30.0) or 30.0))
        limit = min(int(params.get("max_sections", 0) or 0) or _section_limit(catalog, total_s),
                    _section_limit(catalog, total_s))
        if len(role_points) > limit:
            step = len(role_points) / limit
            picked = []
            for i in range(limit):
                picked.append(role_points[int(i * step)])
            role_points = picked
            warnings.append(f"内容点 {len(used_role_merge)} 个超上限，按均匀抽样保留 {limit} 个")

        seed = str(params.get("seed", "") or "").strip() or _derive_seed(
            json.dumps([r.get("text") for r in role_points], ensure_ascii=False),
            {"total": round(total_s, 1), "limit": limit})
        rng = random.Random(seed)
        progress(0.2)

        # 目标锚点（均匀铺开 + 末尾收束点）
        anchors: List[float] = []
        if role_points:
            n = len(role_points)
            for i, rp in enumerate(role_points):
                t = (i + 1) / (n + 1) * total_s
                anchors.append(t)
        used: set = set()
        recent: List[str] = []
        segments_out: List[Dict[str, Any]] = []

        def _add_seg(role: str, intensity: int, t_start: float, t_end: float) -> None:
            track = _choose_track(rng, catalog, role, intensity, used, recent)
            used.add(track["track_id"])
            recent.append(track["track_id"])
            if len(recent) > 4:
                recent.pop(0)
            src_start, src_dur = _safe_duration(track, max(1.0, t_end - t_start))
            segments_out.append({
                "track_id": track["track_id"],
                "music_id": track["music_id"],
                "role": role,
                "intensity": intensity,
                "source_start": round(src_start, 3),
                "source_duration": round(src_dur, 3),
                "target_start": round(t_start, 3),
                "target_end": round(t_end, 3),
                "target_duration": round(max(0.0, t_end - t_start), 3),
                "volume": track["default_volume"],
                "fade_in_s": round(min(fade_s, (t_end - t_start) / 2), 3),
                "fade_out_s": round(min(fade_s, (t_end - t_start) / 2), 3),
            })

        # Rust-style 顺序填充：内容点段 + 其间过渡段 + 结尾收束段
        cursor = 0.0
        for idx, t in enumerate(anchors):
            role = role_points[idx].get("role", "推进")
            intensity = int(role_points[idx].get("intensity", 3) or 3)
            seg_dur = min(max(min_content_s, total_s * 0.12), terminal_s + 5.0)
            seg_start = max(cursor, t - seg_dur / 2)
            seg_end = min(total_s, seg_start + seg_dur)
            if seg_end <= seg_start + 0.05:
                continue
            if seg_start > cursor + 1.0:
                gap_end = seg_start
                _add_seg("推进", 2 if (gap_end - cursor) < 40 else 3, cursor, gap_end)
            _add_seg(role, intensity, seg_start, seg_end)
            cursor = seg_end
        if total_s - cursor >= min(10.0, terminal_s):
            _add_seg("收束", 3, cursor, total_s)
        else:
            _add_seg("收束", 2, max(cursor, total_s - terminal_s), total_s)

        segments_out.sort(key=lambda s: s["target_start"])
        # 相邻段淡入淡出重叠修正（交叉淡化由 target 区间天然衔接）
        for i in range(len(segments_out) - 1):
            if segments_out[i]["target_end"] > segments_out[i + 1]["target_start"]:
                segments_out[i]["target_end"] = segments_out[i + 1]["target_start"]
                segments_out[i]["target_duration"] = round(
                    segments_out[i]["target_end"] - segments_out[i]["target_start"], 3)
        progress(0.8)

        # ---- 4. 输出 ----
        timeline_asset = store.save_asset(
            Asset(id="", kind=PortType.TIMELINE.value, meta={"role": "bgm", "count": len(segments_out)}),
            payload={"segments": segments_out, "schema": "bgm_segments", "total_duration": round(total_s, 3)})
        unknown_cues = []
        report = {
            "seed": seed,
            "total_duration_s": round(total_s, 3),
            "content_points": len(role_points),
            "content_limit": limit,
            "segments": segments_out,
            "tracks_used": list(used),
            "unknown_cues": unknown_cues,
            "warnings": warnings,
            "catalog": {"count": len(catalog["tracks"]), "external": bool(catalog_json_param(params))},
        }
        report_asset = store.save_asset(Asset(id="", kind=PortType.JSON.value, meta={"role": "bgm_report"}),
                                        payload=report)
        progress(1.0)
        return {"bgm_timeline": timeline_asset, "report": report_asset}


def catalog_json_param(params: Dict[str, Any]) -> str:
    return str(params.get("catalog_json", "") or "")