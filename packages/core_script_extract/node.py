"""解说提纯切分节点：把文案切成适合配音的短句。

两件事，故意做成两步而不是合成的：
  1. 切分 —— 按句末标点切；太长按逗号再切；太短与下一句合并
  2. 提纯 —— 去掉「首先」「其次」「这部电影讲述了」这类套话开头

为什么分开：文案生成出来是给人读的段落，不是给配音读的句子。把「切」和「改」
混在一个函数里，出问题时说不清是切错了还是改坏了——而这两种错的修法完全不同。

**不改写**。命中套话词只是切分或删除，不做同义替换：替换需要语义判断，
那是 LLM 的事。这里是确定性处理，同样的输入永远同样的输出。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, PortType

_SENT_END = "。！？!?；;…"
_COMMA = "，,、：:"
_LINE_SEP = re.compile(r"[\r\n]+")


def _read_text(store: Any, asset: Any) -> str:
    if asset is None:
        raise RuntimeError("缺少文案输入")
    try:
        data = store.read_json(asset.id) if hasattr(asset, "id") else asset
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("文案读取失败: %s" % e)
    if isinstance(data, dict):
        return str(data.get("text") or data.get("script") or "")
    return str(data or "")


def _split_sentences(text: str) -> List[str]:
    out: List[str] = []
    for raw in _LINE_SEP.split(str(text or "")):
        buf = ""
        for ch in raw:
            buf += ch
            if ch in _SENT_END:
                if buf.strip():
                    out.append(buf.strip())
                buf = ""
        if buf.strip():
            out.append(buf.strip())
    return out


def _hard_split(s: str, limit: int) -> List[str]:
    """超过 limit 就在逗号处切；没有逗号就按 limit 硬切。"""
    s = s.strip()
    if len(s) <= limit:
        return [s] if s else []
    pieces: List[str] = []
    rest = s
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for i in range(len(window) - 1, max(limit // 3, 0) - 1, -1):
            if window[i] in _COMMA:
                cut = i + 1
                break
        if cut <= 0:
            cut = limit
        piece = rest[:cut].strip()
        if piece:
            pieces.append(piece)
        rest = rest[cut:].lstrip(_COMMA + " ")
    if rest.strip():
        pieces.append(rest.strip())
    return pieces


def _strip_leads(s: str, leads: List[str]) -> str:
    """去掉开头的套话。只删，不改写。"""
    cur = s.lstrip()
    changed = True
    while changed and cur:
        changed = False
        for w in leads:
            if not w:
                continue
            if cur.startswith(w):
                rest = cur[len(w):].lstrip("，,、：: 。. ")
                # 「本片中的配乐」去掉「本片中」会剩下「的配乐」——结构助词跟着删掉，
                # 否则句子开头是个光秃秃的「的」。只删一层，不多删。
                if rest[:1] in ("的", "里", "中", "上", "下"):
                    rest = rest[1:].lstrip("，,、：: 。. ")
                if rest:
                    cur = rest
                    changed = True
                    break
    return cur.strip()


def _merge_short(items: List[str], min_chars: int) -> List[str]:
    """短句并进下一句；末句太短就并进上一句。"""
    out: List[str] = []
    carry = ""
    for s in items:
        s = (carry + s).strip() if carry else s
        carry = ""
        if len(s) < min_chars:
            carry = s
            continue
        out.append(s)
    if carry:
        if out:
            out[-1] = (out[-1] + carry).strip()
        else:
            out.append(carry)
    return out


class ScriptExtractNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        text = _read_text(store, inputs.get("script"))
        if not text.strip():
            raise RuntimeError("文案为空，没有可切分的内容")

        try:
            min_chars = int(params.get("min_chars", 8) or 8)
        except (TypeError, ValueError):
            min_chars = 8
        min_chars = min(max(min_chars, 2), 200)
        try:
            max_chars = int(params.get("max_chars", 30) or 30)
        except (TypeError, ValueError):
            max_chars = 30
        max_chars = max(max_chars, min_chars + 2)

        strip = bool(params.get("strip_leads", True))
        leads = [w.strip() for w in str(params.get("lead_words") or "").split(",") if w.strip()]

        warnings: List[str] = []
        raw_sents = _split_sentences(text)

        pieces: List[str] = []
        long_split = 0
        for s in raw_sents:
            if strip and leads:
                before = s
                s = _strip_leads(s, leads)
                if s != before and not s:
                    continue
                if s != before:
                    warnings.append("去掉套话：%s → %s" % (before[:12], s[:12]))
            parts = _hard_split(s, max_chars)
            if len(parts) > 1:
                long_split += 1
            pieces.extend(parts)

        merged = _merge_short(pieces, min_chars)
        merged = [m for m in merged if m]

        if not merged:
            raise RuntimeError("切分后没有剩下有效句子（可能整段都是套话）")

        removed = len(raw_sents) - len([x for x in raw_sents if _strip_leads(x, leads)] if strip else raw_sents)
        meta = {
            "count": len(merged),
            "chars": sum(len(x) for x in merged),
            "source_sentences": len(raw_sents),
            "long_split": long_split,
            "stripped": max(removed, 0),
            "min_chars": min_chars,
            "max_chars": max_chars,
            "warnings": warnings[:20],
            "degraded": False,
        }
        payload = {"text": "\n".join(merged), "lines": merged, "count": len(merged)}
        asset = store.save_asset(
            Asset(id="", kind=PortType.SCRIPT.value, meta=meta), payload=payload)
        return {"narration": asset}
