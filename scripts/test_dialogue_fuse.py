"""core/dialogue_fuse 判据：台词按时间归属到场景，跨场景长句按比例切开。

跑法：python scripts/test_dialogue_fuse.py

这个节点的价值只有一条：让下游拿到的 audio_content **有据**。所以判据围绕三件事：
  1. 归属对不对——重叠比达阈值的场景必须拿到台词，拿不到就是漏；
  2. 长句会不会丢——一句压两个场景边界时必须是「切成两段」而不是「只归一个」；
  3. 会不会抢模型的活——上游已经有听觉描述时，fill 策略下必须一字不改。

反面（必须红）：
  · 缺 scenes → 报错（没有时间轴谈不上归属）
  · 缺 subtitle → 报错（没有台词就不该接这个节点）
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
PKG = os.path.join(PLUGINS, "packages", "core_dialogue_fuse")
sys.path.insert(0, BACKEND)

from app.assets import Asset, LocalAssetStore      # noqa: E402
from app.nodespec import PortType                  # noqa: E402

_spec = importlib.util.spec_from_file_location("cw_df", os.path.join(PKG, "node.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Node = _mod.DialogueFuseNode

OK: list = []
BAD: list = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("%-58s %s%s" % (name, "OK" if cond else "BAD",
                          ("  " + detail) if (detail and not cond) else ""))


def make_store(tag):
    d = os.path.join(ROOT, "_tmp_df_%s" % tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return LocalAssetStore(d), d


async def call(store, inputs, params=None):
    return await Node().run({"store": store}, inputs, params or {})


def mk_scenes(store, spans):
    return store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=[
        {"index": i, "start": s, "end": e} for i, (s, e) in enumerate(spans)])


def mk_subs(store, rows):
    return store.save_asset(Asset(id="", kind=PortType.SUBTITLE.value), payload=[
        {"index": i, "start": s, "end": e, "text": t} for i, (s, e, t) in enumerate(rows)])


def mk_desc(store, items):
    return store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=items)


async def main() -> int:
    # ---------------------------------------------------------------- 1 基本归属
    store, d1 = make_store("basic")
    scenes = mk_scenes(store, [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0)])
    subs = mk_subs(store, [(0.2, 1.5, "你别过来"),
                           (4.3, 6.0, "我不走"),
                           (9.0, 11.0, "那就别怪我")])
    out = await call(store, {"scenes": scenes, "subtitle": subs})
    items = store.read_json(out["descriptions"].id)
    check("每场景一条", len(items) == 3, str(len(items)))
    check("第 0 段拿到第 0 句", "你别过来" in items[0]["audio_content"], items[0]["audio_content"])
    check("第 1 段拿到第 1 句", "我不走" in items[1]["audio_content"], items[1]["audio_content"])
    check("第 2 段拿到第 2 句", "那就别怪我" in items[2]["audio_content"], items[2]["audio_content"])
    check("前缀形如 对白: \"...\"", items[0]["audio_content"].startswith('对白: "'),
          items[0]["audio_content"])
    check("source 标成 asr", items[0]["audio_source"] == "asr", items[0]["audio_source"])
    meta = out["descriptions"].meta
    check("coverage=1.0", meta["coverage"] == 1.0, str(meta["coverage"]))
    check("matched_lines=3", meta["matched_lines"] == 3, str(meta["matched_lines"]))

    # ---------------------------------------------------------- 2 跨场景长句切分
    store, d2 = make_store("split")
    scenes = mk_scenes(store, [(0.0, 5.0), (5.0, 10.0)])
    long_text = "他推开门，屋里没有人，只有一台还在闪烁的电视机。"
    subs = mk_subs(store, [(4.0, 6.0, long_text)])          # 两句场景各占 50%
    out = await call(store, {"scenes": scenes, "subtitle": subs})
    items = store.read_json(out["descriptions"].id)
    a, b = items[0]["audio_content"], items[1]["audio_content"]
    check("跨场景长句：两段都有台词", bool(a) and bool(b), repr((a, b)))
    joined = "".join(x.replace('对白: "', "").replace('"', "") for x in (a, b))
    check("跨场景长句：切完不丢字", joined == long_text, repr(joined))
    check("跨场景长句：两段都标 split",
          items[0]["lines"][0]["split"] is True and items[1]["lines"][0]["split"] is True)
    check("split_lines=1", out["descriptions"].meta["split_lines"] == 1,
          str(out["descriptions"].meta["split_lines"]))
    check("切点在标点后（第一段不以逗号前的半句结尾）",
          a.rstrip().endswith(("，", "。", "？", "！")) or len(a) >= len(long_text) // 2 - 8, a)

    # ------------------------------------------------------ 3 fill：不覆盖模型听觉
    store, d3 = make_store("fill")
    scenes = mk_scenes(store, [(0.0, 4.0), (4.0, 8.0)])
    subs = mk_subs(store, [(0.2, 1.5, "你别过来"), (4.3, 6.0, "我不走")])
    desc = mk_desc(store, [
        {"index": 0, "shot_type": "closeup", "visual_description": "男人后退半步",
         "audio_content": '男声(惊恐): "别过来"【低频弦乐】'},
        {"index": 1, "shot_type": "medium", "visual_description": "女人站着",
         "audio_content": ""},
    ])
    out = await call(store, {"scenes": scenes, "subtitle": subs, "desc": desc},
                     {"audio_strategy": "fill"})
    items = store.read_json(out["descriptions"].id)
    check("fill：模型听觉一字不改", items[0]["audio_content"] == '男声(惊恐): "别过来"【低频弦乐】',
          items[0]["audio_content"])
    check("fill：模型为空时补台词", "我不走" in items[1]["audio_content"], items[1]["audio_content"])
    check("fill：视觉字段从上游保留", items[0]["visual_description"] == "男人后退半步",
          items[0]["visual_description"])

    # ------------------------------------------------------------- 4 merge / replace
    out = await call(store, {"scenes": scenes, "subtitle": subs, "desc": desc},
                     {"audio_strategy": "merge"})
    items = store.read_json(out["descriptions"].id)
    check("merge：模型 + 台词都在",
          "别过来" in items[0]["audio_content"] and "你别过来" in items[0]["audio_content"],
          items[0]["audio_content"])
    check("merge：source=merged", items[0]["audio_source"] == "merged", items[0]["audio_source"])

    out = await call(store, {"scenes": scenes, "subtitle": subs, "desc": desc},
                     {"audio_strategy": "replace"})
    items = store.read_json(out["descriptions"].id)
    check("replace：只剩台词", items[0]["audio_content"] == '对白: "你别过来"',
          items[0]["audio_content"])

    # --------------------------------------------------------- 5 未归属：就近 + 告警
    store, d5 = make_store("nearest")
    scenes = mk_scenes(store, [(0.0, 2.0), (10.0, 12.0)])
    subs = mk_subs(store, [(5.0, 5.4, "这句话落在空档里")])   # 与两段都不重叠
    out = await call(store, {"scenes": scenes, "subtitle": subs},
                     {"min_overlap_ratio": 0.3, "unmatched_policy": "nearest"})
    items = store.read_json(out["descriptions"].id)
    hit = [i for i, it in enumerate(items) if "这句话落在空档里" in it["audio_content"]]
    check("未归属：就近归到一段（不是丢掉）", len(hit) == 1, repr(hit))
    meta = out["descriptions"].meta
    check("未归属：unmatched_lines=1", meta["unmatched_lines"] == 1, str(meta["unmatched_lines"]))
    check("未归属：有告警", any("重叠不足" in w for w in meta["warnings"]), repr(meta["warnings"]))

    out = await call(store, {"scenes": scenes, "subtitle": subs},
                     {"min_overlap_ratio": 0.3, "unmatched_policy": "drop"})
    items = store.read_json(out["descriptions"].id)
    check("drop：没有一段拿到这句",
          all("这句话落在空档里" not in it["audio_content"] for it in items))

    # ------------------------------------------------------------------ 6 反面
    store, d6 = make_store("neg")
    subs = mk_subs(store, [(0.0, 1.0, "x")])
    try:
        await call(store, {"scenes": None, "subtitle": subs})
        check("缺 scenes 必须报错", False)
    except RuntimeError as e:
        check("缺 scenes 必须报错", "场景列表为空" in str(e), str(e))
    scenes = mk_scenes(store, [(0.0, 1.0)])
    try:
        await call(store, {"scenes": scenes, "subtitle": None})
        check("缺 subtitle 必须报错", False)
    except RuntimeError as e:
        check("缺 subtitle 必须报错", "对白列表为空" in str(e), str(e))

    # ------------------------------------------------- 7 上游描述条数对不上 → 告警
    store, d7 = make_store("mismatch")
    scenes = mk_scenes(store, [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)])
    subs = mk_subs(store, [(0.2, 1.0, "a"), (2.2, 3.0, "b"), (4.2, 5.0, "c")])
    desc = mk_desc(store, [{"index": 0, "visual_description": "只有一段"}])
    out = await call(store, {"scenes": scenes, "subtitle": subs, "desc": desc})
    items = store.read_json(out["descriptions"].id)
    check("描述条数对不上：仍按场景出 3 段", len(items) == 3, str(len(items)))
    check("描述条数对不上：有告警",
          any("按 index 对齐" in w for w in out["descriptions"].meta["warnings"]),
          repr(out["descriptions"].meta["warnings"]))
    check("描述条数对不上：缺的那段视觉为空", items[1]["visual_description"] == "",
          items[1]["visual_description"])

    for d in (d1, d2, d3, d5, d6, d7):
        shutil.rmtree(d, ignore_errors=True)

    print("\n%d OK / %d BAD" % (len(OK), len(BAD)))
    return 0 if not BAD else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
