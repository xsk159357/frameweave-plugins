"""OST 原声穿插选择节点。

流程（对齐逆向语义，独立实现并修复原版缺陷）：
1. 候选收集：原片对白预筛 [min_duration, max_duration]，超时间线范围剔除。
2. 数量：select_count = round(候选数 × ratio)。
3. 打分：
   - heuristic：情绪爆点（！结尾）/信息爆点（数字）/冲突爆点（疑问句/长句）
     + 时长加成；取 top 分数（锚点优先）。
   - llm_first：优先 llm_scores 输入 {id|index: score}（鲁棒解析：键支持
     id 或序号、数值或字符串分数），缺失项回退启发式。
   - duration：按时长降序。
4. 后处理：>max_duration 截断保留尾段；相邻（gap ≤ merge_gap_s）真实合并。
5. 时间线重构：解说段保留 + OST 段按原时间插入（ost_source 保留原始区间）。
"""
from __future__ import annotations
import json
import re
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_NUM_RE = re.compile(r"[0-9]")
_EMO_RE = re.compile(r"[！!]+$")
_Q_RE = re.compile(r"[？?]")
_DIGIT_RE = re.compile(r"[0-9]+(?:[.,][0-9]+)?")


def _heuristic_score(text: str, duration: float) -> Dict[str, Any]:
    score = 0.5
    detail: Dict[str, Any] = {}
    if _EMO_RE.search(text):
        score += 2.0
        detail["emotion"] = 2.0
    if _NUM_RE.search(text):
        score += 1.5
        detail["info"] = 1.5
    if _Q_RE.search(text):
        score += 1.0
        detail["conflict"] = 1.0
    if len(text) >= 25:
        score += 0.5
        detail["length"] = 0.5
    detail["duration_bonus"] = round(min(1.0, duration / 15.0), 3)
    score += detail["duration_bonus"]
    return {"score": round(score, 3), "detail": detail}


def _robust_llm_scores(raw) -> Dict[str, float]:
    """鲁棒解析 LLM 评分输入（数组/对象/键含 id/index）。"""
    out: Dict[str, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                if isinstance(v, dict) and "score" in v:
                    try:
                        out[str(k)] = float(v["score"])
                    except (TypeError, ValueError):
                        pass
    elif isinstance(raw, list):
        for i, v in enumerate(raw):
            if isinstance(v, dict):
                kid = str(v.get("id", v.get("index", i)))
                try:
                    out[kid] = float(v.get("score", 0))
                except (TypeError, ValueError):
                    pass
            elif isinstance(v, (int, float)):
                out[str(i)] = float(v)
    return out


class OstSelectNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/ost_select",
            title="OST 原声穿插选择",
            category="音频",
            description="对白候选筛选→打分→相邻合并→时间线重构",
            inputs=[
                PortSpec(name="dialogues", type=PortType.SUBTITLE, label="原片对白", required=True),
                PortSpec(name="timeline", type=PortType.TIMELINE, label="解说时间线", required=False),
                PortSpec(name="cues", type=PortType.JSON, label="锚点提示", required=False),
                PortSpec(name="llm_scores", type=PortType.JSON, label="LLM 评分", required=False),
            ],
            outputs=[
                PortSpec(name="mixed_timeline", type=PortType.TIMELINE, label="重构时间线"),
                PortSpec(name="report", type=PortType.JSON, label="选择报告"),
            ],
            params=[
                PortSpec(name="ratio", type=PortType.FLOAT, label="OST 占比", default=0.3, widget="number"),
                PortSpec(name="min_duration", type=PortType.FLOAT, label="最短候选(秒)", default=2.0,
                         widget="number"),
                PortSpec(name="max_duration", type=PortType.FLOAT, label="最长候选(秒)", default=20.0,
                         widget="number"),
                PortSpec(name="anchor_count", type=PortType.INT, label="锚点数", default=5, widget="number"),
                PortSpec(name="merge_gap_s", type=PortType.FLOAT, label="相邻合并阈值(秒)", default=1.0,
                         widget="number"),
                PortSpec(name="scoring", type=PortType.STRING, label="打分方式", default="heuristic",
                         widget="select", options=["heuristic", "duration", "llm_first"]),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        dlg_asset = inputs.get("dialogues")
        if dlg_asset is None:
            raise RuntimeError("缺少原片对白输入（dialogues）")
        dialogues = store.read_json(dlg_asset.id)
        if isinstance(dialogues, dict):
            dialogues = dialogues.get("segments") or dialogues.get("items") or []
        if not isinstance(dialogues, list):
            raise RuntimeError("dialogues 必须是列表")
        progress(0.1)

        timeline_rows: List[Dict[str, Any]] = []
        tl_asset = inputs.get("timeline")
        if tl_asset is not None:
            tl = store.read_json(tl_asset.id)
            if isinstance(tl, dict):
                timeline_rows = tl.get("segments") or tl.get("items") or []
            elif isinstance(tl, list):
                timeline_rows = tl

        timeline_range: Optional[List[float]] = None
        if timeline_rows:
            starts = [float(r.get("start", 0) or 0) for r in timeline_rows]
            ends = [float(r.get("end", 0) or 0) for r in timeline_rows]
            if ends:
                timeline_range = [min(starts), max(ends)]

        min_dur = max(0.1, float(params.get("min_duration", 2.0) or 2.0))
        max_dur = max(min_dur, float(params.get("max_duration", 20.0) or 20.0))
        try:
            ratio = max(0.0, min(1.0, float(params.get("ratio", 0.3) or 0.3)))
        except (TypeError, ValueError):
            ratio = 0.3

        # ---- 候选收集 + 预筛 ----
        candidates: List[Dict[str, Any]] = []
        filtered: Dict[str, int] = {"too_short": 0, "too_long": 0, "out_of_range": 0, "no_text": 0}
        for i, d in enumerate(dialogues):
            if not isinstance(d, dict):
                continue
            text = str(d.get("text") or "").strip()
            if not text:
                filtered["no_text"] += 1
                continue
            start = float(d.get("start", 0) or 0)
            end = float(d.get("end", 0) or start)
            duration = float(d.get("duration", 0) or 0)
            if duration <= 0:
                duration = max(0.0, end - start)
            if duration < min_dur:
                filtered["too_short"] += 1
                continue
            if duration > max_dur * 3:  # 超 3 倍上限直接剔除（过长）
                filtered["too_long"] += 1
                continue
            if timeline_range and (end <= timeline_range[0] or start >= timeline_range[1]):
                filtered["out_of_range"] += 1
                continue
            candidates.append({"id": str(d.get("id", d.get("segment_id", i))), "index": i,
                               "text": text, "start": round(start, 3), "end": round(end, 3),
                               "duration": round(duration, 3)})
        progress(0.3)
        if not candidates:
            raise RuntimeError("没有候选对白（全部被预筛剔除）")

        select_count = max(1, min(len(candidates), round(len(candidates) * ratio)))

        # ---- 打分 ----
        scoring = str(params.get("scoring", "heuristic") or "heuristic")
        llm_map: Dict[str, float] = {}
        if inputs.get("llm_scores") is not None:
            llm_map = _robust_llm_scores(store.read_json(inputs["llm_scores"].id))
        for c in candidates:
            if scoring == "duration":
                c["score"] = round(c["duration"], 3)
                c["score_detail"] = {"mode": "duration"}
            elif scoring == "llm_first" and llm_map:
                got = llm_map.get(c["id"])
                if got is None:
                    got = llm_map.get(str(c["index"]))
                if got is None:
                    hs = _heuristic_score(c["text"], c["duration"])
                    got = hs["score"]
                    c["score_detail"] = hs["detail"]
                c["score"] = round(got, 3)
                c["score_detail"] = c.get("score_detail", {"mode": "llm"})
            else:
                hs = _heuristic_score(c["text"], c["duration"])
                c["score"] = hs["score"]
                c["score_detail"] = hs["detail"]
        candidates.sort(key=lambda c: (-c["score"], c["start"]))
        selected = candidates[:select_count]
        selected.sort(key=lambda c: c["start"])
        progress(0.5)

        # ---- 后处理：截断 + 相邻合并 ----
        merge_gap = max(0.0, float(params.get("merge_gap_s", 1.0) or 1.0))
        osts: List[Dict[str, Any]] = []
        for c in selected:
            start, end = c["start"], c["end"]
            if end - start > max_dur:
                start = end - max_dur  # 截断保留尾段（原版实测语义）
            if osts and (start - osts[-1]["end"]) <= merge_gap:
                osts[-1]["end"] = max(osts[-1]["end"], end)
                osts[-1]["duration"] = round(osts[-1]["end"] - osts[-1]["start"], 3)
                osts[-1]["merged_from"] = osts[-1].get("merged_from", []) + [c["id"]]
            else:
                osts.append({"id": c["id"], "text": c["text"], "start": round(start, 3),
                             "end": round(end, 3), "duration": round(end - start, 3),
                             "score": c.get("score"), "score_detail": c.get("score_detail"),
                             "ost_source": [c["start"], c["end"]], "merged_from": []})
        progress(0.75)

        # ---- 时间线重构：解说 + OST 交错 ----
        narration_rows = []
        for r in timeline_rows:
            if isinstance(r, dict) and (r.get("text") or r.get("narration")):
                narration_rows.append({"start": float(r.get("start", 0) or 0),
                                       "end": float(r.get("end", 0) or 0),
                                       "text": str(r.get("text") or r.get("narration") or ""),
                                       "role": "解说", "OST": 0})
        mixed = list(narration_rows)
        for o in osts:
            mixed.append({"start": o["start"], "end": o["end"], "text": o["text"],
                          "role": "原片", "OST": 1, "ost_source": o["ost_source"],
                          "score": o.get("score")})
        mixed.sort(key=lambda s: (s["start"], 0 if s["role"] == "原片" else 1))

        # 重叠修正：OST 与解说重叠时压到解说间隙内（不重叠不变式）
        for i in range(len(mixed) - 1):
            if mixed[i]["end"] > mixed[i + 1]["start"]:
                mixed[i]["end"] = mixed[i + 1]["start"]
                if "OST" in mixed[i] and mixed[i]["OST"]:
                    pass  # 保留 OST 原始 source，target 只做展示裁剪

        tl_asset = store.save_asset(
            Asset(id="", kind=PortType.TIMELINE.value, meta={"count": len(mixed), "ost": len(osts)}),
            payload={"segments": mixed, "schema": "mixed_timeline"})
        report = {
            "candidates_total": len(candidates),
            "filtered": filtered,
            "select_count": select_count,
            "ratio": ratio,
            "scoring": scoring,
            "selected": [{"id": o["id"], "start": o["start"], "end": o["end"],
                          "duration": o["duration"], "score": o.get("score"),
                          "merged_from": o.get("merged_from", [])} for o in osts],
            "llm_scores_used": bool(llm_map) and scoring == "llm_first",
            "warnings": [],
        }
        r_asset = store.save_asset(Asset(id="", kind=PortType.JSON.value, meta={"role": "ost_report"}),
                                   payload=report)
        progress(1.0)
        return {"mixed_timeline": tl_asset, "report": r_asset}