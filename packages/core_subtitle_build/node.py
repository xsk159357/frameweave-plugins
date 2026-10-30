"""字幕生成节点：按时间线生成字幕。

它替换的不是 subtitle_compose 的样式能力，而是**时长的来源**。
subtitle_compose 按 0.45s/字 自己估时长，与 timeline_build 算出来的对不上
（实测同一条：字幕 8.1s、画面 3.0s）。这里直接读 timeline 的 out_start/out_end，
两边天然一致——不会有第二套估算。

拆行按字数比例分时间，不做均匀分配：一个 8 秒的六字句均匀分给两行，
每行都会空着等，读起来像卡带。
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, PortType

# 断行优先在这些字符之后（句读 > 停顿 > 空格）
_SOFT_BREAK = "。！？!?；;…，,、 "
_HARD_LIMIT = 400          # 单条原文超过这个长度就整段截断，防止一行字幕几百字


def _as_list(store: Any, asset: Any) -> List[Dict[str, Any]]:
    if asset is None:
        raise RuntimeError("缺少时间线输入")
    try:
        data = store.read_json(asset.id) if hasattr(asset, "id") else asset
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("时间线读取失败: %s" % e)
    if isinstance(data, dict):
        for key in ("items", "segments", "timeline", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not data:
        raise RuntimeError("时间线为空或格式不对（期望数组）")
    return data


def _split_by_width(text: str, width: int, max_lines: int) -> List[str]:
    """把一段文本切成若干「条」，每条不超过 width*max_lines 个字。

    优先在句读处断开，找不到就硬切；硬切位置会尽量往前找最近的空白。
    """
    text = str(text or "").strip()
    if not text:
        return []
    cap = max(width * max_lines, width, 1)
    chunks: List[str] = []
    rest = text[:_HARD_LIMIT]
    while rest:
        if len(rest) <= cap:
            chunks.append(rest)
            break
        cut = -1
        window = rest[:cap]
        for i in range(len(window) - 1, max(cap // 2, 0) - 1, -1):
            if window[i] in _SOFT_BREAK:
                cut = i + 1
                break
        if cut <= 0:
            # 没有合适断点：在窗口里找最后一个空白，再不行就硬切
            sp = window.rfind(" ")
            cut = sp + 1 if sp > cap // 2 else cap
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return [c for c in chunks if c]


def _lines_of(chunk: str, width: int) -> List[str]:
    """把一条切成多行（每行不超过 width）。返回行列表，供前端按行渲染。"""
    if len(chunk) <= width:
        return [chunk]
    lines: List[str] = []
    rest = chunk
    while rest:
        if len(rest) <= width:
            lines.append(rest)
            break
        cut = -1
        window = rest[:width]
        for i in range(len(window) - 1, max(width // 2, 0) - 1, -1):
            if window[i] in _SOFT_BREAK:
                cut = i + 1
                break
        if cut <= 0:
            sp = window.rfind(" ")
            cut = sp + 1 if sp > width // 2 else width
        lines.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return [x for x in lines if x]


class SubtitleBuildNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        rows = _as_list(store, inputs.get("timeline"))

        try:
            width = int(params.get("max_chars_per_line", 16) or 16)
        except (TypeError, ValueError):
            width = 16
        width = min(max(width, 4), 60)
        try:
            max_lines = int(params.get("max_lines", 2) or 2)
        except (TypeError, ValueError):
            max_lines = 2
        max_lines = min(max(max_lines, 1), 6)
        try:
            min_dur = float(params.get("min_duration", 0.5) or 0.5)
        except (TypeError, ValueError):
            min_dur = 0.5
        try:
            max_dur = float(params.get("max_duration", 8.0) or 8.0)
        except (TypeError, ValueError):
            max_dur = 8.0
        max_dur = max(max_dur, min_dur)

        warnings: List[str] = []
        out: List[Dict[str, Any]] = []
        squashed = 0

        for it in rows:
            text = str(it.get("text") or "").strip()
            if not text:
                continue
            try:
                s = float(it.get("out_start"))
                e = float(it.get("out_end"))
            except (TypeError, ValueError):
                # 老时间线只有 start/end（源片时间）：能跑就跑，但要说清楚
                try:
                    s = float(it.get("start"))
                    e = float(it.get("end"))
                    warnings.append("第 %s 条没有 out_start/out_end，回落到源片时间"
                                    % it.get("index"))
                except (TypeError, ValueError):
                    continue
            if e <= s:
                e = s + min_dur

            chunks = _split_by_width(text, width, max_lines)
            if not chunks:
                continue
            total_chars = sum(len(c) for c in chunks) or 1
            span = e - s
            cursor = s
            for ci, chunk in enumerate(chunks):
                share = len(chunk) / total_chars
                seg = span * share
                last = ci == len(chunks) - 1
                if seg < min_dur:
                    seg = min(min_dur, max(span, min_dur))
                    squashed += 1
                if seg > max_dur:
                    seg = max_dur
                    squashed += 1
                seg_start = round(cursor, 3)
                seg_end = round(e if last else min(cursor + seg, e), 3)
                if seg_end - seg_start < 0.05:
                    seg_end = round(seg_start + 0.05, 3)
                out.append({
                    "index": len(out),
                    "line_index": it.get("index"),
                    "start": seg_start,
                    "end": seg_end,
                    "duration": round(seg_end - seg_start, 3),
                    "text": chunk,
                    "lines": _lines_of(chunk, width),
                })
                cursor = seg_end

        if not out:
            raise RuntimeError("时间线里没有可用文本，生成不了字幕")
        if squashed:
            warnings.append("%d 条因最短/最长显示限制被调整时长" % squashed)

        total = round(sum(x["duration"] for x in out), 3)
        asset = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value,
                  meta={"count": len(out), "total_duration": total,
                        "max_chars_per_line": width, "max_lines": max_lines,
                        "from": "out_start/out_end", "warnings": list(warnings),
                        "degraded": bool(squashed)}),
            payload=out)
        return {"subtitle": asset}
