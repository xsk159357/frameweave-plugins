"""core/ai_narrate 3.1.0 判据：原片标记从正文摘成结构化字段。

跑法：python scripts/test_narrate_ost.py

用 passthrough 模式测——它不调 LLM，但走的是**同一个收尾段**，而标记抽取就装在那里。
这样判据不依赖网络与模型，跑得起来也跑得快。

最要紧的一条：标记不能留在 text 里。留一句「【原片:12-16】」进 TTS，
成片里就会有人把括号念出来；烧进字幕更难看。

反面（必须红）：
  · items 缺 text → 报错（passthrough 的既有契约，防回归）
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
PKG = os.path.join(PLUGINS, "packages", "core_ai_narrate")
sys.path.insert(0, BACKEND)

from app.assets import Asset, LocalAssetStore      # noqa: E402
from app.nodespec import PortType                  # noqa: E402

_spec = importlib.util.spec_from_file_location("cw_an", os.path.join(PKG, "node.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Node = _mod.AINarrateNode

OK: list = []
BAD: list = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("%-58s %s%s" % (name, "OK" if cond else "BAD",
                          ("  " + detail) if (detail and not cond) else ""))


def make_store(tag):
    d = os.path.join(ROOT, "_tmp_nost_%s" % tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return LocalAssetStore(d), d


async def run_pass(store, items):
    asset = store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=items)
    return await Node().run({"store": store, "progress": lambda p: None},
                            {"items": asset}, {"mode": "passthrough"})


async def main() -> int:
    # ------------------------------------------- 1 句中标记：剥离 + 挂字段
    store, d1 = make_store("inline")
    out = await run_pass(store, [
        {"text": "他推开门。【原片:12-16】屋里没人。", "start": 0.0, "end": 3.0},
        {"text": "只有电视还亮着。", "start": 3.0, "end": 6.0},
    ])
    narr = store.read_json(out["narration"].id)
    script = store.read_json(out["script"].id)
    check("段数不变（2 段）", len(narr) == 2, str(len(narr)))
    check("正文里没有原片标记", "原片" not in narr[0]["text"], narr[0]["text"])
    check("正文内容保留", narr[0]["text"] == "他推开门。屋里没人。", narr[0]["text"])
    check("ost 字段挂上了", narr[0].get("ost") == {"start": 12.0, "end": 16.0},
          repr(narr[0].get("ost")))
    check("script 全文也不含标记", "原片" not in script["text"], script["text"])
    check("meta 记了标记数", out["narration"].meta["ost"]["marks"] == 1,
          repr(out["narration"].meta.get("ost")))

    # --------------------------------------- 2 独占一行的纯原片段：摘段 + 前挂
    store, d2 = make_store("pure")
    out = await run_pass(store, [
        {"text": "【原片:10-14】", "start": 0.0, "end": 2.0},
        {"text": "他愣住了。", "start": 2.0, "end": 5.0},
    ])
    narr = store.read_json(out["narration"].id)
    check("纯原片段被摘掉（只剩 1 段）", len(narr) == 1, str(len(narr)))
    check("标记前挂到后一句", narr[0].get("ost") == {"start": 10.0, "end": 14.0},
          repr(narr[0].get("ost")))
    check("ost_before 也在", narr[0].get("ost_before") == [{"start": 10.0, "end": 14.0}],
          repr(narr[0].get("ost_before")))
    check("被摘段不留空文本条目", all(sg["text"] for sg in narr), repr(narr))
    check("index 连续重编", [sg["index"] for sg in narr] == [0], repr([sg["index"] for sg in narr]))

    # ------------------------------------------------ 3 结尾悬挂：挂到最后 + 告警
    store, d3 = make_store("tail")
    out = await run_pass(store, [
        {"text": "他走了。", "start": 0.0, "end": 3.0},
        {"text": "【原片:30-33】", "start": 3.0, "end": 5.0},
    ])
    narr = store.read_json(out["narration"].id)
    check("结尾悬挂不新增段", len(narr) == 1, str(len(narr)))
    check("结尾标记挂 ost_after",
          narr[0].get("ost_after") == [{"start": 30.0, "end": 33.0}],
          repr(narr[0].get("ost_after")))
    check("结尾悬挂有告警",
          any("没有后继解说句" in w for w in out["narration"].meta["warnings"]),
          repr(out["narration"].meta["warnings"]))

    # -------------------------------------------------------- 4 无标记：不动
    store, d4 = make_store("clean")
    out = await run_pass(store, [
        {"text": "普通的一句。", "start": 0.0, "end": 3.0},
    ])
    narr = store.read_json(out["narration"].id)
    check("无标记时 text 原样", narr[0]["text"] == "普通的一句。", narr[0]["text"])
    check("无标记时没有 ost 字段", "ost" not in narr[0], repr(narr[0].keys()))
    check("无标记时 marks=0", out["narration"].meta["ost"]["marks"] == 0,
          repr(out["narration"].meta.get("ost")))

    # ------------------------------------------------------------ 5 反面
    store, d5 = make_store("neg")
    try:
        await run_pass(store, [{"start": 0.0, "end": 1.0}])
        check("缺 text 必须报错", False)
    except RuntimeError as e:
        check("缺 text 必须报错", "缺 text" in str(e), str(e))

    for d in (d1, d2, d3, d4, d5):
        shutil.rmtree(d, ignore_errors=True)

    print("\n%d OK / %d BAD" % (len(OK), len(BAD)))
    return 0 if not BAD else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
