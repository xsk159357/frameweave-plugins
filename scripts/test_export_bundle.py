"""core/export_bundle 判据：整段直出分支（双向）。

跑法：python scripts/test_export_bundle.py

为什么要双向：只证明「直出能出包」不够。这个改动把 timeline 从必填改成可选，
真正的风险是两件——(a) 有 timeline 的老路径被改坏；(b) 既没 timeline 也没视频时
不报错，而是悄悄产出一个空包。前者要证明结果没变，后者要证明它确实红。
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
PKG = os.path.join(PLUGINS, "packages", "core_export_bundle")
sys.path.insert(0, BACKEND)

from app.assets import Asset, LocalAssetStore      # noqa: E402
from app.nodespec import PortType                  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cw_export_bundle", os.path.join(PKG, "node.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Node = _mod.ExportBundleNode

OK = []
BAD = []


def check(name, cond, detail=""):
    """detail 只在失败时打印。

    通过时打印「没报错」这类字面兜底文案会自相矛盾——读日志的人看到
    「OK  没报错」会以为判据写反了。通过与否只看 OK/BAD 这一列。
    """
    (OK if cond else BAD).append(name)
    print("%-56s %s%s" % (name, "OK" if cond else "BAD", ("  " + detail) if (detail and not cond) else ""))


def make_store(tag):
    d = os.path.join(ROOT, "_tmp_store_%s" % tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return LocalAssetStore(d), d


async def call(store, inputs, params=None):
    return await Node().run({"store": store}, inputs, params or {})


async def case1_straight_through():
    """无 timeline + 有 video(duration=6.0) + 2 条字幕 → 整段直出。"""
    store, d = make_store("c1")
    video = store.save_asset(Asset(id="", kind=PortType.VIDEO.value,
                                   path="F:/media/src.mp4", meta={"duration": 6.0}))
    subs = store.save_asset(Asset(id="", kind=PortType.SUBTITLE.value),
                            payload=[{"index": 0, "start": 0.0, "end": 2.0, "text": "第一句"},
                                     {"index": 1, "start": 2.0, "end": 4.5, "text": "第二句"}])
    out = await call(store, {"video": video, "subtitle": subs})
    b = out["bundle"]
    segs = store.read_json(b.id)
    m = b.meta

    check("c1 段数=1", len(segs) == 1, "got %d" % len(segs))
    check("c1 源片区间=(0,6)", segs[0]["src_start"] == 0.0 and segs[0]["src_end"] == 6.0,
          "got %r" % ((segs[0]["src_start"], segs[0]["src_end"]),))
    check("c1 成片区间=(0,6)", segs[0]["out_start"] == 0.0 and segs[0]["out_end"] == 6.0,
          "got %r" % ((segs[0]["out_start"], segs[0]["out_end"]),))
    check("c1 straight_through=True", m.get("straight_through") is True, "got %r" % (m.get("straight_through"),))
    check("c1 有整段直出 warning",
          any("整段直出" in str(w) for w in m.get("warnings") or []), "warnings=%r" % (m.get("warnings"),))
    check("c1 两条字幕挂进该段", len(segs[0].get("subtitle") or []) == 2,
          "got %d" % len(segs[0].get("subtitle") or []))
    check("c1 total_duration=6.0", m.get("total_duration") == 6.0, "got %r" % (m.get("total_duration"),))
    srt = m.get("srt_path") or ""
    check("c1 SRT 落盘", bool(srt) and os.path.isfile(srt), "path=%r" % srt)
    if srt and os.path.isfile(srt):
        body = open(srt, encoding="utf-8").read()
        check("c1 SRT 首条时间戳正确", "00:00:00,000 --> 00:00:02,000" in body)
    check("c1 srt 端口有全文",
          isinstance(out.get("srt"), str) and " --> " in (out.get("srt") or ""),
          "got %r" % (type(out.get("srt")).__name__,))
    check("c1 进程内 srt_path 不算临时", m.get("srt_path_is_temp") is False,
          "got %r" % (m.get("srt_path_is_temp"),))
    shutil.rmtree(d, ignore_errors=True)


async def case2_legacy_unchanged():
    """有 timeline（2 段）→ 老路径行为不变，不走直出。"""
    store, d = make_store("c2")
    video = store.save_asset(Asset(id="", kind=PortType.VIDEO.value,
                                   path="F:/media/src.mp4", meta={"duration": 7.0}))
    tl = store.save_asset(Asset(id="", kind=PortType.TIMELINE.value), payload=[
        {"index": 0, "start": 0.0, "end": 3.0, "out_start": 0.0, "out_end": 3.0,
         "duration": 3.0, "scene_index": 0, "text": "A"},
        {"index": 1, "start": 5.0, "end": 7.0, "out_start": 3.0, "out_end": 5.0,
         "duration": 2.0, "scene_index": 1, "text": "B"},
    ])
    subs = store.save_asset(Asset(id="", kind=PortType.SUBTITLE.value),
                            payload=[{"index": 0, "start": 1.0, "end": 2.0, "text": "落第0段"},
                                     {"index": 1, "start": 4.0, "end": 4.5, "text": "落第1段"}])
    out = await call(store, {"timeline": tl, "video": video, "subtitle": subs})
    b = out["bundle"]
    segs = store.read_json(b.id)
    m = b.meta

    check("c2 段数=2（未被直出覆盖）", len(segs) == 2, "got %d" % len(segs))
    check("c2 straight_through=False", m.get("straight_through") is False, "got %r" % (m.get("straight_through"),))
    check("c2 无整段直出 warning",
          not any("整段直出" in str(w) for w in m.get("warnings") or []), "warnings=%r" % (m.get("warnings"),))
    check("c2 第0段挂 1 条字幕", len(segs[0].get("subtitle") or []) == 1,
          "got %d" % len(segs[0].get("subtitle") or []))
    check("c2 第1段挂 1 条字幕", len(segs[1].get("subtitle") or []) == 1,
          "got %d" % len(segs[1].get("subtitle") or []))
    check("c2 成片区间保持 (0,3)/(3,5)",
          (segs[0]["out_start"], segs[0]["out_end"]) == (0.0, 3.0)
          and (segs[1]["out_start"], segs[1]["out_end"]) == (3.0, 5.0))
    check("c2 total_duration=5.0", m.get("total_duration") == 5.0, "got %r" % (m.get("total_duration"),))
    shutil.rmtree(d, ignore_errors=True)


async def case3_no_timeline_no_video():
    """无 timeline 且无 video → 必须红，且错误要说清缺什么。"""
    store, d = make_store("c3")
    subs = store.save_asset(Asset(id="", kind=PortType.SUBTITLE.value),
                            payload=[{"index": 0, "start": 0.0, "end": 1.0, "text": "x"}])
    msg = ""
    try:
        await call(store, {"subtitle": subs})
    except RuntimeError as e:
        msg = str(e)
    check("c3 无输入必须报错", bool(msg), "没报错")
    check("c3 错误点名「既没有时间线」", "既没有时间线" in msg, "msg=%r" % msg)
    shutil.rmtree(d, ignore_errors=True)


async def case4_video_without_duration():
    """有 video 但 meta 无 duration → 同样必须红（不能产出 0 长空包）。"""
    store, d = make_store("c4")
    video = store.save_asset(Asset(id="", kind=PortType.VIDEO.value,
                                   path="F:/media/broken.mp4", meta={}))
    msg = ""
    try:
        await call(store, {"video": video})
    except RuntimeError as e:
        msg = str(e)
    check("c4 无时长必须报错", bool(msg), "没报错")
    check("c4 错误点名 meta.duration", "meta.duration" in msg, "msg=%r" % msg)
    shutil.rmtree(d, ignore_errors=True)


async def case5_orphan_subtitle():
    """字幕起始时间超出成片区间 → 挂不上，要报出来，但仍写进 SRT。"""
    store, d = make_store("c5")
    video = store.save_asset(Asset(id="", kind=PortType.VIDEO.value,
                                   path="F:/media/src.mp4", meta={"duration": 6.0}))
    subs = store.save_asset(Asset(id="", kind=PortType.SUBTITLE.value),
                            payload=[{"index": 0, "start": 0.0, "end": 2.0, "text": "a"},
                                     {"index": 1, "start": 2.0, "end": 4.0, "text": "b"},
                                     {"index": 2, "start": 7.0, "end": 8.0, "text": "越界"}])
    out = await call(store, {"video": video, "subtitle": subs})
    b = out["bundle"]
    segs = store.read_json(b.id)
    m = b.meta
    ws = [str(w) for w in (m.get("warnings") or [])]

    check("c5 段内只挂 2 条", len(segs[0].get("subtitle") or []) == 2,
          "got %d" % len(segs[0].get("subtitle") or []))
    check("c5 有越界字幕 warning", any("1 条字幕" in w for w in ws), "warnings=%r" % (ws,))
    check("c5 subtitle_count 仍记 3", m.get("subtitle_count") == 3, "got %r" % (m.get("subtitle_count"),))
    srt = m.get("srt_path") or ""
    if srt and os.path.isfile(srt):
        body = open(srt, encoding="utf-8").read()
        check("c5 SRT 仍是 3 条（越界的不丢）", body.count(" --> ") == 3,
              "got %d" % body.count(" --> "))
    else:
        check("c5 SRT 落盘", False, "path=%r" % srt)
    shutil.rmtree(d, ignore_errors=True)


async def case6_worker_temp_dir():
    """worker 模式下 files_dir 在 runtime/work 里 → 必须标成临时，并明确告警。

    supervisor 把每个 job 的 scratch 建在 <store>/runtime/work/<job_id>，
    sweep_stale_scratch 回收 24 小时前的目录。所以 srt_path 在这种位置时，
    用户隔天就拿到一个不存在的文件——除非我们标出来，并且把全文放进 srt 端口。
    """
    store, d = make_store("c6")
    tmp_out = os.path.join(d, "runtime", "work", "job_probe", "out")
    os.makedirs(tmp_out, exist_ok=True)
    store.files_dir = tmp_out
    video = store.save_asset(Asset(id="", kind=PortType.VIDEO.value,
                                   path="F:/media/src.mp4", meta={"duration": 5.0}))
    subs = store.save_asset(Asset(id="", kind=PortType.SUBTITLE.value),
                            payload=[{"index": 0, "start": 0.0, "end": 1.0, "text": "x"}])
    out = await call(store, {"video": video, "subtitle": subs})
    m = out["bundle"].meta
    ws = [str(w) for w in (m.get("warnings") or [])]

    check("c6 临时目录 srt_path_is_temp=True", m.get("srt_path_is_temp") is True,
          "got %r" % (m.get("srt_path_is_temp"),))
    check("c6 有回收告警", any("回收" in w for w in ws), "warnings=%r" % (ws,))
    check("c6 临时目录下 srt 端口仍给全文",
          isinstance(out.get("srt"), str) and " --> " in (out.get("srt") or ""),
          "got %r" % (type(out.get("srt")).__name__,))
    shutil.rmtree(d, ignore_errors=True)


async def main():
    await case1_straight_through()
    await case2_legacy_unchanged()
    await case3_no_timeline_no_video()
    await case4_video_without_duration()
    await case5_orphan_subtitle()
    await case6_worker_temp_dir()
    print("\n合计 %d OK / %d BAD" % (len(OK), len(BAD)))
    if BAD:
        print("失败项: %s" % ", ".join(BAD))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
