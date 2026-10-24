"""核心插件：保留原声决策（B6，对齐解说猫 r2 §5 scene_dialogue_processor 决策语义）。

数据流：RichScene(带对话) → LLM 决定保留哪些原声（keep_dialogue_ids）→ mode→'mixed'。
- Prompt 完整复刻 r2 §5.4 捕获原文（单场景/批量两式），额外补充"情绪爆发保留/日常对话不保留"。
- 批量：max_scenes_per_batch（原版批 50）逐批 LLM，输出 {"selections":[{scene_index,
  keep_dialogue_ids, reason}], "summary"}。
- 解析优于原版（r2 §11 怪癖 7）：顶层 list 也兼容（原版只认 dict，list 直接 AttributeError 被吞）；
  非法 scene_index / 非法 dialogue id 逐条告警，不整批丢弃；解析错误透传到 report（不静默）。
- 决策可解释：report 逐场景含 keep_dialogue_ids + LLM reason + mode 变更。
- LLM 失败策略：warn（默认，不保留原声继续跑）/ error（抛错），退避 [2,5,10] 秒重试。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

BACKOFF = (2.0, 5.0, 10.0)


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _resolve_secret(ctx: Any, raw: str) -> str:
    if not raw or not str(raw).startswith("@secret:"):
        return str(raw or "")
    ref = str(raw).split(":", 1)[1]
    resolver = ctx.get("resolve_secret") if isinstance(ctx, dict) else None
    if resolver is not None:
        return str(resolver(ref) or "")
    from app.secrets import read_secret
    return str(read_secret(ref) or "")


def _chat_once(messages: List[Dict[str, Any]], api_key: str, base_url: str, model: str,
               temperature: float, timeout: float = 120.0) -> str:
    """OpenAI 兼容 chat/completions 单次调用（urllib，同步）。"""
    import urllib.request
    payload = {"model": model, "messages": messages, "temperature": temperature}
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("keep_original: LLM 响应解析失败: " + json.dumps(data, ensure_ascii=False)[:300]) from e


def _extract_json(text: str) -> Any:
    """从 LLM 输出提取 JSON（支持 markdown 围栏与首个 { / [ 块）。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if "```" in t[3:] else t[3:]
        t = t.rsplit("```", 1)[0] if t.endswith("```") else t
    t = t.strip()
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
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


def scenes_with_dialogues(rich_scenes: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取含对话的场景（保持输入顺序）。"""
    scenes = rich_scenes.get("scenes") if isinstance(rich_scenes, dict) else None
    if not isinstance(scenes, list):
        raise ValueError("rich_scenes 缺少 scenes 列表（请先接 B5 场景-对白融合）")
    out = []
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            raise ValueError(f"rich_scenes.scenes[{i}] 必须是对象")
        dlg = sc.get("dialogues") or []
        if isinstance(dlg, list) and dlg:
            out.append(sc)
    return out


def build_batch_prompt(batch: List[Dict[str, Any]], style: str) -> str:
    """批量选择 Prompt（对齐 r2 §5.4 捕获原文）。"""
    lines: List[str] = []
    for sc in batch:
        idx = sc.get("index", 0)
        start = _num(sc.get("start_time", sc.get("start", 0.0)))
        end = _num(sc.get("end_time", sc.get("end", 0.0)))
        shot = str(sc.get("shot_type") or "")
        desc = str(sc.get("visual_description") or "")
        header = f"【场景 {idx}】{start:g}s-{end:g}s"
        if shot:
            header += f" 镜头：{shot}"
        if desc:
            header += f" 画面：{desc}"
        lines.append(header)
        for d in sc.get("dialogues") or []:
            lines.append(f'[ID: {d.get("id", 0)}] "{d.get("text", "")}" ({d.get("start", 0):g}s-{d.get("end", 0):g}s)')
    prompt = (
        "【场景分析任务】\n\n"
        f"你是一个专业的视频剪辑师，正在制作一个\"{style}\"风格的解说视频。\n"
        "请判断下面的台词中，哪些值得保留原声（而不是用解说词覆盖）。\n\n"
        "保留原声的标准：\n"
        "1. 台词本身很有张力、很精彩（如：威胁、告白、反转）\n"
        f"2. 台词能增强\"{style}\"氛围\n"
        "3. 台词是关键剧情点，观众需要听到原声\n"
        "4. 情绪爆发的台词（愤怒/悲伤/惊讶）值得保留\n\n"
        "不应保留：\n"
        "- 日常对话 / 过渡台词 / 背景闲聊\n\n"
        "【候选台词】\n" + "\n".join(lines) + "\n\n"
        "【输出格式】\n"
        '请返回 JSON，不要任何解释：\n'
        '{"selections": [{"scene_index": 0, "keep_dialogue_ids": [0], "reason": "简短说明"}], "summary": "总体说明"}\n'
    )
    return prompt


def parse_selection_response(response: str) -> List[Dict[str, Any]]:
    """解析批量决策响应：兼容 {selections:[...]} 与裸 list（r2 §11 怪癖 7 修复）。"""
    data = _extract_json(response)
    if data is None:
        raise ValueError("keep_original: LLM 输出无 JSON 可解析")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        sel = data.get("selections")
        if isinstance(sel, list):
            return sel
        # 单场景形态 {keep_dialogue_ids, reason} 兜底
        if "keep_dialogue_ids" in data:
            return [data]
    raise ValueError("keep_original: 响应缺少 selections 列表")


def apply_decisions(scenes: List[Dict[str, Any]], selections: List[Dict[str, Any]],
                    warnings: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把 selections 应用到场景：置 keep、写 keep_dialogue_ids、mode→mixed。
    返回 (scenes, decisions_report)。非法条目逐条告警（优于原版整批丢弃）。"""
    by_index: Dict[int, Dict[str, Any]] = {}
    for sc in scenes:
        by_index[int(sc.get("index", 0))] = sc
    decisions: List[Dict[str, Any]] = []
    for raw in selections:
        if not isinstance(raw, dict):
            warnings.append(f"忽略非法决策条目（非对象）: {raw!r}")
            continue
        try:
            si = int(raw.get("scene_index"))
        except (TypeError, ValueError):
            warnings.append(f"忽略非法决策条目（scene_index 缺失/非法）: {raw!r}")
            continue
        sc = by_index.get(si)
        if sc is None:
            warnings.append(f"忽略场景 {si} 的决策：场景不存在")
            continue
        kept = raw.get("keep_dialogue_ids")
        if not isinstance(kept, list):
            warnings.append(f"忽略场景 {si} 的决策：keep_dialogue_ids 非列表")
            continue
        ids = []
        valid = {int(d.get("id", i)) for i, d in enumerate(sc.get("dialogues") or [])}
        for k in kept:
            try:
                kid = int(k)
            except (TypeError, ValueError):
                warnings.append(f"忽略场景 {si} 非法 dialogue id {k!r}")
                continue
            if kid not in valid:
                warnings.append(f"忽略场景 {si} 不存在的 dialogue id {kid}")
                continue
            ids.append(kid)
        ids = sorted(set(ids))
        if not ids:
            decisions.append({"scene_index": si, "keep_dialogue_ids": [], "mode": "narration",
                              "reason": str(raw.get("reason") or "")})
            continue
        for d in sc.get("dialogues") or []:
            d["keep"] = int(d.get("id", 0)) in ids
        sc["keep_dialogue_ids"] = ids
        sc["mode"] = "mixed"
        decisions.append({"scene_index": si, "keep_dialogue_ids": ids,
                          "mode": "mixed", "reason": str(raw.get("reason") or "")})
    return scenes, decisions


class KeepOriginalDecisionNode(NodeBase):
    """B6 保留原声决策。type_id: core/keep_original_decision"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/keep_original_decision",
            title="保留原声决策",
            category="分析",
            description="B6：LLM 按风格挑选值得保留原声的台词（keep_dialogue_ids + 理由）",
            inputs=[
                PortSpec(name="rich_scenes", type=PortType.JSON, label="富场景", required=True),
            ],
            outputs=[
                PortSpec(name="rich_scenes_decided", type=PortType.JSON, label="富场景(已决策)", required=True),
                PortSpec(name="report", type=PortType.JSON, label="决策报告", required=False),
            ],
            params=[
                PortSpec(name="style", type=PortType.STRING, label="解说风格", default="悬疑",
                         widget="select", options=["悬疑", "搞笑", "惊悚", "情感", "热血", "科普", "通用"]),
                PortSpec(name="api_key", type=PortType.STRING, label="API Key", required=False,
                         default="", widget="password", description="支持 @secret:名称 引用保险箱"),
                PortSpec(name="base_url", type=PortType.STRING, label="API 地址", required=False,
                         default="", widget="text"),
                PortSpec(name="model", type=PortType.STRING, label="模型", default="", widget="text"),
                PortSpec(name="max_scenes_per_batch", type=PortType.INT, label="批大小(场景数)",
                         default=50, widget="number"),
                PortSpec(name="retries", type=PortType.INT, label="重试次数", default=2, widget="number"),
                PortSpec(name="temperature", type=PortType.FLOAT, label="温度", default=0.2, widget="number"),
                PortSpec(name="on_llm_fail", type=PortType.STRING, label="LLM 失败策略", default="warn",
                         widget="select", options=["warn", "error"]),
            ],
            version="1.0.0",
        )

    async def _llm(self, prompt: str, api_key: str, base_url: str, model: str,
                   temperature: float, retries: int) -> str:
        import asyncio
        last_err = ""
        for attempt in range(max(1, int(retries) + 1)):
            try:
                return await asyncio.to_thread(
                    _chat_once, [{"role": "user", "content": prompt}],
                    api_key, base_url, model, temperature)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                if attempt < int(retries):
                    delay = BACKOFF[attempt] if attempt < len(BACKOFF) else BACKOFF[-1] * (2 ** (attempt - len(BACKOFF) + 1))
                    await asyncio.sleep(delay)
        raise RuntimeError(f"keep_original: LLM 请求失败（重试 {retries} 次）: {last_err}")

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress")
        asset = inputs.get("rich_scenes")
        if asset is None:
            raise RuntimeError("需要连接 富场景 输入（B5 场景-对白融合 输出）")
        payload = store.read_json(asset.id) or {}
        style = str(params.get("style", "悬疑") or "悬疑")
        api_key = _resolve_secret(ctx, str(params.get("api_key", "") or ""))
        base_url = str(params.get("base_url", "") or "").strip() or ""
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
        if not base_url:
            base_url = ("https://generativelanguage.googleapis.com/v1beta"
                        if str(params.get("provider", "auto") or "auto") == "gemini"
                        else "https://api.openai.com/v1")
        max_batch = max(1, int(params.get("max_scenes_per_batch", 50) or 50))
        retries = max(0, int(params.get("retries", 2) or 2))
        try:
            temperature = float(params.get("temperature", 0.2) or 0.2)
        except (TypeError, ValueError):
            temperature = 0.2
        on_fail = str(params.get("on_llm_fail", "warn") or "warn")

        if progress:
            progress(0.05)
        scenes = payload.get("scenes") if isinstance(payload, dict) else None
        if not isinstance(scenes, list):
            raise RuntimeError("rich_scenes 缺少 scenes 列表（请先接 B5）")
        cand = scenes_with_dialogues(payload)

        warnings: List[str] = []
        decisions: List[Dict[str, Any]] = []
        summary = ""
        llm_calls = 0
        if cand:
            if not api_key:
                if on_fail == "error":
                    raise RuntimeError("请填写 API Key 或用 @secret:名称 引用保险箱")
                warnings.append("未提供 API Key，跳过 LLM 决策（不保留原声）")
            else:
                batches = [cand[i:i + max_batch] for i in range(0, len(cand), max_batch)]
                for bi, batch in enumerate(batches):
                    prompt = build_batch_prompt(batch, style)
                    if progress:
                        progress(min(0.9, 0.1 + (bi + 1) / max(1, len(batches)) * 0.7))
                    try:
                        raw = await self._llm(prompt, api_key, base_url, model, temperature, retries)
                        llm_calls += 1
                        selections = parse_selection_response(raw)
                        scenes, dec = apply_decisions(scenes, selections, warnings)
                        decisions.extend(dec)
                        data = _extract_json(raw)
                        if isinstance(data, dict) and isinstance(data.get("summary"), str):
                            summary = data["summary"]
                    except Exception as e:  # noqa: BLE001
                        if on_fail == "error":
                            raise RuntimeError(f"keep_original: 批次 {bi} 决策失败: {e}") from e
                        warnings.append(f"批次 {bi} LLM/解析失败，该批不保留原声: {e}")

        # 输出：保持 rich_scenes 结构，scenes 已原地写入 keep 标记
        out_payload = dict(payload)
        out_payload["scenes"] = scenes
        out_payload["mode"] = "mixed" if any(sc.get("mode") == "mixed" for sc in scenes) else "narration"
        rich_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"scenes": len(scenes), "mode": out_payload["mode"]}),
            payload=out_payload)
        kept_total = sum(len(sc.get("keep_dialogue_ids") or []) for sc in scenes)
        report = {
            "schema_version": 1,
            "style": style, "model": model, "base_url": base_url,
            "llm_calls": llm_calls,
            "decisions": decisions,
            "summary": summary,
            "kept_dialogues": kept_total,
            "mode": out_payload["mode"],
            "warnings": warnings,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": out_payload["mode"]}), payload=report)
        if progress:
            progress(1.0)
        return {"rich_scenes_decided": rich_asset, "report": report_asset}


def _run_selftest() -> int:
    """纯 stdlib 自测：解析兼容 + 应用决策 + 非法条目告警。"""
    import io, json as _json  # noqa: F401

    scenes = [
        {"index": 0, "start_time": 0.0, "end_time": 10.0, "dialogues": [
            {"id": 0, "text": "你终于来了。", "start": 1.0, "end": 3.0, "keep": False},
            {"id": 1, "text": "我等了很久。", "start": 5.0, "end": 7.5, "keep": False},
        ], "mode": "narration", "keep_dialogue_ids": []},
        {"index": 1, "start_time": 10.0, "end_time": 20.0, "dialogues": [
            {"id": 0, "text": "别过来！", "start": 11.0, "end": 12.0, "keep": False},
        ], "mode": "narration", "keep_dialogue_ids": []},
    ]
    p = {"schema_version": 1, "mode": "narration", "scenes": scenes}
    prompt = build_batch_prompt(scenes, "悬疑")
    assert "你终于来了。" in prompt and "keep_dialogue_ids" in prompt
    # list 形态响应（原版怪癖 7：list 直接崩溃）
    sel = parse_selection_response(
        '[{"scene_index": 0, "keep_dialogue_ids": [0], "reason": "关键台词"},'
        ' {"scene_index": 9, "keep_dialogue_ids": [0], "reason": "场景不存在"}]')
    assert len(sel) == 2
    warnings: List[str] = []
    _, dec = apply_decisions(scenes, sel, warnings)
    assert scenes[0]["mode"] == "mixed" and scenes[0]["keep_dialogue_ids"] == [0]
    assert scenes[0]["dialogues"][0]["keep"] is True and scenes[0]["dialogues"][1]["keep"] is False
    assert scenes[1]["mode"] == "narration"
    assert any("场景 9" in w for w in warnings), "不存在的场景须告警"
    # dict 形态响应
    sel2 = parse_selection_response('{"selections": [{"scene_index": 1, "keep_dialogue_ids": [0], "reason": "情绪爆发"}], "summary": "ok"}')
    _, dec2 = apply_decisions(scenes, sel2, warnings)
    assert scenes[1]["mode"] == "mixed"
    print("[B6 selftest] OK: prompt/解析兼容(list+dict)/应用决策/非法条目告警 均通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_selftest())
