"""选材大纲转换节点：plan(JSON) → 大纲文本(SCRIPT)。

为什么必须有它（不是可选的胶水）：
    core/ai_narrate 的 draft 口是「文案草稿/示例」，node.py:690-694 用 _read_text()
    把入参当**纯文本**读、原样拼进提示词的【用户草稿】段。若把 user/jsm_highlight_plan
    的 plan JSON 直接接到 draft，模型看到的是 {"items":[...],"chapters":[...]}，
    它会去仿写这段 JSON —— 语义层级全错。所以中间必须有一次「结构化 → 人话」的渲染。

本节点的唯一职责：把选材计划渲染成一份文案节点能照着写的大纲：
    ① 总时长与段数（给模型一个长度锚点）
    ② 章节结构（story 模式下 chapters 带 title/start/end/story_orders）
    ③ 每章里有哪些片段、各占多长、哪一段是开场钩子
    ④ 叙事要求（钩子、节奏、收尾）
不生成文案，不改选材，不猜内容。
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType


def _num(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _ts(sec: Any) -> str:
    s = max(0.0, _num(sec))
    m, r = divmod(int(round(s)), 60)
    return "%d:%02d" % (m, r)


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _items_of(plan: Any) -> List[Dict[str, Any]]:
    """从各种可能的 plan 形态里取片段列表（容错，不抛）。"""
    if isinstance(plan, list):
        return [x for x in plan if isinstance(x, dict)]
    if not isinstance(plan, dict):
        return []
    for key in ("items", "selected", "segments", "clips", "rows"):
        v = plan.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    for wrap in ("plan", "data", "result"):
        v = plan.get(wrap)
        if isinstance(v, (dict, list)):
            got = _items_of(v)
            if got:
                return got
    return []


def _chapters_of(plan: Any) -> List[Dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    for key in ("chapters", "chapter_list", "sections"):
        v = plan.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _guides_of(plan: Any) -> List[str]:
    if not isinstance(plan, dict):
        return []
    v = plan.get("guides")
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return []


def render_outline(plan: Any, fmt: str = "chapter", max_chars: int = 1200,
                   hook_hint: str = "") -> str:
    """把 plan 渲染成大纲文本。永不抛异常、永不返回空串。"""
    items = _items_of(plan)
    chapters = _chapters_of(plan)
    guides = _guides_of(plan)

    total = 0.0
    rows: List[Dict[str, Any]] = []
    for i, it in enumerate(items):
        st = _num(it.get("start"))
        en = _num(it.get("end"), st + _num(it.get("duration")))
        if en <= st:
            continue
        dur = en - st
        total += dur
        rows.append({"order": i + 1, "start": st, "end": en, "dur": dur,
                     "is_hook": bool(it.get("is_hook")), "role": str(it.get("role") or ""),
                     "text": str(it.get("text") or it.get("summary") or "")[:60]})

    lines: List[str] = ["【成片大纲】"]
    if not rows:
        lines.append("选材结果为空：按整片自行决定要讲哪几段，但总时长必须控制在目标时长内。")
    else:
        lines.append("目标成片时长约 %.1f 分钟；已选 %d 段素材，素材合计 %.1f 分钟。"
                     % (total / 60.0, len(rows), total / 60.0))
    lines.append("")

    fmt = (fmt or "chapter").strip().lower()
    if rows and chapters and fmt != "bullet":
        for ci, ch in enumerate(chapters, 1):
            idx = ch.get("chapter") or ch.get("index") or ci
            title = str(ch.get("title") or "未命名").strip() or "未命名"
            cst, cen = _num(ch.get("start")), _num(ch.get("end"))
            orders = ch.get("story_orders") or []
            inside = [r for r in rows
                      if (cst <= r["start"] < cen) or (not orders and False)]
            if not inside and orders:
                want = {int(x) for x in orders if str(x).strip().lstrip("-").isdigit()}
                inside = [r for r in rows if r["order"] in want]
            if not inside:
                inside = rows
            lines.append("第%s章 %s（%s–%s，%d 段）"
                         % (idx, title, _ts(cst), _ts(cen), len(inside)))
            for r in inside:
                tag = "开头钩子" if r["is_hook"] else (r["role"] or "")
                tail = ("　" + tag) if tag else ""
                lines.append("  · 第%d段 %s–%s（%.1f 秒）%s"
                             % (r["order"], _ts(r["start"]), _ts(r["end"]), r["dur"], tail))
            lines.append("")
    elif rows:
        head = "按顺序讲这 %d 段：" % len(rows) if fmt == "narrative" else "素材顺序："
        lines.append(head)
        for r in rows:
            tag = "开头钩子" if r["is_hook"] else (r["role"] or "")
            lines.append("  · 第%d段 %s–%s（%.1f 秒）%s"
                         % (r["order"], _ts(r["start"]), _ts(r["end"]), r["dur"],
                            ("　" + tag) if tag else ""))
        lines.append("")

    lines.append("【叙事要求】")
    lines.append("1. 严格按上面的顺序与段数写，不要新增素材之外的剧情。")
    lines.append("2. 开场 3 秒内抛出最大的悬念或反差，不要交代背景。")
    if hook_hint.strip():
        lines.append("3. 开场钩子方向：%s" % hook_hint.strip())
    lines.append("%d. 每段解说控制在 2–4 句，与对应素材时长匹配，不要一句话讲完一整段。" %
                 (4 if hook_hint.strip() else 3))
    lines.append("%d. 收尾给一个明确落点（反转/结论/呼应开头），不要拖尾。" %
                 (5 if hook_hint.strip() else 4))
    for g in guides[:6]:
        lines.append("· %s" % g[:120])

    text = "\n".join(lines).strip()
    limit = max(200, int(max_chars or 1200))
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…（大纲已截断）"
    return text


class PlanToOutlineNode(NodeBase):
    """选材计划 → 大纲文本。type_id: core/plan_to_outline"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/plan_to_outline",
            title="选材大纲转换",
            category="语义",
            description="把选材计划（片段+章节+引导）渲染成文案节点可消费的大纲文本；"
                        "解决 plan JSON 不能直接进文案口的问题",
            inputs=[
                PortSpec(name="plan", type=PortType.JSON, label="选材计划", required=True,
                         description="user/jsm_highlight_plan 的 plan 输出"),
            ],
            outputs=[
                PortSpec(name="outline", type=PortType.SCRIPT, label="大纲文本", required=True),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="选材片段表", required=True,
                         description="plan.items 摊平成源片区间，可直接接 timeline_to_segments.base"),
                PortSpec(name="report", type=PortType.JSON, label="转换报告", required=True),
            ],
            params=[
                PortSpec(name="format", type=PortType.STRING, label="大纲格式", default="chapter",
                         widget="select", options=["chapter", "bullet", "narrative"],
                         description="chapter=按章节分组；bullet=平铺要点；narrative=叙述式"),
                PortSpec(name="max_chars", type=PortType.INT, label="大纲最长字数", default=1200,
                         widget="number", description="超出截断，避免把提示词撑爆"),
                PortSpec(name="hook_hint", type=PortType.STRING, label="开场钩子方向", default="",
                         widget="text", description="留空则只给通用要求"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        plan = inputs.get("plan")
        store = ctx["store"]
        if plan is None:
            raise RuntimeError("需要选材计划输入（plan）")

        # 资产对象解引用；dict 直接可用（允许直接连 JSON）
        data: Any = plan
        pid = getattr(plan, "id", "")
        if pid:
            try:
                data = store.read_json(pid)
            except Exception:
                data = getattr(plan, "payload", None) or {}

        fmt = str(params.get("format", "chapter") or "chapter")
        budget = int(_num(params.get("max_chars", 1200), 1200))
        hint = str(params.get("hook_hint", "") or "")

        text = render_outline(data, fmt=fmt, max_chars=budget, hook_hint=hint)

        items = _items_of(data)
        chapters = _chapters_of(data)
        total = sum(max(0.0, _num(i.get("end")) - _num(i.get("start"))) for i in items)

        # FW-DEV-24：plan.items 摊平成源片区间，直接接 timeline_to_segments.base。
        # 旧版把 len(seg_rows) 写进 outline meta、却在后面才赋值，且遍历了
        # render_outline() 的局部变量 rows（run() 里根本不存在）——
        # 任何合法输入都会 UnboundLocalError: seg_rows。这里先算、再落资产。
        seg_rows: List[Dict[str, Any]] = []
        for idx, r in enumerate(items):
            st = _num(r.get("start"), -1.0)
            en = _num(r.get("end"), -1.0)
            if en <= st:
                continue
            seg_rows.append({
                "start": round(st, 3),
                "end": round(en, 3),
                "duration": round(en - st, 3),
                "order": int(_num(r.get("order"), idx + 1)),
                "is_hook": bool(r.get("is_hook")),
                "role": str(r.get("role") or ""),
                "source": "plan_to_outline",
            })

        outline_asset = store.save_asset(
            Asset(id="", kind=PortType.SCRIPT.value,
                  meta={"chars": len(text), "plan_segments": len(items),
                        "chapters": len(chapters), "segment_rows": len(seg_rows)}),
            payload={"text": text})

        segments_asset = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"count": len(seg_rows), "source": "core/plan_to_outline"}),
            payload=seg_rows)

        report = {
            "outline_chars": len(text),
            "segments": len(items),
            "chapters": len(chapters),
            "material_seconds": round(total, 3),
            "format": fmt,
            "truncated": bool(len(text) >= max(200, budget)),
            "source_planner": str(_as_dict(data).get("planner_status") or ""),
            "source_mode": str(_as_dict(data).get("selection_mode") or ""),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"keys": list(report.keys())}),
            payload=report)
        return {"outline": outline_asset, "segments": segments_asset, "report": report_asset}
