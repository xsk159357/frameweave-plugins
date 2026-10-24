# -*- coding: utf-8 -*-
"""对白字级细切节点（user/jsm_dialogue_refine）。

能力（对应解说猫 dialogue_refiner 语义，独立重写并修复原版缺陷）：
- 输入契约：word_timestamps = [[start_ms, end_ms, word], ...]（毫秒，相对 trim_start）；
  兼容 {word_timestamps:[...]}、{words:[...]}、直接列表三种形态，统一归一化。
- 严格校验：非法条目显式抛错（原版 words/dict 形态静默返回原样，属隐形 bug）。
- 切分：相邻字间隔 next.start - prev.end ≥ 阈值 → 切分；毫秒→秒输出。
- 自适应阈值：按字均时长/语速映射缩放系数（0.7~1.8），修复原版恒 base×1.5。
- 后处理：过短残段并入前段；无停顿超长段按字数硬切。
- 批量：标准 batch envelope，条目级隔离、checkpoint、并发、重试。
"""
from __future__ import annotations
import json
import os
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

try:
    from app import batchlib
except Exception:  # noqa: BLE001
    batchlib = None


# ---------------------------------------------------------------- 契约校验/归一化
def normalize_word_timestamps(value: Any) -> List[List[Any]]:
    """把三种输入形态归一为 [[start_ms,end_ms,word], ...]；非法显式抛错。"""
    data = value
    if isinstance(data, dict):
        for key in ("word_timestamps", "words", "timestamps"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ValueError("word_timestamps 输入是对象但缺少 word_timestamps/words 列表键")
    if not isinstance(data, list):
        raise ValueError("word_timestamps 必须是列表（[[start_ms,end_ms,word],...]）")
    out: List[List[Any]] = []
    for i, item in enumerate(data):
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            raise ValueError(f"word_timestamps[{i}] 必须是 [start_ms, end_ms, word] 三元组")
        start_ms, end_ms, word = item[0], item[1], item[2]
        try:
            start_ms = float(start_ms)
            end_ms = float(end_ms)
        except (TypeError, ValueError):
            raise ValueError(f"word_timestamps[{i}] 时间戳必须为数字: {item!r}")
        if end_ms < start_ms:
            raise ValueError(f"word_timestamps[{i}] end_ms({end_ms}) < start_ms({start_ms})")
        out.append([start_ms, end_ms, str(word)])
    return out


# ---------------------------------------------------------------- 自适应阈值（修复原版恒 1.5×）
def calculate_adaptive_threshold(dialogue_words: List[List[Any]], base_threshold: float) -> float:
    """按字均时长（语速）缩放阈值：慢速放大、快速缩小。

    原版恒返回 base×1.5（r2 §11-3 缺陷）；本实现依据真实字均时长映射：
      avg_ms = mean(end-start)，典型汉语单字约 250ms；
      系数 = clamp(avg_ms / 250, 0.7, 1.8)。
    空字表时按默认语速返回 base×1.0（不再凭空 ×1.5）。
    """
    if not dialogue_words:
        return base_threshold
    durs = [e - s for s, e, _w in dialogue_words if (e - s) > 0]
    if not durs:
        return base_threshold
    avg_ms = sum(durs) / len(durs)
    factor = max(0.7, min(1.8, avg_ms / 250.0))
    return base_threshold * factor


# ---------------------------------------------------------------- 切分
def split_dialogue_by_pause(word_timestamps: List[List[Any]], pause_threshold: float,
                            trim_start: float = 0.0, min_segment_chars: int = 1,
                            max_segment_chars: int = 60) -> List[Dict[str, Any]]:
    """字级停顿细切 → [{text,start,end,duration,words:[...]}]（秒，相对视频时间）。"""
    if not word_timestamps:
        return []
    segments: List[Dict[str, Any]] = []
    cur: List[List[Any]] = [word_timestamps[0]]
    for i in range(1, len(word_timestamps)):
        prev = word_timestamps[i - 1]
        cur_word = word_timestamps[i]
        gap_s = (cur_word[0] - prev[1]) / 1000.0
        if gap_s >= pause_threshold:
            segments.append(cur)
            cur = [cur_word]
        else:
            cur.append(cur_word)
    if cur:
        segments.append(cur)

    # 组装（毫秒→秒，减 trim_start）
    out: List[Dict[str, Any]] = []
    for seg in segments:
        text = "".join(w for _, _, w in seg)
        start_ms = seg[0][0]
        end_ms = seg[-1][1]
        out.append({
            "text": text,
            "start": round(max(0.0, start_ms / 1000.0 - trim_start), 3),
            "end": round(max(0.0, end_ms / 1000.0 - trim_start), 3),
            "duration": round(max(0.0, end_ms - start_ms) / 1000.0, 3),
            "words": seg,
        })

    # 后处理：过短残段并入前段
    if min_segment_chars >= 2 and len(out) > 1:
        merged: List[Dict[str, Any]] = []
        for seg in out:
            if merged and len(seg["text"]) < min_segment_chars:
                prev = merged[-1]
                prev["text"] += seg["text"]
                prev["end"] = seg["end"]
                prev["duration"] = round(prev["end"] - prev["start"], 3)
                prev["words"] = prev.get("words", []) + seg.get("words", [])
            else:
                merged.append(seg)
        out = merged

    # 无停顿超长段按字数硬切
    final: List[Dict[str, Any]] = []
    for seg in out:
        if len(seg["text"]) <= max_segment_chars:
            final.append(seg)
            continue
        words = seg.get("words") or []
        for j in range(0, len(seg["text"]), max_segment_chars):
            chunk_text = seg["text"][j:j + max_segment_chars]
            if not chunk_text:
                continue
            chunk_words = [w for w in words if _word_span_in(w, j, j + max_segment_chars, seg["text"])]
            if not chunk_words:
                final.append({"text": chunk_text, "start": seg["start"], "end": seg["end"],
                              "duration": seg["duration"], "words": []})
                continue
            st = chunk_words[0][0]
            en = chunk_words[-1][1]
            final.append({
                "text": chunk_text,
                "start": round(max(0.0, st / 1000.0 - trim_start), 3),
                "end": round(max(0.0, en / 1000.0 - trim_start), 3),
                "duration": round(max(0.0, en - st) / 1000.0, 3),
                "words": chunk_words,
            })
    return final


def _word_span_in(word: List[Any], c_start: int, c_end: int, full_text: str) -> bool:
    """近似判断字级词条是否落在 [c_start,c_end) 字符区间（按全文匹配位置）。"""
    idx = full_text.find(str(word[2]), c_start)
    return 0 <= idx < c_end


# ---------------------------------------------------------------- 单条处理
def _process_payload(payload: Dict[str, Any], pause_threshold: float,
                     adaptive: bool, trim_start: float, min_segment_chars: int,
                     max_segment_chars: int) -> Dict[str, Any]:
    """处理单个载荷：{word_timestamps:[...]} 或 {dialogues:[{...}]}。"""
    if "dialogues" in payload and isinstance(payload.get("dialogues"), list):
        dialogues = payload["dialogues"]
        per_dialogue: List[Dict[str, Any]] = []
        stats: List[Dict[str, Any]] = []
        for d in dialogues:
            if not isinstance(d, dict):
                raise ValueError("dialogue 必须是对象")
            wt = normalize_word_timestamps(d.get("word_timestamps", []))
            thr = calculate_adaptive_threshold(wt, pause_threshold) if adaptive else float(pause_threshold)
            segs = split_dialogue_by_pause(wt, thr, trim_start, min_segment_chars, max_segment_chars)
            did = d.get("id", d.get("row_id", str(len(per_dialogue))))
            for si, seg in enumerate(segs):
                per_dialogue.append({
                    "id": f"{did}_{si}",
                    "parent_id": str(did),
                    "segment_index": si,
                    "is_refined": True,
                    "keep": bool(d.get("keep", False)),
                    "start": seg["start"], "end": seg["end"],
                    "duration": seg["duration"], "text": seg["text"],
                })
            stats.append({
                "dialogue_id": str(did), "words": len(wt),
                "threshold": round(thr, 3), "segments": len(segs),
                "pauses": max(0, len(segs) - 1),
            })
        return {"refined": per_dialogue, "stats": stats,
                "mode": "dialogues", "count": len(per_dialogue)}

    wt = normalize_word_timestamps(payload.get("word_timestamps", payload))
    thr = calculate_adaptive_threshold(wt, pause_threshold) if adaptive else float(pause_threshold)
    segs = split_dialogue_by_pause(wt, thr, trim_start, min_segment_chars, max_segment_chars)
    refined = [{
        "id": f"0_{i}", "parent_id": "0", "segment_index": i,
        "is_refined": True, "keep": False,
        "start": s["start"], "end": s["end"], "duration": s["duration"], "text": s["text"],
    } for i, s in enumerate(segs)]
    return {"refined": refined, "stats": [{"dialogue_id": "0", "words": len(wt),
                                           "threshold": round(thr, 3), "segments": len(refined),
                                           "pauses": max(0, len(refined) - 1)}],
            "mode": "single", "count": len(refined)}


class DialogueRefineNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_dialogue_refine",
            title="对白字级细切",
            category="语义",
            description="字级时间戳停顿检测切分长对白（语速自适应阈值，严格校验输入）",
            inputs=[
                PortSpec(name="word_timestamps", type=PortType.JSON, label="字级时间戳", required=False),
                PortSpec(name="dialogues", type=PortType.JSON, label="对话列表", required=False),
                PortSpec(name="items", type=PortType.JSON, label="批量条目", required=False),
            ],
            outputs=[
                PortSpec(name="refined", type=PortType.SUBTITLE, label="细切字幕"),
                PortSpec(name="report", type=PortType.JSON, label="细切报告"),
                PortSpec(name="results", type=PortType.JSON, label="批量结果"),
            ],
            params=[
                PortSpec(name="pause_threshold", type=PortType.FLOAT, label="基础停顿阈值(秒)",
                         default=2.0, widget="number"),
                PortSpec(name="adaptive", type=PortType.BOOL, label="语速自适应阈值",
                         default=True, widget="toggle"),
                PortSpec(name="trim_start", type=PortType.FLOAT, label="起始裁剪(秒)",
                         default=0.0, widget="number"),
                PortSpec(name="min_segment_chars", type=PortType.INT, label="最小段字数",
                         default=1, widget="number"),
                PortSpec(name="max_segment_chars", type=PortType.INT, label="最大段字数",
                         default=60, widget="number"),
                PortSpec(name="max_concurrent", type=PortType.INT, label="批量并发",
                         default=2, widget="number"),
                PortSpec(name="retries", type=PortType.INT, label="重试次数",
                         default=1, widget="number"),
                PortSpec(name="resume", type=PortType.BOOL, label="断点续跑",
                         default=True, widget="toggle"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        wt_asset = inputs.get("word_timestamps")
        dlg_asset = inputs.get("dialogues")
        items_asset = inputs.get("items")
        store = ctx["store"]

        pause_threshold = float(params.get("pause_threshold", 2.0) or 2.0)
        adaptive = bool(params.get("adaptive", True))
        trim_start = float(params.get("trim_start", 0.0) or 0.0)
        min_segment_chars = max(1, int(params.get("min_segment_chars", 1) or 1))
        max_segment_chars = max(10, int(params.get("max_segment_chars", 60) or 60))
        concurrency = max(1, int(params.get("max_concurrent", 2) or 2))
        retries = max(0, int(params.get("retries", 1) or 0))
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
                        raw_items.append({"word_timestamps": it})
            elif isinstance(data, list):
                raw_items = [x if isinstance(x, dict) else {"word_timestamps": x} for x in data]
        if dlg_asset is not None or wt_asset is not None or not raw_items:
            payload: Dict[str, Any] = {}
            d = _read(dlg_asset)
            if isinstance(d, list):
                payload["dialogues"] = d
            w = _read(wt_asset)
            if w is not None and "dialogues" not in payload:
                payload["word_timestamps"] = w
            if payload or not raw_items:
                raw_items.append(payload)

        if not raw_items:
            raise RuntimeError("缺少输入：请连接字级时间戳、对话列表或批量条目")

        cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                          str(ctx.get("node_id", "dialogue_refine")) + ".json")

        async def worker(item: Dict[str, Any]) -> Dict[str, Any]:
            payload = item.get("input")
            if not isinstance(payload, dict):
                raise RuntimeError("条目输入必须是对象")
            out = _process_payload(payload, pause_threshold, adaptive,
                                   trim_start, min_segment_chars, max_segment_chars)
            return out

        if batchlib is not None:
            batch = batchlib.new_batch(raw_items, source="user/jsm_dialogue_refine",
                                       options={"threshold": pause_threshold})
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
        all_refined: List[Dict[str, Any]] = []
        all_stats: List[Dict[str, Any]] = []
        for it in batch["items"]:
            if it.get("status") == "succeeded" and isinstance(it.get("result"), dict):
                r = it["result"]
                all_refined.extend(r.get("refined", []))
                all_stats.extend(r.get("stats", []))
                if first is None:
                    first = r
        if first is None:
            errs = [str(it.get("error")) for it in batch["items"] if it.get("status") == "failed"]
            raise RuntimeError("全部条目失败: " + "; ".join(errs[:3]))

        refined_asset = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value, meta={"count": len(all_refined)}),
            payload=all_refined)
        report = {
            "count": len(all_refined),
            "dialogues": all_stats,
            "threshold": pause_threshold,
            "adaptive": adaptive,
            "batch": batch.get("batch_id"),
        }
        report_asset = store.save_asset(Asset(id="", kind=PortType.JSON.value), payload=report)
        results_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"count": len(batch["items"]), "ok": sum(1 for i in batch["items"] if i.get("status") == "succeeded")}),
            payload={"batch_id": batch.get("batch_id"), "items": batch["items"]})
        return {"refined": refined_asset, "report": report_asset, "results": results_asset}
