"""字幕装配节点：将 ASR 字幕 + 样式参数合成为带花字/动画的样式化字幕。"""
from __future__ import annotations
import json
import os
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app.registry import register

STYLE_PRESETS = {
    "默认": {"font": "微软雅黑", "size": 48, "color": "#FFFFFF", "stroke": "#000000",
             "stroke_width": 2, "animation": "none", "bold": True},
    "悬疑惊悚": {"font": "思源宋体", "size": 44, "color": "#E8E8E8", "stroke": "#1A1A1A",
                "stroke_width": 2, "animation": "typewriter", "bold": False},
    "搞笑鬼畜": {"font": "站酷快乐体", "size": 56, "color": "#FFD700", "stroke": "#C0392B",
                "stroke_width": 3, "animation": "bounce", "bold": True},
    "知识科普": {"font": "思源黑体", "size": 42, "color": "#00E5FF", "stroke": "#0D47A1",
                "stroke_width": 2, "animation": "fade", "bold": False},
    "情感讲述": {"font": "方正静蕾简体", "size": 46, "color": "#FFB6C1", "stroke": "#8B0000",
                "stroke_width": 2, "animation": "fadeup", "bold": False},
}


class SubtitleComposeNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/subtitle_compose",
            title="字幕装配",
            category="输出",
            description="字幕高级样式：花字/特效/逐句卡点动画",
            inputs=[
                PortSpec(name="subtitle", type=PortType.SUBTITLE, label="字幕", required=True),
                PortSpec(name="script", type=PortType.SCRIPT, label="文案(可选)", required=False),
            ],
            outputs=[
                PortSpec(name="styled_subtitle", type=PortType.SUBTITLE, label="样式化字幕"),
            ],
            params=[
                PortSpec(name="manual_text", type=PortType.STRING, label="手动字幕",
                         default="", widget="text",
                         description="每行一条，可用 | 指定起始秒，如: 0.2|第一句字幕"),
                PortSpec(name="preset", type=PortType.STRING, label="样式预设", default="默认",
                         widget="select", options=list(STYLE_PRESETS.keys())),
                PortSpec(name="font", type=PortType.STRING, label="字体", default="微软雅黑", widget="text"),
                PortSpec(name="size", type=PortType.INT, label="字号", default=48, widget="number"),
                PortSpec(name="color", type=PortType.STRING, label="字色", default="#FFFFFF", widget="text"),
                PortSpec(name="stroke", type=PortType.STRING, label="描边色", default="#000000", widget="text"),
                PortSpec(name="animation", type=PortType.STRING, label="动画", default="none",
                         widget="select", options=["none", "typewriter", "bounce", "fade", "fadeup"]),
                PortSpec(name="karaoke", type=PortType.BOOL, label="逐字卡点", default=True),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        sub_asset = inputs.get("subtitle")
        manual = str(params.get("manual_text", "") or "").strip()
        if sub_asset is None and not manual:
            raise RuntimeError("缺少字幕输入（连接字幕或填写手动字幕）")
        if manual:
            subs = []
            t = 0.0
            for line in manual.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "|" in line:
                    s, txt = line.split("|", 1)
                    try:
                        st = float(s.strip())
                    except ValueError:
                        st = t
                else:
                    st = t
                    txt = line
                dur = max(len(txt) * 0.45, 1.2)  # 按字数估读速
                subs.append({"start": st, "end": st + dur, "text": txt.strip()})
                t = st + dur
        else:
            subs = ctx["store"].read_json(sub_asset.id) or []
            if not isinstance(subs, list):
                raise RuntimeError("字幕格式错误")

        preset = STYLE_PRESETS.get(params.get("preset", "默认"), STYLE_PRESETS["默认"])
        style = {
            "font": params.get("font", preset["font"]),
            "size": int(params.get("size", preset["size"]) or preset["size"]),
            "color": params.get("color", preset["color"]),
            "stroke": params.get("stroke", preset["stroke"]),
            "stroke_width": preset["stroke_width"],
            "animation": params.get("animation", preset["animation"]),
            "bold": preset["bold"],
            "karaoke": bool(params.get("karaoke", True)),
        }

        styled = []
        for s in subs:
            styled.append({**s, "style": style})

        store = ctx["store"]
        out = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value, meta={"count": len(styled), "styled": True}),
            payload=styled)
        return {"styled_subtitle": out}
