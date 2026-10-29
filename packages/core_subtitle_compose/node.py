"""字幕装配节点：将 ASR 字幕 + 样式参数合成为带花字/动画的样式化字幕。"""
from __future__ import annotations
import json
import os
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, PortType

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

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        sub_asset = inputs.get("subtitle")
        script_asset = inputs.get("script")
        manual = str(params.get("manual_text", "") or "").strip()
        if sub_asset is None and not manual:
            # P2-10 修正：script 输入被消费 —— 无字幕资产时以文案分句生成字幕（仍可手动覆盖）
            script_text = ""
            if script_asset is not None:
                try:
                    data = ctx["store"].read_json(script_asset.id) if hasattr(script_asset, "id") else script_asset
                    script_text = str(data.get("text", "") if isinstance(data, dict) else (data or ""))
                except Exception:  # noqa: BLE001
                    script_text = ""
            if not script_text.strip():
                raise RuntimeError("缺少字幕输入（连接字幕、填写手动字幕或提供文案 script）")
            subs = []
            t = 0.0
            for line in script_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                dur = max(len(line) * 0.45, 1.2)
                subs.append({"start": round(t, 3), "end": round(t + dur, 3), "text": line})
                t += dur
        elif manual:
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

        # FW-DEV-58：空集合不许「既空又无痕」。
        # 不失败（避免破坏既有工作流），但必须让下游能看出「这次没字幕」：
        #   degraded=true + warnings 明写原因，落在 asset.meta（v3 已无 report 端口）。
        warnings: List[str] = []
        degraded = False
        if not styled:
            degraded = True
            warnings.append("输入字幕为空，输出空字幕（styled_subtitle 为空列表）")

        store = ctx["store"]
        out = store.save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value,
                  meta={"count": len(styled), "styled": True,
                        "degraded": degraded, "warnings": list(warnings)}),
            payload=styled)
        return {"styled_subtitle": out}
