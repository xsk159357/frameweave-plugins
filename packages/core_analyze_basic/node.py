"""核心插件：普通模式视频分析（A9，video_pipeline 普通模式节点化）。

与 core/analyze_chunks（A4，分块语义）区分：
- A9 无分块概念：整片采样 frame_batch 张中间帧（默认 15，避开首尾 5% 边缘），
  按 frames_per_group（默认 5）分组，每组一次简化视觉分析调用，并发 ≤5。
- 每组 → 一个 description_item {index, time_start, time_end, shot_type,
  visual_description, audio_content}；输出端口名 description_items。
- 契约参数化：批 15（frame_batch）/ 并发 5（concurrency）语义均为参数，
  run 报告逐项记录生效值。
- 包自包含：视觉调用/抽帧/解析/校验纯函数就地实现（不与 core_analyze_chunks
  互相依赖，独立安装可用）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

SHOT_TYPES: set = {
    "wide", "medium", "closeup", "action", "dialogue", "transition", "other",
}
EDGE_SKIP = 0.05  # 中间帧语义：跳过首尾 5% 边缘
_BACKOFF = 1.5  # 重试退避基数（秒）：_BACKOFF * (attempt+1)；测试可调小


def _num(v: Any, dflt: Optional[float]) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _extract_json(text: str) -> Any:
    """从 LLM 输出提取 JSON（支持 markdown 围栏与首个 { 块）。失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    i = t.find("{")
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(t)):
        if t[j] == "{":
            depth += 1
        elif t[j] == "}":
            depth -= 1
            if depth == 0:
                chunk = t[i:j + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    break
    return None


def est_image_tokens(width: int, height: int) -> int:
    w = max(16, int(width or 16))
    h = max(16, int(height or 16))
    tiles = max(1, math.ceil(w / 512)) * max(1, math.ceil(h / 512))
    return 85 + 170 * tiles


def est_text_tokens(text: str) -> int:
    return max(1, len(text or ""))


def check_desc_item(it: Any) -> Tuple[bool, str]:
    """契约校验：shot_type 枚举 + visual_description/audio_content 非空缺省。"""
    if not isinstance(it, dict):
        return False, "条目非对象"
    st = str(it.get("shot_type") or "").strip().lower()
    if st not in SHOT_TYPES:
        return False, f"shot_type 非法/缺失: {st!r}（枚举: {sorted(SHOT_TYPES)}）"
    vd = it.get("visual_description")
    if not isinstance(vd, str) or not vd.strip():
        return False, "visual_description 缺失或为空"
    if not isinstance(it.get("audio_content"), str):
        return False, "audio_content 缺失或非字符串"
    return True, ""


def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")


def _resolve_provider(base_url: str, provider: str) -> str:
    if provider == "auto":
        return "gemini" if "generativelanguage" in base_url else "openai"
    return provider


def _default_base_url(provider: str) -> str:
    return ("https://generativelanguage.googleapis.com/v1beta"
            if provider == "gemini" else "https://api.openai.com/v1")


def _chat_gemini_vision(images_b64: List[str], prompt: str, api_key: str, base_url: str,
                        model: str, temperature: float, max_tokens: int) -> str:
    import urllib.request
    parts: List[Dict[str, Any]] = [{"text": prompt}]
    for b64 in images_b64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url.rstrip('/')}/models/{model}:generateContent{sep}key={api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("gemini: 响应解析失败: " + json.dumps(data, ensure_ascii=False)[:300]) from e


def _chat_openai_vision(images_b64: List[str], prompt: str, api_key: str, base_url: str,
                        model: str, temperature: float, max_tokens: int) -> str:
    import urllib.request
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for b64 in images_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature, "max_tokens": max_tokens,
    }
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("openai: 响应解析失败: " + json.dumps(data, ensure_ascii=False)[:300]) from e


async def _extract_frames(ctx: dict, video_path: str, times: List[float],
                          out_dir: str, scale: int) -> List[str]:
    """按时间点逐帧抽取（子进程托管 run_tracked，回退 to_thread；失败帧静默跳过）。"""
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    ff = r["path"] if r.get("ready") else ""
    if not ff:
        return []
    os.makedirs(out_dir, exist_ok=True)
    paths: List[str] = []
    rt = ctx.get("run_tracked")
    for i, t in enumerate(times):
        out = os.path.join(out_dir, f"f_{i:03d}_{int(t * 1000)}.jpg")
        cmd = [ff, "-v", "error", "-ss", f"{t:.3f}", "-i", video_path,
               "-frames:v", "1", "-vf", f"scale={int(scale)}:-1", "-y", out]
        ok = False
        try:
            if rt is not None:
                import subprocess as _sp
                try:
                    proc = await rt(cmd, timeout=min(30.0, 15.0), text=True,
                                    stdout=_sp.PIPE, stderr=_sp.PIPE)
                    ok = proc.returncode == 0
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                import subprocess as _sp2
                proc = await asyncio.to_thread(
                    lambda: _sp2.run(cmd, capture_output=True, timeout=30))
                ok = proc.returncode == 0
        except Exception:  # noqa: BLE001
            ok = False
        if ok and os.path.isfile(out):
            paths.append(out)
    return paths


class AnalyzeBasicNode(NodeBase):
    """普通模式视频分析（A9）。type_id: core/analyze_basic"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/analyze_basic",
            title="普通模式视频分析（A9）",
            category="分析",
            description="video_pipeline 普通模式：中间帧批 15 图/并发 5 简化视觉分析 → description_items（无分块语义）",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=True),
            ],
            outputs=[
                PortSpec("description_items", PortType.JSON, label="描述条目", required=True),
                PortSpec("report", PortType.JSON, label="分析报告", required=False),
            ],
            params=[
                PortSpec("provider", PortType.STRING, label="Provider", default="auto",
                         widget="select", options=["auto", "openai", "gemini"]),
                PortSpec("api_key", PortType.STRING, label="API Key", required=False, default="",
                         widget="password", description="支持 @secret:名称 引用保险箱"),
                PortSpec("base_url", PortType.STRING, label="API 地址", required=False, default="", widget="text"),
                PortSpec("model", PortType.STRING, label="视觉模型", default="gemini-1.5-flash", widget="text"),
                PortSpec("frame_batch", PortType.INT, label="中间帧批数", default=15, widget="number"),
                PortSpec("frames_per_group", PortType.INT, label="每组帧数", default=5, widget="number"),
                PortSpec("concurrency", PortType.INT, label="并发数", default=5, widget="number"),
                PortSpec("retries", PortType.INT, label="重试次数", default=2, widget="number"),
                PortSpec("frame_scale", PortType.INT, label="帧宽(px)", default=448, widget="number"),
                PortSpec("temperature", PortType.FLOAT, label="温度", default=0.2, widget="number"),
                PortSpec("max_tokens", PortType.INT, label="最大 Token", default=1024, widget="number"),
            ],
            version="1.0.0",
        )

    async def _analyze_group(self, ctx: dict, group_idx: int, group_times: List[float],
                             video_path: str, out_dir: str, t_start: float, t_end: float,
                             params: Dict[str, Any], provider: str, api_key: str,
                             base_url: str, model: str, sem: asyncio.Semaphore) -> Dict[str, Any]:
        async with sem:
            frames = await _extract_frames(ctx, video_path, group_times, out_dir, params["frame_scale"])
            images_b64: List[str] = []
            for fp in frames:
                try:
                    with open(fp, "rb") as f:
                        images_b64.append(base64.b64encode(f.read()).decode("ascii"))
                except OSError:
                    continue
            prompt = (
                f"这是一段视频中间帧的 {len(images_b64)} 张采样画面（约 {t_start:.1f}s-{t_end:.1f}s）。\n"
                "请做简化视觉分析：用一两句话概括画面（场景/主体/动作），并判断镜头类型与能推断的"
                "声音线索。只输出 JSON：\n"
                '{"shot_type": "wide|medium|closeup|action|dialogue|transition|other 之一",\n'
                ' "visual_description": "一段中文画面概括",\n'
                ' "audio_content": "能推断的听觉内容，无法判断填空字符串"}'
            )
            warnings: List[str] = []
            last_err = "无画面帧可用" if not images_b64 else ""
            item: Dict[str, Any] = {}
            calls = 0
            if images_b64:
                for attempt in range(max(1, int(params["retries"]) + 1)):
                    calls += 1
                    try:
                        if provider == "gemini":
                            raw = await asyncio.to_thread(
                                _chat_gemini_vision, images_b64, prompt, api_key, base_url,
                                model, params["temperature"], params["max_tokens"])
                        else:
                            raw = await asyncio.to_thread(
                                _chat_openai_vision, images_b64, prompt, api_key, base_url,
                                model, params["temperature"], params["max_tokens"])
                        data = _extract_json(raw)
                        valid = False
                        if data is not None:
                            valid, err = check_desc_item(data)
                            if valid:
                                item = data
                                break
                            last_err = err
                        else:
                            last_err = "JSON 解析失败"
                    except Exception as e:  # noqa: BLE001
                        last_err = str(e)
                    if attempt < max(1, int(params["retries"])):
                        await asyncio.sleep(_BACKOFF * (attempt + 1))
            if not item:
                warnings.append(f"帧组 {group_idx} 分析失败（{last_err}，尝试 {calls} 次），输出占位描述")
                item = {
                    "shot_type": "other",
                    "visual_description": "【简化分析失败：画面不可用】",
                    "audio_content": "",
                }
            return {
                "item": {
                    "index": group_idx,
                    "time_start": round(t_start, 3),
                    "time_end": round(t_end, 3),
                    "shot_type": str(item.get("shot_type") or "other").strip().lower(),
                    "visual_description": str(item.get("visual_description") or "").strip(),
                    "audio_content": str(item.get("audio_content") or "").strip(),
                },
                "warnings": warnings,
                "frames": len(images_b64),
                "calls": calls,
            }

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        if video_asset is None or not getattr(video_asset, "path", ""):
            raise RuntimeError("invalid: 需要视频输入")
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)

        provider = str(params.get("provider", "auto") or "auto")
        api_key_raw = str(params.get("api_key", "") or "").strip()
        base_url = str(params.get("base_url", "") or "").strip()
        model = str(params.get("model", "gemini-1.5-flash") or "gemini-1.5-flash").strip() or "gemini-1.5-flash"
        frame_batch = max(1, int(params.get("frame_batch", 15) or 15))
        frames_per_group = max(1, int(params.get("frames_per_group", 5) or 5))
        concurrency = max(1, min(5, int(params.get("concurrency", 5) or 5)))  # 并发 5 语义
        retries = max(0, int(params.get("retries", 2) or 2))
        frame_scale = max(64, int(params.get("frame_scale", 448) or 448))
        temperature = max(0.0, float(params.get("temperature", 0.2) or 0.2))
        max_tokens = max(128, int(params.get("max_tokens", 1024) or 1024))

        api_key = _resolve_secret(ctx, api_key_raw)
        if not api_key:
            raise RuntimeError("contract: 请填写 API Key 或用 @secret:名称 引用保险箱")
        provider = _resolve_provider(base_url, provider)
        if not base_url:
            base_url = _default_base_url(provider)

        video_duration = 0.0
        vm = getattr(video_asset, "meta", None) or {}
        try:
            video_duration = float(vm.get("container", {}).get("duration") or vm.get("duration") or 0)
        except (TypeError, ValueError):
            video_duration = 0.0
        if not video_duration:
            # ffprobe 探测（子进程托管），失败则报错（无法建立中间帧时间轴）
            from app.plugin import deps
            r = deps.resolve_binary("ffprobe", env=os.environ)
            fp_ = r["path"] if r.get("ready") else ""
            rt = ctx.get("run_tracked")
            if fp_:
                import subprocess as _sp
                cmd = [fp_, "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", video_asset.path]
                try:
                    if rt is not None:
                        proc = await rt(cmd, timeout=15.0, text=True,
                                        stdout=_sp.PIPE, stderr=_sp.PIPE)
                        out = proc.stdout.strip() if proc.returncode == 0 else ""
                    else:
                        proc = await asyncio.to_thread(
                            lambda: _sp.run(cmd, capture_output=True, timeout=15))
                        out = proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""
                    if out:
                        video_duration = float(out)
                except Exception:  # noqa: BLE001
                    video_duration = 0.0
        if not video_duration:
            raise RuntimeError("invalid: 无法获取视频时长（meta 与 ffprobe 均失败）")

        # 中间帧语义：在 [5%, 95%] 区间均分采样 frame_batch 帧
        lo = video_duration * EDGE_SKIP
        hi = video_duration * (1 - EDGE_SKIP)
        span = hi - lo
        if frame_batch == 1:
            times = [video_duration / 2]
        else:
            times = [lo + (i + 1) * span / (frame_batch + 1) for i in range(frame_batch)]
        times = [max(0.0, min(t, max(0.0, video_duration - 1e-3))) for t in times]

        groups: List[List[float]] = [
            times[i:i + frames_per_group] for i in range(0, len(times), frames_per_group)
        ]
        out_dir = os.path.join(ctx.get("workdir", "."), "analyze_basic_frames")
        os.makedirs(out_dir, exist_ok=True)

        sem = asyncio.Semaphore(concurrency)
        pdef = {"retries": retries, "frame_scale": frame_scale,
                "temperature": temperature, "max_tokens": max_tokens}

        async def _one(g: int, gt: List[float]) -> Dict[str, Any]:
            t0 = gt[0]
            t1 = gt[-1]
            return await self._analyze_group(ctx, g, gt, video_asset.path, out_dir,
                                             t0, t1, pdef, provider, api_key, base_url, model, sem)

        progress(0.1)
        results = await asyncio.gather(*(_one(g, gt) for g, gt in enumerate(groups)))
        progress(0.9)

        items = [r["item"] for r in results]
        warnings: List[str] = []
        total_frames = 0
        total_calls = 0
        for r in results:
            warnings.extend(r["warnings"])
            total_frames += r["frames"]
            total_calls += r["calls"]

        payload = {
            "schema_version": 1,
            "tool": "core/analyze_basic",
            "video_duration": round(video_duration, 3),
            "params": {"frame_batch": frame_batch, "frames_per_group": frames_per_group,
                       "concurrency": concurrency},
            "summary": {"items": len(items), "frames_sampled": total_frames,
                        "vision_calls": total_calls},
            "description_items": items,
        }
        items_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(items), "tool": "core/analyze_basic"}),
            payload=payload)
        report = {
            "ok": True,
            "tool": "core/analyze_basic",
            "version": "1.0.0",
            "item_count": len(items),
            "params": payload["params"],
            "summary": payload["summary"],
            "tokens_estimate": {
                "image": sum(est_image_tokens(frame_scale, max(144, int(frame_scale * 9 / 16)))
                             for _ in range(total_frames)),
                "text": sum(est_text_tokens(i["visual_description"]) for i in items),
            },
            "warnings": warnings,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"items": len(items)}), payload=report)
        progress(1.0)
        return {"description_items": items_asset, "report": report_asset}