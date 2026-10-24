"""S3-T3 真实小视频夹具实测 — 临时驱动（验证 S3-T3 verify 项）。

使用真实 ffmpeg 生成 4s 测试视频（testsrc+正弦音），走 A4/A9 节点的真实
_deextract_frames（ffmpeg 抽帧）路径，仅视觉 LLM 用桩替代（保住确定性）。
输出：抽帧数量/描述条目/耗时至 stdout。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

BACKEND = r"F:\1223\解说工坊\FrameWeave\backend"
sys.path.insert(0, BACKEND)
PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)
sys.path.insert(0, REPO)
sys.path.insert(0, PKG)

from app.plugin import deps  # noqa: E402  真实平台依赖解析

WORK = os.path.abspath(os.path.join(REPO, "..", ".."))
SCRATCH = os.path.join(WORK, ".tmp", "s3t3_fixture_real")
os.makedirs(SCRATCH, exist_ok=True)

VIDEO = os.path.join(SCRATCH, "fixture_4s.mp4")


class FakeStore:
    def __init__(self):
        self._a = {}
        self._n = 0
        self.root = SCRATCH
        self.files_dir = SCRATCH

    def read_json(self, aid):
        return self._a.get(aid)

    def save_asset(self, asset, payload=None):
        self._n += 1
        aid = f"aid{self._n}"
        self._a[aid] = payload
        asset.id = aid
        asset.meta = dict(asset.meta or {})
        return asset


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"cmd 失败 {cmd}: {r.stderr.decode('utf-8','replace')[:500]}")
    return r.stdout.decode("utf-8", "replace")


def make_video():
    ff = deps.resolve_binary("ffmpeg", env=os.environ)["path"]
    if not os.path.exists(VIDEO):
        _run([ff, "-v", "error", "-y", "-f", "lavfi",
              "-i", "testsrc2=size=320x180:rate=10:duration=4",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
              "-shortest", VIDEO])
    return ff


def fake_vision(images_b64, prompt, api_key, base_url, model, temperature, max_tokens):
    time.sleep(0.01)
    return ('{"shot_type":"wide","visual_description":"彩条测试图，画面连续变化，'
            '时间 4 秒。","audio_content":"440Hz 正弦音"}')
import core_analyze_chunks.node as ac  # noqa: E402
import core_analyze_basic.node as ab  # noqa: E402

ac._chat_gemini_vision = fake_vision
ab._chat_gemini_vision = fake_vision
ac._BACKOFF = 0.01
ab._BACKOFF = 0.01


def main():
    ff = make_video()
    duration = float(_run([deps.resolve_binary("ffprobe", env=os.environ)["path"],
                           "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=noprint_wrappers=1:nokey=1", VIDEO]))
    store = FakeStore()
    ctx = {"store": store, "progress": lambda p: None, "workdir": SCRATCH}
    import app.assets as A
    video = store.save_asset(
        A.Asset(id="", kind="VIDEO", path=VIDEO,
                meta={"container": {"duration": duration}}))

    chunks = {"schema_version": 1, "chunks": [
        {"chunk_index": 0, "start": 0.0, "end": 2.0, "scene_indices": [0],
         "video_time_offset": 0.0},
        {"chunk_index": 1, "start": 2.0, "end": 4.0, "scene_indices": [1],
         "video_time_offset": 2.0},
    ]}
    chunks_a = store.save_asset(A.Asset(id="", kind="JSON"), payload=chunks)

    t0 = time.time()
    a4 = ac.AnalyzeChunksNode()
    out4 = asyncio.run(a4.run(ctx, {"video": video, "chunks": chunks_a},
                              {"provider": "gemini", "api_key": "test",
                               "base_url": "https://generativelanguage.googleapis.com/v1beta",
                               "concurrency": 2, "retries": 3, "frames_per_chunk": 3,
                               "frame_scale": 224}))
    dt4 = time.time() - t0
    d4 = store.read_json(out4["descriptions"].id)
    report4 = store.read_json(out4["report"].id)
    frames_dir = os.path.join(SCRATCH, "analyze_chunks_frames")
    n_frames = len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
    print(json.dumps({
        "a4": {
            "elapsed_s": round(dt4, 3), "items": len(d4["items"]),
            "real_frames_extracted": n_frames,
            "chunk_ok": report4["ok"], "failed": report4["failed_chunks"],
            "path_compare": report4["path_compare"],
            "sample_item": d4["items"][0],
        }
    }, ensure_ascii=False, indent=2))

    t1 = time.time()
    a9 = ab.AnalyzeBasicNode()
    out9 = asyncio.run(a9.run(ctx, {"video": video},
                              {"provider": "gemini", "api_key": "test",
                               "base_url": "https://generativelanguage.googleapis.com/v1beta",
                               "frame_batch": 6, "frames_per_group": 2,
                               "concurrency": 5, "retries": 2, "frame_scale": 224}))
    dt9 = time.time() - t1
    d9 = store.read_json(out9["description_items"].id)
    r9 = store.read_json(out9["report"].id)
    print(json.dumps({
        "a9": {
            "elapsed_s": round(dt9, 3),
            "items": len(d9["description_items"]),
            "summary": d9["summary"],
            "params": d9["params"],
            "sample_item": d9["description_items"][0],
        }
    }, ensure_ascii=False, indent=2))
    assert len(d4["items"]) == 2 and not report4["failed_chunks"]
    assert n_frames >= 4  # 真实 ffmpeg 抽帧（2 块 × 3 时间点，非占位）
    assert len(d9["description_items"]) == 3  # 6 帧/2 = 3 组
    print("FIXTURE_OK")


if __name__ == "__main__":
    main()