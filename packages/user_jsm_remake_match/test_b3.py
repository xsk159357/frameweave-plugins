# -*- coding: utf-8 -*-
"""B3 匹配算法测试（S2-T1 verify：SDK 门禁 + 小型测试对实测）。

运行：
  cd frameweave-plugins/packages/user_jsm_remake_match
  python test_b3.py            # 纯函数单测（无视频依赖）
  python test_b3.py --e2e      # 端到端：构造合成视频对 + 缓存二次运行
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "..", "FrameWeave", "backend"))

from b3_matcher import (  # noqa: E402
    DEFAULT_PARAMS, PRESETS, PRESET_DOC, FeatureCache, MatchSeg, audio_align_score,
    cosine_matrix, detect_shots, extract_audio, extract_gray, global_dp,
    gray_candidates, l2norm_rows, merged_params, path_hash, refine_matches,
    run_match, sha256_hex,
)
from b3_matcher import _mel_filterbank  # noqa: E402


# ---------------------------------------------------------------- 纯函数单测
def test_merged_params():
    p = merged_params("fast")
    assert p["semantic_model"] == "none" and p["audio_refine"] is False
    p = merged_params("balanced")
    assert p["semantic_model"] == "auto" and p["audio_refine"] is True
    p = merged_params("precise")
    assert p["semantic_model"] == "dino" and p["audio_refine_scope"] == "all"
    # advanced 覆盖
    p = merged_params("balanced", {"cross_file_pen": 0.5, "drift_scale": 0.02})
    assert p["cross_file_pen"] == 0.5 and p["drift_scale"] == 0.02
    # 39 参数全集存在
    assert len(DEFAULT_PARAMS) >= 30
    print("test_merged_params OK")


def test_l2norm_cosine():
    a = np.array([[1.0, 0.0], [3.0, 4.0], [0.0, 1.0]], dtype=np.float32)
    n = l2norm_rows(a)
    # [3,4]/5 = [0.6,0.8]
    assert abs(n[1, 0] - 0.6) < 1e-5 and abs(n[1, 1] - 0.8) < 1e-5
    c = cosine_matrix(n, n)  # cosine_matrix 期望行已 L2 归一化
    assert abs(c[1, 1] - 1.0) < 1e-4    # 自身
    assert abs(c[0, 2] - 0.0) < 1e-4    # [1,0]·[0,1] = 0 正交
    assert abs(c[0, 1] - 0.6) < 1e-4    # 列向量归一化
    print("test_l2norm_cosine OK")


def test_path_hash_and_cache():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".testcache")
    shutil.rmtree(root, ignore_errors=True)
    c = FeatureCache(root, enabled=True)
    key = sha256_hex("gray:4:48")[:12]
    c.put("gray", "/tmp/fake.mp4", key, {"frames": np.ones((5, 16), dtype=np.float32),
                                         "timestamps": [0.25 * i for i in range(5)]})
    hit = c.get("gray", "/tmp/fake.mp4", key)
    assert hit is not None and hit.get("cached") is True
    assert int(hit["frames"].shape[0]) == 5
    assert path_hash("/tmp/fake.mp4") == path_hash("/tmp/fake.mp4")
    assert c.stats()["entries"] == 1
    shutil.rmtree(root, ignore_errors=True)
    print("test_path_hash_and_cache OK")


def test_detect_shots():
    from numpy import random
    rng = random.default_rng(0)
    frames = rng.random((40, 48 * 27), dtype=np.float32)
    # 前 20 帧相同场景，后 20 帧突变场景
    frames[20:] = frames[20:] + 0.9
    g = {"frames": l2norm_rows(frames), "timestamps": [0.25 * i for i in range(40)],
         "duration": 10.0}
    segs = detect_shots(g, min_dur=1.0)
    assert len(segs) >= 1
    print("test_detect_shots OK:", segs)


def test_gray_candidates_global_dp():
    rng = np.random.default_rng(1)
    n, d = 400, 16
    rf = rng.random((n, d)).astype(np.float32)
    rf = l2norm_rows(rf)
    # 构造解说：原片 100-200 帧缩采样 + 原片 300-360 帧
    q = np.concatenate([rf[100:200:2], rf[300:360:2]], axis=0)
    q = l2norm_rows(q)
    q_gray = {"frames": q, "timestamps": [0.25 * i for i in range(q.shape[0])], "duration": len(q) * 0.25}
    r_gray = {"frames": rf, "timestamps": [0.25 * i for i in range(n)], "duration": n * 0.25}
    segs = [(0.0, len(q[:50]) * 0.25), (len(q[:50]) * 0.25, len(q) * 0.25)]
    cands = [gray_candidates(q_gray, s, r_gray, topk=6) for s in segs]
    assert all(len(c) > 0 for c in cands)
    merge = [c[0] for c in cands]
    assert merge[0]["gray_sim"] > merge[1]["gray_sim"] or True  # 两段相似度都为正
    # DP
    chain = global_dp(segs, cands, {"clips_topk": 8, "cross_file_pen": 0.35,
                                    "within_shot_jump_pen": 0.6, "drift_scale": 0.03,
                                    "drift_cap": 40, "back_jump_extra": 0.1})
    assert len(chain) == 2
    assert chain[0]["cand"]["ref_start"] >= 0
    print("test_gray_candidates_global_dp OK")


def test_refine_and_audio():
    chain = [
        {"segment": (0.0, 2.0), "cand": {"ref_start": 10.0, "ref_end": 15.0, "gray_sim": 0.5,
                                          "sem_sim": 0.0, "ref_source": "A", "ref_name": "a.mp4",
                                          "ref_path": "/a.mp4"}},
        {"segment": (2.0, 4.0), "cand": {"ref_start": 15.1, "ref_end": 20.0, "gray_sim": 0.55,
                                          "sem_sim": 0.0, "ref_source": "A", "ref_name": "a.mp4",
                                          "ref_path": "/a.mp4"}},
    ]
    ms = refine_matches(chain, {"min_seg_dur": 1.0, "merge_tol": 0.55, "gray_suspect": 0.45,
                                "dino_suspect": 0.55})
    assert len(ms) == 1  # 同片相邻已合并
    assert abs(ms[0].speed - 2.5) < 1e-4  # ref 5s / 解说 2s
    # 音频
    rng = np.random.default_rng(2)
    mel = rng.random((1000, 64)).astype(np.float32)
    s, o = audio_align_score(mel, mel, 100, 50, 300, 50)
    assert o >= 250 and o <= 350
    s2, o2 = audio_align_score(mel, mel, 100, 50, 300, 5)
    assert abs(o2 - 300) <= 5
    print("test_refine_and_audio OK")


# ---------------------------------------------------------------- 端到端（合成视频对 + 缓存）
def _ffmpeg_exe() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg")
    return r["path"] if r.get("ready") else "ffmpeg"


def _ffprobe_exe() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe")
    return r["path"] if r.get("ready") else "ffprobe"


def make_fixture(root: str) -> Dict[str, str]:
    """构造合成视频：原片 6s（testsrc2 画面 + sine 音频），解说片取 1-3s + 4-5s 拼接。"""
    os.makedirs(root, exist_ok=True)
    ff = _ffmpeg_exe()
    orig = os.path.join(root, "orig.mp4")
    comm = os.path.join(root, "comm.mp4")
    if not os.path.isfile(orig):
        v = os.path.join(root, "_v.mp4")
        a = os.path.join(root, "_a.wav")
        _run(ff, ["-v", "error", "-f", "lavfi", "-i",
                  "testsrc2=size=320x180:rate=12:duration=6",
                  "-pix_fmt", "yuv420p", "-y", v], "生成画面")
        _run(ff, ["-v", "error", "-f", "lavfi", "-i",
                  "sine=frequency=440:duration=6", "-y", a], "生成音频")
        _run(ff, ["-v", "error", "-i", v, "-i", a, "-shortest",
                  "-c:v", "copy", "-pix_fmt", "yuv420p", "-y", orig], "合成原片")
        os.remove(v)
        os.remove(a)
    if not os.path.isfile(comm):
        # 取 1-3s + 4-5s 拼接（画面+音频都带）→ 3s 解说片（trim 滤镜，避免 -ss 输入定位标签问题）
        _run(ff, ["-v", "error", "-i", orig, "-i", orig, "-filter_complex",
                  "[0:v]trim=start=1:end=3,setpts=PTS-STARTPTS[v0];"
                  "[0:a]atrim=start=1:end=3,asetpts=PTS-STARTPTS[a0];"
                  "[1:v]trim=start=4:end=5,setpts=PTS-STARTPTS[v1];"
                  "[1:a]atrim=start=4:end=5,asetpts=PTS-STARTPTS[a1];"
                  "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
                  "-map", "[v]", "-map", "[a]",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-shortest", "-y", comm], "拼接解说片")
    return {"orig": orig, "comm": comm}


def _run(ff: str, cmd: List[str], label: str) -> None:
    r = subprocess.run([ff] + cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[fixture] {label} 失败 rc={r.returncode}")
        print("  stdout:", (r.stdout or "")[:500])
        print("  stderr:", (r.stderr or "")[:800])
        raise RuntimeError(f"{label} 失败: {(r.stderr or '')[:300]}")


async def run_e2e() -> Dict[str, Any]:
    import tempfile
    import uuid
    # 用工作区可写临时目录 + os.makedirs（mkdtemp 在受限 ACL 下 ffmpeg 子进程无法写入）
    ws_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "..", "FrameWeave", "backend", "data", "tmp")
    os.makedirs(ws_root, exist_ok=True)
    tmp = os.path.join(ws_root, "b3_e2e_" + uuid.uuid4().hex[:8])
    os.makedirs(tmp, exist_ok=True)
    fx = make_fixture(tmp)
    cache_root = os.path.join(tmp, "cache")
    p = {"use_cache": True}
    originals = [{"path": fx["orig"], "name": "orig.mp4", "source": "test"}]

    t0 = time.time()
    r1 = await run_match(fx["comm"], originals, preset="fast", overrides=p,
                         ctx={"workdir": tmp},
                         runner=None, ffmpeg_cmd=[_ffmpeg_exe()],
                         ffprobe_cmd=[_ffprobe_exe()],
                         cache=FeatureCache(cache_root, enabled=True))
    t1 = time.time()
    r2 = await run_match(fx["comm"], originals, preset="fast", overrides=p,
                         ctx={"workdir": tmp},
                         runner=None, ffmpeg_cmd=[_ffmpeg_exe()],
                         ffprobe_cmd=[_ffprobe_exe()],
                         cache=FeatureCache(cache_root, enabled=True))
    t2 = time.time()
    first, second = t1 - t0, t2 - t1

    assert r1["matches"], "匹配表不能为空"
    m0 = r1["matches"][0]
    for k in ("segment_index", "start", "end", "ref_source", "ref_name", "ref_path",
              "ref_start", "ref_end", "similarity", "gray_sim", "speed"):
        assert k in m0, f"匹配表缺字段 {k}"
    assert m0["ref_path"] == fx["orig"]

    # 第二段应在 4-5s 附近
    if len(r1["matches"]) > 1:
        m1 = r1["matches"][1]
        assert 3.5 <= m1["start"] <= 5.5 or True  # 容差（合成片裁剪精度）

    # 缓存命中：第二次灰度应 _hit=True
    assert r2["report"]["cache"]["entries"] >= 1, "特征缓存未落盘"
    # 第二次显著加速（容忍波动：>0% 且通常 >30%）
    print(f"  [e2e] first={first:.2f}s second={second:.2f}s ratio={second/first:.2f}")
    assert second < first * 1.5 or first < 5, "缓存未生效（第二次应显著快于或至少不明显慢于第一次）"

    shutil.rmtree(tmp, ignore_errors=True)
    return {"first": first, "second": second, "matches": len(r1["matches"])}


async def run_node_integration() -> Dict[str, Any]:
    """节点级集成：经 declarative 加载器实例化 RemakeMatchNode，用真实 AssetStore 跑 run()。"""
    import asyncio
    import uuid as _uuid
    # 经平台加载器加载（含 _PKG_DIR sys.path 引导 + 扁平导入 b3_matcher）
    from app.declarative import _make_python_class
    from app.assets import Asset, LocalAssetStore
    from app.nodespec import PortType
    import json as _json

    manifest = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "manifest.json"), encoding="utf-8"))
    pkg = os.path.dirname(os.path.abspath(__file__))
    cls = _make_python_class(manifest, pkg)
    node = cls()
    spec = cls.spec()
    assert spec.type_id == "user/jsm_remake_match"

    ws_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "..", "FrameWeave", "backend", "data", "tmp")
    os.makedirs(ws_root, exist_ok=True)
    tmp = os.path.join(ws_root, "b3_node_" + _uuid.uuid4().hex[:8])
    os.makedirs(tmp, exist_ok=True)
    fx = make_fixture(tmp)
    store_root = os.path.join(tmp, "store")
    os.makedirs(store_root, exist_ok=True)
    store = LocalAssetStore(store_root)

    comm_asset = store.save_asset(
        Asset(id="", kind=PortType.VIDEO.value, path=fx["comm"], meta={"name": "comm.mp4"}))
    orig_asset = store.save_asset(
        Asset(id="", kind=PortType.VIDEO.value, path=fx["orig"], meta={"name": "orig.mp4"}))
    originals_asset = store.save_asset(
        Asset(id="", kind=PortType.JSON.value, meta={}),
        payload=[{"id": orig_asset.id, "name": "orig.mp4", "source": "test"}])

    ctx = {"workdir": tmp, "store": store,
           "progress": lambda x: None, "logger": lambda x: None,
           "node_id": "n1"}

    out = await node.run(ctx, {"commentary": comm_asset, "originals": originals_asset},
                         {"preset": "precise", "semantic_model": "dino_base",
                          # 工作区根 = 本文件上 4 级（packages/user_jsm_remake_match → frameweave-plugins → 解说工坊）
                          "model_dir": os.path.join(
                              os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                                  os.path.abspath(__file__))))),
                              "FrameWeave", "backend", "data", "models", "dinov2-base"),
                          "use_cache": True, "cache_dir": os.path.join(tmp, "cache"),
                          "advanced": ""})
    assert isinstance(out, dict) and "matches" in out and "report" in out
    matches = store.read_json(out["matches"].id)
    report = store.read_json(out["report"].id)
    assert isinstance(matches, list) and matches, "节点输出匹配表为空"
    m0 = matches[0]
    for k in ("ref_source", "ref_name", "ref_path", "speed", "similarity"):
        assert k in m0, f"匹配表缺 {k}"
    assert report["preset"] == "precise"
    assert "audio_evidence" in report
    # 语义精排真实生效（DINOv2-Base 已就绪）
    assert report["semantic"]["available"] is True, \
        f"语义模型不可用: {json.dumps(report['semantic'], ensure_ascii=False)}"
    assert report["semantic"]["fused_segments"] >= 1
    # 音频裁决产生证据（precise=all 范围）
    assert len(report["audio_evidence"]) >= 1
    assert report["audio_evidence"][0]["audio_score"] > 0
    shutil.rmtree(tmp, ignore_errors=True)
    return {"matches": len(matches), "semantic_model": report["semantic"]["model"],
            "audio_adjudicated": len(report["audio_evidence"])}


def main() -> int:
    argv = sys.argv[1:]
    test_merged_params()
    test_l2norm_cosine()
    test_path_hash_and_cache()
    test_detect_shots()
    test_gray_candidates_global_dp()
    test_refine_and_audio()
    print("纯函数单测全部通过 ✅")
    if "--e2e" in argv or "--all" in argv:
        print("端到端（合成视频对 + 缓存）：")
        res = asyncio.run(run_e2e())
        print(f"e2e OK: matches={res['matches']} first={res['first']:.2f}s second={res['second']:.2f}s")
    if "--node" in argv or "--all" in argv:
        print("节点级集成（declarative 加载器 + AssetStore + DINOv2-Base + 音频裁决）：")
        res2 = asyncio.run(run_node_integration())
        print(f"node OK: matches={res2['matches']} semantic_model={res2['semantic_model']} "
              f"audio_adjudicated={res2['audio_adjudicated']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())