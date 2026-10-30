"""core/tts_synthesize 判据（双向）。

跑法：python scripts/test_tts_synthesize.py

本沙箱里真正合成不可用，原因有据可查：
  · 系统语音（SAPI）：python 子进程跑在 Low 完整性级别，Voice 列表能列出来
    但 SelectVoice 报「没有可用语音」——语音令牌在 Medium IL 才读得到。
  · IndexTTS：权重与独立运行时都齐，但模型目录路径含中文，wetext 的 kaldifst
    是 C++ 扩展、用窄字符 API 打开词典，中文路径打不开。

所以这里验的是**节点自己的那部分**：拆句、逐句合成编排、拼接、时长与时间轴、
以及两条错误路径。合成的真实声音不在本判据覆盖范围内，用固定时长音频桩替代，
桩按句长线性给时长，好让时间轴的期望值是算得出来的。

反面（必须红）：
  · 文案为空 / 没有输入 → 报错且说清缺什么
  · engine=indextts 而环境不满足 → 报错，且原因是路径而不是静默降级
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(ROOT)
BACKEND = os.path.join(os.path.dirname(PLUGINS), "FrameWeave", "backend")
PKG = os.path.join(PLUGINS, "packages", "core_tts_synthesize")
sys.path.insert(0, BACKEND)

from app.assets import Asset, LocalAssetStore      # noqa: E402
from app.media_tools import ffmpeg_path            # noqa: E402
from app.nodespec import PortType                  # noqa: E402

_spec = importlib.util.spec_from_file_location("cw_tts", os.path.join(PKG, "node.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Node = _mod.TtsSynthesizeNode

# 桩：每句时长 = 字数 × SEC_PER_CHAR，制造可预测的时间轴
SEC_PER_CHAR = 0.12


def fake_sapi(text, out_wav, voice, rate, volume, work):
    dur = max(0.3, round(len(str(text)) * SEC_PER_CHAR, 3))
    ff = ffmpeg_path()
    r = subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=%.3f" % dur,
                        "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le", out_wav],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("桩合成失败: %s" % r.stderr[:200])


_mod._sapi_one = fake_sapi

OK: list = []
BAD: list = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("%-56s %s%s" % (name, "OK" if cond else "BAD", ("  " + detail) if (detail and not cond) else ""))


def make_store(tag):
    d = os.path.join(ROOT, "_tmp_tts_%s" % tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return LocalAssetStore(d), d


async def call(store, inputs, params=None):
    return await Node().run({"store": store}, inputs, params or {})


def make_script(store, lines):
    return store.save_asset(Asset(id="", kind=PortType.SCRIPT.value),
                            payload={"text": "\n".join(lines), "lines": list(lines),
                                     "count": len(lines)})


def expect_dur(text):
    return max(0.3, round(len(str(text)) * SEC_PER_CHAR, 3))


async def case1_timeline():
    store, d = make_store("t1")
    lines = ["第一句话。", "第二句话长一点点。", "第三句。"]
    gap = 0.2
    out = await call(store, {"script": make_script(store, lines)},
                     {"gap": gap, "engine": "sapi"})
    a = out["audio"]
    segs = store.read_json(out["segments"].id)

    check("t1 逐句 3 条", len(segs) == 3, "got %d" % len(segs))
    check("t1 文本对得上", [s["text"] for s in segs] == lines,
          "got %r" % ([s["text"] for s in segs],))

    want = [expect_dur(x) for x in lines]
    got = [s["duration"] for s in segs]
    check("t1 每句时长与合成结果一致",
          all(abs(g - w) < 0.05 for g, w in zip(got, want)),
          "got %r want %r" % (got, want))

    cursor = 0.0
    ok_start = True
    for i, s in enumerate(segs):
        if abs(s["start"] - round(cursor, 3)) > 0.001:
            ok_start = False
        cursor += s["duration"] + (gap if i < len(segs) - 1 else 0.0)
    check("t1 逐句 start 严格累加（含 gap）", ok_start,
          "got %r" % ([s["start"] for s in segs],))

    total = float(a.meta.get("duration") or 0)
    want_total = sum(want) + gap * (len(lines) - 1)
    check("t1 总时长=逐句之和+间隔", abs(total - want_total) < 0.15,
          "total=%r want=%r" % (total, want_total))
    check("t1 无警告（桩合成不应降级）", not a.meta.get("degraded"),
          "warnings=%r" % (a.meta.get("warnings"),))
    seg_meta = out["segments"].meta
    check("t1 segments.total_duration 与音频一致",
          abs(float(seg_meta.get("total_duration") or 0) - total) < 0.01,
          "seg=%r audio=%r" % (seg_meta.get("total_duration"), total))
    shutil.rmtree(d, ignore_errors=True)


async def case2_no_gap():
    """gap=0 时相邻句首尾相接，不该凭空多出间隔。"""
    store, d = make_store("t2")
    lines = ["走吧。", "回家吧。"]
    out = await call(store, {"script": make_script(store, lines)},
                     {"gap": 0.0, "engine": "sapi"})
    segs = store.read_json(out["segments"].id)
    check("t2 相邻句首尾相接", abs(segs[1]["start"] - segs[0]["end"]) < 0.001,
          "%r vs %r" % (segs[1]["start"], segs[0]["end"]))
    shutil.rmtree(d, ignore_errors=True)


async def case2b_fragment_merged():
    """只剩标点的碎片并进下一句；正常短句（去标点 2 字以上）保持独立。"""
    store, d = make_store("t2b")
    frag = ["甲。", "我们一起回家吧。"]
    out = await call(store, {"script": make_script(store, frag)},
                     {"gap": 0.0, "engine": "sapi"})
    segs = store.read_json(out["segments"].id)
    check("t2b 单字碎片被并进下一句", len(segs) == 1, "got %d 条" % len(segs))
    check("t2b 合并后文本完整",
          len(segs) == 1 and segs[0]["text"] == "甲。我们一起回家吧。",
          "got %r" % ([s["text"] for s in segs],))
    normal = ["走吧。", "回家吧。"]
    out2 = await call(store, {"script": make_script(store, normal)},
                      {"gap": 0.0, "engine": "sapi"})
    segs2 = store.read_json(out2["segments"].id)
    check("t2b 两字短句不被误合并", len(segs2) == 2, "got %d 条" % len(segs2))
    shutil.rmtree(d, ignore_errors=True)


async def case3_long_line_split():
    store, d = make_store("t3")
    long_one = "这是一个很长的句子，里面有逗号，也有顿号、还有别的标点，需要被切开才行。"
    out = await call(store, {"script": make_script(store, [long_one])},
                     {"max_chars": 12, "gap": 0.0})
    segs = store.read_json(out["segments"].id)
    check("t3 长句被切成多条", len(segs) > 1, "got %d 条" % len(segs))
    check("t3 每条不超长太多", all(len(s["text"]) <= 26 for s in segs),
          "max=%d" % max(len(s["text"]) for s in segs))
    shutil.rmtree(d, ignore_errors=True)


async def case4_empty_script():
    store, d = make_store("t4")
    msg = ""
    try:
        await call(store, {"script": make_script(store, ["   "])})
    except RuntimeError as e:
        msg = str(e)
    check("t4 空文案必须报错", bool(msg), "没报错")
    check("t4 错误说清是文案为空", "文案为空" in msg, "msg=%r" % msg)
    shutil.rmtree(d, ignore_errors=True)


async def case5_no_input():
    store, d = make_store("t5")
    msg = ""
    try:
        await call(store, {})
    except RuntimeError as e:
        msg = str(e)
    check("t5 无输入必须报错", bool(msg), "没报错")
    check("t5 错误说清缺文案", "缺少文案输入" in msg, "msg=%r" % msg)
    shutil.rmtree(d, ignore_errors=True)


async def case6_indextts_path_guard():
    """显式选 indextts 而环境不满足 → 必须报错，且原因要具体。

    本机的情形正是「权重齐、运行时齐、但路径含中文」。判据按实际原因断言，
    不写死某一句话——原因会随环境变，判据不该跟着变。
    """
    store, d = make_store("t6")
    ok, why = _mod._indextts_ready()
    if ok:
        print("%-56s SKIP（本机 IndexTTS 可用）" % "t6 显式 indextts 报错")
        shutil.rmtree(d, ignore_errors=True)
        return
    msg = ""
    try:
        await call(store, {"script": make_script(store, ["测试。"])}, {"engine": "indextts"})
    except RuntimeError as e:
        msg = str(e)
    check("t6 显式 indextts 必须报错", bool(msg), "没报错")
    check("t6 错误含 IndexTTS 不可用", "IndexTTS 不可用" in msg, "msg=%r" % msg)
    check("t6 错误里带上真实原因", why[:12] in msg, "原因=%r msg=%r" % (why[:40], msg[:120]))
    shutil.rmtree(d, ignore_errors=True)


async def case7_auto_falls_back():
    """auto 且 IndexTTS 不可用 → 落到 sapi，并把原因写进 warnings。"""
    store, d = make_store("t7")
    ok, why = _mod._indextts_ready()
    out = await call(store, {"script": make_script(store, ["甲。"])}, {"engine": "auto"})
    m = out["audio"].meta
    check("t7 引擎落到 sapi", m.get("engine") == "sapi", "got %r" % (m.get("engine"),))
    if not ok:
        check("t7 降级原因写进 warnings",
              any("IndexTTS 不可用" in str(w) for w in (m.get("warnings") or [])),
              "warnings=%r" % (m.get("warnings"),))
    shutil.rmtree(d, ignore_errors=True)


async def main():
    await case1_timeline()
    await case2_no_gap()
    await case2b_fragment_merged()
    await case3_long_line_split()
    await case4_empty_script()
    await case5_no_input()
    await case6_indextts_path_guard()
    await case7_auto_falls_back()
    print("\n合计 %d OK / %d BAD" % (len(OK), len(BAD)))
    if BAD:
        print("失败项: %s" % ", ".join(BAD))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
