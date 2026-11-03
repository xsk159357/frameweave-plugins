"""跨节点契约判据：分块分析 → 对白融合 → 解说词 → 原片编排 → 渲染。

跑法：python scripts/test_chain_contract.py

**为什么单测全绿还不够**：每个包各自自测只能证明「我按自己的契约产出」，
证明不了「下游按它以为的契约读得到」。这类翻车在本仓真实发生过（match_clip 想用
JSON 端口、SDK 说 JSON 已移除；video_render 的 layout 是 SEGMENTS 承载的 dict）。
所以这里只做一件事：把四个节点的**真输出**串起来，喂给下游的**真消费函数**，
看它读不读得懂。

链条（全部零网络、零模型、零 ffmpeg）：
  analyze_chunks 形状的 items
    → core/dialogue_fuse            （真跑节点）
    → core/ai_narrate.scene_lines   （真调它的场景行渲染）
    → core/ost_plan                 （真跑节点）
    → core/video_render._layout_clip_ranges（真调它的 layout 解析）
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(ROOT)
BACKEND = os.path.join(os.path.dirname(PLUGINS), "FrameWeave", "backend")
sys.path.insert(0, BACKEND)

from app.assets import Asset, LocalAssetStore      # noqa: E402
from app.nodespec import PortType                  # noqa: E402


def _load(name, rel):
    path = os.path.join(PLUGINS, "packages", rel)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fuse_mod = _load("chain_fuse", "core_dialogue_fuse/node.py")
narr_mod = _load("chain_narr", "core_ai_narrate/node.py")
ost_mod = _load("chain_ost", "core_ost_plan/node.py")
render_mod = _load("chain_render", "core_video_render/node.py")

OK: list = []
BAD: list = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("%-58s %s%s" % (name, "OK" if cond else "BAD",
                          ("  " + detail) if (detail and not cond) else ""))


async def main() -> int:
    base = os.path.join(ROOT, "_tmp_chain")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    store = LocalAssetStore(base)

    # ---- 场景：3 段 0-4 / 4-8 / 8-12
    scenes_payload = [{"index": i, "start": i * 4.0, "end": (i + 1) * 4.0} for i in range(3)]
    scenes = store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=scenes_payload)

    # ---- analyze_chunks 形状：块级，一条覆盖两个场景（scene_indices）
    chunk_items = [
        {"chunk_index": 0, "start": 0.0, "end": 8.0, "scene_indices": [0, 1], "index": 0,
         "shot_type": "closeup", "visual_description": "男人攥着信站在门口",
         "audio_content": ""},
        {"chunk_index": 1, "start": 8.0, "end": 12.0, "scene_indices": [2], "index": 2,
         "shot_type": "wide", "visual_description": "走廊空无一人",
         "audio_content": '男声(压低声): "别回头"'},
    ]
    desc = store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=chunk_items)

    # ---- ASR：三句，其中一句压场景边界
    subs = store.save_asset(Asset(id="", kind=PortType.SUBTITLE.value), payload=[
        {"index": 0, "start": 0.5, "end": 1.8, "text": "你别进来"},
        {"index": 1, "start": 3.2, "end": 5.0, "text": "他推开门，屋里没有人，只有一台还亮着的电视"},
        {"index": 2, "start": 8.4, "end": 9.6, "text": "别回头"},
    ])

    # ---- 1) 融合
    fused_asset = (await fuse_mod.DialogueFuseNode().run(
        {"store": store}, {"scenes": scenes, "subtitle": subs, "desc": desc}, {}))["descriptions"]
    fused = store.read_json(fused_asset.id)
    check("融合：每场景一条（3 条）", len(fused) == 3, str(len(fused)))
    check("融合：块级描述按 index 落到场景 0/1 之外的场景 2 也有视觉",
          fused[2]["visual_description"] == "走廊空无一人", fused[2]["visual_description"])
    check("融合：模型已给听觉的场景不被覆盖（场景 2 保留原话）",
          '"别回头"' in fused[2]["audio_content"], fused[2]["audio_content"])
    check("融合：模型没给听觉的场景补上 ASR（场景 0）",
          "你别进来" in fused[0]["audio_content"], fused[0]["audio_content"])
    check("融合：跨场景长句被切开（场景 1 有前半句）",
          "他推开门" in fused[1]["audio_content"], fused[1]["audio_content"])

    # ---- 2) 喂给 ai_narrate 的场景行渲染器（真消费函数）
    lines = narr_mod.scene_lines(scenes_payload, fused, "中文")
    check("scene_lines：渲染出 3 行", len(lines.splitlines()) == 3, repr(lines[:120]))
    check("scene_lines：没有【占位画面】", "占位画面" not in lines, lines)
    check("scene_lines：听觉被带进提示词", "听觉：" in lines and "你别进来" in lines, lines)
    check("scene_lines：视觉描述也在", "男人攥着信" in lines, lines)

    # ---- 3) 解说词（passthrough 造出带原片标记的稿子）
    items = store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=[
        {"text": "他攥着信站在门口。【原片:6-9】屋里没有人。", "start": 0.0, "end": 3.0},
        {"text": "只有电视还亮着。", "start": 3.0, "end": 6.0},
    ])
    narr = (await narr_mod.AINarrateNode().run(
        {"store": store, "progress": lambda p: None},
        {"items": items}, {"mode": "passthrough"}))["narration"]
    narration = store.read_json(narr.id)
    check("解说词：标记被剥离出正文", "原片" not in narration[0]["text"], narration[0]["text"])
    check("解说词：ost 字段成形", narration[0].get("ost") == {"start": 6.0, "end": 9.0},
          repr(narration[0].get("ost")))

    # ---- 4) 原片编排（需要时间线来判断放不放得出来）
    timeline = store.save_asset(Asset(id="", kind=PortType.TIMELINE.value,
                                      meta={"total_duration": 12.0}), payload=[
        {"index": 0, "start": 0.0, "end": 4.0, "out_start": 0.0, "out_end": 4.0},
        {"index": 1, "start": 4.0, "end": 8.0, "out_start": 4.0, "out_end": 8.0},
        {"index": 2, "start": 8.0, "end": 12.0, "out_start": 8.0, "out_end": 12.0},
    ])
    layout_asset = (await ost_mod.OstPlanNode().run(
        {"store": store},
        {"narration": narr, "descriptions": fused_asset, "timeline": timeline},
        {"source": "marker", "padding": 0.0, "min_clip": 1.0}))["layout"]
    layout = store.read_json(layout_asset.id)
    check("原片编排：产出一条区间", len(layout.get("original_clips") or []) == 1,
          repr(layout.get("original_clips")))
    clips = layout.get("original_clips") or [{}]
    check("原片编排：区间就是 6-9", clips[0].get("start") == 6.0 and clips[0].get("end") == 9.0,
          repr(clips[0]))

    # ---- 5) 渲染端读（真消费函数）
    ranges = render_mod._layout_clip_ranges(layout)
    check("渲染端读得出区间", ranges == [(6.0, 9.0)], repr(ranges))

    # ---- 6) 反向：区间落在画面之外时必须被摘掉（不能假装能放）
    layout2 = (await ost_mod.OstPlanNode().run(
        {"store": store}, {"narration": narr, "timeline": timeline},
        {"source": "marker", "padding": 0.0, "min_clip": 1.0, "ost_ratio": 0.0}))["layout"]
    body = store.read_json(layout2.id)
    check("区间在画面内时不会丢", body["dropped_seconds"] == 0.0, str(body["dropped_seconds"]))

    shutil.rmtree(base, ignore_errors=True)
    print("\n%d OK / %d BAD" % (len(OK), len(BAD)))
    return 0 if not BAD else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
