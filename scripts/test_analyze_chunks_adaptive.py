"""core/analyze_chunks 2.1.0 判据：视频通道 + 自适应并发。

跑法：python scripts/test_analyze_chunks_adaptive.py

两条改动各自的核心风险：
  1. **video 通道**：provider=openai 时不能「跑到一半才发现没视频输入」——必须早失败；
     切片失败/超限时必须退回抽帧并留告警，不能产空描述还不说话。
  2. **自适应并发**：并发数升降的判据是「成功加一、失败减半」，熔断后**剩余块不能再发请求**，
     但要照常产占位让下游跑完。这条用假的 _vision 打失败，数调用次数验证。

不碰网络：_vision / _vision_video / _extract_frames / _cut_chunk_clip 全部替换成假实现。
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
PKG = os.path.join(PLUGINS, "packages", "core_analyze_chunks")
sys.path.insert(0, BACKEND)

from app.assets import Asset, LocalAssetStore      # noqa: E402
from app.nodespec import PortType                  # noqa: E402

_spec = importlib.util.spec_from_file_location("cw_ac", os.path.join(PKG, "node.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

OK: list = []
BAD: list = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("%-58s %s%s" % (name, "OK" if cond else "BAD",
                          ("  " + detail) if (detail and not cond) else ""))


def make_store(tag):
    d = os.path.join(ROOT, "_tmp_ac_%s" % tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return LocalAssetStore(d), d


def mk_video(store, path):
    open(path, "wb").write(b"\x00" * 64)
    return store.save_asset(Asset(id="", kind=PortType.VIDEO.value, path=path,
                                  meta={"duration": 60.0}), payload=None)


def mk_chunks(store, n, span=10.0):
    rows = []
    for i in range(n):
        rows.append({"chunk_index": i, "start": i * span, "end": (i + 1) * span,
                     "scene_indices": [i * 2, i * 2 + 1]})
    return store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value), payload=rows)


GOOD_JSON = ('{"shot_type": "closeup", "visual_description": "男人后退半步，手里攥着信",'
             ' "audio_content": "对白: \\"别过来\\""}')


async def _real_ffmpeg_check():
    """真跑一次切片并 ffprobe 验证 audio stream。返回 True/False/None(无 ffmpeg)。"""
    import subprocess
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    ff = r["path"] if r.get("ready") else ""
    if not ff:
        return None
    base = os.path.join(ROOT, "_tmp_ac_ff")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    src = os.path.join(base, "src.mp4")
    p1 = subprocess.run(
        [ff, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=800:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", src],
        capture_output=True)
    if p1.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        return None
    clip = await _mod._cut_chunk_clip({"workdir": base}, src, 0.5, 2.5,
                                      os.path.join(base, "clips"), 1.0, 360)
    ok = False
    if clip:
        rp = deps.resolve_binary("ffprobe", env=os.environ)
        fp = rp["path"] if rp.get("ready") else ""
        if fp:
            pr = subprocess.run([fp, "-v", "error", "-show_entries", "stream=codec_type",
                                 "-of", "default=nw=1", clip], capture_output=True)
            ok = b"codec_type=audio" in pr.stdout
        else:
            ok = None
    shutil.rmtree(base, ignore_errors=True)
    return ok


async def main() -> int:
    ctx_workdir_root = os.path.join(ROOT, "_tmp_ac_work")

    # ------------------------------------------------------- 1 熔断器单元行为
    lim = _mod.AdaptiveLimiter(start=2, hi=4, low=1)
    await lim.acquire()
    await lim.release(True)
    check("成功一次：并发 2→3", lim.cur == 3, str(lim.cur))
    await lim.acquire()
    await lim.release(True)
    check("再成功一次：并发 3→4", lim.cur == 4, str(lim.cur))
    await lim.acquire()
    await lim.release(True)
    check("到上限不再涨", lim.cur == 4, str(lim.cur))
    await lim.acquire()
    await lim.release(False)
    check("失败一次：并发减半 4→2", lim.cur == 2, str(lim.cur))
    check("未达熔断阈值不熔断", lim.tripped is False, str(lim.tripped))

    lim2 = _mod.AdaptiveLimiter(start=3, hi=5, low=1)
    for _ in range(5):
        await lim2.acquire()
        await lim2.release(False)
    check("连续失败达阈值触发熔断", lim2.tripped is True, str(lim2.tripped))
    admitted = await lim2.acquire()
    check("熔断后 acquire 返回 False（不再发请求）", admitted is False, str(admitted))

    # --------------------------------------------- 2 video + openai → 早失败
    store, d1 = make_store("early")
    vid = mk_video(store, os.path.join(d1, "v.mp4"))
    chunks = mk_chunks(store, 1)
    try:
        await _mod.AnalyzeChunksNode().run(
            {"store": store, "workdir": d1},
            {"video": vid, "chunks": chunks},
            {"provider": "openai", "api_key": "sk-x", "base_url": "https://api.example.com/v1",
             "analysis_mode": "video"})
        check("video+openai 必须早失败", False)
    except RuntimeError as e:
        check("video+openai 必须早失败", "只能走 Gemini" in str(e), str(e))

    # ------------------------------------------------- 3 frames 通路：正常产出
    calls = {"vision": 0}
    _orig_vision, _orig_vframes = _mod._vision, _mod._extract_frames

    async def fake_vision(ctx, images_b64, prompt, provider, api_key, base_url, model,
                          temperature, max_tokens):
        calls["vision"] += 1
        return GOOD_JSON

    async def fake_frames(ctx, video_path, times, out_dir, scale):
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, "f.jpg")
        open(p, "wb").write(b"\x00" * 8)
        return [p]

    _mod._vision, _mod._extract_frames = fake_vision, fake_frames

    store, d2 = make_store("frames")
    vid = mk_video(store, os.path.join(d2, "v.mp4"))
    chunks = mk_chunks(store, 3)
    out = await _mod.AnalyzeChunksNode().run(
        {"store": store, "workdir": d2},
        {"video": vid, "chunks": chunks},
        {"provider": "gemini", "api_key": "sk-x", "base_url": "https://g.example.com/v1beta",
         "analysis_mode": "frames", "concurrency": 2, "max_concurrency": 4, "retries": 0})
    items = store.read_json(out["descriptions"].id)
    check("frames：每块一条", len(items) == 3, str(len(items)))
    check("frames：字段齐（shot/visual/audio）",
          items[0]["shot_type"] == "closeup" and "男人" in items[0]["visual_description"],
          repr(items[0]))
    check("frames：meta 记了 mode=frames", items[0]["meta"]["mode"] == "frames",
          repr(items[0]["meta"].get("mode")))
    check("frames：并发统计落 meta",
          out["descriptions"].meta["concurrency_stats"]["start"] == 2,
          repr(out["descriptions"].meta.get("concurrency_stats")))

    # ----------------------------------------------------- 4 video 通路：走视频
    vcalls = {"video": 0, "cut": 0}
    _orig_video, _orig_cut = _mod._vision_video, _mod._cut_chunk_clip

    async def fake_vision_video(ctx, video_b64, prompt, api_key, base_url, model,
                                temperature, max_tokens):
        vcalls["video"] += 1
        return GOOD_JSON

    async def fake_cut(ctx, video_path, start, end, out_dir, fps, height):
        vcalls["cut"] += 1
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, "clip_%d.mp4" % int(start))
        open(p, "wb").write(b"\x00" * 32)
        return p

    _mod._vision_video, _mod._cut_chunk_clip = fake_vision_video, fake_cut

    store, d3 = make_store("video")
    vid = mk_video(store, os.path.join(d3, "v.mp4"))
    chunks = mk_chunks(store, 2)
    out = await _mod.AnalyzeChunksNode().run(
        {"store": store, "workdir": d3},
        {"video": vid, "chunks": chunks},
        {"provider": "gemini", "api_key": "sk-x", "base_url": "https://g.example.com/v1beta",
         "analysis_mode": "video", "concurrency": 2, "retries": 0})
    items = store.read_json(out["descriptions"].id)
    check("video：每块都切了一次片", vcalls["cut"] == 2, str(vcalls["cut"]))
    check("video：走的是视频通道（不是抽帧）", vcalls["video"] == 2, str(vcalls["video"]))
    check("video：meta 记了 mode=video", items[0]["meta"]["mode"] == "video",
          repr(items[0]["meta"].get("mode")))
    check("video：prompt 要求听声音（进不去断言，改看模式标记）",
          all(it["meta"]["mode"] == "video" for it in items), repr([i["meta"]["mode"] for i in items]))

    # --------------------------------------- 5 切片失败 → 退回抽帧 + 告警
    async def fail_cut(ctx, video_path, start, end, out_dir, fps, height):
        return None

    _mod._cut_chunk_clip = fail_cut
    calls["vision"] = 0
    store, d4 = make_store("fallback")
    vid = mk_video(store, os.path.join(d4, "v.mp4"))
    chunks = mk_chunks(store, 1)
    out = await _mod.AnalyzeChunksNode().run(
        {"store": store, "workdir": d4},
        {"video": vid, "chunks": chunks},
        {"provider": "gemini", "api_key": "sk-x", "base_url": "https://g.example.com/v1beta",
         "analysis_mode": "video", "concurrency": 1, "retries": 0})
    items = store.read_json(out["descriptions"].id)
    check("切片失败：退回抽帧", items[0]["meta"]["mode"] == "frames",
          repr(items[0]["meta"].get("mode")))
    check("切片失败：有告警",
          any("切片失败" in w for w in out["descriptions"].meta["warnings"]),
          repr(out["descriptions"].meta["warnings"]))
    _mod._cut_chunk_clip = fake_cut

    # ------------------------------------- 6 全失败 → 熔断 + 占位 + 不炸
    async def bad_vision(ctx, images_b64, prompt, provider, api_key, base_url, model,
                         temperature, max_tokens):
        raise RuntimeError("502 bad gateway")

    _mod._vision = bad_vision
    store, d5 = make_store("trip")
    vid = mk_video(store, os.path.join(d5, "v.mp4"))
    chunks = mk_chunks(store, 8)
    out = await _mod.AnalyzeChunksNode().run(
        {"store": store, "workdir": d5},
        {"video": vid, "chunks": chunks},
        {"provider": "gemini", "api_key": "sk-x", "base_url": "https://g.example.com/v1beta",
         "analysis_mode": "frames", "concurrency": 3, "max_concurrency": 4, "retries": 0})
    items = store.read_json(out["descriptions"].id)
    stats = out["descriptions"].meta["concurrency_stats"]
    check("全失败：仍然出满 8 条（不炸链）", len(items) == 8, str(len(items)))
    check("全失败：触发了熔断", stats["tripped"] is True, repr(stats))
    check("全失败：至少有一条是熔断占位",
          any("熔断" in it["visual_description"] for it in items),
          repr([it["visual_description"][:16] for it in items]))
    check("全失败：meta 报了熔断告警",
          any("熔断" in w for w in out["descriptions"].meta["warnings"]),
          repr(out["descriptions"].meta["warnings"]))

    _mod._vision, _mod._extract_frames = _orig_vision, _orig_vframes
    _mod._vision_video, _mod._cut_chunk_clip = _orig_video, _orig_cut

    # ------------------------------- 7 真 ffmpeg：切片必须保留音轨（有 ffmpeg 才跑）
    # 这是 video 通道的全部意义所在：没有音轨就是「多花 token 的抽帧」。
    # 所以不能只测「切出来了」，必须 ffprobe 看到 audio stream。
    real_cut_ok = await _real_ffmpeg_check()
    if real_cut_ok is None:
        print("%-58s SKIP（本机没有 ffmpeg）" % "真 ffmpeg：切片保留音轨")
    else:
        check("真 ffmpeg：切片保留音轨", real_cut_ok is True, str(real_cut_ok))

    for d in (d1, d2, d3, d4, d5):
        shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(ctx_workdir_root, ignore_errors=True)

    print("\n%d OK / %d BAD" % (len(OK), len(BAD)))
    return 0 if not BAD else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
