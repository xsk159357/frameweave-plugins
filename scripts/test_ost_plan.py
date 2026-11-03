"""core/ost_plan 判据：原片标记 → 渲染端认的 layout 区间。

跑法：python scripts/test_ost_plan.py

最要紧的一条判据不是「我算得对」，而是**渲染端读得懂**。所以最后一个用例直接
把 core/video_render 的 `_layout_clip_ranges` 加载进来喂本节点的 payload——
两个包各自自测全绿、拼起来接不上，是这类契约最典型的翻车方式。

其余判据：
  1. 标记从哪来：结构化 ost 字段 / 正文【原片:s-e】两种写法都要认；
  2. 补位有据：没有标记时按台词密度补，补出来的必须标 reason=dialogue；
  3. 放不出来的先摘掉：落在画面之外的原声算 dropped_seconds 并告警，不静默。

反面（必须红）：
  · 既没解说词也没描述 → 报错
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
PKG = os.path.join(PLUGINS, "packages", "core_ost_plan")
RENDER = os.path.join(PLUGINS, "packages", "core_video_render", "node.py")
sys.path.insert(0, BACKEND)

from app.assets import Asset, LocalAssetStore      # noqa: E402
from app.nodespec import PortType                  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load("cw_ost", os.path.join(PKG, "node.py"))
_rd = _load("cw_render", RENDER)
Node = _mod.OstPlanNode

OK: list = []
BAD: list = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("%-58s %s%s" % (name, "OK" if cond else "BAD",
                          ("  " + detail) if (detail and not cond) else ""))


def make_store(tag):
    d = os.path.join(ROOT, "_tmp_ost_%s" % tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return LocalAssetStore(d), d


async def call(store, inputs, params=None):
    return await Node().run({"store": store}, inputs, params or {})


def mk_narr(store, rows):
    return store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=rows)


def mk_desc(store, rows):
    return store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=rows)


def mk_timeline(store, rows, total):
    return store.save_asset(Asset(id="", kind=PortType.TIMELINE.value,
                                  meta={"total_duration": total}), payload=rows)


async def main() -> int:
    # ------------------------------------------------------- 1 标记：结构化字段
    store, d1 = make_store("marker")
    narr = mk_narr(store, [
        {"index": 0, "text": "他推开门", "start": 0.0, "end": 3.0},
        {"index": 1, "text": "屋里没人", "start": 3.0, "end": 6.0,
         "ost": {"start": 12.0, "end": 16.0}},
    ])
    out = await call(store, {"narration": narr}, {"padding": 0.2, "min_clip": 1.0})
    lay = store.read_json(out["layout"].id)
    check("结构化 ost 被采纳", len(lay["original_clips"]) == 1,
          str(len(lay["original_clips"])))
    c = lay["original_clips"][0]
    check("padding 已并入区间（12-0.2 / 16+0.2）",
          abs(c["start"] - 11.8) < 1e-6 and abs(c["end"] - 16.2) < 1e-6,
          "%s-%s" % (c["start"], c["end"]))
    check("reason=marker", c["reason"] == "marker", c["reason"])

    # ------------------------------------------------- 2 标记：正文里的文字标记
    store, d2 = make_store("textmark")
    narr = mk_narr(store, [
        {"index": 0, "text": "他愣住了。【原片:30-34】然后转身就走。", "start": 0.0, "end": 6.0},
        {"index": 1, "text": "门在身后关上。", "start": 6.0, "end": 9.0},
    ])
    out = await call(store, {"narration": narr}, {"padding": 0.0, "min_clip": 1.0})
    lay = store.read_json(out["layout"].id)
    check("正文标记被解析出来", len(lay["original_clips"]) == 1,
          str(lay["original_clips"]))
    c = lay["original_clips"][0] if lay["original_clips"] else {}
    check("正文标记区间正确(30-34)", c.get("start") == 30.0 and c.get("end") == 34.0,
          repr(c))

    # ------------------------------------------------------- 3 台词密度补位
    store, d3 = make_store("dialogue")
    narr = mk_narr(store, [{"index": 0, "text": "他推开门", "start": 0.0, "end": 3.0},
                           {"index": 1, "text": "屋里没人", "start": 0.0, "end": 3.0},
                           {"index": 2, "text": "只剩下风声", "start": 3.0, "end": 6.0}])
    desc = mk_desc(store, [
        {"index": 0, "start": 0.0, "end": 4.0,
         "audio_content": '对白: "别进来"',
         "lines": [{"start": 0.5, "end": 1.6, "text": "你别进来，千万别进来"}]},
        {"index": 1, "start": 4.0, "end": 8.0,
         "audio_content": "",
         "lines": [{"start": 4.5, "end": 5.0, "text": "嗯"}]},
    ])
    out = await call(store, {"narration": narr, "descriptions": desc},
                     {"source": "dialogue", "ost_ratio": 0.2, "padding": 0.0,
                      "min_clip": 0.5})
    lay = store.read_json(out["layout"].id)
    reasons = [c["reason"] for c in lay["original_clips"]]
    check("台词密度补位产出区间", len(lay["original_clips"]) >= 1, str(len(lay["original_clips"])))
    check("补位的 reason=dialogue", "dialogue" in reasons, str(reasons))
    check("台词多的场景排在前面（起点 0.5 附近）",
          any(abs(c["start"] - 0.5) < 0.2 for c in lay["original_clips"]),
          repr([(c["start"], c["end"]) for c in lay["original_clips"]]))

    # ------------------------------------------------- 4 夹到时间线：放不出的摘掉
    store, d4 = make_store("clamp")
    narr = mk_narr(store, [
        {"index": 0, "text": "a", "start": 0.0, "end": 3.0, "ost": {"start": 5.0, "end": 9.0}},
        {"index": 1, "text": "b", "start": 3.0, "end": 6.0, "ost": {"start": 40.0, "end": 44.0}},
    ])
    tl = mk_timeline(store, [
        {"index": 0, "start": 0.0, "end": 3.0, "out_start": 0.0, "out_end": 3.0},
        {"index": 1, "start": 3.0, "end": 6.0, "out_start": 3.0, "out_end": 6.0},
    ], total=6.0)
    out = await call(store, {"narration": narr, "timeline": tl},
                     {"padding": 0.0, "min_clip": 0.5})
    lay = store.read_json(out["layout"].id)
    check("越界片段被摘掉（只剩 1 段）", len(lay["original_clips"]) == 1,
          str(lay["original_clips"]))
    c = lay["original_clips"][0] if lay["original_clips"] else {}
    check("夹到时间线覆盖范围内（5-6）",
          c.get("start") == 5.0 and abs(c.get("end") - 6.0) < 1e-6, repr(c))
    check("dropped_seconds 记了 4s", abs(lay["dropped_seconds"] - 4.0) < 1e-6,
          str(lay["dropped_seconds"]))
    check("丢弃有告警",
          any("不在任何画面片段里" in w for w in out["layout"].meta["warnings"]),
          repr(out["layout"].meta["warnings"]))

    # ------------------------------------------------------------- 5 相邻合并
    store, d5 = make_store("merge")
    narr = mk_narr(store, [
        {"index": 0, "text": "a", "start": 0.0, "end": 3.0, "ost": {"start": 10.0, "end": 12.0}},
        {"index": 1, "text": "b", "start": 3.0, "end": 6.0, "ost": {"start": 12.1, "end": 14.0}},
    ])
    out = await call(store, {"narration": narr},
                     {"padding": 0.0, "min_clip": 0.5, "merge_gap": 0.5})
    lay = store.read_json(out["layout"].id)
    check("间隔小于 merge_gap 合成一段", len(lay["original_clips"]) == 1,
          str(lay["original_clips"]))
    check("合并后区间 10-14",
          lay["original_clips"][0]["start"] == 10.0 and lay["original_clips"][0]["end"] == 14.0,
          repr(lay["original_clips"]))

    # --------------------------------------------------------- 6 最短长度补长
    store, d6 = make_store("short")
    narr = mk_narr(store, [{"index": 0, "text": "a", "start": 0.0, "end": 3.0,
                            "ost": {"start": 20.0, "end": 20.4}}])
    out = await call(store, {"narration": narr}, {"padding": 0.0, "min_clip": 1.5})
    lay = store.read_json(out["layout"].id)
    check("短于下限被补到 1.5s",
          abs(lay["original_clips"][0]["duration"] - 1.5) < 1e-6,
          str(lay["original_clips"][0]["duration"]))
    check("补长有告警",
          any("短于下限" in w for w in out["layout"].meta["warnings"]),
          repr(out["layout"].meta["warnings"]))

    # ----------------------------------------------------- 7 与渲染端的真契约
    ranges = _rd._layout_clip_ranges(lay)
    check("渲染端 _layout_clip_ranges 读得出区间", len(ranges) == 1, repr(ranges))
    if ranges:
        a, b = ranges[0]
        check("渲染端读到的区间与写出的一致(20-21.5)",
              abs(a - 20.0) < 1e-6 and abs(b - 21.5) < 1e-6, repr(ranges))

    # ------------------------------------------------------------------ 8 反面
    store, d8 = make_store("neg")
    try:
        await call(store, {"narration": None, "descriptions": None})
        check("无解无词必须报错", False)
    except RuntimeError as e:
        check("无解无词必须报错", "至少要一个输入" in str(e), str(e))

    for d in (d1, d2, d3, d4, d5, d6, d8):
        shutil.rmtree(d, ignore_errors=True)

    print("\n%d OK / %d BAD" % (len(OK), len(BAD)))
    return 0 if not BAD else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
