"""场景 × 对白融合节点：把 ASR 的句级台词按时间归属到场景，写回「听觉」字段。

## 为什么需要这个节点

上游 `core/analyze_chunks` 只看得到**静帧**。它的 prompt 要求「对白写作
角色名(情绪副词):"完整台词"（逐字转录）」，但节点手里没有任何音频通道——
这条契约在只有帧的链路上是空的。解说猫之所以能写出带原话的解说词，是因为它
把带音轨的视频整段喂给模型，并且另外跑一遍 Whisper 做句级回填。

本节点补的就是后者：ASR 已经给出「哪一秒到哪一秒说了什么」，这里把它按时间
重叠归属到场景，作为 `audio_content` 的**有据来源**。有了它，下游的保留原声
决策（`core/ost_plan`）、解说词的「这句该留原话还是复述」才有原料。

## 融合策略：默认 fill，不抢模型的活

模型听到的东西比 ASR 多：情绪色、画外音、音效、配乐。所以默认只在模型**没给
听觉信息**时补位（fill）。上游给了就保留，需要叠加时用 merge，需要以 ASR 为准
时用 replace。三种策略都写进 asset.meta，谁覆盖了谁可追溯。

## 跨场景的长句怎么办

一句台词压过两个场景的边界是常态（尤其快切）。只归给重叠比最大的那个场景会
让另一个场景变成无声画面，解说词就会在那里硬编。这里按重叠比**按比例切分文本**，
切点就近吸附到标点，切不动就硬切，并在段上标 `split=true`。

## 输出形状

与 `core/analyze_chunks` 的 descriptions **同构**（每场景一条、带 index/start/end/
shot_type/visual_description/audio_content），所以 `core/ai_narrate` 的 scene_lines
两条路都能吃：接在分析之后是「视觉 + 对白」，接在分析之前是「只有对白」。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, PortType

# 切点候选：中文句读 + 英文标点。切在这些字符**之后**读起来不断气。
_CUTS = "。！？；…，、,.!?;"
_SILENT_TOKENS = {"", "【无】", "【静音】", "【】", "无", "静音", "none", "N/A"}


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _read_payload(store: Any, asset: Any) -> Any:
    """资产 → Python 对象；读不出来返回 None（由调用方决定是报错还是降级）。"""
    if asset is None:
        return None
    try:
        if hasattr(asset, "id"):
            return store.read_json(asset.id)
        return asset
    except Exception:  # noqa: BLE001
        return None


def _norm_scenes(raw: Any) -> List[Dict[str, Any]]:
    """场景列表归一：只认带 start/end 的条目，按时间排序，index 缺失时按位置编号。"""
    if isinstance(raw, dict):
        for key in ("scenes", "items", "segments", "data"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            return []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for pos, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        start = _num(item.get("start"), _num(item.get("start_time"), -1.0))
        end = _num(item.get("end"), _num(item.get("end_time"), -1.0))
        if start < 0 or end <= start:
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            idx = pos
        out.append(dict(item, index=idx, start=round(start, 3), end=round(end, 3)))
    out.sort(key=lambda s: (s["start"], s["end"]))
    return out


def _norm_lines(raw: Any) -> List[Dict[str, Any]]:
    """对白归一：SUBTITLE / SEGMENTS / TRANSCRIPT 三种载荷都接受。"""
    if isinstance(raw, dict):
        for key in ("lines", "items", "segments", "subtitle", "cues", "data"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            return []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("line") or "").strip()
        if not text:
            continue
        start = _num(item.get("start"), -1.0)
        end = _num(item.get("end"), -1.0)
        if start < 0:
            continue
        if end <= start:
            end = start + 0.2
        out.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    out.sort(key=lambda x: (x["start"], x["end"]))
    return out


def _snap(text: str, pos: int, window: int = 6) -> int:
    """把切点吸附到附近标点之后；找不到就硬切。返回值恒在 [1, len-1]。"""
    n = len(text)
    pos = min(max(pos, 1), n - 1)
    for delta in range(0, window + 1):
        for cand in (pos + delta, pos - delta):
            if 1 <= cand <= n - 1 and text[cand - 1] in _CUTS:
                return cand
    return pos


def _split_text(text: str, ratios: List[float]) -> List[str]:
    """按比例切分文本；比例个数 = 目标段数。空段不返回。"""
    n = len(text)
    if n == 0:
        return []
    if len(ratios) <= 1:
        return [text]
    total = sum(ratios) or 1.0
    bounds = [0]
    acc = 0.0
    for r in ratios[:-1]:
        acc += r / total
        bounds.append(_snap(text, int(round(n * acc))))
    bounds.append(n)
    # 保证边界单调（吸附可能把两个切点吸到同一处）
    for i in range(1, len(bounds)):
        if bounds[i] <= bounds[i - 1]:
            bounds[i] = min(n, bounds[i - 1] + 1)
    out: List[str] = []
    for i in range(len(bounds) - 1):
        part = text[bounds[i]:bounds[i + 1]].strip()
        if part:
            out.append(part)
    return out


class DialogueFuseNode(NodeBase):
    """type_id: core/dialogue_fuse"""

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress")

        scenes = _norm_scenes(_read_payload(store, inputs.get("scenes")))
        if not scenes:
            raise RuntimeError("场景列表为空：本节点需要镜头切分产出的 start/end 边界，"
                               "没有时间轴就无法把台词归属到画面")
        lines = _norm_lines(_read_payload(store, inputs.get("subtitle")))
        if not lines:
            raise RuntimeError("对白列表为空：请把语音转写节点（core/asr_whisper）的 subtitle "
                               "接到本节点的 subtitle 端口；没有台词时不需要这个节点")

        desc_raw = _read_payload(store, inputs.get("desc"))
        desc_list = desc_raw if isinstance(desc_raw, list) else []
        desc_by_idx: Dict[int, Dict[str, Any]] = {}
        for pos, d in enumerate(desc_list):
            if not isinstance(d, dict):
                continue
            key = d.get("index")
            if key is None:
                key = d.get("scene_index", pos)
            try:
                desc_by_idx[int(key)] = d
            except (TypeError, ValueError):
                continue

        try:
            min_ratio = float(params.get("min_overlap_ratio", 0.15) or 0.0)
        except (TypeError, ValueError):
            min_ratio = 0.15
        min_ratio = min(max(min_ratio, 0.0), 1.0)
        try:
            merge_gap = max(float(params.get("merge_gap", 0.35) or 0.0), 0.0)
        except (TypeError, ValueError):
            merge_gap = 0.35
        strategy = str(params.get("audio_strategy", "fill") or "fill").strip().lower()
        if strategy not in ("fill", "merge", "replace"):
            strategy = "fill"
        unmatched_policy = str(params.get("unmatched_policy", "nearest") or "nearest").strip().lower()
        if unmatched_policy not in ("nearest", "drop"):
            unmatched_policy = "nearest"
        speaker_prefix = bool(params.get("speaker_prefix", True))
        try:
            max_lines = int(params.get("max_lines_per_scene", 0) or 0)
        except (TypeError, ValueError):
            max_lines = 0

        warnings: List[str] = []
        per_scene: Dict[int, List[Dict[str, Any]]] = {s["index"]: [] for s in scenes}
        matched = 0
        unmatched = 0
        split_lines = 0

        if progress:
            progress(0.05)

        for li, line in enumerate(lines):
            dur = max(line["end"] - line["start"], 1e-6)
            cand: List[tuple] = []
            for sc in scenes:
                ov = min(line["end"], sc["end"]) - max(line["start"], sc["start"])
                if ov > 1e-6:
                    cand.append((sc["index"], ov / dur))
            cand.sort(key=lambda x: -x[1])
            keep = [(idx, r) for idx, r in cand if r >= min_ratio]

            if not keep:
                unmatched += 1
                if unmatched_policy == "drop":
                    continue
                # nearest：归给时间上最近的场景（中点距离），标注未达阈值
                mid = (line["start"] + line["end"]) / 2.0
                best_sc = min(scenes, key=lambda s: abs((s["start"] + s["end"]) / 2.0 - mid))
                keep = [(best_sc["index"], 0.0)]
                warnings.append("第 %d 句「%s」与任何场景重叠不足 %.2f，按时间就近归到场景 %d"
                                % (li + 1, line["text"][:14], min_ratio, best_sc["index"]))
            else:
                matched += 1

            if len(keep) > 1:
                parts = _split_text(line["text"], [r for _, r in keep])
                for (idx, r), part in zip(keep, parts):
                    per_scene[idx].append({"start": line["start"], "end": line["end"],
                                           "text": part, "overlap": round(r, 3),
                                           "split": True})
                split_lines += 1
            else:
                idx = keep[0][0]
                per_scene[idx].append({"start": line["start"], "end": line["end"],
                                       "text": line["text"], "overlap": round(keep[0][1], 3),
                                       "split": False})

        if progress:
            progress(0.6)

        items: List[Dict[str, Any]] = []
        covered = 0
        for pos, sc in enumerate(scenes):
            idx = sc["index"]
            rows = sorted(per_scene.get(idx, []), key=lambda x: (x["start"], x["end"]))
            if max_lines > 0 and len(rows) > max_lines:
                dropped = len(rows) - max_lines
                rows = rows[:max_lines]
                warnings.append("场景 %d 的台词超过上限 %d 条，丢弃 %d 条（调大"
                                "「每场景台词上限」可保留）" % (idx, max_lines, dropped))
            # 拼听觉文本：间隔大于 merge_gap 用 / 断开，读起来才有停顿感
            chunks: List[str] = []
            prev_end: Optional[float] = None
            for r in rows:
                if prev_end is not None and r["start"] - prev_end > merge_gap:
                    chunks.append("/")
                chunks.append('对白: "%s"' % r["text"] if speaker_prefix else r["text"])
                prev_end = max(prev_end or r["end"], r["end"])
            asr_audio = " ".join(chunks).replace(" / ", " / ").strip()
            asr_audio = re.sub(r"\s*/\s*$", "", asr_audio)

            up = desc_by_idx.get(idx, {})
            up_audio = str(up.get("audio_content") or "").strip()
            up_visual = str(up.get("visual_description") or up.get("description")
                            or up.get("text") or "").strip()
            up_shot = str(up.get("shot_type") or "").strip()

            if strategy == "replace":
                audio, source = (asr_audio, "asr") if asr_audio else (up_audio, "model")
            elif strategy == "merge":
                if asr_audio and up_audio and up_audio.strip() not in _SILENT_TOKENS:
                    audio, source = "%s｜%s" % (up_audio, asr_audio), "merged"
                elif asr_audio:
                    audio, source = asr_audio, "asr"
                else:
                    audio, source = up_audio, ("model" if up_audio else "none")
            else:  # fill
                if up_audio.strip() in _SILENT_TOKENS and asr_audio:
                    audio, source = asr_audio, "asr"
                elif up_audio:
                    audio, source = up_audio, "model"
                elif asr_audio:
                    audio, source = asr_audio, "asr"
                else:
                    audio, source = "", "none"

            if rows:
                covered += 1
            items.append({
                "index": idx,
                "start": sc["start"],
                "end": sc["end"],
                "shot_type": up_shot,
                "visual_description": up_visual,
                "audio_content": audio,
                "audio_source": source,
                "scene_indices": [idx],
                "lines": [{"start": r["start"], "end": r["end"], "text": r["text"],
                           "overlap": r["overlap"], "split": r["split"]} for r in rows],
                "meta": {"line_count": len(rows),
                         "visual_source": "upstream" if up_visual else "none"},
            })

        if progress:
            progress(0.9)

        if desc_list and len(desc_list) != len(scenes):
            warnings.append("上游场景描述 %d 条，场景 %d 段：按 index 对齐，缺的描述留空"
                            % (len(desc_list), len(scenes)))
        if not desc_list:
            warnings.append("未接场景描述：输出的视觉字段为空，只供解说词用「对白」参考。"
                            "要画面描述请把 core/analyze_chunks 接在 desc 端口")
        if unmatched and lines:
            warnings.append("%d/%d 句台词与场景重叠不足 %.2f，已按「%s」处理"
                            % (unmatched, len(lines), min_ratio, unmatched_policy))

        coverage = round(covered / max(len(scenes), 1), 3)
        out = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"count": len(items),
                        "scene_count": len(scenes),
                        "line_count": len(lines),
                        "matched_lines": matched,
                        "unmatched_lines": unmatched,
                        "split_lines": split_lines,
                        "covered_scenes": covered,
                        "coverage": coverage,
                        "audio_strategy": strategy,
                        "params": {"min_overlap_ratio": min_ratio, "merge_gap": merge_gap,
                                   "unmatched_policy": unmatched_policy,
                                   "speaker_prefix": speaker_prefix,
                                   "max_lines_per_scene": max_lines},
                        "degraded": bool(unmatched or (not desc_list)),
                        "warnings": list(warnings)}),
            payload=items)
        if progress:
            progress(1.0)
        return {"descriptions": out}
