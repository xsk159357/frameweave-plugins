"""核心插件：解说文案生成（增强版 v2，对齐解说猫 r1 §3/§5 语义）。

四种模式（mode）：
- 上帝视角：从视频关键帧 / 场景描述 JSON / 场景片段中理解内容 → 生成带时间戳解说词。
  （有 descriptions 时零关键帧、最省 Token；无描述无场景时抽关键帧走视觉模型。）
- 扩写：用户文案草稿 → 按目标字数/时长扩写润色。
- 配画面：用户文案 + 场景列表 → 逐句分配场景（LLM 优先；local_match 零 Token 本地兜底）。
- 仿写：示例文案 → 模仿其风格生成新解说词。

LLM 兼容：OpenAI 兼容 chat/completions 与 Gemini generateContent 双协议（provider=auto 按 base_url 识别）；
密钥支持 @secret:名称 引用平台保险箱（resolve_secret），绝不落盘/入日志。
输出契约：script 文本 + narration 结构化分段 {index,text,start,end,scene_ids,tts_duration} +
report（provider/model/strategy_used/warnings）。本地匹配为 r1 §3.10 方案A 语义：
顺序消费场景 + 全局去重 + TTS 时长(4.5字/s)超长后扩。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

CPS = 4.5  # 默认语速（字/秒，r1 §4.3 实测）
DEFAULT_STYLE = "悬疑解说"

# ---------------------------------------------------------------- 资源目录（S3-T14：data/ 资产动态加载）
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load_data_json(name: str, default=None):
    """从包内 data/ 加载 JSON 资源；缺失/损坏返回 default（不阻断节点）。"""
    path = os.path.join(_DATA_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _load_extra_style_packs() -> Dict[str, Dict[str, Any]]:
    """迁移原版 8 套解说风格模板（config/narration_styles → data/narration_styles.json）。

    每套含 prompt_template（题材上下文）、example_hooks、forbidden_words、
    target_chars_per_second、recommended_voices（风格-音色联动）。
    """
    packs: Dict[str, Dict[str, Any]] = {}
    for raw in (_load_data_json("narration_styles.json") or []):
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        name = raw.get("name") or raw["id"]
        if not name:
            continue
        packs[name] = {
            "role": str(raw.get("description", "") or ""),
            "tone": "",
            "forbidden": list(raw.get("forbidden_words") or []),
            "hooks": list(raw.get("example_hooks") or []),
            "target_cps": float(raw.get("target_chars_per_second", 4.5) or 4.5),
            "recommended_voices": list(raw.get("recommended_voices") or []),
            "prompt_template": str(raw.get("prompt_template", "") or ""),
            "icon": str(raw.get("icon", "") or ""),
        }
    return packs


def _load_extra_lang_hints() -> Dict[str, str]:
    """迁移原版 13 语 narration 配置（config/language → data/lang_config.json）。

    语言下拉扩展为 13 语；已有内置指令优先，新增语种由资源数据生成指令
    （format_note + role + hook_example），display_name 取自数据。
    """
    cfg = _load_data_json("lang_config.json") or {}
    langs = cfg.get("languages") or {}
    out: Dict[str, str] = {}
    for code, c in langs.items():
        if not isinstance(c, dict):
            continue
        display = c.get("display_name") or code
        parts = []
        if c.get("format_note"):
            parts.append(str(c["format_note"]))
        if c.get("role"):
            parts.append(str(c["role"]))
        if c.get("hook_example"):
            parts.append("开场示例：" + str(c["hook_example"]))
        h = " ".join(p for p in parts if p)
        if h:
            out[display] = h
    return out


_BUILTIN_STYLE_PACKS: Dict[str, Dict[str, Any]] = {
    "悬疑解说": {
        "role": "资深悬疑电影解说博主",
        "tone": "悬念前置、信息逐步释放、结尾留钩子",
        "forbidden": ["首先", "其次", "最后", "总的来说", "这部电影讲述了", "注意看这个男人叫小帅"],
        "hooks": ["你敢信？", "没人想到，这一切的起点是……", "他以为自己逃掉了，直到……"],
        "target_cps": 4.5,
    },
    "搞笑解说": {
        "role": "高密度笑点的影视吐槽博主",
        "tone": "快节奏、玩梗适度、吐槽与剧情并进",
        "forbidden": ["总而言之", "值得注意的是", "这体现了", "这代表了"],
        "hooks": ["这剧情离谱到我以为在看喜剧片", "导演这脑回路，我给满分"],
        "target_cps": 4.5,
    },
    "知识科普": {
        "role": "把复杂概念讲明白的科普作者",
        "tone": "概念降维、术语即时解释、举例贴近生活",
        "forbidden": ["众所周知", "不言而喻", "想必大家都了解"],
        "hooks": ["一个细节，藏着整个真相", "三分钟看懂这件事的底层逻辑"],
        "target_cps": 4.3,
    },
    "情感讲述": {
        "role": "擅长情绪叙事的影视解说作者",
        "tone": "共情、细腻、点到为止，不强行煽情",
        "forbidden": ["感动哭了", "泪目", "太扎心了"],
        "hooks": ["有些告别，是没有声音的", "他做这个决定时，手是抖的"],
        "target_cps": 4.2,
    },
    "客观平实": {
        "role": "克制、准确、不夹带情绪的电影解说作者",
        "tone": "事实优先、结构清晰、不做过度解读",
        "forbidden": ["神作", "烂片", "绝了", "yyds"],
        "hooks": ["这是一部……的电影", "我们先把故事捋一遍"],
        "target_cps": 4.5,
    },
}

# 原版 8 套风格模板（数据驱动，加载后合并进 STYLE_PACKS；5 套内置风格任选其一）
STYLE_PACKS: Dict[str, Dict[str, Any]] = {**_BUILTIN_STYLE_PACKS, **_load_extra_style_packs()}

# 原版 13 语 narration 配置（中文等语言指令仍以内置为准，其余取资源）
LANG_HINTS: Dict[str, str] = {
    "中文": "请用简体中文撰写解说词，口语化，每句不超过30字。",
    "英语": "Write the narration in English, conversational, each sentence under 20 words.",
    "日语": "日本語でナレーションを書いてください。口語的に、一文20語以内で。",
    "韩语": "한국어로 내레이션을 작성하세요. 구어체로, 문장당 20단어 이내로.",
    "泰语": "เขียนบทบรรยายเป็นภาษาไทย แบบภาษาพูด ประโยคละไม่เกิน 20 คำ",
    "越南语": "Viết lời bình bằng tiếng Việt, khẩu ngữ, mỗi câu dưới 20 từ.",
    "自动": "Use the language of the source video/content; conversational.",
}
LANG_HINTS.update(_load_extra_lang_hints())


def _extract_json(text: str) -> Any:
    """从 LLM 输出提取 JSON（支持 markdown 围栏与首个 { / [ 块）。失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
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
    return None


def _chat_openai(messages: List[Dict[str, Any]], api_key: str, base_url: str, model: str,
                 temperature: float, max_tokens: int, json_mode: bool) -> str:
    import urllib.request
    payload: Dict[str, Any] = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("openai: 响应解析失败: " + json.dumps(data, ensure_ascii=False)[:300]) from e


def _chat_gemini(messages: List[Dict[str, Any]], api_key: str, base_url: str, model: str,
                 temperature: float, max_tokens: int) -> str:
    import urllib.request
    parts: List[Dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        role = "user" if m.get("role") in ("user", "system") else "model"
        if isinstance(content, str):
            parts.append({"text": content})
        elif isinstance(content, list):
            for c in content:
                if c.get("type") == "text":
                    parts.append({"text": c.get("text", "")})
                elif c.get("type") == "image_url":
                    url = c.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        mime, b64 = url.split(",", 1)[:2]
                        parts.append({"inline_data": {"mime_type": mime.split(";")[0].split(":")[1],
                                                      "data": b64}})
    payload = {
        "contents": [{"role": role, "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url.rstrip('/')}/models/{model}:generateContent{sep}key={api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("gemini: 响应解析失败: " + json.dumps(data, ensure_ascii=False)[:300]) from e


def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")


def _split_sentences(text: str, max_len: int = 50) -> List[str]:
    """按标点切句，超长句再折行；返回 10-50 字符/段（r1 §3.10 粒度）。"""
    parts = re.split(r"(?<=[。！？!?；;])", text)
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        while len(p) > max_len:
            cut = p[:max_len]
            out.append(cut)
            p = p[max_len:].strip()
        if p:
            out.append(p)
    return out


def estimate_tts(text: str, cps: float = CPS) -> float:
    return round(max(1.0, len(text) / cps), 3)


def local_match(sentences: List[str], scenes: List[Dict[str, Any]],
                cps: float = CPS) -> List[Dict[str, Any]]:
    """零 Token 本地匹配（r1 §3.10 方案A）：顺序消费场景、全局去重、TTS 超长后扩。"""
    out: List[Dict[str, Any]] = []
    k = 0
    n = len(scenes)
    for i, s in enumerate(sentences):
        tts = estimate_tts(s, cps)
        if k >= n:
            last = scenes[-1]
            out.append({"index": i, "text": s, "scene_ids": [last.get("index", 0)],
                        "start": last["start"], "end": last["end"], "tts_duration": tts})
            continue
        sc = scenes[k]
        end = sc["end"]
        j = k
        while (end - sc["start"]) < tts and j + 1 < n:
            j += 1
            end = scenes[j]["end"]
        out.append({"index": i, "text": s,
                    "scene_ids": [scenes[x].get("index", x) for x in range(k, j + 1)],
                    "start": sc["start"], "end": end, "tts_duration": tts})
        k = j + 1
    return out


def scene_lines(scenes: List[Dict[str, Any]], descriptions: Optional[List[Dict[str, Any]]] = None,
                lang: str = "中文") -> str:
    """场景数据段（r1 §5.2）：有描述写描述；无描述/过短显式标记【占位画面】。

    A4 接入（S3-T3）：core/analyze_chunks 的每条描述（index=块内首个场景 + scene_indices=
    块内全部场景）通过 scene_indices 隶属回退挂到块内其余场景，避免块内场景被占位符替代。
    """
    desc_map: Dict[int, Dict[str, Any]] = {}
    for d in descriptions or []:
        if isinstance(d, dict):
            idx = d.get("index")
            if idx is None:
                idx = d.get("scene_index")
            if idx is not None:
                try:
                    desc_map[int(idx)] = d
                except (TypeError, ValueError):
                    pass
    # A4 分块描述覆盖块内多场景：无直接 index 命中的场景按 scene_indices 隶属回退
    for sc in scenes:
        try:
            idx = int(sc.get("index", 0))
        except (TypeError, ValueError):
            continue
        if idx in desc_map:
            continue
        for d in descriptions or []:
            sidx = d.get("scene_indices") if isinstance(d, dict) else None
            if not isinstance(sidx, list):
                continue
            try:
                hit = idx in {int(x) for x in sidx}
            except (TypeError, ValueError):
                hit = False
            if hit:
                desc_map[idx] = d
                break
    lines = []
    for sc in scenes:
        idx = sc.get("index", 0)
        d = desc_map.get(idx) or {}
        desc = (d.get("visual_description") or d.get("description") or d.get("text") or "").strip()
        audio = (d.get("audio_content") or "").strip()
        shot = (d.get("shot_type") or "").strip()
        seg = f"[{sc.get('start', 0):.1f}s -> {sc.get('end', 0):.1f}s]"
        if desc or audio:
            body = (shot + ("；" if shot else "") + desc + ("｜听觉：" + audio if audio else ""))
        else:
            body = "【占位画面：无描述】"
        lines.append(f"{seg} {body}")
    if not lines:
        return "（无场景数据）"
    return "\n".join(lines)


class AINarrateNode(NodeBase):
    """解说文案生成。type_id: core/ai_narrate"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/ai_narrate",
            title="AI 文案生成",
            category="语义",
            description="解说文案：上帝视角（视频/场景/描述）· 扩写 · 配画面 · 仿写；OpenAI/Gemini 兼容",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=False),
                PortSpec("scenes", PortType.SEGMENTS, label="场景片段", required=False),
                PortSpec("descriptions", PortType.JSON, label="场景描述", required=False),
                PortSpec("draft", PortType.SCRIPT, label="文案草稿/示例", required=False),
                PortSpec("items", PortType.JSON, label="手写分镜", required=False,
                         description="passthrough 模式专用：JSON 数组，每项 {text,start,end}；"
                                     "原样透传成 narration（不改时间、不重切）"),
            ],
            outputs=[
                PortSpec("script", PortType.SCRIPT, label="解说文案", required=True),
                PortSpec("narration", PortType.JSON, label="结构化文案", required=True),
                PortSpec("report", PortType.JSON, label="生成报告", required=False),
            ],
            params=[
                PortSpec("mode", PortType.STRING, label="模式", default="上帝视角",
                         widget="select", options=["上帝视角", "扩写", "配画面", "仿写", "神回复",
                                                   "passthrough"]),
                PortSpec("style", PortType.STRING, label="风格", default=DEFAULT_STYLE,
                         widget="select", options=list(STYLE_PACKS.keys()) + ["自定义"]),
                PortSpec("custom_style", PortType.STRING, label="自定义风格描述", required=False, default="", widget="text"),
                PortSpec("perspective", PortType.STRING, label="视角", default="第三人称",
                         widget="select", options=["第三人称", "第一人称"]),
                PortSpec("language", PortType.STRING, label="语言", default="中文",
                         widget="select", options=list(LANG_HINTS.keys())),
                PortSpec("provider", PortType.STRING, label="Provider", default="auto",
                         widget="select", options=["auto", "openai", "gemini"]),
                PortSpec("api_key", PortType.STRING, label="API Key", required=False, default="",
                         widget="password", description="支持 @secret:名称 引用保险箱"),
                PortSpec("base_url", PortType.STRING, label="API 地址", required=False, default="", widget="text"),
                PortSpec("model", PortType.STRING, label="模型", default="", widget="text"),
                PortSpec("target_duration", PortType.FLOAT, label="目标时长(秒,0=自动)", default=0.0, widget="number"),
                PortSpec("target_chars", PortType.INT, label="目标字数(0=自动)", default=0, widget="number"),
                PortSpec("avg_segment_chars", PortType.INT, label="平均每段字数", default=25, widget="number"),
                PortSpec("temperature", PortType.FLOAT, label="温度", default=0.7, widget="number"),
                PortSpec("max_tokens", PortType.INT, label="最大 Token", default=4096, widget="number"),
                PortSpec("keyframes", PortType.INT, label="关键帧数", default=8, widget="number"),
                PortSpec("json_mode", PortType.BOOL, label="结构化输出", default=True, widget="toggle"),
                PortSpec("local_match", PortType.BOOL, label="本地匹配兜底", default=True, widget="toggle"),
                PortSpec("retries", PortType.INT, label="重试次数", default=2, widget="number"),
                PortSpec("llm_channel", PortType.STRING, label="LLM 通道", default="llm_call",
                         widget="select", options=["llm_call", "internal"],
                         description="llm_call=经 core/llm_call 注册表复用（统一退避/回退，S3-T2）；internal=原内嵌调用（零回归）"),
            ],
            version="2.2.0",
        )

    # ------------------------------------------------------------ 工具
    async def _extract_keyframes(self, ctx: dict, video_path: str, count: int) -> List[str]:
        from app.plugin import deps
        r = deps.resolve_binary("ffmpeg", env=os.environ)
        ff = r["path"] if r.get("ready") else ""
        if not ff:
            return []
        out_dir = os.path.join(ctx.get("workdir", "."), "narrate_frames")
        os.makedirs(out_dir, exist_ok=True)
        for f in os.listdir(out_dir):
            try:
                os.remove(os.path.join(out_dir, f))
            except OSError:
                pass
        filt = f"select=not(mod(n\\,{max(1, int(count))})),scale=320:-1"
        cmd = [ff, "-v", "error", "-i", video_path, "-vf", filt,
               "-frames:v", str(max(1, int(count))),
               os.path.join(out_dir, "kf_%02d.jpg")]
        rt = ctx.get("run_tracked")
        try:
            if rt is not None:
                import subprocess as _sp
                try:
                    proc = await rt(cmd, timeout=min(120.0, 15.0), text=True,
                                    stdout=_sp.PIPE, stderr=_sp.PIPE)
                    if proc.returncode != 0:
                        return []
                except Exception:  # noqa: BLE001
                    pass
            import asyncio
            import subprocess as _sp2
            proc = await asyncio.to_thread(
                lambda: _sp2.run(cmd, capture_output=True, timeout=120))
            if proc.returncode != 0:
                return []
        except Exception:  # noqa: BLE001
            return []
        return sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir))

    def _read_payload(self, ctx: dict, asset: Any) -> Any:
        if asset is None:
            return None
        store = ctx["store"]
        try:
            return store.read_json(asset.id)
        except Exception:  # noqa: BLE001
            return None

    def _read_text(self, ctx: dict, asset: Any) -> str:
        payload = self._read_payload(ctx, asset)
        if isinstance(payload, dict):
            return str(payload.get("text", "") or "")
        if isinstance(payload, str):
            return payload
        if payload is None and asset is not None and getattr(asset, "path", ""):
            try:
                with open(asset.path, "r", encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return ""
        return ""

    # ------------------------------------------------------------ LLM
    async def _llm(self, ctx: dict, messages: List[Dict[str, Any]], provider: str,
                   api_key: str, base_url: str, model: str, temperature: float,
                   max_tokens: int, json_mode: bool, retries: int,
                   images: Optional[List[str]] = None) -> str:
        import asyncio
        if images:
            content: List[Dict[str, Any]] = [{"type": "text", "text": messages[-1]["content"]}]
            for img in images[:12]:
                ext = os.path.splitext(img)[1].lower().lstrip(".") or "jpg"
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                        "webp": "image/webp"}.get(ext, "image/jpeg")
                with open(img, "rb") as f:
                    import base64
                    b64 = base64.b64encode(f.read()).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            msgs = [{"role": "user", "content": content}]
        else:
            msgs = messages

        last_err = ""
        channel = str(getattr(self, "_llm_channel", "llm_call") or "llm_call")
        if not images and channel == "llm_call":
            # S3-T2（D6）：经注册表复用 core/llm_call——统一退避[2,5,10]/fallback/schema/R1/R2
            # （视觉关键帧路径无 llm_call 等价能力，继续走 internal）
            from app.registry import get_class
            cls = get_class("core/llm_call")
            msgs_asset = ctx["store"].save_asset(
                Asset(id="", kind=PortType.JSON.value, meta={"count": len(msgs)}),
                payload=msgs)
            out = await cls().run(ctx, {"prompt": None, "messages": msgs_asset, "system": None}, {
                "provider": provider, "api_key": api_key, "base_url": base_url,
                "model": model, "fallback_models": "",
                "temperature": temperature, "max_tokens": max_tokens,
                "json_mode": json_mode, "schema": "", "timeout": 600,
                "backoff": "[2,5,10]",
            })
            payload = ctx["store"].read_json(out["text"].id)
            return str(payload.get("text", "") or "") if isinstance(payload, dict) else str(payload or "")
        for attempt in range(max(1, retries + 1)):
            try:
                if provider == "gemini":
                    return await asyncio.to_thread(
                        _chat_gemini, msgs, api_key, base_url, model, temperature, max_tokens)
                return await asyncio.to_thread(
                    _chat_openai, msgs, api_key, base_url, model, temperature, max_tokens, json_mode)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"llm: 请求失败（重试 {retries} 次）: {last_err}")

    # ------------------------------------------------------------ run
    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        scenes_asset = inputs.get("scenes")
        desc_asset = inputs.get("descriptions")
        script_asset = inputs.get("draft")
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)

        mode = str(params.get("mode", "上帝视角") or "上帝视角")
        style = str(params.get("style", DEFAULT_STYLE) or DEFAULT_STYLE)
        custom_style = str(params.get("custom_style", "") or "").strip()
        perspective = str(params.get("perspective", "第三人称") or "第三人称")
        language = str(params.get("language", "中文") or "中文")
        provider = str(params.get("provider", "auto") or "auto")
        api_key_raw = str(params.get("api_key", "") or "").strip()
        base_url = str(params.get("base_url", "") or "").strip()
        model = str(params.get("model", "") or "").strip() or ""
        try:
            target_duration = float(params.get("target_duration", 0) or 0)
        except (TypeError, ValueError):
            target_duration = 0.0
        try:
            target_chars = int(params.get("target_chars", 0) or 0)
        except (TypeError, ValueError):
            target_chars = 0
        avg_seg = max(5, int(params.get("avg_segment_chars", 25) or 25))
        try:
            temperature = float(params.get("temperature", 0.7) or 0.7)
        except (TypeError, ValueError):
            temperature = 0.7
        max_tokens = max(256, int(params.get("max_tokens", 4096) or 4096))
        keyframes_n = max(1, int(params.get("keyframes", 8) or 8))
        json_mode = bool(params.get("json_mode", True))
        local_match_on = bool(params.get("local_match", True))
        retries = max(0, int(params.get("retries", 2) or 2))
        self._llm_channel = str(params.get("llm_channel", "llm_call") or "llm_call")

        # LLM 通道配置化：显式参数 > 全局配置 > provider 默认
        api_key = _resolve_secret(ctx, api_key_raw)
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
        if provider == "auto":
            if "generativelanguage" in (base_url or ""):
                provider = "gemini"
            else:
                provider = "openai"
        if not base_url:
            base_url = ("https://generativelanguage.googleapis.com/v1beta"
                        if provider == "gemini" else "https://api.openai.com/v1")

        # ---- 场景/描述上下文
        scenes = self._read_payload(ctx, scenes_asset) or []
        if not isinstance(scenes, list):
            scenes = []
        descriptions = self._read_payload(ctx, desc_asset) or []
        if isinstance(descriptions, dict):
            descriptions = descriptions.get("scenes") or descriptions.get("items") or []
        if not isinstance(descriptions, list):
            descriptions = []
        scenes_sorted = sorted(
            [s for s in scenes if isinstance(s, dict) and s.get("start") is not None],
            key=lambda s: float(s["start"]))
        video_duration = 0.0
        if video_asset is not None:
            try:
                vm = getattr(video_asset, "meta", None) or {}
                video_duration = float(vm.get("container", {}).get("duration")
                                       or vm.get("duration") or 0)
            except (TypeError, ValueError):
                video_duration = 0.0
        if not video_duration and scenes_sorted:
            video_duration = float(scenes_sorted[-1]["end"])

        # ---- 目标参数（r1 §4.3：指定 duration → chars=×4.5；指定 chars → duration；都不给 → 时长×80%）
        if target_duration <= 0 and target_chars <= 0:
            target_duration = round(video_duration * 0.8, 1) if video_duration else 60.0
        if target_chars <= 0:
            target_chars = max(100, int(round(target_duration * CPS)))
        if target_duration <= 0:
            target_duration = round(target_chars / CPS, 1)
        target_segments = max(1, int(round(target_chars / avg_seg)))

        style_pack = STYLE_PACKS.get(style) or {}
        style_desc = custom_style or style_pack.get("role", "")
        forbidden = "；".join(style_pack.get("forbidden", []))
        hooks = "；".join(style_pack.get("hooks", []))
        cps = float(style_pack.get("target_cps", CPS))

        warnings: List[str] = []
        strategy_used = "llm"
        llm_calls = 0

        def _base_prompt(job: str) -> str:
            p = f"你是一名{style_desc or '资深影视解说博主'}。请用{perspective}完成以下任务：{job}\n"
            p += f"【语言】{LANG_HINTS.get(language, LANG_HINTS['中文'])}\n"
            p += f"【目标时长约束】目标解说时长约 {target_duration:g} 秒 / 目标总字数约 {target_chars} 字 / " \
                 f"预估分段数 {target_segments} 段 / 每段建议不超过 {min(avg_seg + 5, 30)} 字 / 注意总量控制，不要超出。\n"
            if forbidden:
                p += f"【禁词】文中不要出现：{forbidden}。\n"
            if hooks:
                p += f"【钩子参考】开场可参考：{hooks}（不必全部使用）。\n"
            if style_pack.get("tone"):
                p += f"【语气】{style_pack['tone']}。\n"
            p += "【创作方式】事实优先，先理顺故事再表达观点；口语化，避免书面语和套路化开头。\n"
            return p

        def _out_protocol_json(extra: str = "") -> str:
            return ("\n【输出格式】只输出 JSON，不要任何解释或额外文字。\n"
                    f"JSON 结构：[{{\"text\": \"解说句\", \"start\": 秒数(可选), \"end\": 秒数(可选), "
                    f"\"segment_index\": 场景序号(仅配画面需要)}}]{extra}\n")

        # ---- 模式分发
        result_text = ""
        narration_segments: List[Dict[str, Any]] = []
        if mode == "passthrough":
            # 人写分镜原样透传（B 案）：不调 _split_sentences / local_match、不改 start/end。
            # 时间权威 = 作者写的时间；这里只校验 + 补 tts_duration（估算，仅作容量参考）。
            items_in = inputs.get("items")
            data: Any = items_in
            if items_in is not None and getattr(items_in, "id", ""):
                data = store.read_json(items_in.id)
            if isinstance(data, dict):
                data = data.get("segments") or data.get("items")
            if not isinstance(data, list) or not data:
                raise RuntimeError(
                    "invalid: passthrough 需要手写分镜输入（items）："
                    "JSON 数组，每项含 text / start / end")
            for i, raw in enumerate(data):
                if not isinstance(raw, dict):
                    raise RuntimeError(f"invalid: items[{i}] 必须是对象")
                text = str(raw.get("text") or raw.get("narration") or "").strip()
                if not text:
                    raise RuntimeError(f"invalid: items[{i}] 缺 text")
                try:
                    st = float(raw.get("start"))
                    en = float(raw.get("end"))
                except (TypeError, ValueError):
                    raise RuntimeError(
                        f"invalid: items[{i}] 的 start/end 必须是数字（不许缺省猜 0 起）")
                if en <= st:
                    raise RuntimeError(
                        f"invalid: items[{i}] 的 end({en}) 必须大于 start({st})")
                narration_segments.append({
                    "index": i, "text": text,
                    "start": st, "end": en,          # 原样保留：不 round、不扩窗、不重切
                    "scene_ids": list(raw.get("scene_ids") or []),
                    "tts_duration": estimate_tts(text, cps),
                })
            result_text = "\n".join(sg["text"] for sg in narration_segments)  # 行数 = items 数
            strategy_used = "passthrough"
        elif mode == "上帝视角":
            prompt = _base_prompt("分析视频内容并撰写完整解说文案。")
            if descriptions:
                prompt += "\n【场景描述】\n" + scene_lines(scenes_sorted, descriptions, language) + "\n"
                prompt += "时间点只能取场景记录中的边界；每个时间段一条记录；不要发明或合并时间点。\n"
                images: Optional[List[str]] = None
            elif scenes_sorted:
                prompt += "\n【场景时间轴】\n" + scene_lines(scenes_sorted, None, language) + "\n"
                prompt += "时间点只能取场景记录中的边界；每个时间段一条记录。\n"
                images = None
            else:
                if video_asset is None or not getattr(video_asset, "path", "") or not os.path.exists(video_asset.path):
                    raise RuntimeError("invalid: 上帝视角需要 video / scenes / descriptions 至少一项输入")
                prompt += "\n请先观察关键帧画面，理解内容后撰写解说词。\n"
                progress(0.15)
                images = await self._extract_keyframes(ctx, video_asset.path, keyframes_n)
                if not images:
                    warnings.append("关键帧抽取失败（ffmpeg 不可用），仅按提示词生成")
                    images = None
            if json_mode:
                prompt += _out_protocol_json()
            progress(0.4)
            msgs = [{"role": "user", "content": prompt}]
            raw = await self._llm(ctx, msgs, provider, api_key, base_url, model,
                                  temperature, max_tokens, json_mode, retries, images)
            llm_calls += 1
            data = _extract_json(raw) if json_mode else None
            if isinstance(data, list) and data:
                for i, it in enumerate(data):
                    if not isinstance(it, dict) or not it.get("text"):
                        continue
                    narration_segments.append({
                        "index": i, "text": str(it.get("text")).strip(),
                        "start": _num(it.get("start"), None),
                        "end": _num(it.get("end"), None),
                    })
                result_text = "".join(s["text"] for s in narration_segments)
                strategy_used = "llm"
            else:
                if json_mode:
                    warnings.append("LLM 未返回可解析 JSON，回退纯文本并本地匹配")
                result_text = raw.strip()
                strategy_used = "llm+local" if local_match_on else "llm"
        elif mode == "扩写":
            draft = self._read_text(ctx, script_asset)
            if not draft:
                raise RuntimeError("invalid: 扩写模式需要连接文案草稿输入（script）")
            prompt = (_base_prompt("扩写润色用户的文案草稿，补足细节与过渡，使其成为完整解说稿。")
                      + "\n【用户草稿】\n" + draft + "\n"
                      + ("\n扩写后请直接输出完整文案正文（不要解释）。" if not json_mode else _out_protocol_json()))
            msgs = [{"role": "user", "content": prompt}]
            raw = await self._llm(ctx, msgs, provider, api_key, base_url, model,
                                  temperature, max_tokens, json_mode, retries)
            llm_calls += 1
            data = _extract_json(raw) if json_mode else None
            if isinstance(data, list) and data:
                result_text = "".join(str(it.get("text", "")) for it in data if isinstance(it, dict))
            else:
                result_text = raw.strip()
            strategy_used = "llm"
        elif mode == "配画面":
            draft = self._read_text(ctx, script_asset)
            if not draft:
                raise RuntimeError("invalid: 配画面模式需要连接文案草稿输入（script）")
            if not scenes_sorted:
                raise RuntimeError("invalid: 配画面模式需要连接场景片段输入（scenes）")
            prompt = (_base_prompt("为文案逐句分配最合适的场景序号。")
                      + "\n【用户文案】\n" + draft + "\n"
                      + "\n【场景列表】\n" + scene_lines(scenes_sorted, None, language) + "\n"
                      + "\n【输出格式】只输出 JSON："
                      + '[{"sentence": "原文句子", "segment_index": 场景序号}]'
                      + "。要求：按文案顺序分配，每个场景最多使用一次，不要跳过文案句子。\n")
            msgs = [{"role": "user", "content": prompt}]
            raw = await self._llm(ctx, msgs, provider, api_key, base_url, model,
                                  temperature, max_tokens, True, retries)
            llm_calls += 1
            data = _extract_json(raw)
            sentences = _split_sentences(draft)
            mapping: Optional[List[Dict[str, Any]]] = None
            if isinstance(data, list):
                mapping = data
            else:
                warnings.append("配画面 LLM 输出不可解析，回退本地匹配")
            if mapping and all(isinstance(m, dict) for m in mapping):
                used: set = set()
                segments: List[Dict[str, Any]] = []
                for i, m in enumerate(mapping):
                    s = str(m.get("sentence") or (sentences[i] if i < len(sentences) else "")).strip()
                    if not s:
                        continue
                    try:
                        idx = int(m.get("segment_index"))
                    except (TypeError, ValueError):
                        idx = -1
                    if not (0 <= idx < len(scenes_sorted)) or idx in used:
                        warnings.append(f"句子 {i} 场景序号 {idx} 非法/重复，改由本地匹配")
                        idx = -1
                    if idx >= 0:
                        sc = scenes_sorted[idx]
                        used.add(idx)
                        tts = estimate_tts(s, cps)
                        segments.append({"index": i, "text": s, "scene_ids": [sc.get("index", idx)],
                                         "start": sc["start"], "end": sc["end"], "tts_duration": tts})
                    else:
                        segments.append(None)
                narration_segments = [sg for sg in segments if sg is not None]
                if len(narration_segments) < len(sentences):
                    local = local_match(sentences, scenes_sorted, cps)
                    narration_segments = local
                    strategy_used = "local"
                else:
                    strategy_used = "llm"
            else:
                narration_segments = local_match(sentences, scenes_sorted, cps)
                strategy_used = "local"
            result_text = "".join(sg["text"] for sg in narration_segments)
        elif mode == "仿写":
            example = self._read_text(ctx, script_asset)
            if not example:
                raise RuntimeError("invalid: 仿写模式需要连接示例文案输入（script）")
            prompt = (_base_prompt("模仿示例文案的风格、结构与语感，撰写一篇全新的解说词。")
                      + "\n【示例文案】\n" + example + "\n"
                      + ("\n直接输出新文案正文（不要解释）。" if not json_mode else _out_protocol_json()))
            msgs = [{"role": "user", "content": prompt}]
            raw = await self._llm(ctx, msgs, provider, api_key, base_url, model,
                                  temperature, max_tokens, json_mode, retries)
            llm_calls += 1
            data = _extract_json(raw) if json_mode else None
            if isinstance(data, list) and data:
                result_text = "".join(str(it.get("text", "")) for it in data if isinstance(it, dict))
            else:
                result_text = raw.strip()
            strategy_used = "llm"
        elif mode == "神回复":
            # G12：高光混剪"每分镜一句毒舌神回复"（对齐解说猫 highlight_roast 语义，r5 已还原）
            # 输入：scenes（分镜）或 descriptions；输出：每分镜一句神回复（毒舌/幽默，独立字幕轨）
            if not scenes_sorted:
                raise RuntimeError("invalid: 神回复模式需要连接场景片段输入（scenes）")
            prompt = ("你是短视频毒舌神回复写手。针对下面每个分镜场景，各写一句幽默毒舌的神回复旁白。\n"
                      + "\n【分镜列表】\n" + scene_lines(scenes_sorted, None, language) + "\n"
                      + "\n要求：\n"
                      + "1. 每句 5-20 字，口语化、反差幽默、带吐槽感，但不得人身攻击；\n"
                      + "2. 与分镜数量一一对应，严禁合并或跳过；\n"
                      + "3. 保持原语言：" + LANG_HINTS.get(language, LANG_HINTS['中文']) + "；\n"
                      + "4. 只输出结果，每行一条，不要解释、不要序号。\n")
            msgs = [{"role": "user", "content": prompt}]
            raw = await self._llm(ctx, msgs, provider, api_key, base_url, model,
                                  temperature, max_tokens, False, retries)
            llm_calls += 1
            lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
            if len(lines) < len(scenes_sorted):
                warnings.append(
                    f"神回复行数 {len(lines)} < 分镜数 {len(scenes_sorted)}，回退按句拆分")
                lines = _split_sentences(raw)
            narration_segments = []
            for i, sc in enumerate(scenes_sorted):
                if i >= len(lines):
                    break
                tts = estimate_tts(lines[i], cps)
                narration_segments.append({
                    "index": i, "text": lines[i],
                    "scene_ids": [sc.get("index", i)],
                    "start": sc["start"], "end": sc["end"], "tts_duration": tts,
                })
            result_text = "".join(sg["text"] for sg in narration_segments)
            strategy_used = "llm"
        else:
            raise RuntimeError(f"invalid: 未知模式: {mode}")

        progress(0.75)
        # ---- 后处理：没有结构化分段时，按句拆分 + 本地匹配补时间戳
        if not narration_segments and result_text:
            sentences = _split_sentences(result_text)
            if scenes_sorted and local_match_on:
                narration_segments = local_match(sentences, scenes_sorted, cps)
                if strategy_used == "llm":
                    strategy_used = "llm+local"
            else:
                t0 = 0.0
                for i, s in enumerate(sentences):
                    tts = estimate_tts(s, cps)
                    narration_segments.append({"index": i, "text": s, "scene_ids": [],
                                               "start": round(t0, 3), "end": round(t0 + tts, 3),
                                               "tts_duration": tts})
                    t0 += tts
        # 结构化段落补 scene_ids（时间重叠 → 场景索引，供下游时间线/剪映装配复用）
        if narration_segments and scenes_sorted:
            for seg in narration_segments:
                if seg.get("scene_ids") is None:
                    seg["scene_ids"] = []
                if not seg.get("scene_ids"):
                    s = _num(seg.get("start"), None)
                    e = _num(seg.get("end"), None)
                    if s is not None and e is not None:
                        seg["scene_ids"] = [sc.get("index", 0) for sc in scenes_sorted
                                            if not (float(sc["end"]) <= s or float(sc["start"]) >= e)]
                if "tts_duration" not in seg:
                    seg["tts_duration"] = estimate_tts(str(seg.get("text", "")), cps)
        # 覆盖率统计
        chars = sum(len(sg.get("text", "")) for sg in narration_segments)
        max_gap = 0.0
        for i in range(1, len(narration_segments)):
            gap = float(narration_segments[i].get("start") or 0) - float(narration_segments[i - 1].get("end") or 0)
            if gap > max_gap:
                max_gap = gap
        coverage = {
            "chars": chars, "target_chars": target_chars,
            "segment_count": len(narration_segments), "target_segments": target_segments,
            "max_gap": round(max_gap, 3),
        }
        if chars < target_chars * 0.5:
            warnings.append(f"输出字数 {chars} 明显低于目标 {target_chars}（请检查模型/配额）")

        progress(0.9)
        store = ctx["store"]
        script_payload = {
            "text": result_text, "mode": mode, "style": style, "language": language,
            "chars": chars, "duration_estimate": round(chars / cps, 3),
        }
        script_asset = store.save_asset(
            Asset(id="", kind=PortType.SCRIPT.value,
                  meta={"mode": mode, "language": language, "chars": chars,
                        "duration_estimate": round(chars / cps, 3)}),
            payload=script_payload)
        narration_payload = {
            "schema_version": 1,
            "mode": mode, "style": style, "language": language, "perspective": perspective,
            "target": {"duration": round(target_duration, 1), "chars": target_chars,
                       "segments": target_segments},
            "segments": narration_segments,
            "coverage": coverage,
            "strategy_used": strategy_used,
            "warnings": warnings,
        }
        narration_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": mode, "segments": len(narration_segments)}),
            payload=narration_payload)
        report = {
            "provider": provider, "model": model,
            "api_base": base_url,
            "mode": mode, "style": style, "language": language,
            "strategy_used": strategy_used,
            "llm_calls": llm_calls,
            "target": narration_payload["target"],
            "coverage": coverage,
            "warnings": warnings,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": mode}), payload=report)
        progress(1.0)
        return {"script": script_asset, "narration": narration_asset, "report": report_asset}


def _num(v: Any, dflt: Optional[float]) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt