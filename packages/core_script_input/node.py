"""文案输入节点：手工填写或粘贴解说文案，切成逐句文本。

它是流程的另一个入口（第一个是 media_import）。做这个节点不是为了「凑齐节点数」，
而是因为配画面、生成字幕都要一段文案——现在没有一个地方能把用户脑子里的解说词
交给流程。没有它，整条链只能靠外部脚本注入资产。

切分规则与下游一致：先按换行，再按中文句末标点。同一个规则在两处实现过一次就够，
所以这里切完之后，下游看到的就是「一句一条」。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, PortType

_LINE_SEP = re.compile(r"[\r\n]+")
_SENT_END = re.compile(r"(?<=[。！？!?；;…])")


def split_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in _LINE_SEP.split(str(text or "")):
        for part in _SENT_END.split(raw):
            p = part.strip()
            if p:
                out.append(p)
    return out


class ScriptInputNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        raw = str(params.get("text") or "")
        if not str(raw).strip():
            raise RuntimeError("文案为空：请填写「文案正文」参数")

        if bool(params.get("keep_empty_lines", False)):
            lines: List[str] = []
            for raw_line in _LINE_SEP.split(raw):
                if not raw_line.strip():
                    lines.append("")
                    continue
                lines.extend(split_lines(raw_line))
        else:
            lines = split_lines(raw)

        body = [x for x in lines if x]
        if not body:
            raise RuntimeError("文案切分后没有有效句子：可能只有标点或空白")

        payload = {"text": "\n".join(body), "lines": body, "count": len(body)}
        store = ctx["store"]
        out = store.save_asset(
            Asset(id="", kind=PortType.SCRIPT.value,
                  meta={"count": len(body), "chars": sum(len(x) for x in body)}),
            payload=payload)
        return {"script": out}
