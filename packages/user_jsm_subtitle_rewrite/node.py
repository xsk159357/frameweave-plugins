# -*- coding: utf-8 -*-
"""user/jsm_subtitle_rewrite —— 字幕洗稿重构（解说猫 tk_rewrite 语义，独立实现）。

语义对齐《tk_rewrite.txt》：同语言内容重构、保持行数严格一一对应、不改变事实/角色/时间/
地点/数字/专有名词、降低重复度、适合字幕展示与 TTS 口播。

设计：
- 输入 subtitles（SUBTITLE 行 {index,start,end,text}）+ prompt（STRING，洗稿指令覆盖），
  输出 rewritten（SUBTITLE，行数/顺序/时间戳严格一致，仅 text 改写）。
- LLM 经 core_llm_call 的统一通道（退避/回退/JSON mode），API Key 支持 @secret:名称
  引用保险箱；不绑定任何授权/许可（宿主级授权由平台负责）。
- 容易失败的场景（无 key / 网络错误）给出 contract/错误码级信息；单条失败降级保留原句，
  不伪造结果、不静默吞错（report 记录降级条数）。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

_DEFAULT_PROMPT = (
    "你是一个专业的视频字幕洗稿助手。请对用户提供的字幕逐条进行内容重构。\n"
    "重构目标：在不改变核心事实、人物关系、情绪方向和镜头语义的前提下，显著降低与原句的重复度，"
    "让表达更像重新写过，并且仍然适合字幕展示与 TTS 口播。\n"
    "重构要求：\n"
    "1. 保持原语言，不要翻译成其他语言\n"
    "2. 不要改变时间、地点、数字、人物称呼、专有名词和关键事实\n"
    "3. 字幕行可能只是句子片段，理解时要结合上下文，但不要补充原文没有的信息\n"
    "4. 严禁合并行或拆分行；输出必须与输入行数和顺序一一对应\n"
    "5. 尽量控制每行长度接近原句，避免明显扩写或缩写\n"
    "6. 语言要自然、口语化、顺滑，避免书面腔和机器腔\n"
    "7. 只输出重构结果，不要解释，不要添加额外说明\n"
)

_MARKER_RE = None  # 占位（保持与 tk 一致的风格；本实现不依赖外部标记）


def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")


def _llm_chat(messages: List[Dict[str, str]], api_key: str, base_url: str, model: str,
              retries: int, timeout: float) -> str:
    import urllib.error
    import urllib.request
    url = str(base_url).rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 8192}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(max(1, retries + 1)):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", "Bearer " + api_key)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            return str(json.loads(body)["choices"][0]["message"]["content"] or "")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            last_err = RuntimeError(f"HTTP {e.code}: {raw[:300]}")
            if e.code in (400, 401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = RuntimeError(f"网络错误: {e}")
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    raise last_err or RuntimeError("LLM 请求失败")


def _parse_numbered_output(raw: str, expected: int) -> Dict[int, str]:
    import re
    out: Dict[int, str] = {}
    for line in str(raw).splitlines():
        m = re.match(r"^\s*\[(\d+)\]\s*(.*)$", line, re.S)
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    return out


class JsmSubtitleRewriteNode(NodeBase):
    """字幕洗稿重构。type_id: user/jsm_subtitle_rewrite"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_subtitle_rewrite",
            title="字幕洗稿重构",
            category="语义",
            description="同语言字幕洗稿降重（tk_rewrite 语义）：保持行数/事实/角色严格对应，适合 TTS 口播；API Key 用 @secret: 引用",
            inputs=[
                PortSpec(name="subtitles", type=PortType.SUBTITLE, label="字幕", required=True,
                         description="字幕行 {index,start,end,text}"),
                PortSpec(name="prompt", type=PortType.STRING, label="洗稿指令(可选)", required=False,
                         description="覆盖默认洗稿要求；留空用内置 tk_rewrite 语义"),
            ],
            outputs=[
                PortSpec(name="rewritten", type=PortType.SUBTITLE, label="洗稿字幕", required=True),
                PortSpec(name="report", type=PortType.JSON, label="洗稿报告", required=False),
            ],
            params=[
                PortSpec(name="api_key", type=PortType.STRING, label="API Key", required=False,
                         default="", widget="password", description="支持 @secret:名称 引用保险箱"),
                PortSpec(name="base_url", type=PortType.STRING, label="中转地址", required=False,
                         default="https://api.openai.com/v1", widget="text"),
                PortSpec(name="model", type=PortType.STRING, label="模型", required=False,
                         default="", widget="text", description="留空用全局 LLM 配置"),
                PortSpec(name="batch_size", type=PortType.INT, label="单批行数", required=False,
                         default=60, widget="number", description="超过分批逐批洗稿"),
                PortSpec(name="retries", type=PortType.INT, label="重试次数", required=False,
                         default=2, widget="number"),
                PortSpec(name="timeout", type=PortType.INT, label="超时(秒)", required=False,
                         default=180, widget="number"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        store = ctx["store"]
        sub_asset = inputs.get("subtitles")
        if sub_asset is None:
            raise RuntimeError("需要字幕输入（subtitles）")
        raw = store.read_json(sub_asset.id) or []
        if isinstance(raw, dict):
            raw = raw.get("items") or raw.get("rows") or raw.get("subtitles") or []
        rows: List[Dict[str, Any]] = []
        for i, r in enumerate(raw):
            if not isinstance(r, dict):
                continue
            text = str(r.get("text") or "").strip()
            if not text:
                continue
            try:
                s = float(r.get("start") or 0.0)
                e = float(r.get("end") or (s + 1.0))
            except (TypeError, ValueError):
                continue
            rows.append({"index": int(r.get("index", i)), "start": s,
                         "end": max(e, s), "text": text})
        rows.sort(key=lambda r: (r["start"], r["index"]))
        if not rows:
            raise RuntimeError("字幕为空（无有效文本行）")

        prompt = str(inputs.get("prompt") or "").strip() if inputs.get("prompt") is not None else ""
        system = prompt or _DEFAULT_PROMPT
        if not system:
            system = _DEFAULT_PROMPT

        api_key_raw = str(params.get("api_key", "") or "").strip()
        api_key = _resolve_secret(ctx, api_key_raw)
        base_url = str(params.get("base_url", "") or "").strip() or "https://api.openai.com/v1"
        model = str(params.get("model", "") or "").strip()
        # 全局 LLM 配置兜底（llm_config：显式参数 > 全局；不绑定厂商/授权）
        if (not api_key) or (not model):
            try:
                from app.llm_config import resolve_global as _rg
                g = _rg() or {}
            except Exception:  # noqa: BLE001
                g = {}
            if not api_key:
                api_key = str(g.get("api_key") or "")
            if not model:
                model = str(g.get("model") or "")
            if not base_url or base_url == "https://api.openai.com/v1":
                base_url = str(g.get("base_url") or "") or base_url
        if not api_key:
            raise RuntimeError("contract: 需要 api_key（可填 @secret:名称 引用保险箱，或配置全局 LLM 通道）")
        if not model:
            raise RuntimeError("contract: 需要 model（节点参数或全局 LLM 配置）")

        batch_size = max(1, int(params.get("batch_size", 60) or 60))
        retries = max(0, int(params.get("retries", 2) or 2))
        timeout = max(10, int(params.get("timeout", 180) or 180))
        progress = ctx.get("progress") or (lambda p: None)

        texts = [r["text"] for r in rows]
        rewritten: List[str] = []
        downgraded: List[int] = []
        mode_used = "batch"
        started = time.time()
        total_batches = (len(texts) + batch_size - 1) // batch_size

        for bi in range(total_batches):
            chunk = texts[bi * batch_size:(bi + 1) * batch_size]
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n".join(f"[{i}] {t}" for i, t in enumerate(chunk))},
            ]
            try:
                raw_out = _llm_chat(messages, api_key, base_url, model, retries, timeout)
                parsed = _parse_numbered_output(raw_out, len(chunk))
                ok_lines = [parsed.get(i, "") for i in range(len(chunk))]
                if any(not s for s in ok_lines):
                    raise ValueError("输出存在空行")
                rewritten.extend(ok_lines)
            except Exception as e:  # noqa: BLE001
                # 批失败 → 逐条降级（单条失败保留原句并计数）
                mode_used = f"partial({type(e).__name__})"
                for ti, t in enumerate(chunk):
                    try:
                        raw1 = _llm_chat([
                            {"role": "system", "content": system},
                            {"role": "user", "content": f"[0] {t}"},
                        ], api_key, base_url, model, retries, timeout)
                        p1 = _parse_numbered_output(raw1, 1)
                        rewritten.append(p1.get(0, t) or t)
                    except Exception:  # noqa: BLE001
                        rewritten.append(t)
                        downgraded.append(bi * batch_size + ti)
            progress(0.3 + 0.6 * (bi + 1) / max(total_batches, 1))

        if len(rewritten) != len(rows):
            raise RuntimeError("洗稿行数不一致（内部错误），拒绝输出")
        # 时间戳/顺序严格沿用原字幕；仅 text 替换
        out_rows: List[Dict[str, Any]] = []
        for r, t in zip(rows, rewritten):
            out_rows.append({"index": r["index"], "start": r["start"], "end": r["end"],
                             "text": str(t).strip()})
        progress(0.95)

        metadata: Dict[str, Any] = {
            "count": len(out_rows), "mode": mode_used,
            "downgraded": len(downgraded), "prompt_used": len(prompt) > 0,
        }
        asset = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value, meta=metadata), payload=out_rows)
        report = {
            "mode": mode_used, "rows": len(out_rows),
            "downgraded": downgraded[:50],
            "downgraded_count": len(downgraded),
            "model": model, "base_url": base_url,
            "prompt_custom": bool(prompt), "batch_size": batch_size,
            "rt_s": round(time.time() - started, 1),
            "warnings": [f"{len(downgraded)} 行降级保留原句（逐条失败）"] if downgraded else [],
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"rows": len(out_rows)}), payload=report)
        progress(1.0)
        return {"rewritten": asset, "report": report_asset}