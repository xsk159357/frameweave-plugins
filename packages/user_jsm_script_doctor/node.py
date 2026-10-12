# -*- coding: utf-8 -*-
"""脚本医生·文案质检节点（user/jsm_script_doctor）。

能力（对应解说猫 ui/script_doctor_page 语义，独立重写并修复原版缺陷）：
- 6 维深度质检：获得感（信息增量）/ 惊喜感（反转·意外）/ 表达力（句式多样·修辞）/
  感染力（情绪词·代入）/ 节奏感（长短句交替）/ 精准度（事实锚点·无冗余）。
- 12 项硬指标：字数/开头钩子/结尾引导/人称一致/事实锚点/禁词（AI 腔）/重复度/
  口语化/段落长度/标点使用/数字具体/悬念设置——逐项 通过/失败 + 证据。
- 3 套全文改写方案：爆款化（钩子+节奏）/ 精简强化（去水）/ 沉稳可信（事实优先）。
  本地模板改写 + LLM 增强（可选）；改写后输出统一。
- 结论等级：优秀爆款潜质 / 中等偏上 / 需要优化 / 需大幅改进（原版同四级）。
- 修复：不绑定授权（原版正版专属门）、无 RAG 依赖、本地可复现、问题逐条证据化。
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

AI_WORDS = ("首先", "其次", "最后", "总的来说", "综上所述", "值得注意的是", "众所周知",
            "然而", "因此", "所以", "可以说", "其实", "在这个", "关于这个", "让我们",
            "这部作品", "本片", "讲述了", "展现出", "体现了", "标志着")
HOOK_OPENERS = ("你绝对想不到", "好家伙", "就因为", "三分钟看完", "没想到", "直接",
                "竟然", "最后", "看完这个", "这波", "反转", "秘密", "真相")
END_CALLS = ("关注", "点赞", "评论区", "下期", "留言", "收藏", "转发", "告诉我")
EMOTION_WORDS = ("泪", "哭", "笑", "惊", "怕", "爱", "恨", "疯", "燃", "爽", "炸",
                 "心", "命", "死", "赢", "输", "救", "背叛", "真相")
NUM_RE = re.compile(r"[0-9０-９]+[%％万亿个位分钟秒天年]?")


# ---------------------------------------------------------------- 6 维打分
def dimension_scores(text: str, sentences: List[str]) -> Dict[str, Dict[str, Any]]:
    dims: Dict[str, Dict[str, Any]] = {}
    total_chars = len(text)
    sents = [s for s in sentences if s.strip()]
    n = len(sents)

    # 获得感：信息实体/数字/专有名词密度
    nums = len(NUM_RE.findall(text))
    entities = len(re.findall(r"[\u4e00-\u9fff]{2,6}(?:人|国|市|公司|剧|电影|片|案|队|局)", text))
    gain = min(10.0, 4.0 + nums * 0.6 + entities * 0.4)
    dims["获得感"] = {"score": round(gain, 2), "evidence": f"数字{nums}处/实体{entities}处"}

    # 惊喜感：反转词/疑问/转折密度
    surprise_words = len(re.findall(r"反转|真相|秘密|竟然|居然|没想到|谁料|结果", text))
    questions = sum(1 for s in sents if s.rstrip().endswith(("?", "？")))
    surprise = min(10.0, 3.5 + surprise_words * 1.2 + questions * 0.5)
    dims["惊喜感"] = {"score": round(surprise, 2),
                     "evidence": f"反转词{surprise_words}处/疑问句{questions}句"}

    # 表达力：句式长度方差（长短句交替）+ 修辞标点（冒号引号破折号）
    if n >= 2:
        lengths = [len(s) for s in sents]
        avg = sum(lengths) / n
        var = sum((x - avg) ** 2 for x in lengths) / n
        rhythm = min(3.0, var / 80.0)
    else:
        rhythm = 0.0
    rhetoric = len(re.findall(r"[：:—…“”\"']", text))
    expr = min(10.0, 3.5 + rhythm + min(3.0, rhetoric * 0.3))
    dims["表达力"] = {"score": round(expr, 2),
                     "evidence": f"句式方差{var:.0f}/修辞标点{rhetoric}处" if n >= 2 else "句数不足"}

    # 感染力：情感词/叹词/叠词密度
    emo = sum(1 for w in EMOTION_WORDS if w in text)
    interj = len(re.findall(r"啊|呀|哇|哦|呢|吧|卧槽|天哪|救命", text))
    feel = min(10.0, 3.0 + emo * 0.7 + interj * 0.5)
    dims["感染力"] = {"score": round(feel, 2), "evidence": f"情感词{emo}处/语气词{interj}处"}

    # 节奏感：句长分布 + 短句密度（≤15 字句子比例）
    if n:
        short = sum(1 for s in sents if len(s) <= 15)
        short_ratio = short / n
        rhythm_score = min(10.0, 3.0 + short_ratio * 4.0 + (n >= 5) * 1.5)
    else:
        rhythm_score = 1.0
        short_ratio = 0.0
    dims["节奏感"] = {"score": round(rhythm_score, 2),
                     "evidence": f"短句占比{short_ratio:.0%}"}

    # 精准度：事实锚点（时间/地点/数字）+ 去冗余（AI 腔命中扣分）
    anchors = nums + len(re.findall(r"[\u4e00-\u9fff]{2,3}(?:年|月|日|时|分)", text))
    ai_hits = sum(1 for w in AI_WORDS if w in text)
    precise = max(0.0, min(10.0, 5.0 + anchors * 0.5 - ai_hits * 1.2))
    dims["精准度"] = {"score": round(precise, 2),
                     "evidence": f"事实锚点{anchors}处/AI腔命中{ai_hits}处"}
    return dims


# ---------------------------------------------------------------- 12 项硬指标
def hard_checks(text: str, sentences: List[str]) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    n = len(sentences)

    def add(name: str, passed: bool, evidence: str):
        checks.append({"name": name, "passed": passed, "evidence": evidence})

    add("字数达标", len(text) >= 20, f"{len(text)}字（≥20）")
    add("开头钩子", any(text.startswith(h) for h in HOOK_OPENERS) or (
        bool(sentences) and any(s.startswith(h) for s in sentences[:2] for h in HOOK_OPENERS)),
        "开头句: " + (sentences[0][:24] if sentences else "无"))
    add("结尾引导", any(c in text for c in END_CALLS), "含关注/点赞/下期等引导词")
    add("人称一致", _persona_consistent(text), "第一/第三人称混用检查")
    add("事实锚点", len(NUM_RE.findall(text)) >= 1, "至少 1 处具体数字/时间")
    add("无AI腔", not any(w in text for w in AI_WORDS),
        "命中: " + "、".join(w for w in AI_WORDS if w in text)[:60] or "无")
    add("重复度低", _repetition_ok(text, sentences), "相邻句重复片段检查")
    add("口语化", _colloquial_ratio(text) >= 0.25, f"口语词占比{_colloquial_ratio(text):.0%}")
    add("段落适中", all(len(s) <= 60 for s in sentences), "无超长句（>60 字）")
    add("标点正确", text.count("，") + text.count("。") > 0 or n > 1, "含逗号/句号")
    add("悬念设置", any(k in text for k in ("为什么", "秘密", "真相", "反转", "结果", "后来")) or
        any(s.rstrip().endswith(("?", "？")) for s in sentences),
        "疑问句/悬念词")
    add("信息密度", len(NUM_RE.findall(text)) + len(re.findall(r"[\u4e00-\u9fff]{2,6}(?:人|国|市|公司|剧|片)", text)) >= 2,
        "数字+实体≥2")
    return checks


def _persona_consistent(text: str) -> bool:
    first = re.findall(r"我|我们", text)
    third = re.findall(r"他|她|它|他们", text)
    if not first or not third:
        return True
    return len(first) / max(len(first) + len(third), 1) < 0.8


def _repetition_ok(text: str, sentences: List[str]) -> bool:
    for i in range(1, len(sentences)):
        a, b = sentences[i - 1], sentences[i]
        if a and b:
            for j in range(0, max(0, min(len(a), len(b)) - 4)):
                seg = a[j:j + 6]
                if len(seg) >= 6 and seg in b:
                    return False
    return True


def _colloquial_ratio(text: str) -> float:
    if not text:
        return 0.0
    colloquial = len(re.findall(r"啊|呀|嘛|呢|吧|哦|嘿|诶|哇|哈|真的|其实|就是|然后|大家", text))
    return min(1.0, colloquial / max(len(text) // 40, 1))


# ---------------------------------------------------------------- 结论等级
def grade(dims: Dict[str, Dict[str, Any]], checks: List[Dict[str, Any]]) -> Tuple[str, float]:
    avg = sum(d["score"] for d in dims.values()) / max(len(dims), 1)
    passed = sum(1 for c in checks if c["passed"])
    total = max(len(checks), 1)
    score = round(avg * 0.6 + (passed / total) * 100 * 0.4, 2)
    if score >= 7.5 and passed / total >= 0.8:
        level = "优秀爆款潜质"
    elif score >= 6.0:
        level = "中等偏上"
    elif score >= 4.5:
        level = "需要优化"
    else:
        level = "需大幅改进"
    return level, score


# ---------------------------------------------------------------- 本地 3 套改写
def _local_rewrites(text: str, sentences: List[str], count: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    head = sentences[0][:24] if sentences else text[:24]
    tail = sentences[-1][:24] if sentences else text[-24:]

    if count >= 1:
        # 方案一：爆款化（钩子前置 + 短句节奏）
        hook = "你绝对想不到，" + (head or "这背后")
        body = "！".join(s[:40] for s in sentences[:6])
        ending = "这波操作，你怎么看？评论区聊聊。" if not any(c in text for c in END_CALLS) else tail
        new = f"{hook}。{body}。{ending}"
        out.append({"id": "hit", "title": "爆款化改写", "text": new,
                    "diff": f"新增钩子开头/短句化/结尾引导（原{len(text)}字→{len(new)}字）"})

    if count >= 2:
        # 方案二：精简强化（去 AI 腔 + 去水）
        cleaned = text
        for w in AI_WORDS:
            cleaned = cleaned.replace(w, "")
        cleaned = re.sub(r"(啊|呀|呢|吧)\s*", "", cleaned)
        cleaned = re.sub(r"\s+", "", cleaned)
        out.append({"id": "lean", "title": "精简强化改写", "text": cleaned or text,
                    "diff": f"去除AI腔{sum(1 for w in AI_WORDS if w in text)}处/删语气词/压缩（{len(text)}字→{len(cleaned or text)}字）"})

    if count >= 3:
        # 方案三：沉稳可信（事实锚点前置 + 因果链）
        facts = [s for s in sentences if NUM_RE.search(s) or re.search(r"[\u4e00-\u9fff]{2,3}(?:年|月|日)", s)]
        opening = facts[0] if facts else sentences[0]
        body = " ".join(s for s in sentences[:8] if s != opening)
        new = f"{opening}。这是真实发生的：{body}。事实如此，值得深思。"
        out.append({"id": "trust", "title": "沉稳可信改写", "text": new,
                    "diff": f"事实锚点前置/因果链组织（原{len(text)}字→{len(new)}字）"})
    return out[:count]


# ---------------------------------------------------------------- LLM 增强（可选）
def _llm_json(messages, api_key, base_url, model, timeout: float = 120.0) -> str:
    import urllib.error
    import urllib.request
    url = str(base_url).rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 4000}
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


def _llm_rewrites(text: str, api_key: str, base_url: str, model: str,
                  count: int) -> List[Dict[str, Any]]:
    system = (
        "你是资深短视频解说文案编辑。对给定文案给出 3 套全文改写方案，每套不同策略："
        "1)爆款化：钩子开头+短句节奏+引导互动；2)精简强化：去AI腔去水、信息密度拉满；"
        "3)沉稳可信：事实锚点前置、因果链完整、不煽情。"
        '只输出 JSON 数组 [{"id":"hit|lean|trust","title":"...","text":"改写全文"}]。'
    )
    raw = _llm_json([{"role": "system", "content": system},
                     {"role": "user", "content": text[:3000]}],
                    api_key, base_url, model)
    data = _extract_json_block(raw)
    if not isinstance(data, list):
        raise ValueError("LLM 返回不是数组")
    out = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        out.append({
            "id": str(item.get("id", f"r{i}")),
            "title": str(item.get("title", f"方案{i + 1}")),
            "text": str(item["text"]),
            "diff": "LLM 生成改写",
        })
        if len(out) >= count:
            break
    return out


# ---------------------------------------------------------------- 主质检
def _diagnose(text: str, api_key: str, base_url: str, model: str,
              rewrite_count: int) -> Dict[str, Any]:
    text = str(text or "").strip()
    if len(text) < 20:
        raise ValueError(f"文案过短（{len(text)}字 < 20），无法质检")
    sentences = [s.strip() for s in re.split(r"[。！？；!?;\n]", text) if s.strip()]
    dims = dimension_scores(text, sentences)
    checks = hard_checks(text, sentences)
    level, score = grade(dims, checks)
    problems = [{"name": c["name"], "evidence": c["evidence"]} for c in checks if not c["passed"]]
    strengths = [{"name": c["name"], "evidence": c["evidence"]} for c in checks if c["passed"]]
    failed_names = {c["name"] for c in checks if not c["passed"]}

    # 与爆款差距（本地规则版 refgap）：以满分维度对比
    refgap = {name: round(10.0 - d["score"], 2) for name, d in dims.items() if d["score"] < 8.0}

    rewrites: List[Dict[str, Any]] = []
    llm_used = False
    llm_error = ""
    if api_key.strip():
        try:
            rewrites = _llm_rewrites(text, api_key, base_url, model, rewrite_count)
            llm_used = True
        except Exception as e:  # noqa: BLE001
            llm_error = str(e)[:200]
    if not rewrites:
        rewrites = _local_rewrites(text, sentences, rewrite_count)

    diagnosis = {
        "text_length": len(text),
        "sentence_count": len(sentences),
        "dimensions": dims,
        "hard_checks": checks,
        "problems": problems,
        "strengths": strengths,
        "refgap": refgap,
        "conclusion_level": level,
        "total_score": score,
        "failed_names": sorted(failed_names),
        "engine": "llm" if llm_used else "local",
    }
    return {"diagnosis": diagnosis, "rewrites": rewrites,
            "llm_used": llm_used, "llm_error": llm_error}


class ScriptDoctorNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_script_doctor",
            title="脚本医生·文案质检",
            category="语义",
            description="解说文案 6 维质检 + 12 硬指标 + 3 套改写方案 + 结论等级",
            inputs=[
                PortSpec(name="script", type=PortType.SCRIPT, label="待检文案", required=False),
                PortSpec(name="items", type=PortType.JSON, label="批量条目", required=False),
            ],
            outputs=[
                PortSpec(name="diagnosis", type=PortType.JSON, label="质检报告"),
                PortSpec(name="rewrites", type=PortType.JSON, label="改写方案"),
                PortSpec(name="report", type=PortType.JSON, label="执行报告"),
                PortSpec(name="results", type=PortType.JSON, label="批量结果"),
            ],
            params=[
                PortSpec(name="min_length", type=PortType.INT, label="最小字数",
                         default=20, widget="number"),
                PortSpec(name="api_key", type=PortType.STRING, label="API Key",
                         default="", widget="password", required=False,
                         description="可选；填写则 LLM 增强润色（3 套改写由模型生成）"),
                PortSpec(name="base_url", type=PortType.STRING, label="中转地址",
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec(name="model", type=PortType.STRING, label="模型",
                         default="gpt-4o", widget="text"),
                PortSpec(name="rewrite_count", type=PortType.INT, label="改写方案数",
                         default=3, widget="number"),
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
        script_asset = inputs.get("script")
        items_asset = inputs.get("items")
        store = ctx["store"]

        min_length = max(1, int(params.get("min_length", 20) or 20))
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
        rewrite_count = max(1, min(3, int(params.get("rewrite_count", 3) or 3)))
        concurrency = max(1, int(params.get("max_concurrent", 2) or 2))
        retries = max(0, int(params.get("retries", 1) or 0))
        resume = bool(params.get("resume", True))

        def _read(asset):
            if asset is None:
                return None
            try:
                return store.read_json(asset.id)
            except Exception:  # noqa: BLE001
                return None

        def _script_text(asset) -> str:
            data = _read(asset)
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

        raw_items: List[Dict[str, Any]] = []
        if items_asset is not None:
            data = _read(items_asset)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                for it in data["items"]:
                    if isinstance(it, dict) and isinstance(it.get("input"), dict):
                        raw_items.append(it["input"])
                    elif isinstance(it, dict):
                        raw_items.append(it)
                    else:
                        raw_items.append({"text": str(it)})
            elif isinstance(data, list):
                raw_items = [x if isinstance(x, dict) else {"text": str(x)} for x in data]
        if script_asset is not None or not raw_items:
            txt = _script_text(script_asset)
            if txt.strip() or not raw_items:
                raw_items.append({"text": txt})

        if not raw_items:
            raise RuntimeError("缺少输入：请连接待检文案或批量条目")

        cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                          str(ctx.get("node_id", "script_doctor")) + ".json")

        async def worker(item: Dict[str, Any]) -> Dict[str, Any]:
            payload = item.get("input")
            if not isinstance(payload, dict):
                raise RuntimeError("条目输入必须是对象")
            text = str(payload.get("text", "") or payload.get("script_text", "") or "")
            if len(text) < min_length:
                raise ValueError(f"文案过短（{len(text)}字 < {min_length}），无法质检")
            import asyncio
            return await asyncio.to_thread(_diagnose, text, api_key, base_url,
                                           model, rewrite_count)

        if batchlib is not None:
            batch = batchlib.new_batch(raw_items, source="user/jsm_script_doctor",
                                       options={"min_length": min_length})
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

        diag_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"level": first["diagnosis"].get("conclusion_level")}),
            payload=first["diagnosis"])
        rewrite_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(first.get("rewrites", []))}),
            payload=first.get("rewrites", []))
        report_data = {
            "engine": first.get("llm_used", False) and "llm" or "local",
            "llm_error": first.get("llm_error", ""),
            "rewrite_count": len(first.get("rewrites", [])),
            "conclusion_level": first["diagnosis"].get("conclusion_level"),
            "total_score": first["diagnosis"].get("total_score"),
            "problems": len(first["diagnosis"].get("problems", [])),
        }
        report_asset = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload=report_data)
        results_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(batch["items"]), "ok": sum(1 for i in batch["items"] if i.get("status") == "succeeded")}),
            payload={"batch_id": batch.get("batch_id"), "items": batch["items"]})
        return {"diagnosis": diag_asset, "rewrites": rewrite_asset,
                "report": report_asset, "results": results_asset}