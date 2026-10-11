"""翻译节点：中文文案/字幕 → 多语种（英/日/韩/西/阿 + 小语种）。

- 输入：文案草稿(script) 或 字幕(subtitle)，输出：翻译文案 / 翻译字幕
- 走 llm.py 云端 chat（用户自带 Key，ameaao 中转）
- api_key 支持 @secret:名称 引用保险箱
"""
from __future__ import annotations
import json
import os
from typing import Any, Dict

from app.assets import Asset
from app.llm import chat
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app.registry import register

LANGS = [
    ("英文", "English"), ("日文", "Japanese"), ("韩文", "Korean"),
    ("西班牙语", "Spanish"), ("阿拉伯语", "Arabic"), ("法语", "French"),
    ("德语", "German"), ("俄语", "Russian"), ("葡萄牙语", "Portuguese"),
    ("意大利语", "Italian"), ("越南语", "Vietnamese"), ("泰语", "Thai"),
    ("印尼语", "Indonesian"), ("马来语", "Malay"), ("缅甸语", "Burmese"),
    ("印地语", "Hindi"), ("土耳其语", "Turkish"), ("乌克兰语", "Ukrainian"),
]


class TranslateNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/translate",
            title="多语种翻译",
            category="剪辑",
            description="中文文案/字幕翻译为多语种（含小语种），输出可接配音/字幕",
            inputs=[
                PortSpec(name="script", type=PortType.SCRIPT, label="文案草稿", required=False),
                PortSpec(name="subtitle", type=PortType.SUBTITLE, label="字幕", required=False),
            ],
            outputs=[
                PortSpec(name="translated_script", type=PortType.SCRIPT, label="翻译文案"),
                PortSpec(name="translated_subtitle", type=PortType.SUBTITLE, label="翻译字幕"),
            ],
            params=[
                PortSpec(name="target", type=PortType.STRING, label="目标语言", default="英文",
                         widget="select", options=[l for l, _ in LANGS]),
                PortSpec(name="keep_style", type=PortType.BOOL, label="保留解说风格", default=True,
                         widget="toggle"),
                PortSpec(name="api_key", type=PortType.STRING, label="API Key",
                         widget="text", description="用户自带 Key（本机保险箱加密）"),
                PortSpec(name="base_url", type=PortType.STRING, label="中转地址",
                         default="https://ameaaos.com/v1", widget="text"),
                PortSpec(name="model", type=PortType.STRING, label="模型", default="gpt-4o", widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        script_asset = inputs.get("script")
        sub_asset = inputs.get("subtitle")
        if script_asset is None and sub_asset is None:
            raise RuntimeError("需要连接文案草稿或字幕输入")

        api_key = str(params.get("api_key", "") or "").strip()
        if api_key.startswith("@secret:"):
            from app.secrets import read_secret
            api_key = read_secret(api_key.split(":", 1)[1]) or ""
        if not api_key:
            raise RuntimeError("请填写 API Key，或在密钥保险箱存好后用 @secret:名称 引用")
        base_url = str(params.get("base_url", "https://ameaaos.com/v1")).strip() or "https://ameaaos.com/v1"
        model = str(params.get("model", "gpt-4o")).strip() or "gpt-4o"
        target_label = params.get("target", "英文")
        en_name = dict(LANGS).get(target_label, "English")
        keep_style = bool(params.get("keep_style", True))

        store = ctx["store"]

        # 翻译文案
        out_script = None
        if script_asset is not None:
            src = _read_text(store, script_asset)
            prompt = (
                f"将以下中文解说文案翻译成{en_name}。"
                + ("保持解说口吻与节奏，口语化，不要直译腔。" if keep_style else "准确直译即可。")
                + "只输出译文，不要解释。\n\n" + src
            )
            try:
                result = chat([{"role": "user", "content": prompt}], api_key, base_url, model,
                              temperature=0.4, max_tokens=4096)
            except Exception as e:
                raise RuntimeError("翻译失败: %s" % e)
            out_script = store.save_asset(
                Asset(id="", kind=PortType.SCRIPT.value, meta={"lang": target_label, "src": "translate"}),
                payload={"text": result.strip(), "lang": target_label})

        # 翻译字幕（批量：一次翻译全部文本，按行回填）
        out_sub = None
        if sub_asset is not None:
            subs = store.read_json(sub_asset.id) or []
            lines = [(i, s.get("text", "")) for i, s in enumerate(subs) if s.get("text")]
            if lines:
                numbered = "\n".join(f"[{i}] {t}" for i, t in lines)
                prompt = (
                    f"将以下中文字幕逐条翻译成{en_name}。保留 [编号] 前缀和行顺序，"
                    + "只输出翻译结果，格式与输入一致（每行 [编号] 译文）。\n\n" + numbered
                )
                try:
                    result = chat([{"role": "user", "content": prompt}], api_key, base_url, model,
                                  temperature=0.3, max_tokens=4096)
                except Exception as e:
                    raise RuntimeError("字幕翻译失败: %s" % e)
                # 回填
                translated: Dict[int, str] = {}
                for line in result.splitlines():
                    m = line.strip()
                    if m.startswith("["):
                        try:
                            idx = int(m[m.index("[") + 1:m.index("]")])
                            text = m[m.index("]") + 1:].strip()
                            translated[idx] = text
                        except (ValueError, IndexError):
                            continue
                out_subs = []
                for i, s in enumerate(subs):
                    ns = dict(s)
                    if i in translated:
                        ns["text"] = translated[i]
                        ns["lang"] = target_label
                    out_subs.append(ns)
                out_sub = store.save_asset(
                    Asset(id="", kind=PortType.SUBTITLE.value, meta={"lang": target_label}),
                    payload=out_subs)
            else:
                # 无文本字幕
                out_sub = store.save_asset(
                    Asset(id="", kind=PortType.SUBTITLE.value, meta={"lang": target_label}),
                    payload=subs)

        return {"translated_script": out_script, "translated_subtitle": out_sub}


def _read_text(store, asset) -> str:
    """读取文案资产文本。"""
    payload = store.read_json(asset.id)
    if payload is None:
        return ""
    if isinstance(payload, dict):
        return str(payload.get("text", "") or payload)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)
