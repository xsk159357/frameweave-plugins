# -*- coding: utf-8 -*-
"""音色库节点（core/voice_library，C3）。

214 条 tts_library.json（edge 83 / volcano 94 / aliyun 31 / index 6）作为包内资源
（data/tts_library.json，唯一数据源——T6 双份数据不复发的载体）；
本节点只读检索输出 VoiceProfile 列表：
  - id 统一模型 = "{provider}:{native_id}"（与数据文件完全一致，绝不在代码中再造副本）；
  - keywords 搜索：query 空格分词，对 name/id/native_id/tags/categories/description
    不区分大小写全匹配（AND）；
  - 可按 provider / lang / category 筛选、limit 限条。
core/tts 音色下拉经 voice_options() 动态加载本数据（核心单源，不内嵌第二份）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, PortType

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_FILE = os.path.join(_PKG_DIR, "data", "tts_library.json")

PROVIDERS = ["edge", "volcano", "aliyun", "index"]


def load_library() -> Dict[str, Any]:
    """加载音色库资源（唯一数据源）。缺失/损坏 → 明确 RuntimeError（不伪造空库）。"""
    if not os.path.isfile(_DATA_FILE):
        raise RuntimeError("resource_missing: 音色库资源缺失: %s" % _DATA_FILE)
    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"resource_invalid: 音色库资源无法解析: {e}") from e
    voices = data.get("voices") if isinstance(data, dict) else None
    if not isinstance(voices, list):
        raise RuntimeError("resource_invalid: tts_library.json 缺少 voices 数组")
    return {"version": data.get("version", 1), "voices": voices,
            "hidden_voice_ids": data.get("hidden_voice_ids") or [],
            "voice_name_overrides": data.get("voice_name_overrides") or {}}


def _matches_keywords(voice: Dict[str, Any], tokens: List[str]) -> bool:
    if not tokens:
        return True
    haystack = " ".join([
        str(voice.get("id", "")), str(voice.get("name", "")),
        str(voice.get("native_id", "")), str(voice.get("provider", "")),
        " ".join(str(x) for x in (voice.get("tags") or [])),
        " ".join(str(x) for x in (voice.get("categories") or [])),
        str(voice.get("description", "")),
    ]).lower()
    return all(tok in haystack for tok in tokens)


def search_voices(query: str = "", provider: str = "all", lang: str = "",
                  category: str = "", limit: int = 200) -> Dict[str, Any]:
    """检索：provider/lang/category 筛选 + keywords 搜索 + limit。返回 {voices, total, filtered}。"""
    data = load_library()
    voices: List[Dict[str, Any]] = []
    for v in data["voices"]:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        if provider and provider != "all" and str(v.get("provider", "")) != provider:
            continue
        if lang:
            vlang = str(v.get("lang", ""))
            if vlang != lang and not vlang.startswith(lang):
                continue
        if category and category not in [str(x) for x in (v.get("categories") or [])]:
            continue
        voices.append(v)
    total = len(voices)
    tokens = [t.lower() for t in str(query or "").split() if t]  # 关键词统一小写（大小写不敏感）
    if tokens:
        voices = [v for v in voices if _matches_keywords(v, tokens)]
    filtered = len(voices)
    if limit and limit > 0:
        voices = voices[:limit]
    return {"voices": voices, "total": total, "filtered": filtered, "returned": len(voices),
            "data_version": data.get("version"), "provider_filter": provider or "all"}


class VoiceLibraryNode(NodeBase):
    """音色库（C3）。type_id: core/voice_library"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/voice_library",
            title="音色库（C3）",
            category="音频",
            description="214 条 tts_library.json 只读检索：VoiceProfile 列表（id=provider:native_id）、keywords/提供商/语言/分类筛选",
            inputs=[],
            outputs=[
                PortSpec("voices", PortType.JSON, label="音色列表", required=True),
                PortSpec("report", PortType.JSON, label="检索报告", required=False),
            ],
            params=[
                PortSpec("query", PortType.STRING, label="关键词", required=False, default="", widget="text",
                         description="多词空格分隔全匹配 name/id/native_id/tags/categories/description"),
                PortSpec("provider", PortType.STRING, label="提供商", default="all",
                         widget="select", options=["all"] + list(PROVIDERS)),
                PortSpec("lang", PortType.STRING, label="语言", required=False, default="", widget="text"),
                PortSpec("category", PortType.STRING, label="分类", required=False, default="", widget="text"),
                PortSpec("limit", PortType.INT, label="条数上限", default=0, widget="number",
                         description="0=不限制（默认全量 214）"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        store = ctx["store"]
        query = str(params.get("query", "") or "")
        provider = str(params.get("provider", "all") or "all")
        lang = str(params.get("lang", "") or "")
        category = str(params.get("category", "") or "")
        try:
            limit = max(0, int(params.get("limit", 0) or 0))  # 0=不限制（默认全量 214）
        except (TypeError, ValueError):
            limit = 0
        result = search_voices(query, provider, lang, category, limit)
        voices_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(result["voices"]), "tool": "core/voice_library"}),
            payload=result["voices"])
        by_provider: Dict[str, int] = {}
        for v in result["voices"]:
            by_provider[str(v.get("provider", "?"))] = by_provider.get(str(v.get("provider", "?")), 0) + 1
        report = {
            "ok": True,
            "tool": "core/voice_library",
            "data_version": result["data_version"],
            "total_matching_filter": result["total"],
            "keyword_matched": result["filtered"],
            "returned": result["returned"],
            "filters": {"query": query, "provider": provider, "lang": lang, "category": category},
            "by_provider": by_provider,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"tool": "core/voice_library"}),
            payload=report)
        return {"voices": voices_asset, "report": report_asset}