# -*- coding: utf-8 -*-
"""核心插件：花字/关键词/BGM 分析（B4，对齐解说猫 core.flower_text_ai_analyzer 实测语义）。

输入最终字幕时间轴（建议接 core/asr 字幕 + core/ai_narrate 解说分段，原片对白行带 ost），
单次 LLM pass 产出四组节奏锚点：
- opening_flower_texts：片头 10 秒内、7 窗均匀精选（source_text 2-14 连续字 / display_text 2-7 连续字）
- flower_texts：10 秒后候选窗（密度 low/medium/high → 窗口间隔 25-35/15-25/8-15 秒）内的重点花字
- subtitle_keywords：整轴关键词，覆盖非 OST 行 45%-55%（每行最多一个，2-6 连续字）
- bgm_cues：剧情/情绪换歌边界（role ∈ 铺垫/紧张/推进/高潮/收束；intensity 1-5）

与参考实现对齐的实现要点：
- 密度窗口常量 FLOWER_DENSITY_WINDOWS = {low:(25,35), medium:(15,25), high:(8,15)}；
  片头 OPENING_END_SECONDS=10、OPENING_WINDOW_COUNT=7。
- 输出契约 {opening_flower_texts, flower_texts, subtitle_keywords, bgm_cues} 与 S1 对齐。
- **M4 修复**：row_id 缺失/为 None 时按行序合成索引，不再"全丢"。
- **M11 修复**：窗口边界统一 round(3) 且前后相邻窗口边界严格共用同一圆整值
  （逐块独立 round 会产生 2.85699… 之类小数不齐）；行时间同样圆整到 3 位。
- LLM 返回逐项校验（row 存在、文本必须是原文连续子串、字宽区间、窗口映射、
  role/intensity 枚举），非法项丢弃并记 warning；LLM 未回可解析 JSON 时重试。
- 高密度模式：若 LLM 给出的 flower_texts 少于候选窗口数，用 subtitle_keywords 本地补足
  （零额外 LLM 调用，对齐参考 _fill_high_density_from_keywords 语义）。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

OPENING_END_SECONDS = 10.0
OPENING_WINDOW_COUNT = 7
FLOWER_DENSITY_WINDOWS: Dict[str, Tuple[float, float]] = {
    "low": (25.0, 35.0),
    "medium": (15.0, 25.0),
    "high": (8.0, 15.0),
}
BGM_ROLES = {"推进", "铺垫", "高潮", "收束", "紧张"}
IMPORTANCE_SET = {"low", "medium", "high"}

US = 1_000_000  # 秒 → 微秒（与 jy_builder 一致）


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _r3(v: float) -> float:
    """M11 修复：时间统一保留 3 位小数（与 jy_builder/剪映 µs 精度对齐）。"""
    return round(float(v), 3)


def _strip_fence(raw: str) -> str:
    """去 markdown 围栏（```json ... ```）与首尾空白。"""
    if not raw:
        return ""
    t = raw.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _extract_json(text: str) -> Any:
    """从 LLM 输出提取 JSON（围栏 / 首个 { / 首个 [ 块）。失败返回 None。"""
    t = _strip_fence(text)
    for opener, closer in (("{", "}"), ("[", "]")):
        i = t.find(opener)
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(t)):
            if t[j] == opener:
                depth += 1
            elif t[j] == closer:
                depth -= 1
                if depth == 0:
                    chunk = t[i:j + 1]
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        break
    return None


# ---------------------------------------------------------------- 行归一化（M4/M11 修复在 _normalize_rows）

def _is_ost(row: Dict[str, Any]) -> bool:
    return bool(row.get("ost") or row.get("is_ost") or row.get("OST"))


def _row_text(row: Dict[str, Any]) -> str:
    for key in ("text", "content", "line"):
        v = row.get(key)
        if v:
            return str(v).strip()
    return ""


def _row_time(row: Dict[str, Any]) -> Tuple[float, float]:
    """M11 修复：start/end 统一圆整到 3 位小数；缺省回退。"""
    s = _r3(_num(row.get("start"), _num(row.get("start_time"), 0.0)))
    e = _r3(_num(row.get("end"), _num(row.get("end_time"), s)))
    return s, max(e, s)


def _normalize_rows(raw_rows: Any) -> List[Dict[str, Any]]:
    """输入行归一化：{row_id, text, start, end, ost}。

    - M4 修复：row_id 缺失/None → 按行序合成（row_id = i），不再整行丢弃；
    - M11 修复：start/end round(3)；
    - ost 默认 False。
    """
    out: List[Dict[str, Any]] = []
    if isinstance(raw_rows, dict):
        raw_rows = raw_rows.get("rows") or raw_rows.get("items") or raw_rows.get("segments") or []
    if not isinstance(raw_rows, list):
        return out
    for i, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            continue
        text = _row_text(row)
        if not text:
            continue
        s, e = _row_time(row)
        rid = row.get("row_id")
        if rid is None:
            rid = i  # M4：合成行序索引，避免下游 row_id=None 全丢
        out.append({
            "row_id": rid,
            "text": text,
            "start": s,
            "end": e,
            "ost": _is_ost(row),
        })
    out.sort(key=lambda r: r["start"])
    return out


def _mark_ost_from_segments(rows: List[Dict[str, Any]],
                            raw_segments: Any) -> List[Dict[str, Any]]:
    """用解说/时间线分段（含 OST 标记）把重叠的字幕行标为 ost=true。"""
    if not raw_segments or not rows:
        return rows
    segs: List[Dict[str, Any]] = []
    if isinstance(raw_segments, dict):
        raw_segments = raw_segments.get("rows") or raw_segments.get("items") or raw_segments.get("segments") or []
    if not isinstance(raw_segments, list):
        return rows
    for sg in raw_segments:
        if not isinstance(sg, dict):
            continue
        s = _num(sg.get("start"), _num(sg.get("start_time"), 0.0))
        e = _num(sg.get("end"), _num(sg.get("end_time"), s))
        if _is_ost(sg):
            segs.append((s, e))
    if not segs:
        return rows
    for r in rows:
        if r["ost"]:
            continue
        for s, e in segs:
            if r["start"] < e and r["end"] > s:
                r["ost"] = True
                break
    return rows


# ---------------------------------------------------------------- 窗口构建（M11 修复：共用圆整边界）

def build_opening_windows() -> List[Dict[str, Any]]:
    """片头 7 窗：[0,10] 均分。边界统一 round(3) 后共用，保证相邻窗口衔接无缝。"""
    span = OPENING_END_SECONDS / OPENING_WINDOW_COUNT
    bounds = [_r3(i * span) for i in range(OPENING_WINDOW_COUNT + 1)]
    windows = []
    for i in range(OPENING_WINDOW_COUNT):
        windows.append({"window_id": i, "start": bounds[i], "end": bounds[i + 1]})
    return windows


def build_candidate_windows(rows: List[Dict[str, Any]], density: str = "medium") -> List[Dict[str, Any]]:
    """10 秒后的候选窗：按密度窗口间隔扫描（low 25-35s / medium 15-25s / high 8-15s）。

    以行 start ≥ OPENING_END_SECONDS 的行起窗，贪心聚合直到窗口时长达到下限、
    再加下一行会超上限时收窗；窗口边界统一 round(3)（M11）。
    """
    lo, hi = FLOWER_DENSITY_WINDOWS.get(density, FLOWER_DENSITY_WINDOWS["medium"])
    post = [r for r in rows if r["start"] >= OPENING_END_SECONDS - 1e-6]
    windows: List[Dict[str, Any]] = []
    i = 0
    n = len(post)
    while i < n:
        s = post[i]["start"]
        j = i
        cur_end = post[i]["end"]
        while j + 1 < n:
            nxt = post[j + 1]["start"]
            if nxt - s > hi + 1e-6:
                break
            if (nxt - s) >= lo - 1e-6 and cur_end >= lo - 1e-6 and nxt - cur_end > 1e-6:
                break
            cur_end = max(cur_end, post[j + 1]["end"])
            j += 1
        windows.append({"window_id": len(windows),
                        "start": _r3(s), "end": _r3(max(cur_end, post[j]["end"]))})
        i = j + 1
    return windows


def _window_for_row(row: Dict[str, Any], windows: List[Dict[str, Any]]) -> Optional[int]:
    for w in windows:
        if w["start"] - 1e-6 <= row["start"] < w["end"]:
            return w["window_id"]
    return None


def _open_intersects(row: Dict[str, Any], window: Dict[str, Any]) -> bool:
    return row["start"] < window["end"] - 1e-6 and row["end"] > window["start"] + 1e-6


# ---------------------------------------------------------------- 校验（对齐参考 parse_and_validate_analysis 实测）

def _validate_items(kind: str, items: Any, rows_by_id: Dict[Any, Dict[str, Any]],
                    candidate_windows: List[Dict[str, Any]],
                    opening_windows: List[Dict[str, Any]],
                    dropped: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        rid = it.get("row_id")
        row = rows_by_id.get(rid) if rid is not None else None
        if row is None:
            dropped.append(f"{kind}: row_id={rid!r} 不在输入行中")
            continue
        ok = False
        if kind == "opening_flower_texts":
            src = str(it.get("source_text") or "").strip()
            disp = str(it.get("display_text") or "").strip()
            wid = it.get("window_id")
            imp = str(it.get("importance") or "medium").strip().lower()
            if not (2 <= len(src) <= 14 and src in row["text"]):
                dropped.append(f"opening: source_text {src!r} 非 2-14 连续子串")
            elif not (2 <= len(disp) <= 7 and disp in row["text"]):
                dropped.append(f"opening: display_text {disp!r} 非 2-7 连续子串")
            elif imp not in IMPORTANCE_SET:
                dropped.append(f"opening: importance {imp!r} 非法")
            elif isinstance(wid, bool) or not isinstance(wid, int) \
                    or not (0 <= wid < len(opening_windows)):
                dropped.append(f"opening: window_id {wid!r} 超出片头窗口")
            elif not _open_intersects(row, opening_windows[wid]):
                dropped.append(f"opening: row {rid} 与窗口 {wid} 不相交")
            else:
                out.append({"row_id": row["row_id"], "source_text": src,
                            "display_text": disp, "text": disp,
                            "window_id": wid, "importance": imp,
                            "source_ost": bool(row["ost"])})
                ok = True
        elif kind == "flower_texts":
            text = str(it.get("text") or "").strip()
            wid = it.get("window_id")
            imp = str(it.get("importance") or "medium").strip().lower()
            if not (2 <= len(text) <= 7 and text in row["text"]):
                dropped.append(f"flower: text {text!r} 非 2-7 连续子串")
            elif imp not in IMPORTANCE_SET:
                dropped.append(f"flower: importance {imp!r} 非法")
            elif isinstance(wid, bool) or not isinstance(wid, int) \
                    or _window_for_row(row, candidate_windows) != wid:
                dropped.append(f"flower: window_id {wid!r} 与行 {rid} 所在候选窗不一致")
            else:
                out.append({"row_id": row["row_id"], "text": text,
                            "window_id": wid, "importance": imp})
                ok = True
        elif kind == "subtitle_keywords":
            text = str(it.get("text") or "").strip()
            if not (2 <= len(text) <= 6 and text in row["text"]):
                dropped.append(f"keywords: text {text!r} 非 2-6 连续子串")
            else:
                out.append({"row_id": row["row_id"], "text": text, "row_text": row["text"]})
                ok = True
        elif kind == "bgm_cues":
            role = str(it.get("role") or "").strip()
            try:
                intensity = int(it.get("intensity"))
            except (TypeError, ValueError):
                intensity = -1
            if role not in BGM_ROLES:
                dropped.append(f"bgm: role {role!r} 不在 {sorted(BGM_ROLES)}")
            elif not (1 <= intensity <= 5):
                dropped.append(f"bgm: intensity {it.get('intensity')!r} 不在 1-5")
            else:
                out.append({"row_id": row["row_id"], "role": role, "intensity": intensity})
                ok = True
        if ok:
            pass
    return out


def analyze_rows(rows: List[Dict[str, Any]], llm_raw: str, *, flower_density: str,
                 want_flower: bool, want_keywords: bool, want_bgm: bool,
                 want_opening: bool) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    """解析并校验 LLM 单次 pass 的 JSON → 对齐 S1 的四组输出契约。

    返回 (analysis, dropped_warnings)。校验失败项一律丢弃并说明原因（不阻断其余项）。
    """
    opening_windows = build_opening_windows()
    candidate_windows = build_candidate_windows(rows, flower_density)
    data = _extract_json(llm_raw)
    if not isinstance(data, dict):
        return {}, ["LLM 未返回可解析的 JSON 对象"]
    rows_by_id = {r["row_id"]: r for r in rows}
    dropped: List[str] = []
    analysis: Dict[str, List[Dict[str, Any]]] = {
        "opening_flower_texts": [],
        "flower_texts": [],
        "subtitle_keywords": [],
        "bgm_cues": [],
    }
    if want_flower:
        analysis["opening_flower_texts"] = _validate_items(
            "opening_flower_texts", data.get("opening_flower_texts"), rows_by_id,
            candidate_windows, opening_windows, dropped)
        analysis["flower_texts"] = _validate_items(
            "flower_texts", data.get("flower_texts"), rows_by_id,
            candidate_windows, opening_windows, dropped)
    if want_keywords:
        analysis["subtitle_keywords"] = _validate_items(
            "subtitle_keywords", data.get("subtitle_keywords"), rows_by_id,
            candidate_windows, opening_windows, dropped)
    if want_bgm:
        analysis["bgm_cues"] = _validate_items(
            "bgm_cues", data.get("bgm_cues"), rows_by_id,
            candidate_windows, opening_windows, dropped)
    return analysis, dropped


def fill_high_density_from_keywords(analysis: Dict[str, List[Dict[str, Any]]],
                                    rows: List[Dict[str, Any]],
                                    candidate_windows: List[Dict[str, Any]]) -> List[str]:
    """高密度补足：flower_texts 少于候选窗数时，用 subtitle_keywords 零额外 LLM 补足。"""
    fills: List[str] = []
    n_windows = len(candidate_windows)
    have = {it["window_id"] for it in analysis["flower_texts"]}
    if len(analysis["flower_texts"]) >= n_windows:
        return fills
    for kw in analysis["subtitle_keywords"]:
        if len(analysis["flower_texts"]) >= n_windows:
            break
        row = next((r for r in rows if r["row_id"] == kw["row_id"]), None)
        if row is None:
            continue
        wid = _window_for_row(row, candidate_windows)
        if wid is None or wid in have:
            continue
        text = kw["text"]
        if len(text) > 7:
            text = text[:7]
        analysis["flower_texts"].append({"row_id": row["row_id"], "text": text,
                                         "window_id": wid, "importance": "medium"})
        have.add(wid)
        fills.append(f"高密度补足: 窗口 {wid} 用关键词 {kw['text']!r} 生成花字")
    return fills


# ---------------------------------------------------------------- LLM 双协议（对齐平台既有节点）

def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")


def _chat_openai(messages: List[Dict[str, str]], api_key: str, base_url: str, model: str,
                 temperature: float, max_tokens: int, timeout: float) -> Tuple[str, Dict[str, Any]]:
    import urllib.request
    payload: Dict[str, Any] = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        text = str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("openai: 响应解析失败: " + json.dumps(data, ensure_ascii=False)[:300]) from e
    usage = data.get("usage") or {}
    return text, {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                  "completion_tokens": int(usage.get("completion_tokens") or 0)}


def _chat_gemini(messages: List[Dict[str, str]], api_key: str, base_url: str, model: str,
                 temperature: float, max_tokens: int, timeout: float) -> Tuple[str, Dict[str, Any]]:
    import urllib.request
    parts: List[Dict[str, Any]] = []
    for m in messages:
        parts.append({"text": str(m.get("content") or "")})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens,
                             "responseMimeType": "application/json"},
    }
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url.rstrip('/')}/models/{model}:generateContent{sep}key={api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("gemini: 响应解析失败: " + json.dumps(data, ensure_ascii=False)[:300]) from e
    um = data.get("usageMetadata") or {}
    return text, {"prompt_tokens": int(um.get("promptTokenCount") or 0),
                  "completion_tokens": int(um.get("candidatesTokenCount") or 0)}


async def _llm_call(ctx: dict, messages: List[Dict[str, str]], provider: str, api_key: str,
                    base_url: str, model: str, temperature: float, max_tokens: int,
                    retries: int, timeout: float) -> Tuple[str, Dict[str, Any], int]:
    """带重试的 LLM 调用：返回 (text, usage, attempts)。"""
    import asyncio
    last_err = ""
    for attempt in range(max(1, retries + 1)):
        try:
            if provider == "gemini":
                text, usage = await asyncio.to_thread(
                    _chat_gemini, messages, api_key, base_url, model,
                    temperature, max_tokens, timeout)
            else:
                text, usage = await asyncio.to_thread(
                    _chat_openai, messages, api_key, base_url, model,
                    temperature, max_tokens, timeout)
            return text, usage, attempt + 1
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"llm: 请求失败（重试 {retries} 次）: {last_err}")


def _build_prompt(rows: List[Dict[str, Any]], flower_density: str,
                  want_flower: bool, want_keywords: bool, want_bgm: bool,
                  want_opening: bool) -> List[Dict[str, str]]:
    """构建单次 pass 的 LLM 消息（对齐参考 analyze_timeline 的 prompt 语义）。"""
    opening_windows = build_opening_windows()
    candidate_windows = build_candidate_windows(rows, flower_density)
    row_dicts = [{"row_id": r["row_id"], "text": r["text"],
                  "start": r["start"], "end": r["end"], "ost": r["ost"]} for r in rows]
    task = "通读最终字幕时间轴，一次选择片头花字、全程精选花字、普通字幕关键词和背景音乐变化点。" \
           "四个列表用途不同，必须独立判断。只返回 JSON。"
    rules: Dict[str, str] = {}
    if want_opening:
        rules["opening_flower_texts"] = (
            "只处理与0-10秒相交的最终字幕，按opening_windows均匀选择，通常6-7组、最多7组；"
            "每个窗口最多一组，没有合适内容允许跳过。原片对白(ost=true)与解说都可以入选。"
            "source_text必须是对应原文连续的2-14个汉字，用于时间定位；display_text必须是2-7个汉字。"
            "原片对白允许删除口语填充、重复和次要修饰进行忠实提炼，但禁止改变否定、人物身份、数字、"
            "因果、结果和确定程度，禁止增加原文没有的事实。解说display_text优先使用原文连续短语。"
            "返回正确window_id和importance。")
    if want_flower:
        rules["flower_texts"] = (
            "只处理10秒后的候选窗口；窗口间隔为15-25秒。每个候选窗口最多一组，没有真正重点时允许跳过。"
            "优先反转、揭示、冲突、危险、关键决定、身份信息和结论。text必须是对应原文连续的2-7个汉字，"
            "保持完整语义，禁止残缺词组。返回正确window_id，并用importance标记low、medium或high。")
    if want_keywords:
        rules["subtitle_keywords"] = (
            "独立分析整条时间轴，不受花字候选窗口限制；允许与flower_texts使用相同row_id，二者可以共存。"
            "只强调关键人物、动作、信息、转折或结论；每行最多一个，整体约覆盖45%-55%的非OST行。"
            "text必须是对应原文连续的2-6个汉字，不选虚词、语气词或普通连接词。")
    if want_bgm:
        rules["bgm_cues"] = (
            "根据整条文案的剧情和情绪阶段选择自然换歌边界，不决定具体使用哪首音乐，也不是按时长均分。"
            "第一项必须对应最早的有效行；之后只在人物目标、冲突状态、行动阶段、信息认知、重大反转、"
            "高潮或结局方向真正变化时新增。两到三分钟视频通常共2-3项；更短视频允许更少。"
            "数量只用于适配视频尺度，所有锚点仍必须落在真实内容转折；禁止凑数量，禁止为了相隔若干秒而新增。"
            "同一叙事目标和情绪方向下不要仅因进入新内容阶段而重复新增锚点。"
            "row_id必须来自输入；role只能是铺垫、紧张、推进、高潮、收束；intensity只能是1-5的整数。"
            "高潮仅用于明确冲突、动作爆发、重大反转或情绪爆发，普通叙述不要持续标高潮。")
    user = {
        "task": task,
        "rules": rules,
        "opening_windows": opening_windows,
        "windows": candidate_windows,
        "rows": row_dicts,
        "flower_density": flower_density,
        "output": {
            "opening_flower_texts": "[{row_id, source_text, display_text, window_id, importance}]",
            "flower_texts": "[{row_id, text, window_id, importance}]",
            "subtitle_keywords": "[{row_id, text}]",
            "bgm_cues": "[{row_id, role, intensity}]",
        },
    }
    return [
        {"role": "system", "content": "你是严格的视频内容节奏分析器，只输出合法 JSON。"},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


# ---------------------------------------------------------------- 节点

class FlowerTextAnalyzeNode(NodeBase):
    """花字/关键词/BGM 分析。type_id: core/flower_text_analyze"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/flower_text_analyze",
            title="花字/关键词/BGM 分析",
            category="分析",
            description="字幕→片头花字/精选花字/字幕关键词/BGM 换歌点（单次 LLM pass；密度 low/medium/high；片头 10s/7 窗）",
            inputs=[
                PortSpec("subtitles", PortType.SUBTITLE, label="字幕", required=True),
                PortSpec("segments", PortType.SEGMENTS, label="解说/时间线分段", required=False),
            ],
            outputs=[
                PortSpec("analysis", PortType.JSON, label="节奏分析", required=True),
                PortSpec("report", PortType.JSON, label="分析报告", required=False),
            ],
            params=[
                PortSpec("flower_density", PortType.STRING, label="花字密度", default="medium",
                         widget="select", options=["low", "medium", "high"]),
                PortSpec("want_flower", PortType.BOOL, label="分析花字", default=True, widget="toggle"),
                PortSpec("want_keywords", PortType.BOOL, label="分析字幕关键词", default=True, widget="toggle"),
                PortSpec("want_bgm", PortType.BOOL, label="分析 BGM 换歌点", default=True, widget="toggle"),
                PortSpec("want_opening", PortType.BOOL, label="分析片头花字", default=True, widget="toggle"),
                PortSpec("provider", PortType.STRING, label="Provider", default="auto",
                         widget="select", options=["auto", "openai", "gemini"]),
                PortSpec("api_key", PortType.STRING, label="API Key", required=False, default="",
                         widget="password", description="支持 @secret:名称 引用保险箱"),
                PortSpec("base_url", PortType.STRING, label="API 地址", required=False, default="", widget="text"),
                PortSpec("model", PortType.STRING, label="模型", default="", widget="text"),
                PortSpec("temperature", PortType.FLOAT, label="温度", default=0.3, widget="number"),
                PortSpec("max_output_tokens", PortType.INT, label="最大输出 Token", default=4096, widget="number"),
                PortSpec("retries", PortType.INT, label="重试次数", default=2, widget="number",
                         description="LLM 请求失败/解析失败重试次数"),
                PortSpec("api_timeout", PortType.INT, label="API 超时(秒)", default=600, widget="number"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        sub_asset = inputs.get("subtitles")
        seg_asset = inputs.get("segments")
        if sub_asset is None:
            raise RuntimeError("invalid: 需要字幕输入（subtitles）")
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)

        try:
            raw_subs = store.read_json(sub_asset.id) or []
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"invalid: 字幕资产读取失败: {e}") from e
        rows = _normalize_rows(raw_subs)
        if not rows:
            # 字幕为空时回退到 segments（部分链路上游只给解说分段）
            if seg_asset is not None:
                raw_segs = store.read_json(seg_asset.id) or []
                rows = _normalize_rows(raw_segs)
        if not rows:
            raise RuntimeError("invalid: 字幕/分段均为空，无法分析")
        if seg_asset is not None:
            raw_segs = store.read_json(seg_asset.id) or []
            rows = _mark_ost_from_segments(rows, raw_segs)

        flower_density = str(params.get("flower_density", "medium") or "medium").strip().lower()
        if flower_density not in FLOWER_DENSITY_WINDOWS:
            flower_density = "medium"
        want_flower = bool(params.get("want_flower", True))
        want_keywords = bool(params.get("want_keywords", True))
        want_bgm = bool(params.get("want_bgm", True))
        want_opening = bool(params.get("want_opening", True))
        provider = str(params.get("provider", "auto") or "auto")
        api_key = _resolve_secret(ctx, str(params.get("api_key", "") or ""))
        base_url = str(params.get("base_url", "") or "").strip()
        model = str(params.get("model", "") or "").strip() or ""
        # LLM 通道配置化：显式参数 > 全局配置 > provider 默认
        if (not api_key) or (not base_url) or (not model):
            try:
                from app.llm_config import resolve_global as _fw_g
                _fw = _fw_g() or {}
            except Exception:  # noqa: BLE001
                _fw = {}
            if not api_key:
                api_key = str(_fw.get("api_key") or "")
            if not base_url:
                base_url = str(_fw.get("base_url") or "")
            if not model:
                model = str(_fw.get("model") or "")
        if not api_key:
            raise RuntimeError("contract: 请填写 API Key 或用 @secret:名称 引用保险箱，或配置全局 LLM 通道")
        temperature = max(0.0, min(2.0, _num(params.get("temperature", 0.3), 0.3)))
        max_tokens = max(256, int(params.get("max_output_tokens", 4096) or 4096))
        retries = max(0, int(params.get("retries", 2) or 2))
        api_timeout = max(10, int(params.get("api_timeout", 600) or 600))

        if not api_key:
            raise RuntimeError("contract: 请填写 API Key 或用 @secret:名称 引用保险箱")
        if provider == "auto":
            provider = "gemini" if "generativelanguage" in base_url else "openai"
        if not base_url:
            base_url = ("https://generativelanguage.googleapis.com/v1beta"
                        if provider == "gemini" else "")

        progress(0.1)
        messages = _build_prompt(rows, flower_density, want_flower, want_keywords,
                                 want_bgm, want_opening)
        # 单次 LLM pass + 解析重试（LLM 未回合法 JSON 或请求失败 → 重试）
        last_err = ""
        llm_usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        attempts = 0
        analysis: Dict[str, List[Dict[str, Any]]] = {
            "opening_flower_texts": [], "flower_texts": [],
            "subtitle_keywords": [], "bgm_cues": [],
        }
        dropped: List[str] = []
        for attempt in range(max(1, retries + 1)):
            attempts = attempt + 1
            try:
                raw, usage, _calls = await _llm_call(
                    ctx, messages, provider, api_key, base_url, model,
                    temperature, max_tokens, 0, api_timeout)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                if attempt < retries:
                    await asyncio_sleep(1.5 * (attempt + 1))
                continue
            llm_usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            llm_usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            analysis, dropped = analyze_rows(
                rows, raw, flower_density=flower_density,
                want_flower=want_flower, want_keywords=want_keywords,
                want_bgm=want_bgm, want_opening=want_opening)
            if analysis or not dropped:
                break
            last_err = "LLM 输出未通过校验（全部合法项为空）"
            if attempt < retries:
                await asyncio_sleep(1.5 * (attempt + 1))
        if last_err and not any(analysis.values()):
            raise RuntimeError(f"llm: 分析失败（重试 {retries} 次）: {last_err}")

        # 高密度补足（零额外调用）
        fills: List[str] = []
        if want_flower and flower_density == "high":
            wins = build_candidate_windows(rows, flower_density)
            fills = fill_high_density_from_keywords(analysis, rows, wins)

        progress(0.8)
        non_ost = [r for r in rows if not r["ost"]]
        kw_rows = {it["row_id"] for it in analysis["subtitle_keywords"]}
        coverage = round(len(kw_rows & {r["row_id"] for r in non_ost}) / max(len(non_ost), 1) * 100, 1) \
            if non_ost else 0.0
        warnings: List[str] = []
        warnings.extend(dropped[:40])
        warnings.extend(fills)
        if non_ost and not (45.0 <= coverage <= 55.0):
            warnings.append(f"关键词覆盖 {coverage}%（目标 45%-55%），请人工复核")

        opening_windows = build_opening_windows()
        candidate_windows = build_candidate_windows(rows, flower_density)
        analysis_payload = {
            "schema_version": 1,
            "source": "core/flower_text_analyze",
            "analysis_version": "20260820.4-like",
            "flower_density": flower_density,
            "opening_windows": opening_windows,
            "candidate_windows": candidate_windows,
            "rows": rows,
            "analysis": analysis,
        }
        analysis_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={k: len(v) for k, v in analysis.items()}),
            payload=analysis_payload)
        report = {
            "ok": True,
            "rows": len(rows),
            "non_ost_rows": len(non_ost),
            "keyword_coverage_pct": coverage,
            "windows": {"opening": len(opening_windows), "candidate": len(candidate_windows),
                        "density": flower_density},
            "counts": {k: len(v) for k, v in analysis.items()},
            "llm": {"provider": provider, "model": model, "attempts": attempts,
                    "usage": llm_usage},
            "warnings": warnings,
            "m4_fix": "row_id=None 已按行序合成索引，行不再丢失",
            "m11_fix": "窗口边界统一 round(3) 且共用同一圆整边界，相邻窗口无缝衔接",
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": "b4"}), payload=report)
        progress(1.0)
        return {"analysis": analysis_asset, "report": report_asset}


async def asyncio_sleep(t: float) -> None:
    import asyncio
    await asyncio.sleep(t)