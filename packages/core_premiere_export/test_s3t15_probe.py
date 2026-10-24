# -*- coding: utf-8 -*-
"""S3T15 加固项②验收测试：core/premiere_export probe 失败显式 warning（R10 收口）。

运行（backend/.venv）：
    cd FrameWeave/backend
    .venv\\Scripts\\python.exe F:\\1223\\解说工坊\\frameweave-plugins\\packages\\core_premiere_export\\test_s3t15_probe.py

覆盖：
  1. probe 失败（ffprobe 不可用 / 非零退出 / 无视频流）→ report.warnings 含
     [probe_failed] 显式标记 + estimated_frames=round(duration×fps)；
  2. probe 成功路径零回归（warnings 为空、source_width/height/duration 真实）；
  3. pr_builder 纯函数回归（B8 源分辨率 / B9 文件去重 / sec_to_frames / path_url）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys

_PKG = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_PKG)
_WORK = os.path.abspath(os.path.join(_REPO, "..", ".."))
_BACKEND = os.path.join(_WORK, "FrameWeave", "backend")
for p in (_PKG, _REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.assets import Asset, LocalAssetStore  # noqa: E402
import core_premiere_export.node as m  # noqa: E402
from core_premiere_export import pr_builder  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("  | " + detail if detail else ""))


def make_ctx(tag, probe_mode="ok"):
    """probe_mode: ok=成功 / missing=ffprobe 缺失 / fail=非零退出 / novideo=无视频流 / raise=异常。"""
    root = os.path.join(_PKG, ".testrun", tag)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    store = LocalAssetStore(root_dir=root)

    async def run_tracked(cmd, timeout=None, **kw):
        kw.pop("job_id", None)
        head = os.path.basename(cmd[0]) if cmd else ""
        if head not in ("ffprobe", "ffprobe.exe"):
            return subprocess.CompletedProcess(cmd, -1, b"", b"not ffprobe")
        if probe_mode == "fail":
            return subprocess.CompletedProcess(cmd, 1, b"", b"ffprobe crashed")
        if probe_mode == "novideo":
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"streams": [{"codec_type": "audio"}],
                                    "format": {"duration": "9.0"}}).encode("utf-8"), b"")
        if probe_mode == "nodur":
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"streams": [{"codec_type": "video", "width": 1280,
                                                 "height": 720}],
                                    "format": {}}).encode("utf-8"), b"")
        if probe_mode == "raise":
            raise RuntimeError("probe boom")
        # ok
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"streams": [{"codec_type": "video", "width": 1280,
                                             "height": 720, "duration": "9.0"}],
                                "format": {"duration": "9.0"}}).encode("utf-8"), b"")

    ctx = {"store": store, "workdir": root, "progress": (lambda p: None),
           "run_tracked": run_tracked}
    return ctx, store


def make_inputs(store):
    vid = os.path.join(_PKG, ".testrun", "src.mp4")
    with open(vid, "w", encoding="utf-8") as f:
        f.write("fake video placeholder")
    video = Asset(id="", kind="VIDEO", path=vid)
    segs = store.save_asset(
        Asset(id="", kind="SEGMENTS"),
        payload=[{"segment_index": 0, "text": "第一段", "start": 0.0, "end": 3.0,
                  "clip_duration": 3.0},
                 {"segment_index": 1, "text": "第二段", "start": 3.0, "end": 6.0,
                  "clip_duration": 3.0}])
    return video, segs


def test_probe_failure_warning_marks():
    for mode, why in (("missing", "ffprobe 不可用"),
                      ("fail", "ffprobe 非零退出"),
                      ("novideo", "无视频流"),
                      ("nodur", "无时长字段"),
                      ("raise", "探测异常")):
        ctx, store = make_ctx("pf_" + mode, probe_mode=mode)
        if mode == "missing":
            # ffprobe 缺失：stub deps.resolve_binary 返回未就绪，命中 _probe_video 缺失分支
            import app.plugin.deps as _deps
            orig = _deps.resolve_binary
            _deps.resolve_binary = lambda *a, **k: {"ready": False, "path": ""}
            try:
                out = asyncio.run(m.PremiereExportNode().run(
                    ctx, {"video": video_asset(ctx, store), "segments": segs_asset(ctx, store)},
                    {"fps": 30, "sequence_width": 0, "sequence_height": 0,
                     "enable_subtitle": True, "file_base": "t_" + mode, "output_dir": ""}))
            finally:
                _deps.resolve_binary = orig
        else:
            video, segs = make_inputs(store)
            out = asyncio.run(m.PremiereExportNode().run(
                ctx, {"video": video, "segments": segs},
                {"fps": 30, "sequence_width": 0, "sequence_height": 0,
                 "enable_subtitle": True, "file_base": "t_" + mode, "output_dir": ""}))
        rep = store.read_json(out["report"].id)
        warns = rep.get("warnings") or []
        est = rep.get("estimated_frames") or 0
        check(f"R10({mode}): probe_failed=True 且 warnings 含 [probe_failed] 标记",
              rep.get("probe_failed") is True
              and any("[probe_failed]" in w for w in warns),
              f"probe_failed={rep.get('probe_failed')} warnings={warns}")
        check(f"R10({mode}): estimated_frames=round(60×30)=1800 显式",
              est == 1800 and any("估算帧数 1800" in w for w in warns),
              f"est={est} warns={warns}")
        check(f"R10({mode}): probe_error 非空", bool(rep.get("probe_error")), f"{rep.get('probe_error')}")


def video_asset(ctx, store):
    vid = os.path.join(_PKG, ".testrun", "src.mp4")
    with open(vid, "w", encoding="utf-8") as f:
        f.write("fake video placeholder")
    return Asset(id="", kind="VIDEO", path=vid)


def segs_asset(ctx, store):
    return store.save_asset(
        Asset(id="", kind="SEGMENTS"),
        payload=[{"segment_index": 0, "text": "第一段", "start": 0.0, "end": 3.0,
                  "clip_duration": 3.0},
                 {"segment_index": 1, "text": "第二段", "start": 3.0, "end": 6.0,
                  "clip_duration": 3.0}])


def test_probe_success_zero_regression():
    ctx, store = make_ctx("ok", probe_mode="ok")
    video, segs = make_inputs(store)
    out = asyncio.run(m.PremiereExportNode().run(
        ctx, {"video": video, "segments": segs},
        {"fps": 30, "sequence_width": 0, "sequence_height": 0,
         "enable_subtitle": True, "file_base": "t_ok", "output_dir": ""}))
    rep = store.read_json(out["report"].id)
    check("R10(ok): probe_failed=False 且 warnings 为空",
          rep.get("probe_failed") is False and not rep.get("warnings"),
          f"probe_failed={rep.get('probe_failed')} warnings={rep.get('warnings')}")
    check("R10(ok): source 尺寸/时长真实（1280×720/9s）",
          rep.get("source_width") == 1280 and rep.get("source_height") == 720
          and abs(rep.get("duration", 0) - 9.0) < 0.01,
          f"{rep.get('source_width')}x{rep.get('source_height')} d={rep.get('duration')}")
    xml = store.read_json(out["xml"].id)
    check("R10(ok): XML 正常产出", "<xmeml version" in xml.get("text", ""))
    check("R10(ok): SRT 产出", "--> " in store.read_json(out["srt"].id).get("text", ""))


def test_pr_builder_regression():
    # B8：源分辨率序列
    entries = pr_builder.normalize_entries(
        [{"segment_index": 0, "text": "a", "start": 0, "end": 3, "clip_duration": 3.0}],
        r"C:\v\src.mp4")
    built = pr_builder.build_xmeml(entries, video_path=r"C:\v\src.mp4", fps=30,
                                   width=1280, height=720, total_duration=9.0)
    xml = built["xml"]
    check("B8: 序列尺寸按源分辨率", "<width>1280</width>" in xml and "<height>720</height>" in xml)
    # B9：同源文件复用 file id
    e2 = pr_builder.normalize_entries(
        [{"segment_index": 0, "text": "a", "start": 0, "end": 3, "clip_duration": 3.0,
          "video": r"C:\v\src.mp4"},
         {"segment_index": 1, "text": "b", "start": 3, "end": 6, "clip_duration": 3.0,
          "video": r"C:\v\src.mp4"}],
        r"C:\v\src.mp4")
    x2 = pr_builder.build_xmeml(e2, video_path=r"C:\v\src.mp4", fps=30,
                                width=1280, height=720, total_duration=6.0)["xml"]
    check("B9: 同源文件 file id 唯一", x2.count('file id="file-1"') == 2
          and 'file id="file-2"' not in x2)
    check("回归: sec_to_frames/path_url/format_srt",
          pr_builder.sec_to_frames(2.0, 30) == 60
          and pr_builder.path_url(r"C:\v\a b.mp4") == "file://localhost/C%3A/v/a%20b.mp4"
          and pr_builder.format_srt_time(3661.5) == "01:01:01,500")


def main():
    test_probe_failure_warning_marks()
    test_probe_success_zero_regression()
    test_pr_builder_regression()
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