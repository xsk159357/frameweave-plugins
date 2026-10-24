# -*- coding: utf-8 -*-
"""核心插件：SSE 流读取器（core/sse_reader，E5，对齐解说猫 utils/_stream_reader 实测语义）。

把参考实现的 _iter_sse_payloads / consume_openai_sse / consume_google_sse 节点化：
- 输入 endpoint + headers + payload（JSON），流式读取 SSE 事件；
- OpenAI 格式：obj["choices"][0]["delta"]["content"]；
- Google 格式：obj["candidates"][0]["content"]["parts"][0]["text"]；
- format=auto 按 payload 是否含 choices/candidates 自动识别两种格式；
- 0.2s 节流（可参数化 throttle_seconds）：on_push(total_chars, delta)/progress
  每次触发间隔 ≥ 节流值（参考时间窗语义：now - last_cb_time >= 0.2）；
- 坏 payload（JSONDecodeError/KeyError/IndexError/TypeError）跳过继续流，不中断；
- [DONE] 终止；回调异常吞掉不影响主循环（对齐参考）。

中断/重试语义（明确）：
- 中断：run 为可取消（to_thread + CancelledError 传播，平台取消/超时走引擎回收）；
  流中途 ctx.cancel_event 置位 → interrupted=true，返回已收部分；
- 重试：仅在「尚未收到任何内容」的连接/读取级失败时重试 retries 次（退避 1.5s×attempt）；
  400/401/403/404 永久失败不重试；已收到部分内容后的中断/错误不重试
  （返回部分文本 + warning，对齐参考「读到哪算哪、坏数据不回滚」）。
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

DONE_MARKER = "[DONE]"
DATA_PREFIX = b"data:"
PERMANENT_STATUS = {400, 401, 403, 404}


def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")


def iter_sse_payloads(lines: Any) -> Any:
    """逐行过滤 data: 前缀 → 生成 payload 字符串（参考 _iter_sse_payloads 语义）。"""
    for line in lines:
        if not line:
            continue
        if isinstance(line, str):
            line = line.encode("utf-8", "replace")
        line = line.strip()
        if not line.startswith(DATA_PREFIX):
            continue
        yield line[5:].lstrip().decode("utf-8", "replace")


def _extract_delta(obj: Any, fmt: str) -> str:
    """按格式从事件对象提取增量文本；格式非法/字段缺失返回 ""。"""
    if not isinstance(obj, dict):
        return ""
    if fmt == "openai":
        return str(obj.get("choices", [{}])[0].get("delta", {}).get("content", "") or "")
    if fmt == "google":
        return str(obj.get("candidates", [{}])[0].get("content", {})
                   .get("parts", [{}])[0].get("text", "") or "")
    # auto：按响应形状识别
    if "choices" in obj:
        return str(obj.get("choices", [{}])[0].get("delta", {}).get("content", "") or "")
    if "candidates" in obj:
        return str(obj.get("candidates", [{}])[0].get("content", {})
                   .get("parts", [{}])[0].get("text", "") or "")
    return ""


def detect_format(obj: Any, fmt: str) -> str:
    """确定实际使用的解析格式（report 用）。"""
    if fmt != "auto" or not isinstance(obj, dict):
        return fmt
    if "choices" in obj:
        return "openai"
    if "candidates" in obj:
        return "google"
    return "unknown"


def consume_sse(stream_lines: Any, *, fmt: str, throttle_seconds: float,
                on_push: Optional[Callable[[int, str], None]] = None,
                should_stop: Optional[Callable[[], bool]] = None,
                trace: Optional[List[Dict[str, Any]]] = None,
                warnings: Optional[List[str]] = None) -> Tuple[str, str, bool]:
    """消费 SSE 行流 → (text, format_used, done)。

    对齐参考 consume_openai_sse/consume_google_sse：
    - 节流：距上次回调 >= throttle_seconds 才触发 on_push(total_chars, delta)；
    - 坏 payload 跳过（记 warning）；[DONE] 终止；should_stop() 为真时提前中断。
    """
    content_parts: List[str] = []
    total_chars = 0
    last_cb = 0.0
    done = False
    format_used = fmt
    for payload in iter_sse_payloads(stream_lines):
        if payload.strip() == DONE_MARKER:
            done = True
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            if warnings is not None:
                warnings.append(f"坏 payload 已跳过（非 JSON）: {payload[:60]!r}")
            continue
        # auto：逐 payload 按自身形状识别（混流也逐块正确解析）
        fmt_now = detect_format(obj, fmt)
        if fmt_now != "unknown":
            format_used = fmt_now
        delta = _extract_delta(obj, fmt_now)
        if not delta:
            continue
        content_parts.append(delta)
        total_chars += len(delta)
        if trace is not None:
            trace.append({"delta": delta, "total_chars": total_chars,
                          "ts": round(time.time(), 3)})
        if on_push is not None:
            now = time.monotonic()
            if now - last_cb >= max(throttle_seconds, 0.0):
                last_cb = now
                try:
                    on_push(total_chars, delta)
                except Exception as e:  # noqa: BLE001（回调异常吞掉，对齐参考）
                    if warnings is not None:
                        warnings.append(f"推送回调异常已忽略: {type(e).__name__}: {e}")
        if should_stop is not None and should_stop():
            break
    return "".join(content_parts), format_used, done


class SseReaderNode(NodeBase):
    """SSE 流读取器。type_id: core/sse_reader"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/sse_reader",
            title="SSE 流读取",
            category="输入",
            description="读取 OpenAI/Google 两种 SSE 流：text/事件/报告输出；0.2s 节流；可重试、可中断",
            inputs=[],
            outputs=[
                PortSpec("text", PortType.STRING, label="聚合文本", required=True),
                PortSpec("events", PortType.JSON, label="事件流", required=False),
                PortSpec("report", PortType.JSON, label="读取报告", required=False),
            ],
            params=[
                PortSpec("endpoint", PortType.STRING, label="请求地址", required=True, default="", widget="text"),
                PortSpec("method", PortType.STRING, label="方法", default="POST",
                         widget="select", options=["POST", "GET"]),
                PortSpec("payload", PortType.STRING, label="请求体(JSON)", required=False,
                         default="", widget="text", description="POST 请求体；留空发送空体"),
                PortSpec("headers", PortType.STRING, label="请求头(JSON)", required=False,
                         default="", widget="text", description='例 {"Content-Type": "application/json"}'),
                PortSpec("api_key", PortType.STRING, label="API Key", required=False, default="",
                         widget="password", description="支持 @secret:名称；自动加 Authorization: Bearer"),
                PortSpec("format", PortType.STRING, label="SSE 格式", default="auto",
                         widget="select", options=["auto", "openai", "google"]),
                PortSpec("throttle_seconds", PortType.FLOAT, label="节流间隔(秒)", default=0.2, widget="number"),
                PortSpec("stream_timeout", PortType.INT, label="流超时(秒)", default=120, widget="number"),
                PortSpec("retries", PortType.INT, label="重试次数", default=2, widget="number",
                         description="仅未收到任何内容时的连接级重试（部分内容不重试）"),
                PortSpec("retry_delay", PortType.FLOAT, label="重试间隔(秒)", default=1.5, widget="number"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        import asyncio
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        push = ctx.get("stream_push")            # 可选自定义推送（total_chars, delta）
        cancel_event = ctx.get("cancel_event")

        endpoint = str(params.get("endpoint", "") or "").strip()
        if not endpoint:
            raise RuntimeError("invalid: 请填写请求地址 endpoint")
        method = str(params.get("method", "POST") or "POST").upper()
        fmt = str(params.get("format", "auto") or "auto").strip().lower()
        try:
            throttle = max(0.0, float(params.get("throttle_seconds", 0.2) or 0.2))
        except (TypeError, ValueError):
            throttle = 0.2
        stream_timeout = max(5, int(params.get("stream_timeout", 120) or 120))
        retries = max(0, int(params.get("retries", 2) or 2))
        retry_delay = max(0.0, float(params.get("retry_delay", 1.5) or 1.5))

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        raw_headers = str(params.get("headers", "") or "").strip()
        if raw_headers:
            try:
                hd = json.loads(raw_headers)
                if isinstance(hd, dict):
                    headers.update({str(k): str(v) for k, v in hd.items()})
            except json.JSONDecodeError:
                raise RuntimeError(f"invalid: headers 不是合法 JSON 对象: {raw_headers[:120]}")
        api_key = _resolve_secret(ctx, str(params.get("api_key", "") or ""))
        if api_key and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = "Bearer " + api_key

        raw_payload = str(params.get("payload", "") or "")
        body: Optional[bytes] = None
        if method == "POST" and raw_payload.strip():
            try:
                json.loads(raw_payload)
            except json.JSONDecodeError:
                raise RuntimeError(f"invalid: payload 不是合法 JSON: {raw_payload[:120]}")
            body = raw_payload.encode("utf-8")

        warnings: List[str] = []
        trace: List[Dict[str, Any]] = []
        text = ""
        format_used = fmt
        done = False
        interrupted = False
        partial = False
        attempts = 0
        last_err = ""

        def _should_stop() -> bool:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                return True
            return False

        def _push(total_chars: int, delta: str) -> None:
            # 平台进度（0..1 启发式：按已收字符数渐进到 0.95）
            try:
                progress(min(0.95, 0.05 + total_chars / 4000.0))
            except Exception:  # noqa: BLE001
                pass
            if push is not None:
                try:
                    push(total_chars, delta)
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"stream_push 回调异常已忽略: {e}")

        for attempt in range(max(1, retries + 1)):
            attempts = attempt + 1
            if attempt > 0:
                await asyncio.sleep(retry_delay * attempt)
            req = urllib.request.Request(endpoint, data=body, method=method,
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=stream_timeout) as resp:
                    def _read_lines():
                        while True:
                            line = resp.readline()
                            if not line:
                                break
                            yield line

                    def _consume() -> Tuple[str, str, bool]:
                        return consume_sse(_read_lines(), fmt=fmt,
                                           throttle_seconds=throttle,
                                           on_push=_push, should_stop=_should_stop,
                                           trace=trace, warnings=warnings)

                    text, format_used, done = await asyncio.to_thread(_consume)
                break  # 拿到完整/部分流即成功（部分也在 break 后按 partial 标记）
            except urllib.error.HTTPError as e:
                if e.code in PERMANENT_STATUS:
                    raise RuntimeError(f"sse: HTTP {e.code} 永久失败，不重试: "
                                       f"{e.read(200).decode('utf-8', 'replace')[:200]}") from e
                last_err = f"HTTP {e.code}"
                if text:  # 已收到部分内容 → 不重试
                    partial = True
                    break
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
                last_err = f"{type(e).__name__}: {e}"
                if text:  # 已收到部分内容 → 不重试
                    partial = True
                    break
            except asyncio.CancelledError:
                interrupted = True
                raise
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                if text:
                    partial = True
                    break

        if not text and last_err:
            raise RuntimeError(f"sse: 读取失败（重试 {attempts - 1} 次）: {last_err}")
        if _should_stop() and not done:
            interrupted = True
        if interrupted and not done:
            warnings.append("流被取消/中断，返回已接收部分内容")
        if text and not done and not interrupted:
            # 流未以 [DONE] 正常终止（EOF 截断/服务端提前断开）→ 明确标记 partial，不重试
            partial = True
            warnings.append("流未以 [DONE] 正常终止（EOF），返回已接收部分内容（不重试）")
        if partial:
            warnings.append(f"流中途断开（{last_err}），返回已接收部分内容（不重试）")

        text_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value,
                  meta={"chars": len(text), "done": done, "partial": partial}),
            payload={"text": text})
        events_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"count": len(trace)}), payload=trace)
        report = {
            "ok": True,
            "endpoint": endpoint, "method": method,
            "format": format_used, "requested_format": fmt,
            "chars": len(text), "done": done, "partial": partial,
            "interrupted": interrupted, "retries": max(attempts - 1, 0),
            "throttle_seconds": throttle,
            "events": len(trace),
            "skipped_payloads": len([w for w in warnings if w.startswith("坏 payload")]),
            "warnings": warnings,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"format": format_used}), payload=report)
        try:
            progress(1.0)
        except Exception:  # noqa: BLE001
            pass
        return {"text": text_asset, "events": events_asset, "report": report_asset}