# -*- coding: utf-8 -*-
"""多语翻译·批量节点（user/jsm_translate_batch）。

能力（对应解说猫 narration_translator + translation_worker 语义，独立重写并修复缺陷）：
- 语言表：显示名/英文名/语言码双向映射（原版 get_language_code 英文名反查返回 None）。
- 批量翻译：先整批一次调用（expected_count=len）；解析失败/条数不符 → 降级单条
  逐条翻译；单条失败保留原文并标记 failed（原版日志实证此降级链，本实现显式输出）。
- 标记保留：[pause]/[emphasis] 占位符翻译前替换、翻译后回填（原版 prompt 要求保留）。
- 长度容差：译文/原文字符长度比超出 ±length_tolerance 记 warning（不吞错）。
- 稳定 item_id：输入 id 或 index 派生，与 batch envelope 对齐。
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

# (显示名, 英文名, 语言码)
LANGS = [
    ("中文", "Chinese", "zh"),
    ("英语", "English", "en"),
    ("日语", "Japanese", "ja"),
    ("韩语", "Korean", "ko"),
    ("泰语", "Thai", "th"),
    ("印尼语", "Indonesian", "id"),
    ("越南语", "Vietnamese", "vi"),
    ("马来语", "Malay", "ms"),
    ("西班牙语", "Spanish", "es"),
    ("葡萄牙语", "Portuguese", "pt"),
    ("俄语", "Russian", "ru"),
    ("法语", "French", "fr"),
    ("德语", "German", "de"),
]
_DISPLAY_TO_CODE = {d: c for d, _e, c in LANGS}
_NAME_TO_CODE = {e.lower(): c for _d, e, c in LANGS}
_CODE_TO_DISPLAY = {c: d for d, _e, c in LANGS}

_MARKER_RE = re.compile(r"(\[pause\]|\[emphasis\])", re.I)


def resolve_language(value: str) -> Tuple[str, str]:
    """显示名/英文名/语言码 → (display, code)；未知显式报错。"""
    v = str(value or "").strip()
    if v in _DISPLAY_TO_CODE:
        return v, _DISPLAY_TO_CODE[v]
    if v.lower() in _NAME_TO_CODE:
        code = _NAME_TO_CODE[v.lower()]
        return _CODE_TO_DISPLAY[code], code
    if v.lower() in _CODE_TO_DISPLAY:
        return _CODE_TO_DISPLAY[v.lower()], v.lower()
    raise ValueError(f"未知语言: {v!r}（支持: {', '.join(_DISPLAY_TO_CODE)}）")


def _llm_json(messages, api_key, base_url, model, timeout: float = 180.0) -> str:
    import urllib.error
    import urllib.request
    url = str(base_url).rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 8192}
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


def build_translation_prompt(texts: List[str], lang_display: str, code: str,
                             keep_style: bool) -> List[Dict[str, Any]]:
    guideline = (
        "Use natural, conversational language suitable for video narration. "
        if code != "zh" else "使用自然口语化的视频解说语气。"
    )
    system = (
        f"You are a professional video narration translator. Translate Chinese video "
        f"narration texts into {lang_display} ({code}). {guideline}"
        "Preserve [pause] and [emphasis] markers exactly in place. "
        "Keep meaning accurate; length should stay within 20% of the original. "
        "Output numbered lines exactly: each line '[index] translated text'. "
        "No explanations."
    )
    user = "\n".join(f"[{i}] {t}" for i, t in enumerate(texts))
    if keep_style:
        system += " Keep the spoken style and rhythm; avoid literal translation."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_numbered_output(raw: str, expected: int) -> Dict[int, str]:
    """解析 '[i] 译文' 逐行输出 → {index: text}；格式不符抛错。"""
    out: Dict[int, str] = {}
    for line in raw.splitlines():
        m = re.match(r"^\s*\[(\d+)\]\s*(.*)$", line, re.S)
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    if not out or len(out) < expected:
        raise ValueError(f"输出条数不符（期望{expected}，解析{len(out)}）")
    return out


def _len_ratio(src: str, dst: str) -> float:
    """长度比（CJK 按字符，其余按词近似；统一按字符数）。"""
    return (len(dst) + 1.0) / (len(src) + 1.0)


def _translate_batch_one(texts: List[str], lang_display: str, code: str,
                         keep_style: bool, api_key: str, base_url: str,
                         model: str, length_tolerance: float) -> Tuple[List[Dict[str, Any]], str]:
    """先整批，失败降级单条；返回 (结果列表, mode)。"""
    results: List[Dict[str, Any]] = []
    # 占位标记保护
    srcs = []
    markers: List[Dict[str, str]] = []
    for t in texts:
        found = []
        def _sub(m):
            found.append(m.group(0))
            return f"__M{len(found)}__"
        srcs.append(_MARKER_RE.sub(_sub, t))
        markers.append(found)

    mode = "batch"
    translated: List[str] = []
    try:
        raw = _llm_json(build_translation_prompt(srcs, lang_display, code, keep_style),
                        api_key, base_url, model)
        parsed = _parse_numbered_output(raw, len(srcs))
        translated = [parsed.get(i, "") for i in range(len(srcs))]
        if any(not s for s in translated):
            raise ValueError("存在空译文")
    except Exception as e:  # noqa: BLE001
        mode = "fallback_single"
        for i, t in enumerate(srcs):
            try:
                raw1 = _llm_json(build_translation_prompt([t], lang_display, code, keep_style),
                                 api_key, base_url, model)
                parsed1 = _parse_numbered_output(raw1, 1)
                translated.append(parsed1.get(0, ""))
            except Exception:  # noqa: BLE001
                translated.append("")

    for i, (src, tgt) in enumerate(zip(texts, translated)):
        # 回填标记
        if markers[i]:
            for k, m in enumerate(markers[i]):
                tgt = tgt.replace(f"__M{k + 1}__", m)
        ratio = _len_ratio(src, tgt)
        warning = ""
        if abs(ratio - 1.0) > length_tolerance:
            warning = f"长度比 {ratio:.2f} 超出容差 ±{length_tolerance:.0%}"
        status = "succeeded" if tgt.strip() else "failed"
        results.append({
            "item_id": f"t{i}",
            "text": src,
            "translated": tgt,
            "status": status,
            "length_ratio": round(ratio, 3),
            "warning": warning,
        })
    return results, mode


class TranslateBatchNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_translate_batch",
            title="多语翻译·批量",
            category="语义",
            description="文案/字幕多语翻译：整批→单条降级、标记保留、长度容差",
            inputs=[
                PortSpec(name="texts", type=PortType.JSON, label="待翻译文本", required=False),
                PortSpec(name="subtitles", type=PortType.SUBTITLE, label="字幕", required=False),
                PortSpec(name="items", type=PortType.JSON, label="批量条目", required=False),
            ],
            outputs=[
                PortSpec(name="translations", type=PortType.JSON, label="翻译结果"),
                PortSpec(name="translated_subtitles", type=PortType.SUBTITLE, label="翻译字幕",
                         required=False),
                PortSpec(name="report", type=PortType.JSON, label="翻译报告"),
                PortSpec(name="results", type=PortType.JSON, label="批量结果"),
            ],
            params=[
                PortSpec(name="target_language", type=PortType.STRING, label="目标语言",
                         default="英语", widget="select",
                         options=[d for d, _e, _c in LANGS]),
                PortSpec(name="api_key", type=PortType.STRING, label="API Key",
                         default="", widget="password", required=False,
                         description="可填 @secret:名称 引用保险箱"),
                PortSpec(name="base_url", type=PortType.STRING, label="中转地址",
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec(name="model", type=PortType.STRING, label="模型",
                         default="gpt-4o-mini", widget="text"),
                PortSpec(name="keep_style", type=PortType.BOOL, label="保留解说风格",
                         default=True, widget="toggle"),
                PortSpec(name="length_tolerance", type=PortType.FLOAT, label="长度容差",
                         default=0.2, widget="number"),
                PortSpec(name="max_concurrent", type=PortType.INT, label="批量并发",
                         default=2, widget="number"),
                PortSpec(name="retries", type=PortType.INT, label="重试次数",
                         default=2, widget="number"),
                PortSpec(name="resume", type=PortType.BOOL, label="断点续跑",
                         default=True, widget="toggle"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        texts_asset = inputs.get("texts")
        subs_asset = inputs.get("subtitles")
        items_asset = inputs.get("items")
        store = ctx["store"]

        lang_display, code = resolve_language(params.get("target_language", "英语"))
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
        if not api_key:
            raise RuntimeError("请填写 API Key，或在密钥保险箱存好后用 @secret:名称 引用")
        base_url = str(params.get("base_url", "https://ameaaos.com/v1")).strip() or "https://ameaaos.com/v1"
        model = str(params.get("model", "gpt-4o-mini")).strip() or "gpt-4o-mini"
        keep_style = bool(params.get("keep_style", True))
        length_tolerance = max(0.0, float(params.get("length_tolerance", 0.2) or 0.2))
        concurrency = max(1, int(params.get("max_concurrent", 2) or 2))
        retries = max(0, int(params.get("retries", 2) or 0))
        resume = bool(params.get("resume", True))

        def _read(asset):
            if asset is None:
                return None
            try:
                return store.read_json(asset.id)
            except Exception:  # noqa: BLE001
                return None

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
                        raw_items.append({"texts": it})
            elif isinstance(data, list):
                raw_items = [x if isinstance(x, dict) else {"texts": x} for x in data]
        if texts_asset is not None or subs_asset is not None or not raw_items:
            payload: Dict[str, Any] = {}
            tx = _read(texts_asset)
            if isinstance(tx, list):
                payload["texts"] = tx
            subs = _read(subs_asset)
            if isinstance(subs, list):
                payload["subtitles"] = subs
            if payload or not raw_items:
                raw_items.append(payload)

        if not raw_items:
            raise RuntimeError("缺少输入：请连接待翻译文本、字幕或批量条目")

        cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                          str(ctx.get("node_id", "translate_batch")) + ".json")

        async def worker(item: Dict[str, Any]) -> Dict[str, Any]:
            payload = item.get("input")
            if not isinstance(payload, dict):
                raise RuntimeError("条目输入必须是对象")
            import asyncio
            return await asyncio.to_thread(
                _process_payload, payload, lang_display, code, keep_style,
                api_key, base_url, model, length_tolerance)

        if batchlib is not None:
            batch = batchlib.new_batch(raw_items, source="user/jsm_translate_batch",
                                       options={"lang": code})
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

        trans_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"lang": code, "count": len(first.get("translations", []))}),
            payload=first.get("translations", []))
        out_sub = first.get("translated_subtitles")
        sub_asset = None
        if out_sub is not None:
            sub_asset = store.save_asset(
                Asset(id="", kind=PortType.SUBTITLE.value, meta={"lang": code, "count": len(out_sub)}),
                payload=out_sub)
        report_data = {
            "language": lang_display, "code": code,
            "mode": first.get("mode", "batch"),
            "count": len(first.get("translations", [])),
            "succeeded": sum(1 for r in first.get("translations", []) if r.get("status") == "succeeded"),
            "failed": sum(1 for r in first.get("translations", []) if r.get("status") == "failed"),
            "length_warnings": sum(1 for r in first.get("translations", []) if r.get("warning")),
            "retries": retries,
        }
        report_asset = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload=report_data)
        results_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(batch["items"]), "ok": sum(1 for i in batch["items"] if i.get("status") == "succeeded")}),
            payload={"batch_id": batch.get("batch_id"), "items": batch["items"]})
        return {"translations": trans_asset, "translated_subtitles": sub_asset,
                "report": report_asset, "results": results_asset}


def _process_payload(payload: Dict[str, Any], lang_display: str, code: str,
                     keep_style: bool, api_key: str, base_url: str, model: str,
                     length_tolerance: float) -> Dict[str, Any]:
    """单条载荷：{texts:[...]} 或 {subtitles:[...]}。"""
    texts: List[str] = []
    if isinstance(payload.get("texts"), list):
        for t in payload["texts"]:
            if isinstance(t, dict):
                texts.append(str(t.get("text", "") or ""))
            else:
                texts.append(str(t))
    if isinstance(payload.get("subtitles"), list):
        subs = payload["subtitles"]
        texts = [str(s.get("text", "") or "") for s in subs if isinstance(s, dict)]
    texts = [t for t in texts if t.strip()]
    if not texts:
        raise ValueError("待翻译文本为空")
    results, mode = _translate_batch_one(texts, lang_display, code, keep_style,
                                         api_key, base_url, model, length_tolerance)
    out_sub = None
    if isinstance(payload.get("subtitles"), list):
        subs = payload["subtitles"]
        out_sub = []
        for i, s in enumerate(subs):
            ns = dict(s)
            if i < len(results):
                ns["text"] = results[i]["translated"] or ns.get("text", "")
                ns["lang"] = code
                ns["translation_status"] = results[i]["status"]
            out_sub.append(ns)
    return {"translations": results, "mode": mode, "translated_subtitles": out_sub}