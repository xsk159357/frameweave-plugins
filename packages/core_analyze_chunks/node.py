"""核心插件：分块视觉分析（A4，Gemini 分块分析节点化）。

对齐解说猫 A2 链路最省 Token 环节：chunks + video → descriptions JSON。
- 输入：core/smart_chunk 的分块列表（每块含 start/end/scene_indices/video_time_offset）。
- 处理：每块在块内均分采样 frames_per_chunk 帧（默认 3 帧），调用视觉 LLM
  （OpenAI 兼容 / Gemini 双协议，与 core/ai_narrate 同构）逐块分析。
- 输出：descriptions JSON（items 列表，字段对齐 ai_narrate scene_lines 消费契约：
  shot_type / visual_description / audio_content，index=块内首个场景序号，
  scene_indices=块内全部场景序号 → ai_narrate 可直读，零关键帧最省 Token）。
- 契约参数：并发 ≤5（semaphore 硬上限）、解析重试默认 3（JSON 解析失败 /
  schema 校验失败 / 网络失败重试，退避 1.5*(attempt+1)）、JSON schema 校验
  （shot_type ∈ 枚举且 visual_description/audio_content 非空缺省）。
- 报告内置省 Token 路径对比：description_path 估算 Token（文本）显著低于
  keyframe_path 估算 Token（8 关键帧 × 单帧视觉 Token），ratio ≥ 阈值时
  verdict=省Token显著。
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
KEYFRAME_REF = 8  # 路径对比参照：ai_narrate.keyframes 默认抽帧数
_BACKOFF = 1.5  # 重试退避基数（秒）：_BACKOFF * (attempt+1)；测试可调小

# ---------------------------------------------------------------- 纯函数

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
    """单帧视觉 Token 估算（OpenAI 口径：每 512² 平铺 ~170 token + 固定 85）。"""
    w = max(16, int(width or 16))
    h = max(16, int(height or 16))
    tiles = max(1, math.ceil(w / 512)) * max(1, math.ceil(h / 512))
    return 85 + 170 * tiles


def est_text_tokens(text: str) -> int:
    """文本 Token 估算：中文/通用按字符数近似。"""
    return max(1, len(text or ""))


def check_desc_item(it: Any) -> Tuple[bool, str]:
    """A4 契约 JSON schema 校验：shot_type 枚举 + visual_description/audio_content 非空缺省。"""
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


# ---------------------------------------------------------------- 视觉 LLM（双协议）

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


async def _vision(ctx: dict, images_b64: List[str], prompt: str, provider: str,
                  api_key: str, base_url: str, model: str, temperature: float,
                  max_tokens: int) -> str:
    """一次视觉调用（进程外 to_thread，双协议）。"""
    if provider == "gemini":
        return await asyncio.to_thread(
            _chat_gemini_vision, images_b64, prompt, api_key, base_url, model,
            temperature, max_tokens)
    return await asyncio.to_thread(
        _chat_openai_vision, images_b64, prompt, api_key, base_url, model,
        temperature, max_tokens)


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


def _resolve_provider(base_url: str, provider: str) -> str:
    if provider == "auto":
        return "gemini" if "generativelanguage" in base_url else "openai"
    return provider


def _default_base_url(provider: str) -> str:
    return ("https://generativelanguage.googleapis.com/v1beta"
            if provider == "gemini" else "https://api.openai.com/v1")


def _chunk_rows(chunks_payload: Any) -> List[Dict[str, Any]]:
    """chunks 输入归一：裸列表 / {chunks:[...]} 信封均支持，按 chunk_index 稳定排序。"""
    raw = chunks_payload
    if isinstance(raw, dict):
        raw = raw.get("chunks") or raw.get("items") or []
    out: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for i, c in enumerate(raw):
            if not isinstance(c, dict):
                continue
            out.append({
                "chunk_index": c.get("chunk_index", i),
                "start": _num(c.get("start"), 0.0) or 0.0,
                "end": _num(c.get("end"), None),
                "scene_indices": c.get("scene_indices") or [],
                "video_time_offset": _num(c.get("video_time_offset"), None),
            })
    out.sort(key=lambda c: c["chunk_index"])
    return out


class AnalyzeChunksNode(NodeBase):
    """分块视觉分析（A4）。type_id: core/analyze_chunks"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/analyze_chunks",
            title="分块视觉分析（A4）",
            category="分析",
            description="chunks + video → descriptions JSON（shot_type/visual_description/audio_content）；并发≤5、解析重试 3、JSON schema 校验；接入 ai_narrate 零关键帧省 Token 路径",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=True),
                PortSpec("chunks", PortType.JSON, label="分块结果", required=True),
            ],
            outputs=[
                PortSpec("descriptions", PortType.JSON, label="场景描述", required=True,
                         description="分块描述 JSON（items），可直接接入 core/ai_narrate descriptions 输入"),
                PortSpec("report", PortType.JSON, label="分析报告", required=False),
            ],
            params=[
                PortSpec("provider", PortType.STRING, label="Provider", default="auto",
                         widget="select", options=["auto", "openai", "gemini"]),
                PortSpec("api_key", PortType.STRING, label="API Key", required=False, default="",
                         widget="password", description="支持 @secret:名称 引用保险箱"),
                PortSpec("base_url", PortType.STRING, label="API 地址", required=False, default="", widget="text"),
                PortSpec("model", PortType.STRING, label="视觉模型", default="gemini-1.5-flash", widget="text"),
                PortSpec("concurrency", PortType.INT, label="并发上限", default=3, widget="number",
                         description="分块并行分析并发数（硬上限 5）"),
                PortSpec("retries", PortType.INT, label="解析重试次数", default=3, widget="number"),
                PortSpec("frames_per_chunk", PortType.INT, label="每块采样帧数", default=3, widget="number"),
                PortSpec("frame_scale", PortType.INT, label="帧宽(px)", default=448, widget="number"),
                PortSpec("temperature", PortType.FLOAT, label="温度", default=0.2, widget="number"),
                PortSpec("max_tokens", PortType.INT, label="最大 Token", default=1024, widget="number"),
                PortSpec("json_schema", PortType.BOOL, label="JSON schema 校验", default=True, widget="toggle"),
            ],
            version="1.0.0",
        )

    async def _analyze_one(self, ctx: dict, chunk: Dict[str, Any], idx: int, total: int,
                           video_path: str, out_dir: str, params: Dict[str, Any],
                           provider: str, api_key: str, base_url: str, model: str,
                           sem: asyncio.Semaphore) -> Dict[str, Any]:
        """单块分析（在并发信号量内）：抽帧 → 重试循环（解析/schema/网络失败重试）。"""
        async with sem:
            start = float(chunk["start"])
            end_ = chunk.get("end")
            duration = (float(end_) - start) if end_ is not None else 0.0
            if duration <= 0:
                duration = 0.0
            fps = max(1, int(params["frames_per_chunk"]))
            if duration > 0:
                times = [start + (i + 1) * duration / (fps + 1) for i in range(fps)]
            else:
                times = [start]
            frames = await _extract_frames(ctx, video_path, times, out_dir, params["frame_scale"])
            images_b64: List[str] = []
            for fp in frames:
                try:
                    with open(fp, "rb") as f:
                        images_b64.append(base64.b64encode(f.read()).decode("ascii"))
                except OSError:
                    continue

            prompt = (
                f"你是一名视频分镜分析师。这是视频第 {idx + 1}/{total} 段"
                f"（{start:.1f}s-{float(end_) if end_ is not None else start:.1f}s），"
                f"下面给出 {len(images_b64)} 帧采样画面。\n"
                "请综合这些画面输出该分段的 JSON 分析（只输出 JSON，不要解释）：\n"
                '{"shot_type": "wide|medium|closeup|action|dialogue|transition|other 之一",\n'
                ' "visual_description": "一段 40-80 字中文画面描述（人物/场景/动作/构图）",\n'
                ' "audio_content": "根据画面能推断的听觉内容（对话/音效/静音/音乐），无法判断填空字符串"}'
            )
            warnings: List[str] = []
            last_err = "无画面帧可用" if not images_b64 else ""
            item: Dict[str, Any] = {}
            calls = 0
            for attempt in range(max(1, int(params["retries"]) + 1)):
                calls += 1
                try:
                    raw = await _vision(ctx, images_b64, prompt, provider, api_key,
                                        base_url, model, params["temperature"],
                                        params["max_tokens"])
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
                warnings.append(
                    f"块 {chunk['chunk_index']} 分析失败（{last_err}，尝试 {calls} 次），输出占位描述")
                item = {
                    "shot_type": "other",
                    "visual_description": "【分块分析失败：画面不可用】",
                    "audio_content": "",
                }
            scene_indices = [int(x) for x in chunk["scene_indices"] if isinstance(x, (int, float))] \
                if isinstance(chunk["scene_indices"], list) else []
            desc_item: Dict[str, Any] = {
                "chunk_index": int(chunk["chunk_index"]),
                "start": round(start, 3),
                "end": round(float(end_) if end_ is not None else start, 3),
                "scene_indices": scene_indices,
                "shot_type": str(item.get("shot_type") or "other").strip().lower(),
                "visual_description": str(item.get("visual_description") or "").strip(),
                "audio_content": str(item.get("audio_content") or "").strip(),
                "meta": {
                    "frames": len(images_b64),
                    "calls": calls,
                    "tokens_estimate": {
                        "image": sum(est_image_tokens(params["frame_scale"],
                                                      max(144, int(params["frame_scale"] * 9 / 16)))
                                     for _ in images_b64),
                        "text": est_text_tokens(prompt) + est_text_tokens(
                            item.get("visual_description", "") or ""),
                    },
                },
            }
            if scene_indices:
                desc_item["index"] = scene_indices[0]
            return {"item": desc_item, "warnings": warnings, "ok": bool(item.get("visual_description"))
                    and not str(item.get("visual_description", "")).startswith("【分块分析失败")}

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        chunks_asset = inputs.get("chunks")
        if video_asset is None or not getattr(video_asset, "path", ""):
            raise RuntimeError("invalid: 需要视频输入")
        if chunks_asset is None:
            raise RuntimeError("invalid: 需要分块输入（chunks）")
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        try:
            chunks_payload = store.read_json(chunks_asset.id)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"invalid: 分块资产读取失败: {e}") from e
        rows = _chunk_rows(chunks_payload)
        if not rows:
            raise RuntimeError("invalid: 分块列表为空或不可解析")

        provider = str(params.get("provider", "auto") or "auto")
        api_key_raw = str(params.get("api_key", "") or "").strip()
        base_url = str(params.get("base_url", "") or "").strip()
        model = str(params.get("model", "gemini-1.5-flash") or "gemini-1.5-flash").strip() or "gemini-1.5-flash"
        concurrency = max(1, min(5, int(params.get("concurrency", 3) or 3)))  # 硬上限 5
        retries = max(0, int(params.get("retries", 3) or 3))
        frames_per_chunk = max(1, int(params.get("frames_per_chunk", 3) or 3))
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
        try:
            video_duration = float((video_asset.meta or {}).get("container", {}).get("duration")
                                   or (video_asset.meta or {}).get("duration") or 0)
        except (TypeError, ValueError):
            video_duration = 0.0

        out_dir = os.path.join(ctx.get("workdir", "."), "analyze_chunks_frames")
        os.makedirs(out_dir, exist_ok=True)

        sem = asyncio.Semaphore(concurrency)
        task_kwargs = {
            "ctx": ctx, "video_path": video_asset.path, "out_dir": out_dir,
            "params": {"frames_per_chunk": frames_per_chunk, "frame_scale": frame_scale,
                       "retries": retries, "temperature": temperature, "max_tokens": max_tokens},
            "provider": provider, "api_key": api_key, "base_url": base_url, "model": model,
            "sem": sem,
        }

        async def _one(i: int, chunk: Dict[str, Any]) -> Dict[str, Any]:
            return await self._analyze_one(ctx, chunk, i, len(rows),
                                           video_asset.path, out_dir, task_kwargs["params"],
                                           provider, api_key, base_url, model, sem)

        progress(0.1)
        results = await asyncio.gather(*(_one(i, c) for i, c in enumerate(rows)))
        progress(0.85)

        items: List[Dict[str, Any]] = []
        warnings: List[str] = []
        failed_chunks: List[Dict[str, Any]] = []
        img_tok = 0
        txt_tok = 0
        for r in results:
            items.append(r["item"])
            warnings.extend(r["warnings"])
            if not r["ok"]:
                failed_chunks.append({"chunk_index": r["item"]["chunk_index"]})
            img_tok += r["item"]["meta"]["tokens_estimate"]["image"]
            txt_tok += r["item"]["meta"]["tokens_estimate"]["text"]

        # ---- 省 Token 路径对比（ai_narrate 侧消费口径）：
        # 关键帧路径 = ai_narrate 抽 8 关键帧（KEYFRAME_REF）× 单帧视觉 Token + 提示词文本
        # 省 Token 路径 = ai_narrate 直读 descriptions 纯文本（零图像）
        # A4 分析阶段自身的抽帧/视觉成本不计入（该成本已由本节点报告 tokens_estimate 单独列出）
        kf_img = est_image_tokens(frame_scale, max(144, int(frame_scale * 9 / 16)))
        base_prompt = est_text_tokens(
            "分析视频内容并撰写完整解说文案。请先观察关键帧画面，理解内容后撰写解说词。")
        keyframe_path = KEYFRAME_REF * kf_img + base_prompt
        desc_text = sum(est_text_tokens(it.get("visual_description", ""))
                        + est_text_tokens(it.get("audio_content", "")) for it in items)
        description_path = base_prompt + desc_text
        ratio = round(keyframe_path / max(1, description_path), 3)
        path_compare = {
            "description_path_est_tokens": description_path,
            "keyframe_path_est_tokens": keyframe_path,
            "ratio": ratio,
            "verdict": "省Token显著" if ratio >= 3 else ("省Token" if ratio >= 1.5 else "未见显著差异"),
            "note": "估算口径：单帧视觉 Token ≈ 85+170×ceil(w/512)×ceil(h/512)（OpenAI 口径）；"
                    "对比范围=ai_narrate 侧消费：关键帧路径（8 帧图像+文本）vs descriptions 路径（纯文本）；"
                    "A4 分析阶段成本见 tokens_estimate",
        }

        payload = {
            "schema_version": 1,
            "tool": "core/analyze_chunks",
            "video_duration": round(video_duration, 3),
            "summary": {
                "chunks": len(rows),
                "ok": len(rows) - len(failed_chunks),
                "failed": len(failed_chunks),
                "tokens_estimate": {"image": img_tok, "text": txt_tok, "total": img_tok + txt_tok},
            },
            "items": items,
        }
        desc_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(items), "tool": "core/analyze_chunks"}),
            payload=payload)
        report = {
            "ok": True,
            "tool": "core/analyze_chunks",
            "version": "1.0.0",
            "chunk_count": len(rows),
            "failed_chunks": failed_chunks,
            "params": {
                "provider": provider, "model": model, "concurrency": concurrency,
                "retries": retries, "frames_per_chunk": frames_per_chunk,
                "frame_scale": frame_scale, "json_schema": bool(params.get("json_schema", True)),
            },
            "tokens_estimate": {"image": img_tok, "text": txt_tok, "total": img_tok + txt_tok},
            "path_compare": path_compare,
            "warnings": warnings,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"chunks": len(rows)}), payload=report)
        progress(1.0)
        return {"descriptions": desc_asset, "report": report_asset}