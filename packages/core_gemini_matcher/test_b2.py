# -*- coding: utf-8 -*-
"""B2 core/gemini_matcher 验收测试（C3，对齐 s3-plugin-design §4.3 契约 + r2 §2 语义）。

运行（backend/.venv 环境，pytest 收集或直跑）：
    cd FrameWeave/backend
    .venv\\Scripts\\python.exe F:\\1223\\解说工坊\\frameweave-plugins\\packages\\core_gemini_matcher\\test_b2.py

覆盖：
  1. 场景解析 M2 修复：all_des.txt 保留【对话】尾部（r2 §11-2）；_parse_time_str 三格式。
  2. 纯函数：estimate_tts_duration（CJK 计数）/ adapt_scene_duration（蒙太奇阈值 8s）/
     fix_overlapping_clips（LAY_INDEX + 非 OST 重叠顺延）。
  3. 阶段一 match：无 key → 本地启发式兜底（顺序消费 + avoid_adjacent_repeat 真实生效）；
     mock LLM（llm_call 与 internal 双后端）→ JSON 匹配解析 + 非法条目逐条告警 + 缺失补齐。
  4. 阶段二 ost：【原片:00:12 - 00:30】解析 + END 按 MAX_OST_DURATION=15 钳制 +
     按 char_position 排序插入（type=ost）。
  5. 节点 run：stage=match / stage=ost / stage=all（独立/串联）三路径 + report 契约。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PKG = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_PKG)
_WORK = os.path.abspath(os.path.join(_REPO, "..", ".."))  # 工作区根
_BACKEND = os.path.join(_WORK, "FrameWeave", "backend")
for p in (_PKG, _REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("FRAMEWEAVE_USER_NODES", _REPO)

from core_gemini_matcher import node as m  # noqa: E402
from app.assets import Asset, LocalAssetStore  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("  | " + detail if detail else ""))


# ---------------------------------------------------------------- 1. 场景解析（M2 修复）

def test_parse_all_des_txt():
    txt = ("[0.0s -> 4.0s] 特写镜头 | 男人走进房间 | 【对话】喂，你来了。\n"
           "[4.0s -> 10.0s] 中景 | 女人转身\n"
           "[10.0s -> 18.0s] 近景 | 【对话】你别走！")
    scenes = m.parse_all_des_txt(txt)
    check("M2: 解析 3 条场景", len(scenes) == 3, f"count={len(scenes)}")
    s0 = scenes[0]
    check("M2: 第一条 start/end/text", s0["start"] == 0.0 and s0["end"] == 4.0
          and s0["text"] == "特写镜头 - 男人走进房间", f"{s0}")
    check("M2: 【对话】尾部保留为 dialogue（原版丢弃）", s0["dialogue"] == "喂，你来了。",
          f"dialogue={s0['dialogue']!r}")
    s2 = scenes[2]
    check("M2: 纯对白行 text=镜头类型 + dialogue 保留",
          s2["dialogue"] == "你别走！" and s2["text"] == "近景", f"{s2}")
    # 时间格式
    check("parse_time: 秒浮点", m._parse_time_str("7008.68") == 7008.68)
    check("parse_time: MM:SS", m._parse_time_str("01:30.5") == 90.5)
    check("parse_time: HH:MM:SS", m._parse_time_str("1:02:03.4") == 3723.4)


def test_load_scenes_forms():
    d = {"scenes": [{"id": 0, "start": 0, "end": 5, "text": "a"},
                    {"id": 1, "start": 5, "end": 9, "text": "b", "dialogue": "对白b"}]}
    sc = m.load_scenes(d)
    check("load_scenes: dict{scenes} 形态", len(sc) == 2 and sc[1]["dialogue"] == "对白b")
    lst = [{"start": 0.0, "end": 4.0, "text": "x"}, {"start_time": 4.0, "end_time": 8.0}]
    sc2 = m.load_scenes(lst)
    check("load_scenes: list 形态（split_cam/scenes）", len(sc2) == 2
          and sc2[1]["start"] == 4.0 and sc2[1]["end"] == 8.0)


# ---------------------------------------------------------------- 2. 纯函数

def test_tts_duration():
    r = m.estimate_tts_duration("这是一段解说词，测试时长估算。", 4.5)
    check("TTS: 13 汉字/4.5 ≈ 2.889", abs(r - 2.889) < 0.01, f"r={r}")
    check("TTS: 空/标点不计", m.estimate_tts_duration("ab,.!? ") == 1.0)  # 无 CJK → 下限 1.0


def test_adapt_scene_duration():
    scenes = [{"id": 0, "start": 0, "end": 2}, {"id": 1, "start": 2, "end": 5},
              {"id": 2, "start": 5, "end": 12}]
    ids, covered = m.adapt_scene_duration(6.0, [0], scenes)
    check("adapt: 单场景不足→顺序吸收直至覆盖 TTS 时长",
          ids == [0, 1, 2] and covered >= 6.0, f"ids={ids} covered={covered}")
    ids2, covered2 = m.adapt_scene_duration(3.0, [2], scenes)
    check("adapt: 超 8s 场景即覆盖（蒙太奇）", ids2 == [2] and covered2 == 7.0,
          f"ids={ids2} covered={covered2}")
    check("MONTAGE_THRESHOLD=8.0 常量在位", m.MONTAGE_THRESHOLD == 8.0)


def test_fix_overlapping():
    items = [{"text": "a", "start": 0.0, "end": 5.0, "type": "narration"},
             {"text": "b", "start": 3.0, "end": 8.0, "type": "narration"},
             {"text": "c", "start": 7.5, "end": 9.0, "type": "narration"}]
    fixed = m.fix_overlapping_clips([dict(x) for x in items])
    check("overlap: 3 段全部保留且 LAY_INDEX 递增",
          [f["LAY_INDEX"] for f in fixed] == [0, 1, 2], f"{fixed}")
    check("overlap: 后段 start 顺延前段 end", fixed[1]["start"] == 5.0
          and fixed[2]["start"] == 8.0, f"{[ (f['start'], f['end']) for f in fixed ]}")
    short = [{"start": 0, "end": 0.3, "type": "narration"},
             {"start": 0.5, "end": 3.0, "type": "narration"}]
    fs = m.fix_overlapping_clips(short)
    check("overlap: <0.5s 片段跳过", len(fs) == 1 and fs[0]["end"] == 3.0)


# ---------------------------------------------------------------- 3. 阶段一 match

def test_local_match_no_key():
    narr = [{"index": 0, "text": "第一段解说"}, {"index": 1, "text": "第二段解说"},
            {"index": 2, "text": "第三段解说"}]
    scenes = [{"id": 0, "start": 0, "end": 3, "text": "场景0"},
              {"id": 1, "start": 3, "end": 6, "text": "场景1"}]
    matches = m.local_match(narr, scenes)
    check("local: 4 句 2 场景顺序消费（含复用）",
          [mm["narration_index"] for mm in matches] == [0, 1, 2]
          and len(matches[0]["scene_ids"]) >= 1, f"{matches}")
    check("local: avoid_adjacent_repeat 生效（相邻主场景不同）",
          matches[0]["scene_ids"][0] != matches[1]["scene_ids"][0] or len(scenes) < 2)


class MockLLMHandler(BaseHTTPRequestHandler):
    """mock OpenAI 兼容端点：按请求 body 返回固定匹配 JSON；可编程 429/500。"""
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body_raw = self.rfile.read(length) if length else b""
        if self.mode == "500":
            self._json(500, {"error": {"message": "boom"}})
            return
        body = json.loads(body_raw or b"{}")
        msgs = (body.get("messages") or [])
        user = ""
        for mm in msgs:
            if mm.get("role") == "user":
                user = str(mm.get("content") or "")
                break
        # 只解析【解说词分段】之后出现的解说词编号（避免误抓场景 id）
        user = user.split("【解说词分段】", 1)[-1]
        idxs = re.findall(r"\[(\d+)\]", user)
        items = [{"narration_index": int(i), "scene_ids": [0, 1],
                  "reason": "mock 匹配"} for i in idxs]
        self._json(200, {"choices": [{"message": {"content": json.dumps(items,
                                                                        ensure_ascii=False)}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _mock_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockLLMHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def test_llm_match_both_backends():
    narr = [{"index": 0, "text": "第一段"}, {"index": 1, "text": "第二段"}]
    scenes = [{"id": 0, "start": 0, "end": 4, "text": "场景0"},
              {"id": 1, "start": 4, "end": 8, "text": "场景1"}]
    srv, url = _mock_server()
    try:
        for backend in ("llm_call", "internal"):
            matches, meta = m.llm_semantic_match(
                narr, scenes, batch_size=1, api_key="k", base_url=url, model="m",
                backend=backend, retries=0)
            check(f"LLM({backend}): 返回 2 条匹配且 scene_ids 合法",
                  len(matches) == 2 and meta["llm_calls"] == 2
                  and all(m["scene_ids"] == [0, 1] for m in matches)
                  and meta["match_method"] == "llm", f"{meta} matches={len(matches)}")
    finally:
        srv.shutdown()


def test_llm_match_bad_items_and_missing():
    narr = [{"index": 0, "text": "a"}, {"index": 1, "text": "b"}]
    scenes = [{"id": 0, "start": 0, "end": 4, "text": "s0"}]
    raw = [{"narration_index": 0, "scene_ids": [0], "reason": "ok"},
           {"narration_index": 99, "scene_ids": [0], "reason": "bad index"},
           {"narration_index": 0, "scene_ids": [7], "reason": "bad scene"},
           {"narration_index": 1, "scene_ids": [], "reason": "empty"}]
    warns = []
    out = m.sanitize_matches(raw, {0, 1}, {0}, warns)
    check("sanitize: 非法条目告警不整批丢弃", len(out) == 3
          and any("不存在" in w for w in warns), f"out={len(out)} warns={len(warns)}")


def test_no_key_fallback_path():
    narr = [{"index": 0, "text": "唯一段"}]
    scenes = [{"id": 0, "start": 0, "end": 5, "text": "s0"}]
    matches, meta = m.llm_semantic_match(narr, scenes, api_key="")
    check("无 key: 本地兜底 + match_method=local + llm_error 显式",
          meta["match_method"] == "local" and "无 api_key" in meta["llm_error"]
          and len(matches) == 1, f"{meta}")


# ---------------------------------------------------------------- 4. 阶段二 ost

def test_ost_parse_and_clamp():
    script = "第一句解说。【原片:00:12 - 00:30】第二句解说【原片:1:02:03 - 1:05:30】尾句"
    clean, markers = m.parse_fixed_ost_timelines(script, max_ost=15.0)
    check("OST: 2 个标记解析", len(markers) == 2, f"{markers}")
    check("OST: 18s 标记 END 钳制到 start+15", markers[0]["start"] == 12.0
          and markers[0]["end"] == 27.0, f"{markers[0]}")
    check("OST: 447s 标记钳制", markers[1]["start"] == 3723.0 and markers[1]["end"] == 3738.0,
          f"{markers[1]}")
    check("OST: clean_script 去标记", "【原片" not in clean
          and "第一句解说" in clean and "尾句" in clean, f"{clean!r}")
    check("OST: char_position 递增", markers[0]["char_position"] < markers[1]["char_position"])


def test_insert_ost_sorted():
    split = [{"text": "解说段", "start": 0.0, "end": 5.0, "type": "narration", "ost": False}]
    markers = [{"start": 12.0, "end": 27.0, "char_position": 20, "preceding_text": "第一句解说。"},
               {"start": 40.0, "end": 50.0, "char_position": 8, "preceding_text": "开头。"}]
    out = m.insert_ost_to_split_cam(list(split), markers, video_path="orig.mp4")
    osts = [o for o in out if o.get("ost")]
    check("OST: 2 条插入且按 char_position 排序",
          len(osts) == 2 and osts[0]["char_position"] == 8 and osts[1]["char_position"] == 20,
          f"{osts}")
    check("OST: 条目带 type/source", osts[0]["type"] == "ost" and osts[0]["source"] == "orig.mp4")


# ---------------------------------------------------------------- 5. 节点 run（三 stage 路径）

def _make_ctx(tag):
    root = os.path.join(_PKG, ".testrun", tag)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    store = LocalAssetStore(root_dir=root)
    ctx = {"store": store, "workdir": root, "progress": (lambda p: None),
           "resolve_secret": (lambda ref: "")}
    return ctx, store


def test_node_run_stages():
    from app.registry import autodiscover, get_class
    autodiscover()
    cls = get_class("core/gemini_matcher")
    script = ("第一句解说词，讲了开头。【原片:00:05 - 00:20】\n"
              "第二句解说词，推进情节。\n"
              "第三句解说词，收尾。")
    descriptions = {"scenes": [{"id": 0, "start": 0, "end": 4, "text": "开场画面"},
                               {"id": 1, "start": 4, "end": 10, "text": "剧情推进"},
                               {"id": 2, "start": 10, "end": 16, "text": "结尾画面"}]}

    # ---- stage=match（独立：无 key → 本地兜底）
    ctx, store = _make_ctx("match")
    script_asset = store.save_asset(Asset(id="", kind="SCRIPT"), payload={"text": script})
    desc_asset = store.save_asset(Asset(id="", kind="JSON"), payload=descriptions)
    out = asyncio.run(cls().run(ctx, {"script": script_asset, "descriptions": desc_asset},
                                {"stage": "match", "api_key": "", "llm_backend": "llm_call",
                                 "cps": 4.5, "ost_max": 15.0}))
    matches = store.read_json(out["matches"].id)
    timeline = store.read_json(out["timeline"].id)
    split = store.read_json(out["split_cam"].id)
    report = store.read_json(out["report"].id)
    check("node match: matches 非空 + report.match_method=local",
          len(matches) >= 3 and report["match_method"] == "local", f"{report}")
    check("node match: timeline 为解说段（无 OST）",
          all(it.get("type") in (None, "narration") for it in timeline)
          and all(not it.get("ost") for it in split))
    check("node match: split_cam 含 LAY_INDEX",
          all("LAY_INDEX" in it for it in split), f"{[ (it.get('start'), it.get('end')) for it in split ]}")

    # ---- stage=ost（独立：脚本含原片标记 → 插入）
    ctx2, store2 = _make_ctx("ost")
    script_asset2 = store2.save_asset(Asset(id="", kind="SCRIPT"), payload={"text": script})
    out2 = asyncio.run(cls().run(ctx2, {"script": script_asset2},
                                 {"stage": "ost", "api_key": "", "ost_max": 15.0, "cps": 4.5}))
    split2 = store2.read_json(out2["split_cam"].id)
    report2 = store2.read_json(out2["report"].id)
    osts = [x for x in split2 if x.get("ost")]
    check("node ost: 独立执行插入 1 条 OST（END 钳制 5+15=20）",
          len(osts) == 1 and osts[0]["start"] == 5.0 and osts[0]["end"] == 20.0
          and report2["ost_inserted"] == 1, f"{osts} {report2}")

    # ---- stage=all（串联：match → ost 全流程）
    ctx3, store3 = _make_ctx("all")
    script_asset3 = store3.save_asset(Asset(id="", kind="SCRIPT"), payload={"text": script})
    desc_asset3 = store3.save_asset(Asset(id="", kind="JSON"), payload=descriptions)
    out3 = asyncio.run(cls().run(ctx3, {"script": script_asset3, "descriptions": desc_asset3},
                                 {"stage": "all", "api_key": "", "llm_backend": "llm_call",
                                  "cps": 4.5, "ost_max": 15.0}))
    split3 = store3.read_json(out3["split_cam"].id)
    report3 = store3.read_json(out3["report"].id)
    check("node all: 串联输出含解说段 + OST 条目",
          any(x.get("type") == "ost" for x in split3)
          and any(x.get("type") == "narration" for x in split3)
          and report3["stage"] == "all" and report3["matches"] >= 3,
          f"ost={report3['ost_inserted']} matches={report3['matches']}")


def test_node_run_llm_ok():
    """带 mock LLM 的完整串联（llm_call 后端经 mock 端点）。"""
    from app.registry import get_class
    srv, url = _mock_server()
    try:
        cls = get_class("core/gemini_matcher")
        ctx, store = _make_ctx("llm_ok")
        script = "第一段。\n第二段。\n第三段。"
        desc = {"scenes": [{"id": 0, "start": 0, "end": 3, "text": "a"},
                           {"id": 1, "start": 3, "end": 6, "text": "b"},
                           {"id": 2, "start": 6, "end": 9, "text": "c"}]}
        sa = store.save_asset(Asset(id="", kind="SCRIPT"), payload={"text": script})
        da = store.save_asset(Asset(id="", kind="JSON"), payload=desc)
        out = asyncio.run(cls().run(ctx, {"script": sa, "descriptions": da},
                                    {"stage": "all", "api_key": "k", "base_url": url,
                                     "model": "m", "llm_backend": "llm_call",
                                     "batch_size": 2, "retries": 0}))
        rep = store.read_json(out["report"].id)
        matches = store.read_json(out["matches"].id)
        check("node llm: report.match_method=llm 且 3 条匹配",
              rep["match_method"] == "llm" and len(matches) == 3
              and rep["llm_calls"] >= 1, f"{rep}")
    finally:
        srv.shutdown()


def main():
    test_parse_all_des_txt()
    test_load_scenes_forms()
    test_tts_duration()
    test_adapt_scene_duration()
    test_fix_overlapping()
    test_local_match_no_key()
    test_llm_match_both_backends()
    test_llm_match_bad_items_and_missing()
    test_no_key_fallback_path()
    test_ost_parse_and_clamp()
    test_insert_ost_sorted()
    test_node_run_stages()
    test_node_run_llm_ok()
    print("=" * 60)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())