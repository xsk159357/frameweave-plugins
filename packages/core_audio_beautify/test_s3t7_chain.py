"""S3-T7 core/audio_beautify（C7 参考音频美化）验收测试。

pytest 收集兼容；纯 stdlib + 平台 app 直跑：
    python test_s3t7_chain.py

覆盖（对齐 08 §S3-T7 acceptance/verify）：
1. 输出格式契约：24k/单声道/≤30s wav（真实链：core/vocal_separate 去 BGM（demucs 桩，
   pip demo 缺失环境下桩替代，调用/回退语义真实）→ core/audio_master 真实验证剪气口+
   loudnorm → ffmpeg 成品转换），ffprobe 断言 sample_rate=24000/channels=1/duration≤30；
2. <3s 明确报错（invalid: 前缀 + 过短文案）；
3. 30s 上限截断（max_seconds=5 输入 8s → 输出 ≤5.4s、report.capped=true + 告警）；
4. 子进程隔离（T3 教训）：demucs 阶段失败（worker 崩溃语义）→ STAGE_DEMUCS_FAILED
   code + 降级原音出片，不阻断不崩溃；run_tracked 经 ctx 透传到链节点。
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

os.environ["FRAMEWEAVE_USER_NODES"] = os.path.abspath(os.path.join(_REPO, "..", "packages"))

_SCRATCH = os.path.join(os.path.dirname(_REPO), ".tmp", "s3t7_scratch")
os.makedirs(_SCRATCH, exist_ok=True)
atexit.register(lambda: shutil.rmtree(_SCRATCH, ignore_errors=True))

import app.declarative as decl  # noqa: E402
import app.assets as A  # noqa: E402

decl.scan()

import core_audio_beautify.node as bn  # noqa: E402

from app.plugin import deps  # noqa: E402


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


class _Proc:
    def __init__(self, returncode, stdout, stderr, text):
        self.returncode = returncode
        if text:
            self.stdout = stdout.decode("utf-8", "replace")
            self.stderr = stderr.decode("utf-8", "replace")
        else:
            self.stdout = stdout
            self.stderr = stderr


async def _run_tracked(argv, timeout=60.0, text=False, stdout=None, stderr=None):
    import subprocess as sp
    proc = await asyncio.to_thread(
        lambda: sp.run(argv, capture_output=True, timeout=min(float(timeout), 240.0)))
    return _Proc(proc.returncode, proc.stdout, proc.stderr, text)


def _ffprobe():
    return deps.resolve_binary("ffprobe", env=os.environ)["path"]


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, timeout=240)
    if r.returncode != 0:
        raise RuntimeError("cmd 失败: %s" % r.stderr.decode("utf-8", "replace")[:400])
    return r.stdout.decode("utf-8", "replace")


def make_audio(seconds, name):
    p = os.path.join(_SCRATCH, name)
    if not os.path.exists(p):
        ff = deps.resolve_binary("ffmpeg", env=os.environ)["path"]
        _run([ff, "-v", "error", "-y", "-f", "lavfi",
              "-i", "sine=frequency=440:duration=%d" % seconds,
              "-ar", "44100", "-ac", "1", p])
    return p


def _ctx():
    return {"store": FakeStore(), "progress": lambda p: None,
            "workdir": _SCRATCH, "run_tracked": _run_tracked}


def _audio_asset(store, path, dur):
    return store.save_asset(
        A.Asset(id="", kind="AUDIO", path=path, meta={"duration": dur}))


def _stub_vocals(store, src_path):
    """demucs 桩：真实 ffmpeg 产出 '人声' wav（demucs pip 包缺失环境下替代去 BGM 产物）。"""
    out = os.path.join(_SCRATCH, "vocals_stub.wav")
    ff = deps.resolve_binary("ffmpeg", env=os.environ)["path"]
    _run([ff, "-v", "error", "-y", "-i", src_path, "-c:a", "pcm_s16le", out])
    return _audio_asset(store, out, 0.0)


def _probe_format(path):
    out = _run([_ffprobe(), "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate,channels",
                "-show_entries", "format=duration", "-of", "json", path])
    d = json.loads(out)
    st = (d.get("streams") or [{}])[0]
    return {"sample_rate": int(st.get("sample_rate") or 0),
            "channels": int(st.get("channels") or 0),
            "duration": float((d.get("format") or {}).get("duration") or 0)}


# ================================================================ 格式契约
def test_format_contract_24k_mono_30s():
    """① 输出 24k/单声道/≤30s wav（真实 master + finalize；demucs 桩替代去 BGM 产物）。"""
    import asyncio
    store = _ctx()["store"]
    src = make_audio(4, "ref_4s.wav")
    audio_a = _audio_asset(store, src, 4.0)
    orig = bn._invoke

    async def _invoke_stub(type_id, ctx_, inputs, params):
        if type_id == "core/vocal_separate":
            return {"vocals": _stub_vocals(store, inputs["audio"].path)}
        return await orig(type_id, ctx_, inputs, params)

    bn._invoke = _invoke_stub
    try:
        node = bn.AudioBeautifyNode()
        _ctx_ = _ctx()
        out = asyncio.run(node.run(_ctx_, {"audio": audio_a},
                                   {"enable_demucs": True, "sample_rate": 24000,
                                    "channels": 1, "min_seconds": 3.0, "max_seconds": 30.0}))
        store = _ctx_["store"]
    finally:
        bn._invoke = orig
    rep = store.read_json(out["report"].id)
    by_name = {s["name"]: s for s in rep["stages"]}
    for st in ("validate", "demucs", "master", "finalize"):
        assert by_name[st]["status"] == "succeeded", by_name[st]
    fmt = _probe_format(out["beautified"].path)
    assert fmt["sample_rate"] == 24000, fmt
    assert fmt["channels"] == 1, fmt
    assert fmt["duration"] <= 30.0, fmt
    assert 3.0 <= fmt["duration"] <= 5.0, fmt
    assert out["beautified"].path.endswith(".wav")
    assert out["beautified"].kind == "AUDIO"
    print("[t7] 格式断言:", fmt)


def test_too_short_rejects():
    """② <3s 明确报错（invalid: 前缀 + 过短文案），不静默处理。"""
    import asyncio
    store = _ctx()["store"]
    src = make_audio(2, "ref_2s.wav")
    audio_a = _audio_asset(store, src, 2.0)
    node = bn.AudioBeautifyNode()
    try:
        asyncio.run(node.run(_ctx(), {"audio": audio_a},
                             {"min_seconds": 3.0, "max_seconds": 30.0}))
        raise AssertionError("应抛错")
    except RuntimeError as e:
        msg = str(e)
        assert msg.startswith("invalid:"), msg
        assert "过短" in msg, msg


def test_cap_30s():
    """③ 30s 上限：输入超限 → 截断到 max_seconds，输出 ≤上限，report 标记 capped。"""
    import asyncio
    store = _ctx()["store"]
    src = make_audio(8, "ref_8s.wav")
    audio_a = _audio_asset(store, src, 8.0)
    orig = bn._invoke

    async def _invoke_stub(type_id, ctx_, inputs, params):
        if type_id == "core/vocal_separate":
            return {"vocals": _stub_vocals(store, inputs["audio"].path)}
        return await orig(type_id, ctx_, inputs, params)

    bn._invoke = _invoke_stub
    try:
        node = bn.AudioBeautifyNode()
        _ctx_ = _ctx()
        out = asyncio.run(node.run(_ctx_, {"audio": audio_a},
                                   {"enable_demucs": True, "sample_rate": 24000,
                                    "channels": 1, "min_seconds": 3.0, "max_seconds": 5.0}))
        store = _ctx_["store"]
    finally:
        bn._invoke = orig
    rep = store.read_json(out["report"].id)
    assert rep["input"]["capped"] is True, rep
    assert rep["input"]["capped_seconds"] == 5.0
    fmt = _probe_format(out["beautified"].path)
    assert fmt["duration"] <= 5.4, fmt
    assert any("截断" in w for w in rep["warnings"]), rep["warnings"]


def test_subprocess_isolation_demucs_failure():
    """④ 子进程隔离（T3 教训）：demucs 阶段失败（worker 崩溃语义）→ 降级原音出片不崩溃。"""
    import asyncio
    store = _ctx()["store"]
    src = make_audio(4, "ref_iso_4s.wav")
    audio_a = _audio_asset(store, src, 4.0)
    calls = []
    orig = bn._invoke

    async def _invoke_fail(type_id, ctx_, inputs, params):
        calls.append(type_id)
        if type_id == "core/vocal_separate":
            assert callable(ctx_.get("run_tracked")), "demucs 阶段必须经 run_tracked 子进程通道"
            raise RuntimeError("worker 崩溃（测试注入）")
        return await orig(type_id, ctx_, inputs, params)

    bn._invoke = _invoke_fail
    try:
        node = bn.AudioBeautifyNode()
        _ctx_ = _ctx()
        out = asyncio.run(node.run(_ctx_, {"audio": audio_a},
                                   {"enable_demucs": True, "min_seconds": 3.0,
                                    "max_seconds": 30.0}))
        store = _ctx_["store"]
    finally:
        bn._invoke = orig
    rep = store.read_json(out["report"].id)
    by_name = {s["name"]: s for s in rep["stages"]}
    assert by_name["demucs"]["status"] == "failed", by_name["demucs"]
    assert by_name["demucs"]["code"] == "STAGE_DEMUCS_FAILED", by_name["demucs"]
    assert any("STAGE_DEMUCS_FAILED" in w for w in rep["warnings"]), rep["warnings"]
    # 降级原音出片：成品仍产出且格式契约保持
    assert os.path.isfile(out["beautified"].path)
    fmt = _probe_format(out["beautified"].path)
    assert fmt["sample_rate"] == 24000 and fmt["channels"] == 1, fmt
    print("[t7] 隔离降级:", by_name["demucs"]["code"], "| 成品仍然出片")


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