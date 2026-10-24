# -*- coding: utf-8 -*-
"""B3 精选复刻匹配节点 —— user/jsm_remake_match（解说片 + 原片[] → 匹配表）。

干净重实现 jieshuomao curated_remake_matcher：
  灰度缩略图粗筛（多子帧投票 + 滑窗精排）→ DINOv2/Chinese-CLIP 语义精排（可回退）
  → 全局时序一致性 DP（灵魂：偏移分段恒定、换段有代价）→ 段治理 → log-mel 音频裁决（灵魂之二）。

契约（R-节点-1 前半）：
  inputs: commentary(VIDEO) + originals(JSON 原片列表) → outputs: matches(JSON 匹配表) + report(JSON)
  匹配表元素：{segment_index,start,end,ref_source,ref_name,ref_path,ref_start,ref_end,
              similarity,gray_sim,sem_sim,speed,audio_score,audio_evidence,suspect,strategy}
  39 参数档位化（fast/balanced/precise）+ advanced JSON 高级参数组；特征磁盘缓存跨运行复用。

依赖（S2-T0 已就绪）：DINOv2-Small/Base（DATA_DIR/models 或 FRAMEWEAVE_DINO_DIR）、
Chinese-CLIP（HF 缓存 / FRAMEWEAVE_CLIP_DIR）；ffmpeg/ffprobe 统一 resolver。
降级链（不阻断）：dino_base → dino_small → chinese_clip → 灰度投票（fast 档零模型）。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

# 插件包目录引导（宿主按 entrypoint 加载 node.py，不自动把包目录加入 sys.path；
# 与 core_draft_export 的 jy_builder 同款约定：先插入本包目录，再做扁平导入）
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

import b3_matcher
from b3_matcher import FeatureCache, SemanticExtractor


def _asset_video_path(store: Any, asset: Any) -> str:
    """解析 VIDEO 资产路径：Asset 或 @id 引用。"""
    if asset is None:
        return ""
    if isinstance(asset, Asset):
        return asset.path or ""
    if isinstance(asset, str):
        if asset.startswith("@"):
            a = store.get(asset[1:])
            return a.path if a else ""
        return asset
    return ""


def _parse_originals(store: Any, raw: Any, fallback_originals: Any) -> List[Dict[str, Any]]:
    """原片列表规范化为 [{path,name,source}]：支持资产 id / 路径 / 对象 / 资产列表。"""
    entries: List[Any] = []
    if isinstance(raw, Asset):
        # 上游 JSON/VIDEO 资产 → 读载荷
        data = None
        try:
            data = store.read_json(raw.id) if raw.path and raw.path.endswith(".json") else raw
        except Exception:  # noqa: BLE001
            data = None
        if data is None:
            # 资产无 JSON 载荷：尝试把资产本身当单条目
            entries = [raw]
        elif isinstance(data, list):
            entries = list(data)          # JSON 数组载荷 → 直接作为条目列表
        else:
            entries = [data]              # 单对象载荷 → 单条目
    elif isinstance(raw, str):
        raw_s = raw.strip()
        if raw_s.startswith("["):
            try:
                entries = json.loads(raw_s)
            except json.JSONDecodeError:
                entries = [raw_s]
        elif raw_s:
            entries = [raw_s]
    elif isinstance(raw, list):
        entries = raw
    elif raw is None and fallback_originals is not None:
        return _parse_originals(store, fallback_originals, None)
    else:
        raise ValueError("originals 必须为 JSON 数组 / 资产列表 / 路径字符串")

    out: List[Dict[str, Any]] = []
    seen = set()
    for e in entries:
        path, name, source = "", "", ""
        if isinstance(e, dict):
            if e.get("path") or e.get("file"):
                path = str(e.get("path") or e.get("file") or "")
                name = str(e.get("name") or "")
                source = str(e.get("source") or "")
            elif e.get("id"):
                a = store.get(str(e["id"]))
                if a is not None:
                    path, name = a.path, str(e.get("name") or os.path.basename(a.path or ""))
                    source = str(e.get("source") or "")
        elif isinstance(e, Asset):
            path, name, source = e.path, os.path.basename(e.path or ""), ""
        elif isinstance(e, str):
            if e.startswith("@"):
                a = store.get(e[1:])
                path = a.path if a else ""
                name = os.path.basename(path) if path else ""
            else:
                path, name = e, os.path.basename(e)
            source = ""
        else:
            continue
        if not path:
            continue
        ap = os.path.normpath(path)
        if ap in seen:
            continue
        seen.add(ap)
        out.append({"path": ap, "name": name or os.path.basename(ap), "source": source})
    if not out:
        raise ValueError("原片列表为空或全部无法解析（请提供视频文件路径/资产 id）")
    return out


class RemakeMatchNode(NodeBase):
    """B3 精选复刻匹配节点。type_id: user/jsm_remake_match"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_remake_match",
            title="精选复刻匹配",
            category="语义",
            description=("B3：解说画面↔原片画面全本地匹配（灰度粗筛 + DINOv2/CLIP 语义精排 + "
                         "全局时序 DP + log-mel 音频裁决），输出取材匹配表；39 参数档位化 + 高级参数组。"),
            inputs=[
                PortSpec(name="commentary", type=PortType.VIDEO, label="解说视频", required=True),
                PortSpec(name="originals", type=PortType.JSON, label="原片列表", required=True),
            ],
            outputs=[
                PortSpec(name="matches", type=PortType.JSON, label="匹配表",
                         description="[{segment_index,start,end,ref_source,ref_name,ref_path,"
                                     "ref_start,ref_end,similarity,speed,...}]"),
                PortSpec(name="report", type=PortType.JSON, label="匹配报告",
                         description="preset/语义模型与降级/缓存命中/DP 统计/音频裁决证据"),
            ],
            params=[
                PortSpec(name="preset", type=PortType.STRING, label="档位预设", default="balanced",
                         widget="select", options=["fast", "balanced", "precise"]),
                PortSpec(name="semantic_model", type=PortType.STRING, label="语义模型", default="auto",
                         widget="select", options=["auto", "dino", "dino_base", "chinese_clip", "none"]),
                PortSpec(name="model_dir", type=PortType.STRING, label="模型目录", default="",
                         required=False, widget="text"),
                PortSpec(name="use_cache", type=PortType.BOOL, label="特征磁盘缓存", default=True,
                         widget="toggle"),
                PortSpec(name="cache_dir", type=PortType.STRING, label="缓存目录", default="",
                         required=False, widget="text"),
                PortSpec(name="fps", type=PortType.INT, label="灰度采样帧率", default=4, widget="number"),
                PortSpec(name="audio_refine", type=PortType.BOOL, label="音频裁决", default=True,
                         widget="toggle"),
                PortSpec(name="min_seg_dur", type=PortType.FLOAT, label="最短段时长(秒)", default=1.0,
                         widget="number"),
                PortSpec(name="advanced", type=PortType.JSON, label="高级参数组(JSON)", default="",
                         required=False, widget="text",
                         description="覆盖全部 39 参数键（如 {\"cross_file_pen\":0.5,\"drift_scale\":0.02}）"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx: Dict[str, Any], inputs: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        store = ctx["store"]
        commentary_asset = inputs.get("commentary")
        commentary_path = _asset_video_path(store, commentary_asset)
        if not commentary_path or not os.path.isfile(commentary_path):
            raise ValueError(f"解说视频路径无效: {commentary_path!r}")

        originals = _parse_originals(store, inputs.get("originals"), inputs.get("originals"))
        for o in originals:
            if not os.path.isfile(o["path"]):
                raise ValueError(f"原片文件不存在: {o['path']}")

        preset = str(params.get("preset", "balanced") or "balanced")
        if preset not in b3_matcher.PRESETS:
            raise ValueError(f"preset 必须是 fast|balanced|precise，当前: {preset!r}")

        overrides: Dict[str, Any] = {}
        adv_raw = params.get("advanced")
        if adv_raw:
            if isinstance(adv_raw, dict):
                overrides.update(adv_raw)
            elif isinstance(adv_raw, str) and adv_raw.strip():
                try:
                    parsed = json.loads(adv_raw)
                    if isinstance(parsed, dict):
                        overrides.update(parsed)
                except json.JSONDecodeError as e:
                    raise ValueError(f"advanced 不是合法 JSON: {e}")
        # 显式参数覆盖
        for k in ("semantic_model", "model_dir", "fps", "audio_refine",
                  "min_seg_dur", "audio_win", "audio_query", "audio_suspect",
                  "use_cache", "subtitle_crop", "bar_detect", "gray_suspect",
                  "dino_suspect", "dino_strong", "cross_file_pen", "within_shot_jump_pen",
                  "boundary_jump_pen", "drift_scale"):
            if k in params and params.get(k) is not None and str(params.get(k)) != "":
                overrides[k] = params[k]

        cache_root = str(params.get("cache_dir") or "").strip() or \
            os.path.join(ctx.get("workdir", "."), "remake_cache")
        cache = FeatureCache(cache_root, enabled=bool(overrides.get("use_cache", True)))
        use_cache = bool(overrides.get("use_cache", True))
        cache.enabled = use_cache

        # ffmpeg/ffprobe 统一解析（fail → 明确错误码）
        from app.plugin import deps
        ff = deps.resolve_binary("ffmpeg", env=os.environ)
        fp = deps.resolve_binary("ffprobe", env=os.environ)
        if not ff.get("ready"):
            raise RuntimeError(deps.coded_message("binary_missing", "ffmpeg", "", ff.get("repair_hint", "")))
        if not fp.get("ready"):
            raise RuntimeError(deps.coded_message("binary_missing", "ffprobe", "", fp.get("repair_hint", "")))
        ffmpeg_cmd = [ff["path"]]
        ffprobe_cmd = [fp["path"]]

        result = await b3_matcher.run_match(
            commentary_path, originals, preset=preset, overrides=overrides,
            ctx=ctx, runner=None, ffmpeg_cmd=ffmpeg_cmd, ffprobe_cmd=ffprobe_cmd,
            cache=cache, semantic=None,
        )
        matches, report = result["matches"], result["report"]

        matches_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"preset": preset, "count": len(matches), "node": "user/jsm_remake_match"}),
            payload=matches)
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"preset": preset}),
            payload=report)
        return {"matches": matches_asset, "report": report_asset}

    # ---- 平台评审辅助：暴露预设文档（供 /api/specs + 前端渲染） ----
    @staticmethod
    def presets_doc() -> Dict[str, str]:
        return dict(b3_matcher.PRESET_DOC)