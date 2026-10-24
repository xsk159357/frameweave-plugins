"""S3-T10 user/jsm_koubo_process（E1 口播编排）验收测试。

pytest 收集兼容；纯 stdlib + 平台 app 直跑：
    python test_s3t10_chain.py

覆盖（对齐 08 §S3-T10 acceptance/verify）：
1. S10 随机放大决策（纯函数）：zoom_ratio≈50% 且**相邻段不重复放大**（多种子/多段数全量断言）；
2. zoompan 输出抽查（真实 ffmpeg）：分段 zoompan + 平铺重编码 + concat → 输出时长正确、
   放大段产物存在（zoom_seg_*）且段时长≈原段；
3. 单节点一键出片（真实链）：扫描官方插件仓库后经 app.registry 调用真实
   core/audio_silence_trim → core/audio_master → subtitle_burn → draft_export，
   剪静音/响度/烧录/导出阶段状态 + 成片视频与音轨产出；
4. S13 错误码报告：单阶段失败携带 STAGE_* code + warnings，不阻断出片。
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, r"F:\1223\解说工坊\FrameWeave\backend")

_PKG = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_PKG)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

# 官方插件仓库全量扫描（真实 core/user 链节点可经 get_class 解析）
os.environ["FRAMEWEAVE_USER_NODES"] = os.path.abspath(os.path.join(_REPO, "..", "packages"))

# 测试 scratch 落工作区 .tmp（沙箱对 tempfile.mkdtemp 目录拒绝嵌套写入）
_SCRATCH = os.path.join(os.path.dirname(_REPO), ".tmp", "s3t10_scratch")
os.makedirs(_SCRATCH, exist_ok=True)
atexit.register(lambda: shutil.rmtree(_SCRATCH, ignore_errors=True))

import app.declarative as decl  # noqa: E402
import app.assets as A  # noqa: E402

decl.scan()

import user_jsm_koubo_process.node as kn  # noqa: E402

from app.plugin import deps  # noqa: E402

FIXTURE = os.path.join(_SCRATCH, "koubo_fixture_4s.mp4")


class FakeStore:
    def __init__(self):
        self._a = {}
        self._n = 0
        self.root = _SCRATCH
        self.files_dir = _SCRATCH

    def read_json(self, aid):
        return self._a.get(aid)

    def save_asset(self, asset, payload=None):
        self._n += 1
        aid = f"aid{self._n}"
        self._a[aid] = payload
        asset.id = aid
        asset.meta = dict(asset.meta or {})
        return asset

    def get(self, aid):
        return self._a.get(aid)


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError("cmd 失败: %s" % r.stderr.decode("utf-8", "replace")[:400])
    return r.stdout.decode("utf-8", "replace")


def _ffprobe_duration(path):
    fp = deps.resolve_binary("ffprobe", env=os.environ)["path"]
    out = _run([fp, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path]).strip()
    return float(out) if out else 0.0


def make_fixture():
    if not os.path.exists(FIXTURE):
        ff = deps.resolve_binary("ffmpeg", env=os.environ)["path"]
        _run([ff, "-v", "error", "-y", "-f", "lavfi",
              "-i", "testsrc2=size=320x180:rate=10:duration=4",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
              "-shortest", FIXTURE])
    return FIXTURE


class _Proc:
    """run_tracked 返回对象：text=True 时 stdout/stderr 为 str，否则 bytes（对齐平台语义）。"""

    def __init__(self, returncode, stdout, stderr, text):
        self.returncode = returncode
        if text:
            self.stdout = stdout.decode("utf-8", "replace")
            self.stderr = stderr.decode("utf-8", "replace")
        else:
            self.stdout = stdout
            self.stderr = stderr


async def _run_tracked(argv, timeout=60.0, text=False, stdout=None, stderr=None):
    """真实子进程执行（to_thread + capture；供链节点 ctx.run_tracked）。"""
    import subprocess as sp
    proc = await asyncio.to_thread(
        lambda: sp.run(argv, capture_output=True, timeout=min(float(timeout), 240.0)))
    return _Proc(proc.returncode, proc.stdout, proc.stderr, text)


def _ctx():
    return {"store": FakeStore(), "progress": lambda p: None,
            "workdir": _SCRATCH, "run_tracked": _run_tracked}


# ================================================================ S10 纯函数
def test_choose_zoom_non_adjacent():
    """① zoom_ratio=0.5：~50% 段放大且相邻段绝不重复放大（S10，多种子全量断言）。"""
    for n in range(1, 11):
        for seed in (None, 0, 1, 7, 42, 2026):
            choices = kn.choose_zoom(n, 0.5, seed=seed)
            assert len(choices) == n, (n, seed)
            count = sum(choices)
            assert count == round(n * 0.5), (n, seed, count)
            for i in range(n - 1):
                assert not (choices[i] and choices[i + 1]), \
                    "相邻段重复放大(S10 违反): n=%d seed=%s choices=%s" % (n, seed, choices)


def test_choose_zoom_ratio_param():
    """①b zoom_ratio 参数化 + 确定性（同 seed 可复现；>0.5 受不相邻约束封顶 ceil(n/2)）。"""
    c1 = kn.choose_zoom(6, 0.5, seed=7)
    c2 = kn.choose_zoom(6, 0.5, seed=7)
    assert c1 == c2
    assert sum(kn.choose_zoom(8, 0.25, seed=3)) == 2
    # 0.75 > 封顶 4（8 段最多 ceil(8/2)=4 段不相邻）→ 取满偶位槽
    assert sum(kn.choose_zoom(8, 0.75, seed=3)) == 4


def test_zoom_step_ramp():
    """①c zoompan 步进：段尾达到 amount。"""
    assert kn.zoom_step(1.2, 20) == 0.01
    assert kn.zoom_step(1.0, 30) == 0.0


# ================================================================ zoompan 真实抽查
def test_zoompan_render_real():
    """② 真实 ffmpeg zoompan：分段放大 + concat → 输出时长正确 + 放大段产物抽查。"""
    import asyncio
    video = make_fixture()
    workdir = os.path.join(_SCRATCH, "zoom_check")
    os.makedirs(workdir, exist_ok=True)
    scenes = [{"start": 0.0, "end": 2.0}, {"start": 2.0, "end": 4.0}]
    choices = [True, False]           # 段 0 放大、段 1 平铺（不相邻）
    out = os.path.join(workdir, "zoomed_check.mp4")
    res = asyncio.run(kn.render_zoom(_ctx(), video, scenes, choices,
                                     amount=1.2, width=320, fps=10, out_mp4=out,
                                     workdir=workdir))
    assert res.get("ok"), res
    assert res["zoom_count"] == 1 and res["segments_used"] == 2
    assert os.path.isfile(out)
    dur = _ffprobe_duration(out)
    assert 3.4 <= dur <= 4.6, "concat 输出时长异常: %s" % dur
    # 抽查放大段产物（zoompan 段）与平铺段产物
    zseg = os.path.join(workdir, "zoom_seg_000.mp4")
    fseg = os.path.join(workdir, "zoom_seg_001.mp4")
    assert os.path.isfile(zseg) and os.path.isfile(fseg)
    zdur = _ffprobe_duration(zseg)
    assert 1.6 <= zdur <= 2.4, "放大段时长异常: %s" % zdur
    # 放大段确含 zoompan 输出特征：分辨率等于目标宽（320）→ 放大后仍 320x? 
    fp = deps.resolve_binary("ffprobe", env=os.environ)["path"]
    out_w = _run([fp, "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=width",
                  "-of", "default=noprint_wrappers=1:nokey=1", zseg]).strip()
    assert out_w == "320", "放大段输出宽度异常: %s" % out_w


# ================================================================ 单节点一键出片（真实链）
def test_one_click_chain():
    """③ 真实链一键出片：trim/master/burn/export 阶段 + 成片视频与音轨产出。"""
    import asyncio
    video = make_fixture()
    ctx = _ctx()
    store = ctx["store"]
    video_a = store.save_asset(
        A.Asset(id="", kind="VIDEO", path=video, meta={"container": {"duration": 4.0}}))
    sub_a = store.save_asset(
        A.Asset(id="", kind="SUBTITLE"),
        payload=[{"start": 0.5, "end": 1.5, "text": "大家好"},
                 {"start": 2.0, "end": 3.5, "text": "欢迎收看本期节目"}])
    sc_a = store.save_asset(
        A.Asset(id="", kind="SEGMENTS"),
        payload=[{"index": 0, "start": 0.0, "end": 2.0},
                 {"index": 1, "start": 2.0, "end": 4.0}])
    node = kn.JsmKouboProcessNode()
    out = asyncio.run(node.run(ctx, {"video": video_a, "subtitles": sub_a, "scenes": sc_a},
                               {"enable_asr": False, "enable_trim": True,
                                "enable_master": True, "enable_bgm": True,
                                "enable_zoom": True, "zoom_ratio": 0.5, "zoom_amount": 1.2,
                                "zoom_seed": 7, "zoom_width": 320,
                                "enable_burn": True, "enable_export": True,
                                "output_name": "koubo_test", "draft_name": "口播测试"}))
    report = store.read_json(out["report"].id)
    assert report["ok"] is True, report
    by_name = {s["name"]: s for s in report["stages"]}
    for st in ("audio_extract", "trim", "master", "zoom", "burn", "finalize", "export"):
        assert st in by_name, "缺少阶段 %s: %s" % (st, list(by_name))
    for st in ("audio_extract", "zoom", "finalize"):
        assert by_name[st]["status"] == "succeeded", by_name[st]
    assert not report["zoom"]["adjacent_zoom"], "相邻段重复放大(S10 违反)"
    seg_payload = store.read_json(out["segments"].id)
    assert len(seg_payload) == 2
    for i in range(len(seg_payload) - 1):
        assert not (seg_payload[i]["zoom"] and seg_payload[i + 1]["zoom"])
    # 一键出片：成片视频存在且时长≈4s
    vout = out["video_out"]
    assert vout is not None and os.path.isfile(vout.path), "未产出成片视频"
    dur = _ffprobe_duration(vout.path)
    assert 3.0 <= dur <= 4.8, "成片时长异常: %s" % dur
    # 成片音轨
    aout = out["audio"]
    assert aout is not None and os.path.isfile(aout.path), "未产出成片音轨"
    print("[t25] 一键出片阶段状态:", {s["name"]: s["status"] for s in report["stages"]})


def test_stage_error_code_s13():
    """④ S13 错误码报告：单阶段失败携带 STAGE_* code + warnings，不阻断出片。"""
    import asyncio
    video = make_fixture()
    ctx = _ctx()
    store = ctx["store"]
    video_a = store.save_asset(
        A.Asset(id="", kind="VIDEO", path=video, meta={"container": {"duration": 4.0}}))
    sub_a = store.save_asset(
        A.Asset(id="", kind="SUBTITLE"),
        payload=[{"start": 0.5, "end": 1.5, "text": "大家好"}])
    sc_a = store.save_asset(
        A.Asset(id="", kind="SEGMENTS"),
        payload=[{"index": 0, "start": 0.0, "end": 4.0}])

    orig = kn._invoke

    async def _failing_invoke(type_id, ctx_, inputs, params):
        if type_id == "core/audio_master":
            raise RuntimeError("loudnorm 失败（测试注入）")
        return await orig(type_id, ctx_, inputs, params)

    kn._invoke = _failing_invoke
    try:
        node = kn.JsmKouboProcessNode()
        out = asyncio.run(node.run(ctx, {"video": video_a, "subtitles": sub_a, "scenes": sc_a},
                                   {"enable_asr": False, "enable_trim": True,
                                    "enable_master": True, "enable_bgm": True,
                                    "enable_zoom": True, "zoom_seed": 1, "zoom_width": 320,
                                    "enable_burn": True, "enable_export": True,
                                    "output_name": "koubo_s13"}))
    finally:
        kn._invoke = orig
    report = store.read_json(out["report"].id)
    by_name = {s["name"]: s for s in report["stages"]}
    assert by_name["master"]["status"] == "failed", by_name["master"]
    assert by_name["master"]["code"] == "STAGE_MASTER_FAILED", by_name["master"]
    assert any("STAGE_MASTER_FAILED" in w for w in report["warnings"]), report["warnings"]
    # 失败不阻断出片（finalize 用未响度处理的音轨兜底）
    assert out["video_out"] is not None and os.path.isfile(out["video_out"].path)
    print("[t25] S13 错误码:", by_name["master"]["code"], "| warnings:", report["warnings"][:1])


def main():
    failed = 0
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v) and v.__module__ == __name__]
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print("PASS %s" % name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL %s | %s: %s" % (name, type(e).__name__, e))
    print("=" * 56)
    print("TOTAL FAIL=%d/%d" % (failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())