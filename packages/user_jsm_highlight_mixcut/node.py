# -*- coding: utf-8 -*-
"""精彩混剪节点（user/jsm_highlight_mixcut）。

能力（对应解说猫 highlight_mixcut 语义，独立重写并修复原版缺陷）：
- build_candidates：同角色且间隙<merge_gap 合并（text 以" "连接）；跨角色间隙<
  merge_gap 也合并（text 追加" / <role>: <text>"）；单行超 max_clip 留待选择截断。
- 打分：LLM 分批（≤batch_size）0-10 分，原地写 score/reason；批失败保留本地
  启发式分数 + reason（原版失败给默认 5 分且无原因——§11 缺陷，本实现透明报告）。
  无 API Key → 纯本地启发式（长度/情感词/标点/角色权重），确定性可复现。
- select_mixcut：按分数全局贪心择优凑满 target_seconds；单段>max_clip 截断。
- hook 开场：开头插入最高分段前 hook_max 秒副本（is_hook=True，reason 前缀），
  原段保留原位；hook_enabled 参数真实生效。
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

EMOTION_WORDS = ("杀", "死", "泪", "哭", "怒", "恨", "疯", "惊", "怕", "爱", "救", "赢",
                 "输", "骗", "背叛", "真相", "秘密", "反转", "危机", "爆炸", "决裂", "告白")
_HOOK_PREFIX_RE = re.compile(r"(你绝对想不到|好家伙|就因为|三分钟|最后|直接|竟然|居然|没想到)")
_PUNCT_INTENSITY = ("!", "？", "！", "？", "…", "……")


def _read_json(store, asset):
    if asset is None:
        return None
    try:
        return store.read_json(asset.id)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 候选构建
def build_candidates(rows: List[Dict[str, Any]], merge_gap: float = 1.5) -> List[Dict[str, Any]]:
    """按时间排序的 rows → 候选片段（合并规则与原版实测一致）。"""
    sorted_rows = sorted(rows, key=lambda r: float(r.get("start", 0.0) or 0.0))
    cands: List[Dict[str, Any]] = []
    for row in sorted_rows:
        if not isinstance(row, dict):
            continue
        st = float(row.get("start", 0.0) or 0.0)
        en = float(row.get("end", st + 1.0) or (st + 1.0))
        role = str(row.get("role", "") or "")
        text = str(row.get("text", "") or "")
        rec = {"start": st, "end": max(en, st + 0.1), "role": role, "text": text,
               "sentences": 1, "raw_rows": [row]}
        if cands:
            last = cands[-1]
            if (rec["start"] - last["end"]) <= merge_gap:
                if last["role"] == role:
                    last["text"] = (last["text"] + " " + text).strip()
                else:
                    last["text"] = (last["text"] + " / " + (role or "?") + ": " + text).strip()
                last["end"] = max(last["end"], rec["end"])
                last["sentences"] += 1
                last["raw_rows"].append(row)
                continue
        cands.append(rec)
    return cands


# ---------------------------------------------------------------- 本地打分
def local_score(cand: Dict[str, Any], index: int = 0, total: int = 1) -> Tuple[float, str]:
    """确定性启发式精彩度（0-10）：钩子句式/情感词/标点强度/时长适中/位置。"""
    text = cand.get("text", "")
    score = 4.0
    reasons: List[str] = ["基线"]
    if _HOOK_PREFIX_RE.search(text):
        score += 2.0
        reasons.append("钩子句式")
    hits = sum(1 for w in EMOTION_WORDS if w in text)
    if hits:
        score += min(2.0, hits * 0.5)
        reasons.append(f"情感词×{hits}")
    intensity = sum(1 for ch in _PUNCT_INTENSITY if ch in text)
    if intensity:
        score += min(1.5, intensity * 0.3)
        reasons.append(f"标点强度×{intensity}")
    dur = float(cand.get("end", 0.0) or 0.0) - float(cand.get("start", 0.0) or 0.0)
    if 3.0 <= dur <= 20.0:
        score += 0.5
        reasons.append("时长适中")
    if dur < 1.0:
        score -= 1.0
        reasons.append("过短")
    if total > 1 and (index == 0 or index == total - 1):
        score += 0.5
        reasons.append("首尾位置")
    score = max(0.0, min(10.0, score))
    return round(score, 2), "、".join(reasons)


# ---------------------------------------------------------------- LLM 打分
_LLM_SYSTEM = (
    "你是视频精彩度评审。对每个候选片段打分 0-10（整数或一位小数），10=高潮爆点，"
    "5=普通。只输出 JSON 数组："
    '[{"index":0,"score":7,"reason":"一句话原因"}, ...]（index 对应输入序号）。'
)


def _llm_score_batch(cands: List[Dict[str, Any]], api_key: str, base_url: str,
                     model: str, batch_size: int, timeout: float = 120.0) -> Dict[int, Tuple[float, str]]:
    """分批 LLM 打分 → {index:(score,reason)}；任何失败抛错（调用方回退本地）。"""
    out: Dict[int, Tuple[float, str]] = {}
    for bstart in range(0, len(cands), batch_size):
        chunk = cands[bstart:bstart + batch_size]
        lines = "\n".join(
            f'[{i}] {c["start"]:.1f}-{c["end"]:.1f}s {c.get("role","")}: {c.get("text","")[:100]}'
            for i, c in enumerate(chunk))
        user = f"候选片段：\n{lines}\n输出 JSON 数组 [{{\"index\":0,\"score\":7,\"reason\":\"...\"}},...]"
        raw = _llm_json([{"role": "system", "content": _LLM_SYSTEM},
                         {"role": "user", "content": user}],
                        api_key, base_url, model, timeout=timeout)
        data = _extract_json_block(raw)
        if not isinstance(data, list):
            raise ValueError("LLM 返回不是数组")
        for item in data:
            if not isinstance(item, dict):
                continue
            idx = int(item.get("index", -1))
            if 0 <= idx < len(chunk):
                try:
                    sc = float(item.get("score", 5.0) or 5.0)
                except (TypeError, ValueError):
                    sc = 5.0
                out[bstart + idx] = (max(0.0, min(10.0, sc)),
                                     str(item.get("reason", "") or "")[:120])
    return out


def _llm_json(messages, api_key, base_url, model, timeout: float = 120.0) -> str:
    import urllib.error
    import urllib.request
    url = str(base_url).rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 4000}
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
    m = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", raw, re.S)
    if m:
        return json.loads(m.group(1))
    s = raw.strip()
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
    raise ValueError("响应中未找到 JSON")


# ---------------------------------------------------------------- 择优 + hook
def select_mixcut(cands: List[Dict[str, Any]], target_seconds: float,
                  max_clip: float = 20.0) -> List[Dict[str, Any]]:
    """按分数全局贪心择优凑满目标时长；单段>max_clip 截断。"""
    ranked = sorted(cands, key=lambda c: -(float(c.get("score", 0.0) or 0.0)))
    chosen: List[Dict[str, Any]] = []
    acc = 0.0
    for c in ranked:
        if acc >= target_seconds:
            break
        dur = float(c.get("end", 0.0) or 0.0) - float(c.get("start", 0.0) or 0.0)
        if dur > max_clip:
            c = dict(c)
            c["end"] = c["start"] + max_clip
            dur = max_clip
        if dur <= 0:
            continue
        acc += dur
        chosen.append(c)
    chosen.sort(key=lambda c: float(c.get("start", 0.0) or 0.0))
    return chosen


def apply_hook_opening(selected: List[Dict[str, Any]], hook_max: float = 5.0) -> List[Dict[str, Any]]:
    """在开头插入最高分段前 hook_max 秒副本（is_hook=True），原段保留。"""
    if not selected:
        return selected
    top = max(selected, key=lambda c: float(c.get("score", 0.0) or 0.0))
    dur = float(top.get("end", 0.0) or 0.0) - float(top.get("start", 0.0) or 0.0)
    take = min(hook_max, dur)
    hook = dict(top)
    hook["start"] = top["start"]
    hook["end"] = top["start"] + take
    hook["is_hook"] = True
    hook["reason"] = "钩子开场 | " + str(hook.get("reason", "") or "")
    out = [hook] + list(selected)
    return out


# ---------------------------------------------------------------- 单条处理
def _process_payload(payload: Dict[str, Any], target_seconds: float, min_clip: float,
                     max_clip: float, merge_gap: float, hook_max: float,
                     hook_enabled: bool, scoring: str, api_key: str,
                     base_url: str, model: str, batch_size: int) -> Dict[str, Any]:
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows 必须是非空列表")
    target = float(payload.get("target_seconds", target_seconds) or target_seconds)

    cands = build_candidates(rows, merge_gap)
    # 最短片段：不足 min_clip 的与邻段合并
    if min_clip > 0:
        merged: List[Dict[str, Any]] = []
        for c in cands:
            dur = float(c.get("end", 0.0) or 0.0) - float(c.get("start", 0.0) or 0.0)
            if merged and dur < min_clip:
                prev = merged[-1]
                prev["end"] = max(prev["end"], c["end"])
                prev["text"] = (prev["text"] + " " + c.get("text", "")).strip()
                prev["sentences"] += c.get("sentences", 1)
                continue
            merged.append(c)
        cands = merged

    use_llm = scoring == "llm" or (scoring == "auto" and bool(api_key.strip()))
    batch_failures = 0
    local_reasons = 0
    for i, c in enumerate(cands):
        if use_llm:
            try:
                scores = _llm_score_batch(cands, api_key, base_url, model, batch_size)
                for idx, (sc, reason) in scores.items():
                    if 0 <= idx < len(cands):
                        cands[idx]["score"] = sc
                        cands[idx]["reason"] = reason
                # 未覆盖的（解析缺失）补本地
                for j, cc in enumerate(cands):
                    if "score" not in cc:
                        cc["score"], cc["reason"] = local_score(cc, j, len(cands))
                        local_reasons += 1
                break  # 一次全量即可
            except Exception as e:  # noqa: BLE001
                batch_failures += 1
                use_llm = False
                for j, cc in enumerate(cands):
                    if "score" not in cc:
                        cc["score"], cc["reason"] = local_score(cc, j, len(cands))
                        local_reasons += 1
        if "score" not in c:
            c["score"], c["reason"] = local_score(c, i, len(cands))
            local_reasons += 1

    selected = select_mixcut(cands, target, max_clip)
    if hook_enabled:
        selected = apply_hook_opening(selected, hook_max)

    timeline = [{
        "start": round(float(c.get("start", 0.0) or 0.0), 3),
        "end": round(float(c.get("end", 0.0) or 0.0), 3),
        "narration": c.get("text", ""), "role": c.get("role", ""),
        "score": c.get("score"), "is_hook": bool(c.get("is_hook", False)),
        "LAY_INDEX": i,
    } for i, c in enumerate(selected)]

    return {
        "selected": selected,
        "timeline": timeline,
        "candidates": len(cands),
        "scoring_mode": "llm" if (use_llm and not batch_failures) else "local",
        "llm_batch_failures": batch_failures,
        "local_reasons": local_reasons,
        "target_seconds": target,
        "total_selected_seconds": round(sum(
            (float(c.get("end", 0.0) or 0.0) - float(c.get("start", 0.0) or 0.0)) for c in selected), 3),
    }


class HighlightMixcutNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_highlight_mixcut",
            title="精彩混剪",
            category="语义",
            description="精彩片段候选合并/打分/贪心择优/钩子开场",
            inputs=[
                PortSpec(name="rows", type=PortType.JSON, label="候选行", required=False),
                PortSpec(name="items", type=PortType.JSON, label="批量条目", required=False),
            ],
            outputs=[
                PortSpec(name="selected", type=PortType.JSON, label="入选片段"),
                PortSpec(name="timeline", type=PortType.TIMELINE, label="混剪时间线"),
                PortSpec(name="report", type=PortType.JSON, label="混剪报告"),
                PortSpec(name="results", type=PortType.JSON, label="批量结果"),
            ],
            params=[
                PortSpec(name="target_seconds", type=PortType.FLOAT, label="目标时长(秒)",
                         default=300.0, widget="number"),
                PortSpec(name="min_clip", type=PortType.FLOAT, label="最短片段(秒)",
                         default=3.0, widget="number"),
                PortSpec(name="max_clip", type=PortType.FLOAT, label="单段上限(秒)",
                         default=20.0, widget="number"),
                PortSpec(name="merge_gap", type=PortType.FLOAT, label="合并间隙(秒)",
                         default=1.5, widget="number"),
                PortSpec(name="hook_max", type=PortType.FLOAT, label="钩子时长(秒)",
                         default=5.0, widget="number"),
                PortSpec(name="hook_enabled", type=PortType.BOOL, label="钩子开场",
                         default=False, widget="toggle"),
                PortSpec(name="scoring", type=PortType.STRING, label="打分方式", default="auto",
                         widget="select", options=["auto", "local", "llm"]),
                PortSpec(name="api_key", type=PortType.STRING, label="API Key",
                         default="", widget="password", required=False,
                         description="可选；LLM 分批精彩度打分"),
                PortSpec(name="base_url", type=PortType.STRING, label="中转地址",
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec(name="model", type=PortType.STRING, label="模型",
                         default="gpt-4o", widget="text"),
                PortSpec(name="batch_size", type=PortType.INT, label="LLM 批大小",
                         default=40, widget="number"),
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
        rows_asset = inputs.get("rows")
        items_asset = inputs.get("items")
        store = ctx["store"]

        target_seconds = max(1.0, float(params.get("target_seconds", 300.0) or 300.0))
        min_clip = max(0.0, float(params.get("min_clip", 3.0) or 3.0))
        max_clip = max(1.0, float(params.get("max_clip", 20.0) or 20.0))
        merge_gap = max(0.0, float(params.get("merge_gap", 1.5) or 1.5))
        hook_max = max(0.0, float(params.get("hook_max", 5.0) or 5.0))
        hook_enabled = bool(params.get("hook_enabled", False))
        scoring = str(params.get("scoring", "auto") or "auto")
        if scoring not in ("auto", "local", "llm"):
            raise ValueError("scoring 必须是 auto|local|llm")
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
        batch_size = max(1, int(params.get("batch_size", 40) or 40))
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
                        raw_items.append({"rows": it})
            elif isinstance(data, list):
                raw_items = [x if isinstance(x, dict) else {"rows": x} for x in data]
        if rows_asset is not None or not raw_items:
            payload: Dict[str, Any] = {}
            rows = _read_json(store, rows_asset)
            if isinstance(rows, list):
                payload["rows"] = rows
            if payload or not raw_items:
                raw_items.append(payload)

        if not raw_items:
            raise RuntimeError("缺少输入：请连接候选行或批量条目")

        cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                          str(ctx.get("node_id", "highlight_mixcut")) + ".json")

        async def worker(item: Dict[str, Any]) -> Dict[str, Any]:
            payload = item.get("input")
            if not isinstance(payload, dict):
                raise RuntimeError("条目输入必须是对象")
            import asyncio
            return await asyncio.to_thread(
                _process_payload, payload, target_seconds, min_clip, max_clip,
                merge_gap, hook_max, hook_enabled, scoring, api_key, base_url,
                model, batch_size)

        if batchlib is not None:
            batch = batchlib.new_batch(raw_items, source="user/jsm_highlight_mixcut",
                                       options={"target": target_seconds})
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

        selected_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(first.get("selected", []))}),
            payload=first.get("selected", []))
        timeline_asset = store.save_asset(
            Asset(id="", kind=PortType.TIMELINE.value,
                  meta={"count": len(first.get("timeline", []))}),
            payload=first.get("timeline", []))
        report_data = {
            "candidates": first.get("candidates", 0),
            "selected": len(first.get("selected", [])),
            "scoring_mode": first.get("scoring_mode", "local"),
            "llm_batch_failures": first.get("llm_batch_failures", 0),
            "local_reasons": first.get("local_reasons", 0),
            "target_seconds": first.get("target_seconds", target_seconds),
            "total_selected_seconds": first.get("total_selected_seconds", 0.0),
            "hook_enabled": hook_enabled,
        }
        report_asset = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload=report_data)
        results_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(batch["items"]), "ok": sum(1 for i in batch["items"] if i.get("status") == "succeeded")}),
            payload={"batch_id": batch.get("batch_id"), "items": batch["items"]})
        return {"selected": selected_asset, "timeline": timeline_asset,
                "report": report_asset, "results": results_asset}