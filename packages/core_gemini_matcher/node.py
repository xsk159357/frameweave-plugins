# -*- coding: utf-8 -*-
"""核心插件：Gemini 语义匹配节点（B2，对齐解说猫 r2 §2 gemini_script_matcher 语义）。

专用节点语义（与 ost_select / match_visual 近似替代区分）：
  场景描述 + 解说词 → LLM 语义匹配 → OST 插入（两阶段，可独立/串联执行）。

阶段一 match：descriptions（场景描述，text/scenes/split_cam 三种形态）→ 按批送 LLM
  语义匹配（每段解说词 → 1-3 个连续场景 scene_ids + reason，避免大跳跃）→ matches。
  无 key / LLM 失败 → 本地启发式兜底（顺序消费场景 + 全局去重 + TTS 时长扩展），
  report.match_method='local' 且 llm_error 显式记录（禁止静默 None，对齐 r2 §11-6）。
阶段二 ost：script 中【原片:xx:xx - xx:xx】标记解析 → END 按 MAX_OST_DURATION 钳制 →
  按 char_position 排序插入 split_cam（含 OST 条目，type='ost'），LAY_INDEX 重排。

约定（对齐平台）：
- LLM 复用 core_llm_call.llm_call_json（统一退避/回退/R1 R2），llm_backend=internal 走
  原版内嵌 urllib + 重试 [3,8,15]（供对照）；default=llm_call。
- 常量对齐原版：BATCH_SIZE=15、DEFAULT_CHARS_PER_SECOND=4.5、MAX_OST_DURATION=15.0、
  MONTAGE_THRESHOLD=8.0、temperature=0.1。
- M2 修复：all_des.txt 解析保留【对话】对白尾部为 dialogue 字段（原版丢弃）。
- 错误分层：无 key→warn+本地；LLM 永久 401/404→显式 error（不重试）；超时/网络→重试后
  本地兜底并 report.llm_error。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

# ---------------------------------------------------------------- 常量（r2 §2.2 实测）
DEFAULT_BATCH_SIZE = 15
DEFAULT_CHARS_PER_SECOND = 4.5
MAX_OST_DURATION = 15.0
MAX_RETRIES = 3
RETRY_DELAYS = [3.0, 8.0, 15.0]
MONTAGE_THRESHOLD = 8.0
DEFAULT_TEMPERATURE = 0.1
DEFAULT_BASE_URL = "https://ameaaos.com/v1"   # 平台约定中转（原版 api.90087.cn 可参数覆盖）
DEFAULT_MODEL = ""                       # 平台默认（r2 原版 gemini-2.5-flash 可覆盖）

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_SCENE_BOUND_RE = re.compile(r"^\s*\[\s*([0-9:.]+)s?\s*->\s*([0-9:.]+)s?\s*\]\s*(.*)$", re.S)
_OST_MARK_RE = re.compile(r"【原片\s*:\s*([0-9:.]+)\s*-\s*([0-9:.]+)\s*】")
_MARKER_RE = re.compile(r"【原片\s*:\s*([0-9:.]+)\s*-\s*([0-9:.]+)\s*】")


# ---------------------------------------------------------------- 场景解析（M2 修复）

def _parse_time_str(s: str) -> float:
    """支持 7008.68 / MM:SS / HH:MM:SS（r2 §2.5 实测）。"""
    s = str(s).strip()
    if not s:
        return 0.0
    if ":" not in s:
        try:
            return float(s)
        except ValueError:
            return 0.0
    parts = s.split(":")
    try:
        secs = 0.0
        for p in parts:
            secs = secs * 60 + float(p)
        return secs
    except ValueError:
        return 0.0


def _clean_scene_text(raw: str) -> Tuple[str, str]:
    """all_des.txt 行内文本：`|` → ` - `；【对话】尾部改存 dialogue（M2 修复不丢弃）。"""
    segs = [p.strip() for p in str(raw).split("|") if p.strip()]
    dialogue = ""
    keep = []
    for seg in segs:
        m = re.match(r"^【对话】\s*(.*)$", seg)
        if m:
            dialogue = m.group(1).strip()
        else:
            keep.append(seg)
    text = " - ".join(keep)
    if dialogue and not keep:
        text = dialogue
    return text, dialogue


def parse_all_des_txt(text: str) -> List[Dict[str, Any]]:
    """解析 all_des.txt：[0.0s -> 4.0s] 特写镜头 | 男人走进房间 | 【对话】喂，你来了。
    返回 [{id,start,end,text,dialogue}]；M2 修复：dialogue 保留（原版丢弃尾部）。"""
    scenes: List[Dict[str, Any]] = []
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = _SCENE_BOUND_RE.match(ln)
        if not m:
            continue
        start = _parse_time_str(m.group(1))
        end = _parse_time_str(m.group(2))
        body = m.group(3)
        if end <= start:
            continue
        txt, dialogue = _clean_scene_text(body)
        scenes.append({"id": len(scenes), "start": start, "end": end,
                       "text": txt, "dialogue": dialogue})
    return scenes


def load_scenes(data: Any) -> List[Dict[str, Any]]:
    """场景归一化：接受 dict{scenes:[...]} / list（split_cam 或 scenes.json）/ txt 字符串。"""
    if isinstance(data, str):
        return parse_all_des_txt(data)
    if isinstance(data, dict):
        for key in ("scenes", "items", "segments"):
            v = data.get(key)
            if isinstance(v, list):
                data = v
                break
        else:
            return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(data):
        if not isinstance(s, dict):
            continue
        start = s.get("start", s.get("start_time", s.get("start_sec", 0.0)))
        end = s.get("end", s.get("end_time", s.get("end_sec", 0.0)))
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            continue
        text = str(s.get("text") or s.get("visual_description") or s.get("description") or "")
        dialogue = str(s.get("dialogue") or s.get("dialogues") or "")
        if not text and dialogue:
            text = dialogue
        out.append({"id": int(s.get("id", i)), "start": start, "end": end,
                    "text": text, "dialogue": dialogue})
    return out


def split_script(script: str) -> Tuple[List[Dict[str, Any]], str]:
    """解说词分行（提纯切分）：去掉时间戳/章节/镜头标注行，保留原文与【原片】标记原位。
    返回 (narrations:[{index,text}], clean_script)。空行与纯标记行跳过。"""
    narrations: List[Dict[str, Any]] = []
    out_lines: List[str] = []
    for raw in str(script or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if _SCENE_BOUND_RE.match(ln):
            continue                    # 制作标记行（时间戳/章节/镜头）剔除
        text = _OST_MARK_RE.sub("", ln).strip()
        if text:
            narrations.append({"index": len(narrations), "text": text})
        out_lines.append(ln)            # 原行（含 OST 标记）保留给阶段二
    return narrations, "\n".join(out_lines)


# ---------------------------------------------------------------- TTS 时长 / 场景适配

def estimate_tts_duration(text: str, chars_per_second: float = DEFAULT_CHARS_PER_SECOND) -> float:
    """只统计 CJK 字符 ÷ 语速（r2 §2.5 实测：13 汉字→2.889）。最低 1.0s 防空片段。"""
    n = len(_CJK_RE.findall(str(text or "")))
    return round(max(1.0, n / (chars_per_second or DEFAULT_CHARS_PER_SECOND)), 3)


def adapt_scene_duration(tts_duration: float, scene_ids: List[int], all_scenes: List[Dict[str, Any]],
                         min_scene_duration: float = 1.0) -> Tuple[List[int], float]:
    """按 TTS 时长扩展场景区间（顺序向后吸收相邻场景直至覆盖时长）。
    MONTAGE_THRESHOLD：单场景本就超阈值视为蒙太奇（原语义保留）。返回 (scene_ids, covered)。"""
    ids: List[int] = []
    covered = 0.0
    order = sorted(scene_ids)
    for sid in order:
        sc = next((s for s in all_scenes if int(s.get("id", -1)) == sid), None)
        if sc is None:
            continue
        ids.append(sid)
        covered += max(min_scene_duration, float(sc["end"]) - float(sc["start"]))
        if covered >= tts_duration:
            break
    # 不足：继续顺序吸收后续场景（局部去重，不打乱顺序）
    if covered < tts_duration:
        existing = set(ids)
        for sc in sorted(all_scenes, key=lambda s: s["start"]):
            sid = int(sc.get("id", -1))
            if sid in existing:
                continue
            ids.append(sid)
            existing.add(sid)
            covered += max(min_scene_duration, float(sc["end"]) - float(sc["start"]))
            if covered >= tts_duration:
                break
    return ids, round(covered, 3)


# ---------------------------------------------------------------- 阶段一：本地启发式匹配（无 key 兜底）

def local_match(narrations: List[Dict[str, Any]], scenes: List[Dict[str, Any]],
                chars_per_second: float = DEFAULT_CHARS_PER_SECOND,
                avoid_adjacent_repeat: bool = True) -> List[Dict[str, Any]]:
    """本地兜底：顺序消费场景 + 全表去重 + TTS 时长扩展（r1 §3.10 方案A 语义）。
    avoid_adjacent_repeat 真实生效（r2 §11-1 修复：原版开关疑似失效）。"""
    matches: List[Dict[str, Any]] = []
    used: set = set()
    last_primary: Optional[int] = None
    stream = sorted(scenes, key=lambda s: (s["start"], s["id"]))
    pos = 0
    for n in narrations:
        tts = estimate_tts_duration(n["text"], chars_per_second)
        # 顺序取未用场景作为主场景
        picked: List[int] = []
        for k in range(pos, len(stream)):
            sid = int(stream[k].get("id", k))
            if avoid_adjacent_repeat and sid == last_primary:
                continue
            if sid not in used:
                picked.append(sid)
                used.add(sid)
                last_primary = sid
                pos = k + 1
                break
        if not picked:  # 场景耗尽：允许复用（原版场景数<句数交替复用）
            k = pos % len(stream) if stream else 0
            sid = int(stream[k].get("id", k)) if stream else -1
            if sid >= 0:
                picked = [sid]
                pos = k + 1
                last_primary = sid
                used.add(sid)
        ids, _covered = adapt_scene_duration(tts, picked, scenes)
        matches.append({
            "narration_index": int(n["index"]),
            "text": n["text"],
            "scene_ids": ids,
            "reason": "local: 顺序消费+时长扩展" if ids else "local: 无可用场景",
            "match_level": "semantic",
            "tts_duration": tts,
        })
    return matches


# ---------------------------------------------------------------- 阶段一：LLM 语义匹配

def build_match_batch_prompt(narrations_batch: List[Dict[str, Any]],
                             scenes: List[Dict[str, Any]]) -> str:
    """匹配 Prompt（对齐 r5 line 328：每段 1-3 个连续场景，避免大跳跃，输出
    narration_index/scene_ids/reason）。"""
    scene_lines = []
    for s in scenes:
        desc = str(s.get("text") or "")
        dlg = str(s.get("dialogue") or "")
        if dlg and dlg not in desc:
            desc = (desc + f"（对白：{dlg}）") if desc else f"对白：{dlg}"
        scene_lines.append(f"[{s.get('id')}] {s.get('start'):g}s-{s.get('end'):g}s {desc}".rstrip())
    nar_lines = []
    for n in narrations_batch:
        nar_lines.append(f"[{n['index']}] {n['text']}")
    return (
        "你是专业的视频解说词-场景语义匹配专家。\n"
        "请把每一段解说词匹配到最贴合的 1-3 个**连续**场景（scene_ids 按场景输入顺序，"
        "避免大跳跃；无明显匹配的段允许 0 个场景并说明）。\n"
        "严格遵守：scene_id 必须来自下方场景列表；每段一条记录。\n\n"
        "【场景列表】\n" + "\n".join(scene_lines) + "\n\n"
        "【解说词分段】\n" + "\n".join(nar_lines) + "\n\n"
        '【输出】只输出 JSON 数组，不要任何解释：\n'
        '[{"narration_index": 0, "scene_ids": [2, 3], "reason": "简短理由"}]'
    )


def parse_match_response(raw: str) -> List[Dict[str, Any]]:
    """解析匹配 JSON（容忍 markdown 围栏/前后缀）。返回原始条目列表。"""
    t = str(raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    for opener, closer in (("[", "]"), ("{", "}")):
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
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else data.get("matches", []) if isinstance(data, dict) else []


def sanitize_matches(raw_items: List[Dict[str, Any]], narr_index_set: set,
                     scene_ids_set: set, warnings: List[str]) -> List[Dict[str, Any]]:
    """逐条校验（非法 narration_index/scene_id → 告警，不整批丢弃，对齐 r2 §11-7）。"""
    out: List[Dict[str, Any]] = []
    for it in raw_items:
        if not isinstance(it, dict):
            warnings.append("匹配条目非对象，忽略")
            continue
        try:
            ni = int(it.get("narration_index"))
        except (TypeError, ValueError):
            warnings.append(f"忽略 narration_index 非法项: {it!r}")
            continue
        if ni not in narr_index_set:
            warnings.append(f"忽略解说词 {ni} 的匹配：解说词不存在")
            continue
        ids_raw = it.get("scene_ids")
        if not isinstance(ids_raw, list):
            ids_raw = [ids_raw] if ids_raw is not None else []
        ids = []
        for x in ids_raw:
            try:
                xid = int(x)
            except (TypeError, ValueError):
                warnings.append(f"解说词 {ni} 的 scene_id 非法 {x!r}，忽略")
                continue
            if xid not in scene_ids_set:
                warnings.append(f"解说词 {ni} 引用不存在的场景 {xid}，忽略")
                continue
            if xid not in ids:
                ids.append(xid)
        out.append({
            "narration_index": ni,
            "text": "",
            "scene_ids": ids[:3],
            "reason": str(it.get("reason") or "")[:200],
            "match_level": "semantic",
        })
    return out


def _llm_chat_internal(messages, api_key, base_url, model, temperature, max_tokens,
                       timeout=180.0, retries=MAX_RETRIES, delays=RETRY_DELAYS,
                       balance_hook=None) -> str:
    """原版内嵌 urllib（llm_backend=internal 对照通道）：重试 [3,8,15]，R2 余额显式。"""
    import urllib.error
    import urllib.request
    last_err = ""
    for attempt in range(max(1, int(retries) + 1)):
        try:
            payload = {"model": model, "messages": messages,
                       "temperature": temperature, "max_tokens": max_tokens}
            req = urllib.request.Request(
                str(base_url).rstrip("/") + "/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", "Bearer " + api_key)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            try:
                return str(data["choices"][0]["message"]["content"] or "")
            except (KeyError, IndexError, TypeError) as e:
                raise RuntimeError("gemini_matcher: 响应解析失败: "
                                   + json.dumps(data, ensure_ascii=False)[:300]) from e
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (400, 401, 403, 404, 402):
                raise RuntimeError(f"HTTP {e.code}（永久失败）: {body}")
            last_err = f"HTTP {e.code}: {body}"
            if balance_hook and (e.code in (402, 429) or "balance" in body.lower()
                                 or "余额" in body):
                balance_hook(body)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = f"network: {type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < int(retries):
            time.sleep(delays[min(attempt, len(delays) - 1)])
    raise RuntimeError(f"gemini_matcher: LLM 请求失败（重试 {retries} 次）: {last_err}")


def llm_semantic_match(narrations, scenes, batch_size=DEFAULT_BATCH_SIZE, temperature=DEFAULT_TEMPERATURE,
                       max_tokens=4096, backend="llm_call", api_key="", base_url=DEFAULT_BASE_URL,
                       model=DEFAULT_MODEL, timeout=180.0, retries=MAX_RETRIES,
                       on_llm_fail="warn", log=None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """两阶段之一：LLM 语义匹配（按批）。返回 (matches, match_meta)。"""
    meta: Dict[str, Any] = {"llm_calls": 0, "llm_error": "", "batch_count": 0,
                            "match_method": "llm", "warnings": []}
    narr_index_set = {int(n.get("index", i)) for i, n in enumerate(narrations)}
    scene_ids_set = {int(s.get("id", i)) for i, s in enumerate(scenes)}
    if not narrations:
        return [], meta
    if not api_key:
        meta["match_method"] = "local"
        meta["llm_error"] = "无 api_key（@secret 引用或参数），走本地兜底"
        if log:
            log("[gemini_matcher] " + meta["llm_error"])
        return local_match(narrations, scenes), {**meta,
                                                 "llm_calls": 0, "batch_count": 0}
    all_matches: List[Dict[str, Any]] = []
    for i in range(0, len(narrations), max(1, int(batch_size))):
        batch = narrations[i:i + max(1, int(batch_size))]
        prompt = build_match_batch_prompt(batch, scenes)
        meta["batch_count"] += 1
        raw = ""
        try:
            if backend == "llm_call":
                from core_llm_call.node import llm_call_json
                r = llm_call_json(
                    [{"role": "system",
                      "content": "你是视频语义匹配助手，只输出合法 JSON。"},
                     {"role": "user", "content": prompt}],
                    provider="openai", api_key=api_key, base_url=base_url, model=model,
                    temperature=temperature, max_tokens=max_tokens, json_mode=False,
                    timeout=timeout,
                    backoff=[d for d in RETRY_DELAYS[:max(1, int(retries))]],
                    log=log)
                raw = str(r.get("text") or "")
            else:
                raw = _llm_chat_internal(
                    [{"role": "system", "content": "你是视频语义匹配助手，只输出合法 JSON。"},
                     {"role": "user", "content": prompt}],
                    api_key, base_url, model, temperature, max_tokens,
                    timeout=timeout, retries=retries)
            meta["llm_calls"] += 1
        except Exception as e:  # noqa: BLE001
            meta["llm_error"] = f"{type(e).__name__}: {e}"
            meta["warnings"].append(f"批次 {i // max(1, int(batch_size))} LLM 失败: {meta['llm_error']}")
            if log:
                log(f"[gemini_matcher] LLM 失败：{meta['llm_error']}")
            if str(on_llm_fail).lower() == "error":
                raise RuntimeError(f"gemini_matcher LLM 失败（on_llm_fail=error）: {meta['llm_error']}")
            remaining = narrations[i:]
            all_matches += local_match(remaining, scenes)
            meta["match_method"] = "local"
            return all_matches, meta
        items = parse_match_response(raw)
        if not items:
            meta["llm_error"] = "LLM 无 JSON 可解析"
            meta["warnings"].append("LLM 响应空/不可解析，本地兜底")
            all_matches += local_match(batch, scenes)
            meta["match_method"] = "local"
            continue
        batch_matches = sanitize_matches(items, narr_index_set, scene_ids_set, meta["warnings"])
        # 缺失的 narration_index 补齐本地匹配
        got = {int(m["narration_index"]) for m in batch_matches}
        missing = [n for n in narrations if int(n["index"]) not in got
                   and i <= int(n["index"]) < i + max(1, int(batch_size))]
        if missing:
            merged = batch_matches + local_match(missing, scenes)
            merged.sort(key=lambda m: m["narration_index"])
            batch_matches = merged
        all_matches += batch_matches
    all_matches.sort(key=lambda m: m["narration_index"])
    return all_matches, meta


# ---------------------------------------------------------------- 阶段二：OST 插入

def parse_fixed_ost_timelines(script: str, max_ost: float = MAX_OST_DURATION
                              ) -> Tuple[str, List[Dict[str, Any]]]:
    """解析【原片:00:12 - 00:30】标记 → (clean_script, ost_markers)。
    ost_markers: [{start,end(钳制),char_position,preceding_text}]（r2 §2.6 实测）。"""
    clean_chars: List[str] = []
    markers: List[Dict[str, Any]] = []
    offset = 0
    for m in _MARKER_RE.finditer(script):
        s = _parse_time_str(m.group(1))
        e = _parse_time_str(m.group(2))
        if e - s > max_ost:
            e = s + max_ost  # MAX_OST_DURATION 钳制（原版实测行为）
        markers.append({
            "start": round(s, 3), "end": round(e, 3),
            "char_position": m.start(), "preceding_text": script[offset:m.start()].strip(),
        })
        clean_chars.append(script[offset:m.start()])
        offset = m.end()
    clean_chars.append(script[offset:])
    return "".join(clean_chars), markers


def fix_overlapping_clips(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只修解说词（非 OST）片段重叠：后段 start 顺延到前段 end；时长<0.5s 跳过；
    顺手加 LAY_INDEX（r2 §2.6 实测规则）。"""
    out: List[Dict[str, Any]] = []
    prev_end = 0.0
    for it in items:
        start = float(it.get("start", 0.0))
        end = float(it.get("end", start))
        is_ost = bool(it.get("ost", False)) or it.get("type") == "ost"
        if not is_ost:
            if start < prev_end:
                start = round(prev_end, 3)
                it["start"] = start
            if end - start < 0.5:
                continue
        prev_end = max(prev_end, end)
        it["LAY_INDEX"] = len(out)
        out.append(it)
    return out


def insert_ost_to_split_cam(split_cam: List[Dict[str, Any]], ost_markers: List[Dict[str, Any]],
                            video_path: str = "") -> List[Dict[str, Any]]:
    """按 char_position 排序插入 OST 条目到 split_cam（含 LAY_INDEX 重排）。"""
    if not ost_markers:
        return fix_overlapping_clips(split_cam)
    ost_items: List[Dict[str, Any]] = []
    for m in sorted(ost_markers, key=lambda x: x.get("char_position", 0)):
        ost_items.append({
            "start": m["start"], "end": m["end"],
            "text": "[原声原片片段]", "type": "ost", "ost": True,
            "char_position": m.get("char_position", 0),
            "preceding_text": m.get("preceding_text", ""),
            "source": os.path.basename(video_path) if video_path else "",
        })
    merged = split_cam + ost_items
    # r2 语义：OST 按 char_position（文案顺序）插入；解说段按时间序保持
    merged.sort(key=lambda x: (0, x.get("start", 0.0)) if not x.get("ost")
                else (1, x.get("char_position", 0), x.get("start", 0.0)))
    return fix_overlapping_clips(merged)


def build_timeline(narrations, matches, scenes, chars_per_second=DEFAULT_CHARS_PER_SECOND,
                   video_path: str = "") -> List[Dict[str, Any]]:
    """解说时间线生成：MatchResult → TimelineItem{clip 区间} → split_cam。"""
    scenes_by_id = {int(s.get("id", i)): s for i, s in enumerate(scenes)}
    items: List[Dict[str, Any]] = []
    for m in sorted(matches, key=lambda x: x.get("narration_index", 0)):
        ni = m["narration_index"]
        text = next((n["text"] for n in narrations if int(n.get("index", -1)) == ni), "")
        tts = m.get("tts_duration") or estimate_tts_duration(text, chars_per_second)
        ids = m.get("scene_ids") or []
        clips = []
        total = 0.0
        for sid in ids:
            sc = scenes_by_id.get(int(sid))
            if sc is None:
                continue
            c = {"scene_id": int(sid), "start": float(sc["start"]), "end": float(sc["end"]),
                 "text": str(sc.get("text") or "")}
            clips.append(c)
            total += max(0.0, float(sc["end"]) - float(sc["start"]))
        items.append({
            "narration_index": ni, "text": text, "tts_duration": tts,
            "clips": clips, "total_clip_duration": round(total, 3),
            "action": "montage" if total > MONTAGE_THRESHOLD else "single",
            "reason": m.get("reason", ""),
        })
    split_cam = []
    t0 = 0.0
    for it in items:
        tts = it["tts_duration"]
        split_cam.append({"text": it["text"], "start": round(t0, 3),
                          "end": round(t0 + tts, 3), "tts_duration": tts,
                          "narration_index": it["narration_index"],
                          "clips": it["clips"], "action": it["action"],
                          "reason": it["reason"], "type": "narration", "ost": False})
        t0 += tts
    return fix_overlapping_clips(split_cam)


# ---------------------------------------------------------------- 节点

class GeminiMatcherNode(NodeBase):
    """两阶段 Gemini 语义匹配。type_id: core/gemini_matcher"""

    @classmethod
    def spec(cls):
        return NodeSpec(
            type_id="core/gemini_matcher",
            title="Gemini 语义匹配(B2)",
            category="语义",
            description="B2 专用两阶段：场景描述+解说词→LLM 语义匹配→OST 插入（独立/串联）；无 key 本地兜底",
            inputs=[
                PortSpec("descriptions", PortType.JSON, label="场景描述", required=False),
                PortSpec("script", PortType.SCRIPT, label="解说词", required=True),
            ],
            outputs=[
                PortSpec("matches", PortType.JSON, label="语义匹配", required=True),
                PortSpec("timeline", PortType.TIMELINE, label="时间线", required=True),
                PortSpec("split_cam", PortType.JSON, label="分镜(含OST)", required=True),
                PortSpec("report", PortType.JSON, label="执行报告", required=True),
            ],
            params=[
                PortSpec("stage", PortType.STRING, label="执行阶段", default="all",
                         widget="select", options=["all", "match", "ost"],
                         description="all=两阶段串联；match=仅语义匹配；ost=仅 OST 插入"),
                PortSpec("api_key", PortType.STRING, label="API Key", required=False, default="",
                         widget="password", description="支持 @secret:名称 引用保险箱；空则本地兜底"),
                PortSpec("base_url", PortType.STRING, label="API 地址", required=False,
                         default=DEFAULT_BASE_URL, widget="text"),
                PortSpec("model", PortType.STRING, label="模型", default=DEFAULT_MODEL, widget="text"),
                PortSpec("llm_backend", PortType.STRING, label="LLM 后端", default="llm_call",
                         widget="select", options=["llm_call", "internal"],
                         description="llm_call=统一实现（退避/回退）；internal=原版内嵌对照"),
                PortSpec("batch_size", PortType.INT, label="批大小(段)", default=DEFAULT_BATCH_SIZE,
                         widget="number"),
                PortSpec("cps", PortType.FLOAT, label="语速(字/秒)", default=DEFAULT_CHARS_PER_SECOND,
                         widget="number"),
                PortSpec("ost_max", PortType.FLOAT, label="OST 最长(秒)", default=MAX_OST_DURATION,
                         widget="number"),
                PortSpec("temperature", PortType.FLOAT, label="温度", default=DEFAULT_TEMPERATURE,
                         widget="number"),
                PortSpec("max_tokens", PortType.INT, label="最大 Token", default=4096, widget="number"),
                PortSpec("retries", PortType.INT, label="重试次数", default=MAX_RETRIES, widget="number"),
                PortSpec("on_llm_fail", PortType.STRING, label="LLM 失败策略", default="warn",
                         widget="select", options=["warn", "error"],
                         description="warn=本地兜底继续（llm_error 记录）；error=抛错"),
                PortSpec("video_path", PortType.STRING, label="原片路径", required=False,
                         default="", widget="text", description="可选，写入 split_cam.source"),
            ],
            version="1.0.0",
        )

    def _read_input(self, ctx: dict, asset) -> Any:
        if asset is None:
            return None
        try:
            return ctx["store"].read_json(asset.id)
        except Exception:  # noqa: BLE001
            return None

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        t0 = time.time()

        stage = str(params.get("stage", "all") or "all").strip().lower()
        api_key = _resolve_secret(ctx, str(params.get("api_key", "") or ""))
        base_url = str(params.get("base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).strip()
        model = str(params.get("model", DEFAULT_MODEL) or DEFAULT_MODEL).strip()
        backend = str(params.get("llm_backend", "llm_call") or "llm_call").strip().lower()
        try:
            batch_size = max(1, int(params.get("batch_size", DEFAULT_BATCH_SIZE)))
        except (TypeError, ValueError):
            batch_size = DEFAULT_BATCH_SIZE
        try:
            cps = float(params.get("cps", DEFAULT_CHARS_PER_SECOND))
        except (TypeError, ValueError):
            cps = DEFAULT_CHARS_PER_SECOND
        try:
            ost_max = float(params.get("ost_max", MAX_OST_DURATION))
        except (TypeError, ValueError):
            ost_max = MAX_OST_DURATION
        try:
            temperature = float(params.get("temperature", DEFAULT_TEMPERATURE))
        except (TypeError, ValueError):
            temperature = DEFAULT_TEMPERATURE
        try:
            max_tokens = max(64, int(params.get("max_tokens", 4096)))
        except (TypeError, ValueError):
            max_tokens = 4096
        try:
            retries = max(0, int(params.get("retries", MAX_RETRIES)))
        except (TypeError, ValueError):
            retries = MAX_RETRIES
        on_fail = str(params.get("on_llm_fail", "warn") or "warn")
        video_path = str(params.get("video_path", "") or "").strip()

        script_payload = self._read_input(ctx, inputs.get("script"))
        script = ""
        if isinstance(script_payload, dict):
            script = str(script_payload.get("text") or script_payload.get("script") or "")
        elif isinstance(script_payload, str):
            script = script_payload
        if not script.strip():
            raise RuntimeError("contract: 需要解说词输入（script）")

        descriptions_payload = self._read_input(ctx, inputs.get("descriptions"))
        scenes = load_scenes(descriptions_payload)
        narrations, raw_script = split_script(script)

        warnings: List[str] = []
        if not scenes:
            warnings.append("场景描述为空或无法解析；匹配将退化为顺序时间线")

        progress(0.1)
        matches: List[Dict[str, Any]] = []
        match_meta: Dict[str, Any] = {}
        timeline: List[Dict[str, Any]] = []

        if stage in ("all", "match"):
            matches, match_meta = llm_semantic_match(
                narrations, scenes, batch_size=batch_size, temperature=temperature,
                max_tokens=max_tokens, backend=backend, api_key=api_key,
                base_url=base_url, model=model, retries=retries,
                on_llm_fail=on_fail, log=lambda s: None)
            if match_meta.get("llm_error"):
                warnings.append(match_meta["llm_error"])
            warnings += match_meta.get("warnings", [])
            if not matches:
                matches = local_match(narrations, scenes, chars_per_second=cps)
                match_meta["match_method"] = "local"
            timeline = build_timeline(narrations, matches, scenes,
                                      chars_per_second=cps, video_path=video_path)
            progress(0.6)

        split_cam = list(timeline)
        if stage in ("all", "ost"):
            _clean, ost_markers = parse_fixed_ost_timelines(script, max_ost=ost_max)
            if stage == "ost" and not timeline:
                # 独立 OST 阶段：无匹配时间线时，以脚本行为基础生成原生时间线
                local = local_match(narrations, scenes, chars_per_second=cps)
                timeline = build_timeline(narrations, local, scenes,
                                          chars_per_second=cps, video_path=video_path)
                split_cam = list(timeline)
            split_cam = insert_ost_to_split_cam(split_cam, ost_markers, video_path)
            if ost_markers:
                warnings.append(f"已插入 {len(ost_markers)} 条 OST（最长钳制 {ost_max:g}s）")
            progress(0.9)

        # 解说词文本随分镜注入（后续 TTS/字幕可直接消费）
        for it in split_cam:
            if it.get("type") == "narration":
                ni = it.get("narration_index")
                if ni is not None and it.get("text") in ("", None):
                    it["text"] = next((n["text"] for n in narrations
                                       if int(n.get("index", -1)) == int(ni)), "")

        matches_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(matches), "method": match_meta.get("match_method", "local")}),
            payload=matches)
        timeline_asset = store.save_asset(
            Asset(id="", kind=PortType.TIMELINE.value,
                  meta={"count": len(timeline), "ost": sum(1 for i in split_cam if i.get("ost"))}),
            payload=timeline)
        split_cam_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(split_cam),
                        "ost": sum(1 for i in split_cam if i.get("ost")),
                        "method": match_meta.get("match_method", "local")}),
            payload=split_cam)
        report = {
            "ok": True,
            "stage": stage,
            "match_method": match_meta.get("match_method", "local"),
            "llm_calls": match_meta.get("llm_calls", 0),
            "llm_error": match_meta.get("llm_error", ""),
            "llm_backend": backend,
            "provider": "openai-compatible",
            "base_url": base_url, "model": model,
            "narrations": len(narrations), "scenes": len(scenes),
            "matches": len(matches), "ost_inserted": sum(1 for i in split_cam if i.get("ost")),
            "cps": cps, "ost_max": ost_max, "batch_size": batch_size,
            "warnings": warnings,
            "finished_at": time.time(),
            "elapsed_s": round(time.time() - t0, 2),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"stage": stage}), payload=report)
        progress(1.0)
        return {"matches": matches_asset, "timeline": timeline_asset,
                "split_cam": split_cam_asset, "report": report_asset}


def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")