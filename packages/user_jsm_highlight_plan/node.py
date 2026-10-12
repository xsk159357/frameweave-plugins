# -*- coding: utf-8 -*-
"""高光片段规划节点（user/jsm_highlight_plan）。

能力（对应解说猫 story_highlight_planner 语义，独立重写并修复原版缺陷）：
- 双合同：focus（CORE/SUPPORT 主题选片 + 引导）+ story（剧情主线 STORY 节点类型 +
  章节 + 引导）；原版 story 解析公开接口 12k+ 组合全拒（§11-5），本实现全程
  结构化 JSON 合同，不依赖黑盒文本行解析。
- 解析：LLM 响应 /```json 提取 + JSON 校验 + 显式回退（本地启发式），失败原因
  写入 report.planner_status（request_failed / parse_failed / items_invalid），
  绝不静默吞错。
- 无 API Key 时本地确定性启发式规划（时长占比/TF 关键词）/涨幅/钩子/章节合成，
  保证节点离线可用且全程可复现。
- 批量：标准 batch envelope，条目级隔离、checkpoint、并发、重试。
"""
from __future__ import annotations
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

try:
    from app import batchlib
except Exception:  # noqa: BLE001
    batchlib = None

NODE_TYPES = ("背景", "目标", "阻碍", "行动", "转折", "后果", "新选择", "阶段结果")
GUIDE_TYPES = ("人物关系", "时间", "原因", "后果", "目标", "转折")


# ---------------------------------------------------------------- 工具
def _read_json(store, asset):
    if asset is None:
        return None
    try:
        return store.read_json(asset.id)
    except Exception:  # noqa: BLE001
        return None


def _llm_json(messages, api_key, base_url, model, timeout: float = 120.0) -> str:
    """OpenAI 兼容 chat 调用；失败抛 RuntimeError（调用方捕获做回退）。"""
    import urllib.error
    import urllib.request
    url = str(base_url).rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    req = urllib.request.Request(url, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + api_key)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, data, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            last_err = RuntimeError(f"HTTP {e.code}: {raw[:300]}")
            if e.code in (400, 401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = RuntimeError(f"网络错误: {e}")
        if attempt < 2:
            time.sleep(1.0 * (attempt + 1))
    raise last_err or RuntimeError("LLM 请求失败")


def _extract_json_block(raw: str) -> Any:
    """从 LLM 文本提取 JSON：代码块优先，其次首个平衡 { } / [ ]。"""
    if not raw:
        raise ValueError("LLM 返回为空")
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", raw, re.S)
    if m:
        return json.loads(m.group(1))
    s = raw.strip()
    start = s.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(s[start:i + 1])
    start = s.find("[")
    if start >= 0:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "[":
                depth += 1
            elif s[i] == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(s[start:i + 1])
    raise ValueError("响应中未找到 JSON")


# ---------------------------------------------------------------- 本地启发式规划（离线可用）
def _text_of(scene: Dict[str, Any]) -> str:
    return str(scene.get("text", "") or scene.get("narration", "") or scene.get("content", "") or "")


def _score_scene(scene: Dict[str, Any], keywords: List[str], idx: int, total: int) -> Tuple[float, str]:
    """确定性启发式：文本关键词命中 + 时长占比 + 开头/结尾位置加权。"""
    text = _text_of(scene)
    score = 0.0
    reasons = []
    if text:
        for kw in keywords:
            if kw and kw in text:
                score += 2.0
                reasons.append(f"关键词[{kw}]")
    dur = float(scene.get("duration", 0.0) or 0.0)
    if dur >= 5.0:
        score += min(2.0, dur / 10.0)
        reasons.append(f"时长{dur:.0f}s")
    if idx < max(1, total // 5):
        score += 1.5
        reasons.append("开场位置")
    elif idx >= total - max(1, total // 5):
        score += 1.0
        reasons.append("结尾位置")
    if not reasons:
        reasons.append("基线")
    return round(min(10.0, score), 2), "、".join(reasons)


def plan_local(scenes: List[Dict[str, Any]], mode: str, target_duration: float,
               max_clip: float, hook_enabled: bool) -> Dict[str, Any]:
    """本地启发式规划（无 LLM / LLM 失败回退）。"""
    if not scenes:
        return {"items": [], "chapters": [], "guides": [], "planner_status": "items_invalid",
                "selection_mode": mode, "source": "local", "message": "无候选场景"}
    # 关键词：全场景文本高频词（去停用词）
    stopwords = {"的", "了", "是", "我", "你", "他", "她", "它", "这", "那", "在", "有", "和", "也",
                 "就", "都", "而", "及", "与", "着", "或", "一个", "我们", "你们", "他们"}
    freq: Dict[str, int] = {}
    for sc in scenes:
        for ch in re.findall(r"[\u4e00-\u9fff]{2,4}", _text_of(sc)):
            if ch not in stopwords:
                freq[ch] = freq.get(ch, 0) + 1
    keywords = [k for k, _ in sorted(freq.items(), key=lambda x: -x[1])][:8]

    ranked = []
    total = len(scenes)
    for i, sc in enumerate(scenes):
        score, reason = _score_scene(sc, keywords, i, total)
        ranked.append((score, i, sc))
    ranked.sort(key=lambda x: -x[0])

    items: List[Dict[str, Any]] = []
    acc = 0.0
    for score, i, sc in ranked:
        if acc >= target_duration:
            break
        st = float(sc.get("start", 0.0) or 0.0)
        en = float(sc.get("end", 0.0) or 0.0)
        if en <= st:
            en = st + max(3.0, len(_text_of(sc)) * 0.3)
        if en - st > max_clip:
            en = st + max_clip
        tier = "core" if score >= 5.0 else "support"
        node_type = "阶段结果" if i >= total - max(1, total // 5) else ("行动" if score >= 5.0 else "背景")
        acc += en - st
        items.append({
            "start": round(st, 3), "end": round(en, 3),
            "score": score, "reason": reason,
            "story_node": node_type,
            "focus_tier": tier,
            "story_order": len(items) + 1,
            "is_hook": False,
            "story_chapter": 1,
            "story_chapter_title": "剧情",
        })
    items.sort(key=lambda x: x["start"])

    chapters = [{"chapter": 1, "title": "剧情", "start": items[0]["start"],
                 "end": items[-1]["end"],
                 "story_orders": [i["story_order"] for i in items]}] if items else []

    guides: List[Dict[str, Any]] = []
    if items:
        guides.append({
            "story_order": items[0]["story_order"], "story_chapter": 1,
            "guide_type": "原因" if mode == "story" else "目标",
            "offset_percent": 0, "text": "本段亮点：" + items[0]["reason"],
            "anchor_start": items[0]["start"], "anchor_end": items[0]["end"],
        })

    if hook_enabled and items:
        top = max(items, key=lambda x: x["score"])
        hook = dict(top)
        hook["is_hook"] = True
        hook["start"] = max(0.0, top["end"] - min(5.0, top["end"] - top["start"]))
        hook["reason"] = "钩子开场 | " + hook["reason"]
        items.insert(0, hook)
        for it in items:
            it["story_order"] = items.index(it) + 1

    return {"items": items, "chapters": chapters, "guides": guides,
            "planner_status": "ok", "source": "local",
            "selection_mode": mode, "target_duration": target_duration}


# ---------------------------------------------------------------- LLM 增强规划（结构化合同）
def _fmt_scenes(scenes: List[Dict[str, Any]]) -> str:
    lines = []
    for i, sc in enumerate(scenes):
        st = float(sc.get("start", 0.0) or 0.0)
        en = float(sc.get("end", 0.0) or 0.0)
        dur = float(sc.get("duration", 0.0) or 0.0) or (en - st)
        text = _text_of(sc)[:120]
        lines.append(f"[{i}] {st:.1f}-{en:.1f}s (dur {dur:.1f}s) {text}")
    return "\n".join(lines)


_SYSTEM_FOCUS = (
    "你是专业混剪选片规划器。根据用户主题方向，从候选场景中挑选 CORE（主题核心）与 "
    "SUPPORT（有效承接）片段，构成目标时长的主题混剪。输出严格 JSON（不要 markdown）："
    '{"items":[{"start":0,"end":0,"score":7,"reason":"…","focus_tier":"core|support"}],'
    '"guides":[{"story_order":1,"guide_type":"目标","offset_percent":0,"text":"引导文字(≤30字)"}],'
    '"summary":"一句话主题概括"}。要求：时间边界只能取自场景记录，不得自造；'
    "单段不超过 max_clip 秒；总时长接近 target_duration。"
)


_SYSTEM_STORY = (
    "你是专业剧情线混剪规划器。梳理人物-事件-因果主线，从候选场景中挑选 STORY 片段，"
    "划分章节并生成剧情引导。输出严格 JSON（不要 markdown）："
    '{"items":[{"start":0,"end":0,"score":7,"reason":"…",'
    '"story_node":"背景|目标|阻碍|行动|转折|后果|新选择|阶段结果","chapter":1}],'
    '"chapters":[{"chapter":1,"title":"章节名","start":0,"end":0,"story_orders":[1]}],'
    '"guides":[{"story_order":1,"guide_type":"原因","offset_percent":0,"text":"引导文字(≤28字)"}],'
    '"summary":"一句话主线概括"}。要求：MAINLINE 至少 3 个 STORY 编号、章节连续、'
    "时间边界只能取自场景记录，不得自造或近似；单段不超过 max_clip 秒。"
)


def _validate_plan(data: Any, scenes: List[Dict[str, Any]], mode: str,
                   target_duration: float, max_clip: float,
                   hook_enabled: bool) -> Dict[str, Any]:
    """校验并规范化 LLM 计划；任何结构问题抛 ValueError（调用方回退）。"""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("缺少 items 数组")
    ok_starts = {(float(s.get("start", 0.0) or 0.0), float(s.get("end", 0.0) or 0.0)) for s in scenes}
    items: List[Dict[str, Any]] = []
    for i, raw in enumerate(data["items"]):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{i}] 必须是对象")
        st = float(raw.get("start", 0.0) or 0.0)
        en = float(raw.get("end", 0.0) or 0.0)
        if en <= st:
            raise ValueError(f"items[{i}] end 必须大于 start")
        if ok_starts and (round(st, 1), round(en, 1)) not in {(round(a, 1), round(b, 1)) for a, b in ok_starts}:
            # 允许与任一场景有重叠（时间边界尽量贴近，但不强拒——打回给回退逻辑）
            overlap = any(st < b - 0.05 and en > a + 0.05 for a, b in ok_starts)
            if not overlap:
                raise ValueError(f"items[{i}] 时间 ({st},{en}) 与任何候选场景无重叠（疑似自造）")
        if en - st > max_clip:
            en = st + max_clip
        score = float(raw.get("score", 5.0) or 5.0)
        if mode == "focus":
            tier = str(raw.get("focus_tier", "support"))
            if tier not in ("core", "support"):
                tier = "support"
            node_type = "主题核心" if tier == "core" else "有效承接"
        else:
            node_type = str(raw.get("story_node", "行动"))
            if node_type not in NODE_TYPES:
                node_type = "行动"
            tier = "core" if node_type in ("转折", "行动", "后果") else "support"
        items.append({
            "start": round(st, 3), "end": round(en, 3), "score": round(score, 2),
            "reason": str(raw.get("reason", "") or "")[:200],
            "story_node": node_type, "focus_tier": tier,
            "story_order": i + 1, "is_hook": False,
            "story_chapter": int(raw.get("chapter", 1) or 1),
            "story_chapter_title": "剧情",
        })
    if not items:
        raise ValueError("items 为空")
    items.sort(key=lambda x: x["start"])

    sections = data.get("chapters") if mode == "story" else None
    chapters: List[Dict[str, Any]] = []
    if isinstance(sections, list) and sections:
        for i, ch in enumerate(sections):
            if not isinstance(ch, dict):
                continue
            orders = [o.get("story_order") for o in items]
            chapters.append({
                "chapter": int(ch.get("chapter", i + 1) or (i + 1)),
                "title": str(ch.get("title", "章节") or "章节"),
                "start": float(ch.get("start", items[0]["start"]) or 0.0),
                "end": float(ch.get("end", items[-1]["end"]) or 0.0),
                "story_orders": orders,
            })
    if mode == "story" and not chapters:
        chapters = [{"chapter": 1, "title": "剧情", "start": items[0]["start"],
                     "end": items[-1]["end"],
                     "story_orders": [i["story_order"] for i in items]}]
    elif not chapters:
        # focus：按时间间隙本地合成章节
        chapters = []
        current = {"chapter": 1, "title": "主题", "start": items[0]["start"],
                   "end": items[0]["end"], "story_orders": []}
        for it in items:
            if current["story_orders"] and it["start"] - current["end"] > 5.0:
                chapters.append(current)
                current = {"chapter": len(chapters) + 1, "title": "主题",
                           "start": it["start"], "end": it["end"], "story_orders": []}
            current["end"] = max(current["end"], it["end"])
            current["story_orders"].append(it["story_order"])
        chapters.append(current)

    guides: List[Dict[str, Any]] = []
    gs = data.get("guides")
    if isinstance(gs, list):
        for i, g in enumerate(gs):
            if not isinstance(g, dict):
                continue
            order = int(g.get("story_order", 1) or 1)
            gt = str(g.get("guide_type", "原因"))
            if gt not in GUIDE_TYPES:
                gt = "原因"
            anchor = next((x for x in items if x["story_order"] == order), items[0])
            guides.append({
                "story_order": order, "story_chapter": int(g.get("story_chapter", 1) or 1),
                "guide_type": gt, "offset_percent": int(g.get("offset_percent", 0) or 0),
                "text": str(g.get("text", "") or "")[:60],
                "anchor_start": anchor["start"], "anchor_end": anchor["end"],
            })

    if hook_enabled and items:
        top = max(items, key=lambda x: x["score"])
        hook = dict(top)
        hook["is_hook"] = True
        hook["start"] = max(0.0, top["end"] - min(5.0, top["end"] - top["start"]))
        hook["reason"] = "钩子开场 | " + hook["reason"]
        items.insert(0, hook)
        for k, it in enumerate(items):
            it["story_order"] = k + 1

    return {"items": items, "chapters": chapters, "guides": guides,
            "planner_status": "ok", "source": "llm",
            "selection_mode": mode, "target_duration": target_duration}


def plan_with_llm(scenes: List[Dict[str, Any]], mode: str, target_duration: float,
                  max_clip: float, hook_enabled: bool, instruction: str,
                  api_key: str, base_url: str, model: str) -> Tuple[Dict[str, Any], str]:
    """LLM 规划；任何异常返回 (回退计划, reason)。"""
    if not scenes:
        return plan_local([], mode, target_duration, max_clip, hook_enabled), "no-scenes"
    try:
        system = _SYSTEM_STORY if mode == "story" else _SYSTEM_FOCUS
        user = (
            f"目标时长: {target_duration:.0f}s；单段上限: {max_clip:.0f}s；"
            + (f"主题指令: {instruction}\n" if instruction.strip() else "")
            + "候选场景:\n" + _fmt_scenes(scenes)
        )
        raw = _llm_json([{"role": "system", "content": system},
                         {"role": "user", "content": user}],
                        api_key, base_url, model)
        data = _extract_json_block(raw)
        return _validate_plan(data, scenes, mode, target_duration, max_clip, hook_enabled), ""
    except Exception as e:  # noqa: BLE001
        status = "request_failed" if "网络" in str(e) or "HTTP" in str(e) or "LLM" in str(e) else "parse_failed"
        plan = plan_local(scenes, mode, target_duration, max_clip, hook_enabled)
        plan["planner_status"] = status
        plan["error"] = str(e)[:300]
        return plan, str(e)[:300]


# ---------------------------------------------------------------- 单条处理
def _process_payload(payload: Dict[str, Any], mode: str, target_duration: float,
                     max_clip: float, hook_enabled: bool, api_key: str,
                     base_url: str, model: str) -> Dict[str, Any]:
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("scenes 必须是非空列表")
    instruction = str(payload.get("instruction", "") or "")
    if api_key.strip():
        plan, err = plan_with_llm(scenes, mode, target_duration, max_clip,
                                  hook_enabled, instruction, api_key, base_url, model)
        return {
            "items": plan["items"], "chapters": plan["chapters"],
            "guides": plan["guides"], "planner_status": plan["planner_status"],
            "selection_mode": plan["selection_mode"], "source": plan["source"],
            "llm_error": err,
        }
    plan = plan_local(scenes, mode, target_duration, max_clip, hook_enabled)
    return {
        "items": plan["items"], "chapters": plan["chapters"],
        "guides": plan["guides"], "planner_status": plan["planner_status"],
        "selection_mode": plan["selection_mode"], "source": "local", "llm_error": "",
    }


class HighlightPlanNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_highlight_plan",
            title="高光片段规划",
            category="语义",
            description="剧情/主题混剪选片规划（focus/story 双合同，结构化 JSON 输出）",
            inputs=[
                PortSpec(name="scenes", type=PortType.SEGMENTS, label="场景列表", required=False),
                PortSpec(name="instruction", type=PortType.SCRIPT, label="主题指令", required=False),
                PortSpec(name="items", type=PortType.JSON, label="批量条目", required=False),
            ],
            outputs=[
                PortSpec(name="plan", type=PortType.JSON, label="选片计划"),
                PortSpec(name="chapters", type=PortType.JSON, label="章节"),
                PortSpec(name="report", type=PortType.JSON, label="规划报告"),
                PortSpec(name="results", type=PortType.JSON, label="批量结果"),
            ],
            params=[
                PortSpec(name="mode", type=PortType.STRING, label="规划模式", default="focus",
                         widget="select", options=["focus", "story"]),
                PortSpec(name="target_duration", type=PortType.FLOAT, label="目标时长(秒)",
                         default=300.0, widget="number"),
                PortSpec(name="max_clip", type=PortType.FLOAT, label="单段上限(秒)",
                         default=50.0, widget="number"),
                PortSpec(name="hook_enabled", type=PortType.BOOL, label="钩子开场",
                         default=True, widget="toggle"),
                PortSpec(name="api_key", type=PortType.STRING, label="API Key",
                         default="", widget="password", required=False,
                         description="可选；填写则 LLM 增强规划，留空走本地启发式"),
                PortSpec(name="base_url", type=PortType.STRING, label="中转地址",
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec(name="model", type=PortType.STRING, label="模型",
                         default="gpt-4o", widget="text"),
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
        scenes_asset = inputs.get("scenes")
        inst_asset = inputs.get("instruction")
        items_asset = inputs.get("items")
        store = ctx["store"]

        mode = str(params.get("mode", "focus") or "focus")
        if mode not in ("focus", "story"):
            raise ValueError("mode 必须是 focus|story")
        target_duration = max(1.0, float(params.get("target_duration", 300.0) or 300.0))
        max_clip = max(2.0, float(params.get("max_clip", 50.0) or 50.0))
        hook_enabled = bool(params.get("hook_enabled", True))
        api_key = str(params.get("api_key", "") or "").strip()
        if api_key.startswith("@secret:"):
            ref = api_key.split(":", 1)[1]
            resolver = ctx.get("resolve_secret") if hasattr(ctx, "get") else None
            if resolver:
                api_key = resolver(ref) or ""
            else:
                try:
                    from app.secrets import read_secret
                    api_key = read_secret(ref) or ""
                except Exception:  # noqa: BLE001
                    api_key = ""
        base_url = str(params.get("base_url", "https://ameaaos.com/v1")).strip() or "https://ameaaos.com/v1"
        model = str(params.get("model", "gpt-4o")).strip() or "gpt-4o"
        concurrency = max(1, int(params.get("max_concurrent", 2) or 2))
        retries = max(0, int(params.get("retries", 1) or 0))
        resume = bool(params.get("resume", True))

        raw_items: List[Dict[str, Any]] = []
        if items_asset is not None:
            data = _read_json(store, items_asset)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                for it in data["items"]:
                    if isinstance(it, dict) and isinstance(it.get("input"), dict):
                        raw_items.append(it["input"])
                    elif isinstance(it, dict):
                        raw_items.append(it)
                    else:
                        raw_items.append({"scenes": it})
            elif isinstance(data, list):
                raw_items = [x if isinstance(x, dict) else {"scenes": x} for x in data]
        if scenes_asset is not None or inst_asset is not None or not raw_items:
            payload: Dict[str, Any] = {}
            sc = _read_json(store, scenes_asset)
            if isinstance(sc, list):
                payload["scenes"] = sc
            inst = _read_json(store, inst_asset)
            if inst is not None:
                payload["instruction"] = inst.get("text", "") if isinstance(inst, dict) else str(inst)
            if payload or not raw_items:
                raw_items.append(payload)

        if not raw_items:
            raise RuntimeError("缺少输入：请连接场景列表或批量条目")

        cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                          str(ctx.get("node_id", "highlight_plan")) + ".json")

        async def _to_thread(fn, *args):
            import asyncio
            return await asyncio.to_thread(fn, *args)

        async def worker(item: Dict[str, Any]) -> Dict[str, Any]:
            payload = item.get("input")
            if not isinstance(payload, dict):
                raise RuntimeError("条目输入必须是对象")
            return await _to_thread(_process_payload, payload, mode, target_duration,
                                    max_clip, hook_enabled, api_key, base_url, model)

        if batchlib is not None:
            batch = batchlib.new_batch(raw_items, source="user/jsm_highlight_plan",
                                       options={"mode": mode, "target": target_duration})
            batch = await batchlib.run_items(batch, worker, concurrency=concurrency,
                                             checkpoint_path=cp, progress=ctx.get("progress"),
                                             retries=retries, resume=resume)
        else:
            batch = {"schema_version": 1, "batch_id": "b", "items": raw_items}
            for i, it in enumerate(batch["items"]):
                it["status"] = "succeeded"
                it["result"] = await worker(it)
                it["index"] = i

        first = None
        for it in batch["items"]:
            if it.get("status") == "succeeded" and isinstance(it.get("result"), dict):
                first = it["result"]
                break
        if first is None:
            errs = [str(it.get("error")) for it in batch["items"] if it.get("status") == "failed"]
            raise RuntimeError("全部条目失败: " + "; ".join(errs[:3]))

        plan_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(first.get("items", [])), "status": first.get("planner_status")}),
            payload={"items": first.get("items", []), "chapters": first.get("chapters", []),
                     "guides": first.get("guides", []), "planner_status": first.get("planner_status"),
                     "selection_mode": first.get("selection_mode"), "source": first.get("source"),
                     "summary": ""})
        chapters_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value), payload=first.get("chapters", []))
        report_data = {
            "mode": mode, "target_duration": target_duration,
            "planner_status": first.get("planner_status"),
            "selection_mode": first.get("selection_mode"),
            "source": first.get("source"), "llm_error": first.get("llm_error", ""),
            "items": len(first.get("items", [])), "chapters": len(first.get("chapters", [])),
            "guides": len(first.get("guides", [])),
        }
        report_asset = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload=report_data)
        results_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(batch["items"]), "ok": sum(1 for i in batch["items"] if i.get("status") == "succeeded")}),
            payload={"batch_id": batch.get("batch_id"), "items": batch["items"]})
        return {"plan": plan_asset, "chapters": chapters_asset,
                "report": report_asset, "results": results_asset}