# -*- coding: utf-8 -*-
"""S3T15 加固项①验收测试：core/audio_align Whisper 子进程隔离（T3 收口）。

运行（backend/.venv）：
    cd FrameWeave/backend
    .venv\\Scripts\\python.exe F:\\1223\\解说工坊\\frameweave-plugins\\packages\\core_audio_align\\test_s3t15_align.py

覆盖：
  1. 子进程数据流：fake Whisper worker 写词级 JSON → 节点读回 → difflib 对齐成功
     （验证 _run_whisper_words 走 tracked 子进程而非进程内加载）。
  2. 崩溃注入：worker 以 os._exit(137) 自杀 → 主进程存活、节点 fallback 分切、
     report.warnings 含 Whisper 失败标记（子进程隔离断言）。
  3. worker 非零退出/产物损坏 → 同样 fallback 不抛主进程。
  4. 回归：_norm/_auto_split_text/align_words_to_segments/estimate 纯函数在案。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

_PKG = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_PKG)
_WORK = os.path.abspath(os.path.join(_REPO, "..", ".."))
_BACKEND = os.path.join(_WORK, "FrameWeave", "backend")
for p in (_PKG, _REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

import core_audio_align.node as m  # noqa: E402
from app.assets import Asset, LocalAssetStore  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("  | " + detail if detail else ""))


def make_wav(path: str, seconds: float = 3.0):
    """生成最小合法 WAV（8k 单声道，时长≈seconds）。"""
    import math
    import struct
    import wave
    sr = 8000
    n = max(1, int(seconds * sr))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = b"".join(struct.pack("<h", int(2000 * math.sin(2 * math.pi * 220 * i / sr)))
                          for i in range(n))
        w.writeframes(frames)
    return path


# ---------------------------------------------------------------- 最小 ctx（run_tracked 真跑）

def make_ctx(tag):
    root = os.path.join(_PKG, ".testrun", tag)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    store = LocalAssetStore(root_dir=root)

    async def run_tracked(cmd, timeout=None, **kw):
        """ffprobe 调用于测试 stub（时长 3.0s）；Whisper worker 真跑（隔离断言用真实 spawn）。"""
        kw.pop("job_id", None)
        head = os.path.basename(cmd[0]) if cmd else ""
        if head in ("ffprobe", "ffprobe.exe"):
            payload = json.dumps({"format": {"duration": "3.0"}}).encode("utf-8")
            return subprocess.CompletedProcess(cmd, 0, payload, b"")
        return await asyncio.to_thread(
            lambda: subprocess.run(cmd, timeout=timeout or 300, capture_output=True, **kw))

    ctx = {"store": store, "workdir": os.path.join(root, "work"),
           "progress": (lambda p: None), "run_tracked": run_tracked}
    os.makedirs(ctx["workdir"], exist_ok=True)
    return ctx, store


def audio_asset(tag):
    p = os.path.join(_PKG, ".testrun", tag + ".wav")
    make_wav(p)
    return Asset(id="", kind="AUDIO", path=p)


def fake_worker(path: str, kind: str):
    """生成 fake Whisper worker：kind=ok 写词 JSON；kind=crash 以 os._exit(137) 自杀；
    kind=badjson 写损坏 JSON。"""
    body = {"ok": ("import json, sys\n"
               "spec = json.loads(sys.argv[1])\n"
               "words = [{\"word\":\"第一\",\"start\":0.1,\"end\":0.5},\n"
               "         {\"word\":\"段解说\",\"start\":0.5,\"end\":1.2},\n"
               "         {\"word\":\"第二\",\"start\":1.5,\"end\":1.8},\n"
               "         {\"word\":\"段\",\"start\":1.8,\"end\":2.0}]\n"
               "open(spec['out_path'],'w',encoding='utf-8').write(json.dumps(words))\n"),
        "crash": ("import sys, os\n"
                  "os._exit(137)\n"),
        "badjson": ("import json, sys\n"
                    "spec = json.loads(sys.argv[1])\n"
                    "open(spec['out_path'],'w',encoding='utf-8').write('not json {\"a\"')\n"),
    }[kind]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def test_subprocess_success_path():
    """子进程数据流：fake worker 写词 → 节点读回 → 对齐成功。"""
    wrk = fake_worker(os.path.join(_PKG, ".testrun", "fake_ok.py"), "ok")
    orig = m._ALIGN_WORKER
    m._ALIGN_WORKER = wrk
    try:
        ctx, store = make_ctx("ok")
        audio = audio_asset("ok")
        script = store.save_asset(Asset(id="", kind="SCRIPT"),
                                  payload={"text": "第一段解说。第二段。"})
        out = asyncio.run(m.AudioAlignNode().run(ctx, {"audio": audio, "script": script},
                                                 {"whisper_model": "small", "language": "zh",
                                                  "tail_pad": 0.3, "fallback": "proportional",
                                                  "model_dir": "", "avg_segment_chars": 25,
                                                  "python_exe": sys.executable}))
        rows = store.read_json(out["aligned"].id)
        rep = store.read_json(out["report"].id)
        check("T3: 子进程路径对齐成功（strategy=whisper_difflib 且非空行）",
              rep.get("strategy_used", "").startswith("whisper")
              and len(rows) >= 1, f"strategy={rep.get('strategy_used')} rows={len(rows)}")
        check("T3: report 无 whisper_error", not rep.get("whisper_error"), f"{rep.get('whisper_error')}")
    finally:
        m._ALIGN_WORKER = orig
        shutil.rmtree(os.path.join(_PKG, ".testrun", "ok"), ignore_errors=True)


def test_crash_injection_isolation():
    """崩溃注入：worker os._exit(137) → 主进程存活 + fallback（隔离断言）。"""
    alive_pid = os.getpid()
    wrk = fake_worker(os.path.join(_PKG, ".testrun", "fake_crash.py"), "crash")
    orig = m._ALIGN_WORKER
    m._ALIGN_WORKER = wrk
    try:
        ctx, store = make_ctx("crash")
        audio = audio_asset("crash")
        script = store.save_asset(Asset(id="", kind="SCRIPT"),
                                  payload={"text": "第一段解说。第二段解说词内容。"})
        out = asyncio.run(m.AudioAlignNode().run(ctx, {"audio": audio, "script": script},
                                                 {"whisper_model": "small", "language": "zh",
                                                  "tail_pad": 0.3, "fallback": "proportional",
                                                  "model_dir": "", "avg_segment_chars": 25,
                                                  "python_exe": sys.executable}))
        rep = store.read_json(out["report"].id)
        check("T3: 崩溃注入后主进程存活", os.getpid() == alive_pid)
        check("T3: 崩溃后走 fallback 不抛异常", rep.get("strategy_used", "").startswith("fallback"),
              f"strategy={rep.get('strategy_used')}")
        check("T3: report.warnings 显式 Whisper 失败标记",
              any("Whisper" in w for w in rep.get("warnings", [])), f"{rep.get('warnings')}")
    finally:
        m._ALIGN_WORKER = orig
        shutil.rmtree(os.path.join(_PKG, ".testrun", "crash"), ignore_errors=True)


def test_worker_bad_output():
    """worker 产物损坏 → fallback 不抛主进程。"""
    wrk = fake_worker(os.path.join(_PKG, ".testrun", "fake_bad.py"), "badjson")
    orig = m._ALIGN_WORKER
    m._ALIGN_WORKER = wrk
    try:
        ctx, store = make_ctx("bad")
        audio = audio_asset("bad")
        script = store.save_asset(Asset(id="", kind="SCRIPT"),
                                  payload={"text": "第一段解说。第二段解说词。"})
        out = asyncio.run(m.AudioAlignNode().run(ctx, {"audio": audio, "script": script},
                                                 {"whisper_model": "small", "language": "zh",
                                                  "tail_pad": 0.3, "fallback": "proportional",
                                                  "model_dir": "", "avg_segment_chars": 25,
                                                  "python_exe": sys.executable}))
        rep = store.read_json(out["report"].id)
        check("T3: 产物损坏 → fallback 且 warnings 标记",
              rep.get("strategy_used", "").startswith("fallback")
              and any("Whisper" in w for w in rep.get("warnings", [])),
              f"{rep.get('strategy_used')} {rep.get('warnings')}")
    finally:
        m._ALIGN_WORKER = orig
        shutil.rmtree(os.path.join(_PKG, ".testrun", "bad"), ignore_errors=True)


# ---------------------------------------------------------------- 纯函数回归

def test_pure_functions():
    check("回归: _norm 去标点", m._norm("第一段，解说！") == "第一段解说")
    segs = m._auto_split_text("第一段解说词。第二段解说词内容更长了。", 10)
    check("回归: _auto_split_text 分段", len(segs) >= 2, f"{segs}")
    words = [{"word": "第一", "start": 0.1, "end": 0.5},
             {"word": "段解说", "start": 0.5, "end": 1.2}]
    sw, ratio = m.align_words_to_segments(words, ["第一段解说"])
    check("回归: align_words_to_segments 命中", len(sw[0]) == 2 and ratio > 0.5,
          f"ratio={ratio}")
    check("回归: _probe_duration 存在（子进程 ffprobe）",
          callable(m._probe_duration))
    check("回归: 常量在案", m._ALIGN_WORKER.endswith("align_worker.py"))


def main():
    test_pure_functions()
    test_subprocess_success_path()
    test_crash_injection_isolation()
    test_worker_bad_output()
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