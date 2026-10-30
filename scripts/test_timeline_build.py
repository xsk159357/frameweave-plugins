"""core/timeline_build 判据：配音驱动的排片（双向）。

跑法：python scripts/test_timeline_build.py

这个改动的核心风险有两个，判据就围绕它们：
  1. **配音被 max_clip_sec 截断** → 音画不同步。必须证明有配音时 need 不受上限约束，
     而不是被悄悄截到 12 秒。
  2. **配音句与文案句对不上** → 时间轴错位。必须证明：对得上就按配音排，
     对不上就整批退回估算并告警，不能一半一半。

反面（必须红）：
  · 缺 match / scenes → 报错
  · 配音总时长对不上 → 不得静默按 index 硬对
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
PKG = os.path.join(PLUGINS, "packages", "core_timeline_build")
sys.path.insert(0, BACKEND)

from app.assets import Asset, LocalAssetStore      # noqa: E402
from app.nodespec import PortType                  # noqa: E402

_spec = importlib.util.spec_from_file_location("cw_tl", os.path.join(PKG, "node.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Node = _mod.TimelineBuildNode

OK: list = []
BAD: list = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("%-56s %s%s" % (name, "OK" if cond else "BAD", ("  " + detail) if (detail and not cond) else ""))


def make_store(tag):
    d = os.path.join(ROOT, "_tmp_tl_%s" % tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return LocalAssetStore(d), d


async def call(store, inputs, params=None):
    return await Node().run({"store": store}, inputs, params or {})


def mk_match(store, texts):
    return store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=[
        {"index": i, "text": t, "chars": len(t),
         "scenes": [{"index": i, "score": 1.0}]}
        for i, t in enumerate(texts)
    ])


def mk_scenes(store, spans):
    return store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=[
        {"index": i, "start": s, "end": e} for i, (s, e) in enumerate(spans)
    ])


def mk_voice(store, rows):
    """rows: [(text, duration), ...]"""
    return store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=[
        {"index": i, "text": t, "duration": d, "start": 0.0, "end": d}
        for i, (t, d) in enumerate(rows)
    ])


async def case1_voice_same_count():
    """句数一致 → 每段时长就是配音时长，总时长等于配音之和。"""
    store, d = make_store("c1")
    texts = ["第一句。", "第二句。", "第三句。"]
    m = mk_match(store, texts)
    sc = mk_scenes(store, [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)])
    v = mk_voice(store, [("第一句。", 1.5), ("第二句。", 2.0), ("第三句。", 1.2)])
    out = await call(store, {"match": m, "scenes": sc, "voice_segments": v})
    tl = out["timeline"]
    rows = store.read_json(tl.id)
    m_meta = tl.meta

    check("c1 段数=3", len(rows) == 3, "got %d" % len(rows))
    check("c1 每段时长=配音时长",
          [r["duration"] for r in rows] == [1.5, 2.0, 1.2],
          "got %r" % ([r["duration"] for r in rows],))
    check("c1 duration_source=voice", m_meta.get("duration_source") == "voice",
          "got %r" % (m_meta.get("duration_source"),))
    check("c1 每段标注来源", all(r.get("duration_source") == "voice" for r in rows))
    check("c1 总时长=配音之和", abs(m_meta.get("total_duration", 0) - 4.7) < 0.01,
          "got %r" % (m_meta.get("total_duration"),))
    check("c1 成片时间连续",
          [r["out_start"] for r in rows] == [0.0, 1.5, 3.5],
          "got %r" % ([r["out_start"] for r in rows],))
    shutil.rmtree(d, ignore_errors=True)


async def case2_voice_not_truncated():
    """配音比 max_clip_sec 长 → 不得截断配音，只告警。

    这是本改动最关键的一条：按字数估时 max_clip_sec 是上限，
    但有配音时上限不再是上限——截了就是音画不同步。
    """
    store, d = make_store("c2")
    m = mk_match(store, ["这一句配音很长。"])
    sc = mk_scenes(store, [(0.0, 30.0)])      # 画面够长，不是画面不够
    v = mk_voice(store, [("这一句配音很长。", 8.0)])
    out = await call(store, {"match": m, "scenes": sc, "voice_segments": v},
                     {"max_clip_sec": 3.0})
    rows = store.read_json(out["timeline"].id)
    check("c2 配音不被 max_clip_sec 截断", rows[0]["duration"] == 8.0,
          "duration=%r（若为 3.0 说明被截断了）" % (rows[0]["duration"],))
    check("c2 need 记录为配音时长", rows[0]["need"] == 8.0, "got %r" % (rows[0]["need"],))
    shutil.rmtree(d, ignore_errors=True)


async def case3_voice_cut_warns():
    """画面比配音短 → 计时按画面，但必须单独告警「配音会被切断」。"""
    store, d = make_store("c3")
    m = mk_match(store, ["这句配音长但画面很短。"])
    sc = mk_scenes(store, [(0.0, 2.0)])       # 画面只有 2 秒
    v = mk_voice(store, [("这句配音长但画面很短。", 6.0)])
    out = await call(store, {"match": m, "scenes": sc, "voice_segments": v})
    tl = out["timeline"]
    rows = store.read_json(tl.id)
    ws = [str(w) for w in (tl.meta.get("warnings") or [])]

    check("c3 画面短时按画面截断", rows[0]["duration"] == 2.0, "got %r" % (rows[0]["duration"],))
    check("c3 fits=False", rows[0]["fits"] is False)
    check("c3 有 voice_cut 计数", tl.meta.get("voice_cut_count") == 1,
          "got %r" % (tl.meta.get("voice_cut_count"),))
    check("c3 告警说清音画不同步",
          any("音画不同步" in w for w in ws), "warnings=%r" % (ws,))
    shutil.rmtree(d, ignore_errors=True)


async def case4_voice_split_matches():
    """配音把一句切碎（句数不同）但拼接相同 → 按字符位置切分时长。"""
    store, d = make_store("c4")
    m = mk_match(store, ["这是一个很长的句子需要被切开才好合成。"])
    sc = mk_scenes(store, [(0.0, 30.0)])
    v = mk_voice(store, [("这是一个很长的", 1.5), ("句子需要被切开才好合成。", 2.5)])
    out = await call(store, {"match": m, "scenes": sc, "voice_segments": v})
    tl = out["timeline"]
    rows = store.read_json(tl.id)
    check("c4 一句文案对两句配音", len(rows) == 1, "got %d" % len(rows))
    check("c4 时长=两句配音之和", abs(rows[0]["duration"] - 4.0) < 0.01,
          "got %r" % (rows[0]["duration"],))
    check("c4 来源标为 voice", rows[0].get("duration_source") == "voice",
          "got %r" % (rows[0].get("duration_source"),))
    shutil.rmtree(d, ignore_errors=True)


async def case5_voice_mismatch_falls_back():
    """配音文本对不上 → 整批退回估算 + 告警，不得硬对。"""
    store, d = make_store("c5")
    m = mk_match(store, ["完全不同的文案甲。", "另一句乙。"])
    sc = mk_scenes(store, [(0.0, 20.0), (20.0, 40.0)])
    v = mk_voice(store, [("这是一段无关的语音。", 3.0), ("还有一句。", 2.0)])
    out = await call(store, {"match": m, "scenes": sc, "voice_segments": v})
    tl = out["timeline"]
    rows = store.read_json(tl.id)
    ws = [str(w) for w in (tl.meta.get("warnings") or [])]

    check("c5 退回估算", tl.meta.get("duration_source") == "estimate",
          "got %r" % (tl.meta.get("duration_source"),))
    check("c5 每段来源也是 estimate",
          all(r.get("duration_source") == "estimate" for r in rows),
          "got %r" % ([r.get("duration_source") for r in rows],))
    check("c5 有对不上的告警",
          any("对不上" in w for w in ws), "warnings=%r" % (ws,))
    check("c5 时长按字数算（4.5 字/秒）",
          abs(rows[0]["duration"] - min(len("完全不同的文案甲。") / 4.5, 12.0)) < 0.01,
          "got %r" % (rows[0]["duration"],))
    check("c5 降级标记", tl.meta.get("degraded") is True)
    shutil.rmtree(d, ignore_errors=True)


async def case6_no_voice_unchanged():
    """不接配音 → 老行为不变（这条防的是改动把原路径弄坏）。"""
    store, d = make_store("c6")
    m = mk_match(store, ["十个字的句子啊啊啊啊。"])
    sc = mk_scenes(store, [(0.0, 20.0)])
    out = await call(store, {"match": m, "scenes": sc})
    tl = out["timeline"]
    rows = store.read_json(tl.id)
    want = round(len("十个字的句子啊啊啊啊。") / 4.5, 3)
    check("c6 无配音仍按字数估", abs(rows[0]["duration"] - want) < 0.01,
          "got %r want %r" % (rows[0]["duration"], want))
    check("c6 duration_source=estimate", tl.meta.get("duration_source") == "estimate",
          "got %r" % (tl.meta.get("duration_source"),))
    check("c6 不误报降级", not tl.meta.get("degraded"),
          "warnings=%r" % (tl.meta.get("warnings"),))
    shutil.rmtree(d, ignore_errors=True)


async def case7_missing_input():
    store, d = make_store("c7")
    msg = ""
    try:
        await call(store, {})
    except RuntimeError as e:
        msg = str(e)
    check("c7 缺输入必须报错", bool(msg), "没报错")
    check("c7 错误说清缺什么", "缺少" in msg, "msg=%r" % msg)
    shutil.rmtree(d, ignore_errors=True)


async def main():
    await case1_voice_same_count()
    await case2_voice_not_truncated()
    await case3_voice_cut_warns()
    await case4_voice_split_matches()
    await case5_voice_mismatch_falls_back()
    await case6_no_voice_unchanged()
    await case7_missing_input()
    print("\n合计 %d OK / %d BAD" % (len(OK), len(BAD)))
    if BAD:
        print("失败项: %s" % ", ".join(BAD))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
