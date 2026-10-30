"""文案配画面节点：把解说逐句配到场景段上。

两条路，判据只有一条：**有没有场景描述**。
  有 desc ⇒ 文本相似度匹配（字符 bigram 的 Jaccard）。这条不是 CLIP——没有图像向量，
             比的是「文案和这段画面的文字描述像不像」。
  没有 desc ⇒ 顺序均匀分配，并在结果里标 degraded=true。

为什么降级必须留痕：均匀分配能让流程跑通，但配出来的画面和文案没有语义关系。
如果它和真匹配看起来一样，用户会以为工具「配得不好」，而不是「这条流程少接了一个
场景理解节点」。mode/degraded/warnings 三个字段就是给向导层提示用的。

本节点**不产出时间**。句子的起止要等配音、要等时长适配，那是 timeline.build 的事；
在这里算一遍，下游再算一遍，必然两处不一致。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from app.assets import Asset
from app.nodespec import NodeBase, PortType

_SENT_END = re.compile(r"(?<=[。！？!?；;…])")
_LINE_SEP = re.compile(r"[\r\n]+")


def _read_text(store: Any, asset: Any) -> str:
    """SCRIPT 资产 → 纯文本。既接受 {"text": ...} 也接受裸字符串。"""
    if asset is None:
        return ""
    try:
        data = store.read_json(asset.id) if hasattr(asset, "id") else asset
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(data, dict):
        return str(data.get("text") or data.get("script") or "")
    return str(data or "")


def _split_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in _LINE_SEP.split(str(text or "")):
        for part in _SENT_END.split(raw):
            p = part.strip()
            if p:
                out.append(p)
    return out


def _tokens(s: Any) -> Set[str]:
    """字符 bigram：中文不分词也能用，短句比单词更稳。"""
    t = re.sub(r"\s+", "", str(s or "").lower())
    if not t:
        return set()
    if len(t) == 1:
        return {t}
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _norm_desc(raw: Any) -> List[str]:
    """场景描述归一成按 index 排列的字符串列表；认不出形状就返回空。"""
    if isinstance(raw, dict):
        for key in ("items", "scenes", "descriptions", "data"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            # {"0": "...", "1": "..."} 形状
            try:
                pairs = sorted((int(k), v) for k, v in raw.items() if str(k).isdigit())
            except (TypeError, ValueError):
                return []
            return [str(v or "") for _, v in pairs]
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(str(item.get("text") or item.get("description")
                           or item.get("visual") or ""))
        else:
            out.append(str(item or ""))
    return out


def _uniform(n_lines: int, n_scenes: int, avoid_repeat: bool) -> List[List[int]]:
    """顺序均匀分配；一句一个场景，尽量不重复相邻。"""
    picks: List[List[int]] = []
    for i in range(n_lines):
        idx = min(n_scenes - 1, int(i * n_scenes / max(n_lines, 1)))
        if avoid_repeat and picks and picks[-1] and picks[-1][0] == idx and n_scenes > 1:
            idx = (idx + 1) % n_scenes
        picks.append([idx])
    return picks


class MatchClipNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        lines = _split_lines(_read_text(store, inputs.get("narration")))
        if not lines:
            raise RuntimeError("解说文案为空：narration 端口未连接，或文本里没有可切分的句子")

        scenes_asset = inputs.get("scenes")
        if scenes_asset is None:
            raise RuntimeError("缺少场景输入：请先接一个镜头切分节点")
        try:
            scenes = store.read_json(scenes_asset.id) if hasattr(scenes_asset, "id") else scenes_asset
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("场景资产读取失败: %s" % e)
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError("场景列表为空：上游镜头切分没有产出任何段落")
        n_scenes = len(scenes)

        try:
            min_sim = float(params.get("min_similarity", 0.2) or 0.0)
        except (TypeError, ValueError):
            min_sim = 0.2
        min_sim = min(max(min_sim, 0.0), 1.0)
        try:
            top_k = int(params.get("top_k", 1) or 1)
        except (TypeError, ValueError):
            top_k = 1
        top_k = min(max(top_k, 1), min(5, n_scenes))
        avoid_repeat = bool(params.get("avoid_adjacent_repeat", True))

        warnings: List[str] = []
        desc_asset = inputs.get("desc")
        descs: List[str] = []
        if desc_asset is not None:
            try:
                raw = store.read_json(desc_asset.id) if hasattr(desc_asset, "id") else desc_asset
                descs = _norm_desc(raw)
            except Exception as e:  # noqa: BLE001
                warnings.append("场景描述读取失败，已退回均匀分配: %s" % e)
                descs = []
        if descs and len(descs) < n_scenes:
            warnings.append("场景描述只有 %d 条 < 场景 %d 段，缺的部分按无描述处理"
                            % (len(descs), n_scenes))

        use_text = bool(descs) and any(d.strip() for d in descs)
        items: List[Dict[str, Any]] = []

        if use_text:
            scene_tokens = [_tokens(d) for d in descs]
            prev_idx = -1
            for i, line in enumerate(lines):
                lt = _tokens(line)
                scored = []
                for j, st in enumerate(scene_tokens):
                    s = _jaccard(lt, st)
                    if s > 0:
                        scored.append((s, j))
                scored.sort(key=lambda x: (-x[0], x[1]))
                picked = [(s, j) for s, j in scored if s >= min_sim]
                if not picked and scored:
                    picked = scored[:1]
                    warnings.append("第 %d 句「%s」没有超过 %.2f 的场景，退回最佳候选（%.2f）"
                                    % (i + 1, line[:12], min_sim, scored[0][0]))
                if avoid_repeat and prev_idx >= 0:
                    alt = [(s, j) for s, j in (picked or scored) if j != prev_idx]
                    if alt:
                        picked = alt
                picked = picked[:top_k]
                if not picked:
                    # 整片描述都与该句无关：给一个顺位场景，避免空匹配
                    idx = min(n_scenes - 1, i % n_scenes)
                    picked = [(0.0, idx)]
                    warnings.append("第 %d 句「%s」与所有场景描述都无重叠，按顺序分配"
                                    % (i + 1, line[:12]))
                prev_idx = picked[0][1] if picked else prev_idx
                items.append({"line": i, "text": line,
                              "chars": len(line),
                              "scenes": [{"index": j, "score": round(s, 4)}
                                         for s, j in picked]})
            mode = "text"
        else:
            picks = _uniform(len(lines), n_scenes, avoid_repeat)
            for i, line in enumerate(lines):
                items.append({"line": i, "text": line, "chars": len(line),
                              "scenes": [{"index": j, "score": 0.0} for j in picks[i]]})
            mode = "uniform"
            if not warnings:
                warnings.append("未接场景描述，按顺序均匀分配：画面与文案无语义关系")
            else:
                warnings.append("未接场景描述（或读取失败），按顺序均匀分配")

        # 端口类型是 SEGMENTS —— v3 端口体系里没有 JSON（SDK 的类型表把 JSON 移除了）。
        # 所以 payload 就是段列表本身，模式/降级/告警全部落 asset.meta：
        # 下游 read_json 拿到的是数组，不会被多包一层对象键。
        payload = [{"index": i, "text": it["text"], "chars": it["chars"],
                    "scenes": it["scenes"], "start": None, "end": None}
                   for i, it in enumerate(items)]
        out = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"count": len(payload), "mode": mode,
                        "degraded": mode != "text",
                        "line_count": len(payload), "scene_count": n_scenes,
                        "warnings": list(warnings)}),
            payload=payload)
        return {"match": out}
