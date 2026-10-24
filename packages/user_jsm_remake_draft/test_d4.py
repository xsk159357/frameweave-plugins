# -*- coding: utf-8 -*-
"""D4 精选复刻草稿测试（S2-T2 verify：草稿结构 → 剪映校验；R7 专项断言）。

运行：
  cd frameweave-plugins/packages/user_jsm_remake_draft
  python test_d4.py            # 纯结构单测（无视频依赖）
  python test_d4.py --node     # 节点级集成（合成视频对 + ffprobe 实测 wav 时长）
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "..", "FrameWeave", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "core_draft_export"))

from jy_builder import (  # noqa: E402
    US, DraftModel, _REMAKE_FILL_RGB, _REMAKE_LABEL_LEFT_POS, _REMAKE_LABEL_RIGHT_POS,
    _REMAKE_LABEL_SCALE, _REMAKE_STROKE_RGB, _REMAKE_STROKE_W, _REMAKE_TEXT_SIZE,
    build_remake_content, normalize_remake_matches, write_remake_draft,
)
from jy_builder import build_draft_content as _build_std_content  # 回归：既有 3 模式


def _base_draft(mode: str, tmp: str) -> DraftModel:
    m = DraftModel(
        video_path=r"F:\解析\commentary.mp4", video_name="commentary.mp4",
        video_width=1920, video_height=1080, video_duration=9.4,
        canvas={"width": 1920, "height": 1080, "ratio": "16:9"},
        fps=30.0, export_target="jianying", draft_name="d4_test",
        draft_dir=os.path.join(tmp, "d4_test"))
    m.remake_mode = mode
    m.remake_matches = normalize_remake_matches([
        {"start": 0.0, "end": 5.4, "ref_start": 0.0, "ref_end": 5.4,
         "ref_path": r"C:\orig.mp4", "ref_name": "orig.mp4", "ref_source": "A",
         "speed": 1.0},
        {"start": 5.4, "end": 9.4, "ref_start": 5.4, "ref_end": 9.4,
         "ref_path": r"C:\orig.mp4", "ref_name": "orig.mp4", "ref_source": "A",
         "speed": 1.0},
    ], original_path=r"C:\orig.mp4")
    m.remake_commentary_w = 1920
    m.remake_commentary_h = 1080
    m.remake_original_w = 1694
    m.remake_original_h = 720
    m.remake_original_path = r"C:\orig.mp4"
    m.remake_original_dur = 99.1
    m.remake_commentary_audio = {"path": r"F:\解析\comm.wav", "name": "comm.wav",
                                 "duration_us": 9_400_000}  # 真实时长>0
    m.remake_labels = ("原解说", "复刻后")
    m.remake_orig_volume = 0.0
    m.remake_total_us = 9_400_000
    m.entries = [
        {"video_path": m.video_path, "src_start_us": 0, "src_dur_us": 9_400_000,
         "tgt_start_us": 0, "tgt_dur_us": 9_400_000, "is_ost": False,
         "segment_index": 0, "text": "", "speed": 1.0, "volume": 1.0},
        {"video_path": r"C:\orig.mp4", "src_start_us": 0, "src_dur_us": 99_100_000,
         "tgt_start_us": 0, "tgt_dur_us": 9_400_000, "is_ost": True,
         "segment_index": 1, "text": "", "speed": 1.0, "volume": 0.0},
    ]
    return m


def test_compare_structure():
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".testt4")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    m = _base_draft("compare", tmp)
    c = build_remake_content(m)

    # 1. 画布：左右宽和 (cW+rW) x cH —— cW=1920, 原片 1694x720 缩放到高 1080 → rW=2541
    canvas = c["canvas_config"]
    assert canvas["ratio"] == "custom", canvas
    assert canvas["height"] == 1080, canvas
    assert canvas["width"] == 1920 + 2541, canvas  # 4461

    # 2. 轨道：主视频(解说左) + 覆盖视频(原片右) + 音频(解说wav) + 2×文字(LABEL)
    tt = [t["type"] for t in c["tracks"]]
    assert tt == ["video", "video", "audio", "text", "text"], tt
    # 主轨 transform.x = 左半（-rW/canvasW）；覆盖轨 = 右半（cW/canvasW）
    main_x = c["tracks"][0]["segments"][0]["clip"]["transform"]["x"]
    ov_x = c["tracks"][1]["segments"][0]["clip"]["transform"]["x"]
    assert abs(main_x - (-2541 / 4461)) < 1e-6, main_x
    assert abs(ov_x - (1920 / 4461)) < 1e-6, ov_x
    # 覆盖轨 2 段（每匹配 1 段），source=ref 时间
    assert len(c["tracks"][1]["segments"]) == 2
    s0 = c["tracks"][1]["segments"][0]
    assert s0["source_timerange"] == {"duration": 5_400_000, "start": 0}
    assert s0["target_timerange"] == {"duration": 5_400_000, "start": 0}

    # 3. LABEL 黄字#ffde00 + 黑描边 + pos/scale 常量（x 对齐视频半区，对齐 r4_curated 产物）
    texts = c["materials"]["texts"]
    assert len(texts) == 2, len(texts)
    for tm in texts:
        content = json.loads(tm["content"])
        fill = content["styles"][0]["fill"]["content"]["solid"]["color"]
        strokes = content["styles"][0]["strokes"][0]
        assert abs(fill[0] - _REMAKE_FILL_RGB[0]) < 1e-6 and abs(fill[1] - _REMAKE_FILL_RGB[1]) < 1e-6
        assert strokes["content"]["solid"]["color"] == [0.0, 0.0, 0.0]
        assert abs(strokes["width"] - _REMAKE_STROKE_W) < 1e-6
        assert tm["text_color"].lower() == "#ffde00"
    # 两条文字轨各自 render_index 14001/14004；x 对齐各自视频半区（左=-rW/canvasW，右=cW/canvasW）
    ri = [c["tracks"][3]["segments"][0]["render_index"],
          c["tracks"][4]["segments"][0]["render_index"]]
    assert ri == [14001, 14004], ri
    seg0 = c["tracks"][3]["segments"][0]
    assert abs(seg0["clip"]["transform"]["x"] - (-2541 / 4461)) < 1e-9, seg0["clip"]["transform"]["x"]
    assert abs(seg0["clip"]["transform"]["y"] - _REMAKE_LABEL_LEFT_POS[1]) < 1e-9
    assert abs(seg0["clip"]["scale"]["x"] - _REMAKE_LABEL_SCALE) < 1e-9
    assert abs(seg0["clip"]["scale"]["y"] - _REMAKE_LABEL_SCALE) < 1e-9
    seg1 = c["tracks"][4]["segments"][0]
    assert abs(seg1["clip"]["transform"]["x"] - (1920 / 4461)) < 1e-9, seg1["clip"]["transform"]["x"]
    assert abs(seg1["clip"]["transform"]["y"] - _REMAKE_LABEL_RIGHT_POS[1]) < 1e-9

    # 4. R7：commentary wav 在 audio 轨且 duration>0（绝不 0）
    audios = c["materials"]["audios"]
    audio_segs = c["tracks"][2]["segments"]
    assert len(audios) == 1 and len(audio_segs) == 1
    assert audios[0]["path"].endswith("comm.wav")
    assert audios[0]["duration"] == 9_400_000
    assert audios[0]["duration"] > 0, "R7: wav duration 必须 >0"
    assert audio_segs[0]["target_timerange"]["duration"] == 9_400_000
    print("test_compare_structure OK（画布 4461x1080 / 分屏 transform / 黄字 LABEL 常量 / R7 audio>0）")
    shutil.rmtree(tmp, ignore_errors=True)


def test_remake_only_structure():
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".testt4r")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    m = _base_draft("remake_only", tmp)
    c = build_remake_content(m)

    # 画布 = 原片分辨率
    canvas = c["canvas_config"]
    assert (canvas["width"], canvas["height"]) == (1694, 720), canvas
    # 单视频轨（无 overlay、无 LABEL 文字轨）
    tt = [t["type"] for t in c["tracks"]]
    assert tt == ["video", "audio"], tt
    # 视频轨按解说节奏铺满：target=解说时间，source=ref 时间，1.0 速
    segs = c["tracks"][0]["segments"]
    assert len(segs) == 2
    assert segs[0]["target_timerange"] == {"duration": 5_400_000, "start": 0}
    assert segs[0]["source_timerange"] == {"duration": 5_400_000, "start": 0}
    assert segs[1]["target_timerange"] == {"duration": 4_000_000, "start": 5_400_000}
    # R7 音频轨
    audios = c["materials"]["audios"]
    assert len(audios) == 1 and audios[0]["duration"] > 0
    print("test_remake_only_structure OK（原片满铺画布 / 解说节奏铺满 / R7 audio>0）")
    shutil.rmtree(tmp, ignore_errors=True)


def test_standard_modes_backward_compat():
    """回归：既有 3 模式 build_draft_content 不受第 4 模式扩展影响。"""
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".testt4s")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    m = DraftModel(
        video_path=r"F:\解析\v.mp4", video_name="v.mp4",
        video_width=1920, video_height=1080, video_duration=12.0,
        canvas={"width": 1920, "height": 1080, "ratio": "16:9"},
        fps=30.0, export_target="jianying", draft_name="std_backward",
        draft_dir=os.path.join(tmp, "std_backward"))
    m.entries = [
        {"video_path": m.video_path, "src_start_us": 0, "src_dur_us": 12_000_000,
         "tgt_start_us": 0, "tgt_dur_us": 12_000_000, "is_ost": False,
         "segment_index": 0, "text": "测试", "speed": 1.0, "volume": 1.0},
    ]
    m.subtitles = [{"start": 0.5, "end": 3.0, "text": "测试字幕"}]
    c = _build_std_content(m)
    assert c["canvas_config"]["ratio"] == "16:9"
    assert any(t["type"] == "video" for t in c["tracks"])
    assert any(t["type"] == "text" for t in c["tracks"])
    assert len(c["materials"]["videos"]) == 1
    print("test_standard_modes_backward_compat OK（既有 3 模式结构不变）")
    shutil.rmtree(tmp, ignore_errors=True)


def test_reference_conformance():
    """对齐 r4_curated 实测产物：画布/分屏 transform/LABEL 常量/renders 完全一致。"""
    import json as _json
    ref_path = r"F:\1223\解说工坊\_recon\r4_run\drafts\r4_curated\draft_content.json"
    if not os.path.isfile(ref_path):
        print("reference draft 不存在，跳过（无碍其余断言）")
        return
    ref = _json.load(open(ref_path, encoding="utf-8"))
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".testt4r")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    m = _base_draft("compare", tmp)
    c = build_remake_content(m)

    # 画布
    assert c["canvas_config"] == ref["canvas_config"], (c["canvas_config"], ref["canvas_config"])
    # 主轨/覆盖轨 transform.x（左右宽和）
    ours = [t["segments"][0]["clip"]["transform"]["x"]
            for t in c["tracks"] if t["type"] == "video"]
    refs = [t["segments"][0]["clip"]["transform"]["x"]
            for t in ref["tracks"] if t["type"] == "video"]
    assert len(ours) == 2 and len(refs) == 2
    for o, r in zip(ours, refs):
        assert abs(o - r) < 1e-9, (o, r)
    # LABEL render_index + 黄字
    def _text_seg(tracks):
        return [s for t in tracks if t.get("type") == "text" for s in (t.get("segments") or [])]
    ours_ri = [s["render_index"] for s in _text_seg(c["tracks"])]
    ref_ri = [s["render_index"] for s in _text_seg(ref["tracks"])]
    assert ours_ri == ref_ri, (ours_ri, ref_ri)  # [14001, 14004]
    # 缩放常量
    o_s = _text_seg(c["tracks"])[0]["clip"]["scale"]["x"]
    r_s = _text_seg(ref["tracks"])[0]["clip"]["scale"]["x"]
    assert abs(o_s - r_s) < 1e-9 and abs(o_s - _REMAKE_LABEL_SCALE) < 1e-9
    print("test_reference_conformance OK（画布/分屏 transform/LABEL render+scale 与 r4_curated 产物一致）")
    shutil.rmtree(tmp, ignore_errors=True)


def test_normalize_matches():
    s2t1 = [
        {"segment_index": 0, "start": 0.0, "end": 5.4, "ref_source": "A",
         "ref_name": "orig.mp4", "ref_path": "C:/orig.mp4",
         "ref_start": 10.0, "ref_end": 15.4, "speed": 1.0, "suspect": False},
    ]
    n = normalize_remake_matches(s2t1)
    assert len(n) == 1
    assert n[0]["commentary_start_us"] == 0
    assert n[0]["commentary_dur_us"] == 5_400_000
    assert n[0]["ref_start_us"] == 10_000_000
    assert n[0]["ref_dur_us"] == 5_400_000
    assert n[0]["ref_name"] == "orig.mp4"
    print("test_normalize_matches OK（S2-T1 匹配表 → D4 条目）")


# ---------------------------------------------------------------- 节点级集成（R7 ffprobe 实测）
def _ffprobe_exe() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe")
    return r["path"] if r.get("ready") else "ffprobe"


def _ffmpeg_exe() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg")
    return r["path"] if r.get("ready") else "ffmpeg"


def _run(ff: str, cmd: List[str], label: str) -> None:
    r = subprocess.run([ff] + cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{label} 失败 rc={r.returncode}: {(r.stderr or '')[:300]}")


def make_fixture(root: str) -> Dict[str, str]:
    """合成视频对：原片 6s(testsrc2+440Hz)，解说 1-3s+4-5s 拼接，解说 wav 3s。"""
    os.makedirs(root, exist_ok=True)
    ff = _ffmpeg_exe()
    orig = os.path.join(root, "orig.mp4")
    comm = os.path.join(root, "comm.mp4")
    wav = os.path.join(root, "comm.wav")
    if not os.path.isfile(orig):
        v = os.path.join(root, "_v.mp4")
        _run(ff, ["-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=6",
                  "-pix_fmt", "yuv420p", "-y", v], "画面")
        a = os.path.join(root, "_a.wav")
        _run(ff, ["-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-y", a], "音频")
        _run(ff, ["-v", "error", "-i", v, "-i", a, "-shortest", "-c:v", "copy",
                  "-pix_fmt", "yuv420p", "-y", orig], "合成原片")
        os.remove(v)
        os.remove(a)
    if not os.path.isfile(comm):
        _run(ff, ["-v", "error", "-i", orig, "-i", orig, "-filter_complex",
                  "[0:v]trim=start=1:end=3,setpts=PTS-STARTPTS[v0];"
                  "[0:a]atrim=start=1:end=3,asetpts=PTS-STARTPTS[a0];"
                  "[1:v]trim=start=4:end=5,setpts=PTS-STARTPTS[v1];"
                  "[1:a]atrim=start=4:end=5,asetpts=PTS-STARTPTS[a1];"
                  "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
                  "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-shortest", "-y", comm], "拼接解说")
    if not os.path.isfile(wav):
        _run(ff, ["-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=3",
                  "-ar", "16000", "-y", wav], "解说wav")
    return {"orig": orig, "comm": comm, "wav": wav}


async def run_node_integration() -> Dict[str, Any]:
    import uuid as _uuid
    from app.declarative import _make_python_class
    from app.assets import Asset, LocalAssetStore
    from app.nodespec import PortType

    ws_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "..", "FrameWeave", "backend", "data", "tmp")
    os.makedirs(ws_root, exist_ok=True)
    tmp = os.path.join(ws_root, "d4_node_" + _uuid.uuid4().hex[:8])
    os.makedirs(tmp, exist_ok=True)
    fx = make_fixture(tmp)

    manifest = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "manifest.json"), encoding="utf-8"))
    cls = _make_python_class(manifest, os.path.dirname(os.path.abspath(__file__)))
    node = cls()
    assert cls.spec().type_id == "user/jsm_remake_draft"

    store = LocalAssetStore(os.path.join(tmp, "store"))
    os.makedirs(os.path.join(tmp, "store"), exist_ok=True)
    orig_asset = store.save_asset(Asset(id="", kind=PortType.VIDEO.value, path=fx["orig"]))
    comm_asset = store.save_asset(Asset(id="", kind=PortType.VIDEO.value, path=fx["comm"]))
    wav_asset = store.save_asset(Asset(id="", kind=PortType.AUDIO.value, path=fx["wav"]))
    matches_asset = store.save_asset(
        Asset(id="", kind=PortType.JSON.value, meta={}),
        payload=[{"start": 0.0, "end": 2.0, "ref_start": 1.0, "ref_end": 3.0,
                  "ref_path": fx["orig"], "ref_name": "orig.mp4", "ref_source": "test",
                  "speed": 1.0, "suspect": False}])
    ctx = {"workdir": os.path.join(tmp, "work"), "store": store,
           "progress": lambda x: None, "logger": lambda x: None, "node_id": "n1"}

    out = await node.run(ctx, {"matches": matches_asset, "original": orig_asset,
                               "commentary": comm_asset, "commentary_wav": wav_asset},
                         {"mode": "compare", "labels": '["原解说", "复刻后"]',
                          "orig_volume": 0.0, "output_name": "d4verify",
                          "output_dir": os.path.join(tmp, "drafts")})
    draft_dir = out["draft_dir"]
    report = store.read_json(out["report"].id)
    assert os.path.isdir(draft_dir)
    for fn in ("draft_content.json", "draft_meta_info.json", "draft_settings",
               "attachment_draft_enterprise_info.json", "attachment_pc_common.json",
               "draft_agency_config.json", "draft_virtual_store.json"):
        assert os.path.exists(os.path.join(draft_dir, fn)), f"缺 {fn}"
    # R7 专项断言：ffprobe 实测 wav 时长写入且 >0
    assert report["r7"]["wav_duration_sec"] > 0, report["r7"]
    assert report["r7"]["wav_duration_us"] == int(report["r7"]["wav_duration_sec"] * US)
    assert report["r7"]["material_duration_ok"] is True
    assert report["r7"]["segment_duration_ok"] is True
    assert report["r7"]["assertion_ok"] is True, report["r7"]
    # LABEL 生效
    assert report["label_ok"] is True
    # 剪映结构校验
    assert report["validation"]["ok"] is True, report["validation"]
    # 草稿内容物快照
    content = store.read_json(out["draft_content"].id)
    assert content["canvas_config"]["ratio"] == "custom"
    assert len([t for t in content["tracks"] if t["type"] == "audio"]) == 1

    # 回归：remake_only 档位
    out2 = await node.run(ctx, {"matches": matches_asset, "original": orig_asset,
                                "commentary": comm_asset, "commentary_wav": wav_asset},
                          {"mode": "remake_only", "labels": '["原解说", "复刻后"]',
                           "orig_volume": 1.0, "output_name": "d4verify_ro",
                           "output_dir": os.path.join(tmp, "drafts")})
    report2 = store.read_json(out2["report"].id)
    assert report2["mode"] == "remake_only"
    assert report2["r7"]["assertion_ok"] is True, report2["r7"]
    assert report2["validation"]["ok"] is True, report2["validation"]

    shutil.rmtree(tmp, ignore_errors=True)
    return {"compare_r7": report["r7"]["assertion_ok"],
            "remake_only_r7": report2["r7"]["assertion_ok"],
            "label_ok": report["label_ok"]}


def main() -> int:
    argv = sys.argv[1:]
    test_normalize_matches()
    test_compare_structure()
    test_remake_only_structure()
    test_standard_modes_backward_compat()
    test_reference_conformance()
    print("纯结构单测全部通过 ✅")
    if "--node" in argv or "--all" in argv:
        print("节点级集成（合成视频对 + ffprobe R7 实测）：")
        res = asyncio.run(run_node_integration())
        print(f"node OK: compare_r7={res['compare_r7']} remake_only_r7={res['remake_only_r7']} "
              f"label_ok={res['label_ok']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())