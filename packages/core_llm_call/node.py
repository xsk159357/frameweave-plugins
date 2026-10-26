# -*- coding: utf-8 -*-
"""核心插件：统一 LLM 调用（core/llm_call，D6）。

把平台既有 app.llm.chat 语义升级为「契约级统一 LLM 基础节点 + 模块级同步函数」：
- prompt/messages + model → 响应文本（OpenAI 兼容 / Gemini 双协议）；
- 统一超时与退避：默认 backoff=[2,5,10] 秒（S1 §9 稳定性要求），可参数化；
- fallback_models：主模型重试耗尽后按序回退备用模型（JSON 数组参数）；
- JSON mode：openai response_format=json_object；gemini responseMimeType=application/json；
- schema 校验：可选 JSON Schema 子集（type/required/properties 递归）；
  解析/校验失败视为可重试 → 同模型重试，重试耗尽 → 回退模型，全部失败显式报错；
- R1 规避：URL 一律 `base_url.rstrip("/") + "/chat/completions"`（不重复拼 /v1）；
- R2 规避：402 等按 HTTP 状态显式报错（附 body 摘要），不做"余额不足"猜测；
- 4xx 永久错误（400/401/403/404）不重试不回退，直接显式报错。

复用方式（既有 LLM 节点接入）：
- 节点内跨包复用走注册表 get_class("core/llm_call")().run(ctx, inputs, params)（打包安全）；
- 本模块同时导出同步函数 llm_call_json(...) 供同包/同进程直接复用（单实现）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

DEFAULT_BACKOFF = [2.0, 5.0, 10.0]   # S1 §9：统一退避序列（秒）
RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}
PERMANENT_CODES = {400, 401, 403, 404}


class LLMCallError(RuntimeError):
    """带稳定错误码的 LLM 异常（code ∈ http|network|timeout|schema|parse|empty）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")


# ---------------------------------------------------------------- 双协议单次调用（R1 规避：直拼路径）

def chat_openai(messages: List[Dict[str, Any]], api_key: str, base_url: str, model: str,
                temperature: float, max_tokens: int, json_mode: bool,
                timeout: float) -> Tuple[str, Dict[str, int]]:
    """OpenAI 兼容 chat/completions 单次调用。URL 直拼 /chat/completions（R1）。"""
    payload: Dict[str, Any] = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    url = str(base_url).rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        text = str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as e:
        raise LLMCallError("parse", "openai: 响应解析失败: "
                                     + json.dumps(data, ensure_ascii=False)[:300]) from e
    usage = data.get("usage") or {}
    return text, {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                  "completion_tokens": int(usage.get("completion_tokens") or 0)}


def chat_gemini(messages: List[Dict[str, Any]], api_key: str, base_url: str, model: str,
                temperature: float, max_tokens: int, json_mode: bool,
                timeout: float) -> Tuple[str, Dict[str, int]]:
    """Gemini generateContent 单次调用（URL 拼接与既有节点一致，R1 规避）。"""
    parts: List[Dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append({"text": content})
        elif isinstance(content, list):
            for c in content:
                if c.get("type") == "text":
                    parts.append({"text": c.get("text", "")})
    gen: Dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        gen["responseMimeType"] = "application/json"
    payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url.rstrip('/')}/models/{model}:generateContent{sep}key={api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError) as e:
        raise LLMCallError("parse", "gemini: 响应解析失败: "
                                     + json.dumps(data, ensure_ascii=False)[:300]) from e
    um = data.get("usageMetadata") or {}
    return text, {"prompt_tokens": int(um.get("promptTokenCount") or 0),
                  "completion_tokens": int(um.get("candidatesTokenCount") or 0)}


def _single_call(messages, provider, api_key, base_url, model, temperature, max_tokens,
                 json_mode, timeout) -> Tuple[str, Dict[str, int]]:
    if provider == "gemini":
        return chat_gemini(messages, api_key, base_url, model, temperature, max_tokens,
                           json_mode, timeout)
    return chat_openai(messages, api_key, base_url, model, temperature, max_tokens,
                       json_mode, timeout)


# ---------------------------------------------------------------- schema 校验（JSON Schema 子集）

def validate_against_schema(value: Any, schema: Any, path: str = "$") -> List[str]:
    """JSON Schema 子集校验：type / required / properties（递归）。返回错误列表（空=通过）。"""
    errs: List[str] = []
    if not isinstance(schema, dict):
        return errs
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(value, dict):
            return [f"{path}: 期望 object，实际 {type(value).__name__}"]
        for req in schema.get("required") or []:
            if req not in value:
                errs.append(f"{path}: 缺少必填字段 {req!r}")
        props = schema.get("properties") or {}
        for k, sub in props.items():
            if k in value and isinstance(sub, dict):
                errs.extend(validate_against_schema(value[k], sub, f"{path}.{k}"))
    elif stype == "array":
        if not isinstance(value, list):
            return [f"{path}: 期望 array，实际 {type(value).__name__}"]
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(value):
                errs.extend(validate_against_schema(v, items, f"{path}[{i}]"))
    elif stype in ("string", "number", "integer", "boolean"):
        ok = (stype == "string" and isinstance(value, str)) \
            or (stype == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)) \
            or (stype == "integer" and isinstance(value, int) and not isinstance(value, bool)) \
            or (stype == "boolean" and isinstance(value, bool))
        if not ok:
            errs.append(f"{path}: 期望 {stype}，实际 {type(value).__name__} ({value!r})")
    return errs


# ---------------------------------------------------------------- 统一主入口（单实现）

def _strip_code_fence(text: str) -> str:
    """去掉 markdown ```json / ``` 围栏（厂商 OpenAI 兼容层可能不严格遵循 response_format）。"""
    s = text.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl >= 0:
            s = s[first_nl + 1:]      # 跳过 ```json 语言标记行
        else:
            s = s.lstrip("`")
    if s.endswith("```"):
        s = s[: -3]
    return s.strip()


def _balanced_extract_all(text: str):
    """枚举所有顶层平衡块（{…} 或 […]），返回块文本列表（含边界）。"""
    blocks = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch not in "{[":
            i += 1
            continue
        opener = ch
        closer = "}" if opener == "{" else "]"
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == opener:
                    depth += 1
                elif c == closer:
                    depth -= 1
                    if depth == 0:
                        blocks.append(text[i: j + 1])
                        i = j + 1
                        break
            j += 1
        else:
            i += 1
    return blocks


def _extract_json_robust(text: str) -> Any:
    """容错 JSON 提取：直接 loads → 去围栏 → 围栏内容 → 顶层平衡块（从尾向前逐个尝试）→ None。"""
    direct_cands = [text, _strip_code_fence(text)]
    for cand in direct_cands:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    stripped = _strip_code_fence(text)
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # 围栏内容（```json 块）优先
    if "```" in text:
        parts = text.split("```")
        for seg in reversed(parts[1::2]):  # 奇数段为围栏内内容，取最后一段
            seg = seg.strip()
            nl = seg.find("\n")
            if nl >= 0 and not seg[0] in "[{":
                seg = seg[nl + 1:].strip()   # 去掉 ```json 语言标记行
            try:
                return json.loads(seg)
            except json.JSONDecodeError:
                continue
    # 顶层平衡块从尾向前逐个尝试（模型常把 JSON 放在回答末尾）
    for block in reversed(_balanced_extract_all(text)):
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue
    return None


def llm_call_json(messages: List[Dict[str, Any]], *, provider: str = "openai",
                  api_key: str, base_url: str, model: str,
                  fallback_models: Optional[List[str]] = None,
                  temperature: float = 0.4, max_tokens: int = 4096,
                  json_mode: bool = False, schema: Optional[Any] = None,
                  timeout: float = 120.0,
                  backoff: Optional[List[float]] = None,
                  log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """统一 LLM 调用：退避 [2,5,10] + fallback_models + JSON mode + schema 校验。

    返回 {"text", "parsed", "model_used", "attempts", "backoffs", "schema_valid",
          "schema_errors", "usage", "warnings"}；全部失败抛 LLMCallError(code, msg)。
    """
    # ---- LLM 通道配置化（captain 2026-10）：显式参数 > 全局配置 > 中立默认（不绑定厂商）----
    _gcfg: dict = {}
    if ((not api_key) or api_key == "@global"
            or (not str(base_url or "").strip()) or (not str(model or "").strip())):
        try:
            from app.llm_config import resolve_global as _rg
            _gcfg = _rg() or {}
        except Exception:  # noqa: BLE001
            _gcfg = {}
    if (not api_key) or api_key == "@global":
        api_key = str(_gcfg.get("api_key") or "") if _gcfg else ""
    if not api_key:
        raise LLMCallError("auth", "缺少 api_key（可填 @secret:名称 引用保险箱，或配置全局 LLM 通道 /api/settings/llm）")
    if not str(model or "").strip():
        model = str(_gcfg.get("model") or "") if _gcfg else ""
    if not str(model or "").strip():
        raise LLMCallError("invalid", "model 不能为空（请在节点参数或全局 LLM 配置中指定模型）")
    models = [model] + [str(m).strip() for m in (fallback_models or [])
                        if str(m).strip() and str(m).strip() != model]
    delays = [max(0.0, float(d)) for d in (backoff or DEFAULT_BACKOFF)]
    if not delays:
        # 参数化退避为空/非法时回退统一序列（S1 §9），避免 0 次尝试直接失败
        delays = list(DEFAULT_BACKOFF)
    provider = "gemini" if str(provider).lower() == "gemini" else "openai"
    if not str(base_url or "").strip():
        base_url = (str(_gcfg.get("base_url") or "") if _gcfg else "").strip() or (
            "https://generativelanguage.googleapis.com/v1beta"
            if provider == "gemini" else "https://api.openai.com/v1")
    last_err = ""
    warnings: List[str] = []
    attempts = 0
    backoffs_applied: List[float] = []
    usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}

    for mi, mdl in enumerate(models):
        for ai, delay in enumerate(delays):
            attempts += 1
            try:
                text, u = _single_call(messages, provider, api_key, base_url, mdl,
                                       temperature, max_tokens, json_mode, timeout)
                usage["prompt_tokens"] += u.get("prompt_tokens", 0)
                usage["completion_tokens"] += u.get("completion_tokens", 0)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                # R2 规避：402 等显式报错（附 body），不做"余额不足"猜测
                body = raw[:300]
                if e.code in PERMANENT_CODES:
                    raise LLMCallError("http", f"HTTP {e.code}（永久失败）: {body}")
                if e.code == 402:
                    raise LLMCallError("http", f"HTTP 402: {body}")
                last_err = f"HTTP {e.code}: {body}"
                if ai < len(delays) - 1:
                    backoffs_applied.append(delay)
                    time.sleep(delay)
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = f"network: {type(e).__name__}: {e}"
                if ai < len(delays) - 1:
                    backoffs_applied.append(delay)
                    time.sleep(delay)
                continue
            except LLMCallError as e:
                last_err = str(e)
                if ai < len(delays) - 1:
                    backoffs_applied.append(delay)
                    time.sleep(delay)
                continue

            # 收到文本 → JSON 解析 / schema 校验（失败视为可重试）
            parsed: Any = None
            schema_errors: List[str] = []
            if json_mode or schema is not None:
                parsed = _extract_json_robust(text)
                if parsed is None:
                    schema_errors.append(f"JSON 解析失败: {text[:160]!r}")
            if schema is not None and not schema_errors:
                schema_errors = validate_against_schema(parsed, schema)
            if schema_errors:
                last_err = "schema: " + "; ".join(schema_errors[:3])
                if log:
                    log(f"[llm_call] {mdl} schema 校验失败，重试: {last_err}")
                if ai < len(delays) - 1:
                    backoffs_applied.append(delay)
                    time.sleep(delay)
                continue
            if mi > 0:
                warnings.append(f"主模型 {model} 失败，已回退到 {mdl}")
            return {"text": text, "parsed": parsed, "model_used": mdl,
                    "attempts": attempts, "backoffs": backoffs_applied,
                    "schema_valid": not schema_errors, "schema_errors": schema_errors,
                    "usage": usage, "warnings": warnings}
    # 全部模型/重试耗尽：按最后一次错误归类为 LLMCallError（先 HTTP 后网络再 parse）
    prefix = str(last_err).split(":")[0] or "unknown"
    if prefix.startswith("HTTP") or prefix.isdigit():
        code = "http"
    elif prefix in ("network", "timeout"):
        code = "network"
    elif prefix == "schema":
        code = "schema"
    else:
        code = "unknown"
    raise LLMCallError(code, f"llm: 全部模型/重试失败（{len(models)} 模型 × {len(delays)} 次；"
                             f"attempts={attempts}）: {last_err or '未知错误'}")


# ---------------------------------------------------------------- 节点

class LLMCallNode(NodeBase):
    """统一 LLM 调用。type_id: core/llm_call"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/llm_call",
            title="统一 LLM 调用",
            category="语义",
            description="prompt+model→响应；统一超时/退避[2,5,10]/fallback_models/JSON mode/schema 校验；OpenAI/Gemini 双协议",
            inputs=[
                PortSpec("prompt", PortType.STRING, label="提示词", required=False),
                PortSpec("messages", PortType.JSON, label="完整消息列表", required=False),
                PortSpec("system", PortType.STRING, label="系统提示", required=False),
            ],
            outputs=[
                PortSpec("text", PortType.STRING, label="响应文本", required=True),
                PortSpec("response", PortType.JSON, label="结构化响应", required=False),
                PortSpec("report", PortType.JSON, label="调用报告", required=False),
            ],
            params=[
                PortSpec("provider", PortType.STRING, label="Provider", default="auto",
                         widget="select", options=["auto", "openai", "gemini"]),
                PortSpec("api_key", PortType.STRING, label="API Key", required=False, default="",
                         widget="password", description="支持 @secret:名称 引用保险箱"),
                PortSpec("base_url", PortType.STRING, label="API 地址", required=False, default="", widget="text"),
                PortSpec("model", PortType.STRING, label="模型", default="", widget="text"),
                PortSpec("fallback_models", PortType.STRING, label="备用模型(JSON 数组)", required=False,
                         default="", widget="text", description='例 ["", "gemini-2.0-flash"]'),
                PortSpec("temperature", PortType.FLOAT, label="温度", default=0.4, widget="number"),
                PortSpec("max_tokens", PortType.INT, label="最大输出 Token", default=4096, widget="number"),
                PortSpec("json_mode", PortType.BOOL, label="JSON 输出", default=False, widget="toggle"),
                PortSpec("schema", PortType.STRING, label="输出 Schema(JSON)", required=False,
                         default="", widget="text", description='例 {"type":"object","required":["ok"],"properties":{"ok":{"type":"boolean"}}}'),
                PortSpec("timeout", PortType.INT, label="单次超时(秒)", default=120, widget="number"),
                PortSpec("backoff", PortType.STRING, label="退避序列(秒)", required=False,
                         default="[2,5,10]", widget="text", description="JSON 数组；默认 [2,5,10]"),
            ],
            version="1.1.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        import asyncio
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)

        def _read_json_asset(asset: Any) -> Any:
            """读 JSON 资产：优先 path 直读，id 读法回退。

            why（C 链 c-final3 实测根因）：worker 内跨节点直调时，上游 save_asset
            落盘的新资产 id 恒为空串（WorkerStoreProxy 语义），read_json('') 走
            input_index/library 双双落空 → messages 丢空 → 「需要 prompt/messages/
            system」假错。path（out/aN.json）在 worker/宿主两侧都真实可读。
            """
            if asset is None:
                return None
            p = getattr(asset, "path", "") or ""
            if p and os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return json.load(f)
                except (OSError, ValueError):
                    pass
            aid = getattr(asset, "id", "") or ""
            if aid:
                try:
                    return store.read_json(aid)
                except Exception:  # noqa: BLE001
                    return None
            return None

        messages: List[Dict[str, Any]] = []
        sys_asset = inputs.get("system")
        if sys_asset is not None:
            try:
                p = _read_json_asset(sys_asset)
                sys_txt = str(p.get("text", "") or "") if isinstance(p, dict) else str(p or "")
            except Exception:  # noqa: BLE001
                sys_txt = ""
            if sys_txt.strip():
                messages.append({"role": "system", "content": sys_txt})
        msgs_asset = inputs.get("messages")
        if msgs_asset is not None:
            try:
                data = _read_json_asset(msgs_asset)
            except Exception:  # noqa: BLE001
                data = None
            if isinstance(data, list):
                messages = [dict(m) for m in data if isinstance(m, dict)]
        prompt_asset = inputs.get("prompt")
        prompt_txt = ""
        if prompt_asset is not None:
            try:
                p = _read_json_asset(prompt_asset)
                prompt_txt = str(p.get("text", "") or "") if isinstance(p, dict) else str(p or "")
            except Exception:  # noqa: BLE001
                prompt_txt = ""
        if prompt_txt.strip():
            messages.append({"role": "user", "content": prompt_txt})
        if not messages:
            raise RuntimeError("invalid: 需要 prompt / messages / system 至少一项输入")

        provider = str(params.get("provider", "auto") or "auto").strip().lower()
        base_url = str(params.get("base_url", "") or "").strip()
        if provider == "auto":
            provider = "gemini" if "generativelanguage" in base_url else "openai"
        model = str(params.get("model", "") or "").strip() or ""
        _fb = str(params.get("fallback_models", "") or "").strip()
        fallback_models: List[str] = []
        if _fb:
            try:
                _fb_data = json.loads(_fb)
            except json.JSONDecodeError:
                raise RuntimeError(f"invalid: fallback_models 不是 JSON 数组: {_fb[:120]}")
            if not isinstance(_fb_data, list):
                raise RuntimeError(f"invalid: fallback_models 不是 JSON 数组: {_fb[:120]}")
            fallback_models = [str(x) for x in _fb_data]
        temperature = float(params.get("temperature", 0.4) or 0.4)
        max_tokens = max(16, int(params.get("max_tokens", 4096) or 4096))
        json_mode = bool(params.get("json_mode", False))
        schema_raw = str(params.get("schema", "") or "").strip()
        schema = None
        if schema_raw:
            try:
                schema = json.loads(schema_raw)
            except json.JSONDecodeError:
                raise RuntimeError(f"invalid: schema 不是合法 JSON: {schema_raw[:120]}")
        timeout = max(5, int(params.get("timeout", 120) or 120))
        _bo = str(params.get("backoff", "") or "").strip() or "[2,5,10]"
        try:
            _bo_data = json.loads(_bo)
            backoff = ([float(x) for x in _bo_data]
                       if isinstance(_bo_data, list) and _bo_data
                       else list(DEFAULT_BACKOFF))
        except json.JSONDecodeError:
            backoff = list(DEFAULT_BACKOFF)

        api_key = _resolve_secret(ctx, str(params.get("api_key", "") or ""))
        # LLM 通道配置化：空 api_key/model/base_url 允许落到 llm_call_json 的全局配置回退，
        # 由 /api/settings/llm 提供默认；全部缺省时 llm_call_json 给出明确错误。

        progress(0.15)
        try:
            result = await asyncio.to_thread(
                llm_call_json, messages,
                provider=provider, api_key=api_key, base_url=base_url, model=model,
                fallback_models=fallback_models, temperature=temperature,
                max_tokens=max_tokens, json_mode=json_mode, schema=schema,
                timeout=float(timeout), backoff=backoff,
                log=lambda s: None)
        except LLMCallError as e:
            raise RuntimeError(f"[{e.code}] {e}") from e
        progress(0.9)

        text = str(result.get("text") or "")
        text_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value,
                  meta={"chars": len(text), "model": result.get("model_used")}),
            payload={"text": text})
        resp_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"model": result.get("model_used"), "json": result.get("parsed") is not None}),
            payload=result)
        report = {
            "ok": True,
            "provider": provider, "model": result.get("model_used"),
            "attempts": result.get("attempts"), "backoffs": result.get("backoffs"),
            "schema_valid": result.get("schema_valid"),
            "schema_errors": result.get("schema_errors") or [],
            "usage": result.get("usage"),
            "warnings": result.get("warnings") or [],
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"model": result.get("model_used")}),
            payload=report)
        progress(1.0)
        return {"text": text_asset, "response": resp_asset, "report": report_asset}