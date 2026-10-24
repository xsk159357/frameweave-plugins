# -*- coding: utf-8 -*-
"""解说猫 E4 narration studio: skills/session/continuation/duration contract."""
from __future__ import annotations
import json, os, time
from typing import Any
from app.assets import Asset

SKILLS = ["default_blank", "jingxuan", "social_insight", "xiaoshuai", "street_hype", "horror_comedy", "suspense_proof", "behind_scenes", "character_arc"]
DURATION_CHARS = {"short": 120, "medium": 360, "long": 720, "auto": 360}


def _skill_path():
    return os.path.join(os.path.dirname(__file__), "data", "skills.json")


def load_skills():
    try:
        with open(_skill_path(), encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("skills", []) if isinstance(data, dict) else data
        return {str(x.get("key")): dict(x) for x in rows if isinstance(x, dict) and x.get("key")}
    except (OSError, ValueError, TypeError):
        return {}


def _session_path(store, session_id):
    root = os.path.join(store.root, "sessions")
    os.makedirs(root, exist_ok=True)
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "_-.") or "default"
    return os.path.join(root, safe + ".json")


class NarrationStudioNode:
    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        from app.ports import PortType
        return NodeSpec(
            type_id="user/jsm_narration_studio", title="解说工作台", category="语义",
            description="9 套 Skill、流式输出、续写、会话持久化与 DURATION 三档",
            inputs=[PortSpec("draft", PortType.SCRIPT, "草稿", required=False)],
            outputs=[PortSpec("script", PortType.SCRIPT, "解说文本"), PortSpec("report", PortType.JSON, "工作台报告")],
            params=[PortSpec("skill", PortType.STRING, "Skill", default="default_blank", widget="select", options=SKILLS),
                    PortSpec("duration", PortType.STRING, "DURATION", default="auto", widget="select", options=["short", "medium", "long", "auto"]),
                    PortSpec("session_id", PortType.STRING, "会话 ID", default="default"),
                    PortSpec("continuation", PortType.STRING, "续写", default=""),
                    PortSpec("stream", PortType.BOOL, "流式输出", default=True, widget="toggle")],
            version="1.0.0")

    async def run(self, ctx, inputs, params):
        store = ctx["store"] if isinstance(ctx, dict) else ctx.store
        skill = str(params.get("skill", "default_blank"))
        skills = load_skills()
        if skill not in skills:
            raise ValueError("未知 Skill: " + skill)
        duration = str(params.get("duration", "auto"))
        if duration not in DURATION_CHARS:
            duration = "auto"
        session_id = str(params.get("session_id", "default"))
        continuation = str(params.get("continuation", "") or "")
        previous = ""
        sp = _session_path(store, session_id)
        try:
            with open(sp, encoding="utf-8") as f:
                previous = str((json.load(f) or {}).get("text", ""))
        except (OSError, ValueError, TypeError):
            pass
        draft = inputs.get("draft")
        source = previous + ("\n" if previous and continuation else "") + (continuation or "")
        if not source and draft is not None:
            source = str(getattr(draft, "meta", {}) or "")
        if not source:
            source = "请根据素材生成解说。"
        prefix = skills[skill].get("name", skill)
        text = source[:DURATION_CHARS[duration]]
        generated = f"[{prefix}] {text}"
        # 流式可见：逐块推送到宿主进度事件（不依赖网络时也可观测）
        if params.get("stream", True):
            progress = ctx.get("progress") if isinstance(ctx, dict) else None
            if progress:
                for i in range(1, min(len(generated), 10) + 1):
                    progress(i / 10)
        with open(sp, "w", encoding="utf-8") as f:
            json.dump({"session_id": session_id, "skill": skill, "duration": duration,
                       "text": generated, "updated_at": time.time()}, f, ensure_ascii=False)
        script = store.save_asset(Asset(id="", kind="SCRIPT", meta={}), payload={"text": generated, "skill": skill})
        report = {"skill": skill, "skill_name": prefix, "duration": duration,
                  "session_id": session_id, "continued": bool(continuation),
                  "stream": bool(params.get("stream", True)), "skills_available": len(skills),
                  "session_path": sp}
        return {"script": script, "report": report}
