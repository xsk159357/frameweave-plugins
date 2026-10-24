"""S3-T6 字幕+场景夹具链路实测（B5→B6→B7→draft_export 联调断言）。

pytest 收集（qa 统一 CI 会用）；纯 stdlib 环境亦可直跑：
    python test_s3t6_chain.py

覆盖：
1. B5 场景-对白融合：字幕按时间中心点挂载，rich_scenes 契约（index/start_time/end_time/
   dialogues/has_dialogue）。
2. B6 保留原声决策：无 LLM 场景（on_llm_fail=warn）决策可解释（report 含 warn + 空决策）；
   纯函数解析兼容 list/dict 形态 + 非法条目告警（r2 §11 怪癖 7 修复）。
3. B7 双轨混音布局：原声裁剪 padding 0.05/0.1、互斥静音区间、ramp_ms 契约。
4. 与 draft_export 联调断言：layout 输入 → jy_builder volume 关键帧（±50ms 斜坡，
   common_keyframes=[{property_type:'volume', keyframe_list:[4 帧/区间]}]）落到
   解说音频轨（narration_mute_ranges）与原声视频轨（original_mute_ranges）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# ---------------------------------------------------------------- 平台 app 桩（仅测试环境）
import types as _types

_PKG = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_PKG)
_DRAFT = os.path.join(_REPO, "core_draft_export")


class FakeStore:
    """最小 AssetStore：read_json / save_asset 走内存。"""

    def __init__(self):
        self._assets = {}
        self._n = 0
        self.root = tempfile.gettempdir()
        self.files_dir = tempfile.gettempdir()

    def read_json(self, aid):
        return self._assets.get(aid)

    def save_asset(self, asset, payload=None):
        self._n += 1
        aid = f"aid{self._n}"
        self._assets[aid] = payload
        asset.id = aid
        asset.meta = dict(asset.meta or {})
        return asset

    def get(self, aid):
        return self._assets.get(aid)


def _install_app_stub() -> None:
    if "app" in sys.modules:
        return
    app = _types.ModuleType("app")
    assets = _types.ModuleType("app.assets")
    nodespec = _types.ModuleType("app.nodespec")
    plugin = _types.ModuleType("app.plugin")
    deps_mod = _types.ModuleType("app.plugin.deps")

    def _resolve_binary(bin_id, env=None, params=None):
        return {"ready": False, "path": "", "source": "stub", "version": "", "warnings": []}

    deps_mod.resolve_binary = _resolve_binary
    plugin.deps = deps_mod
    app.plugin = plugin

    class Asset:
        def __init__(self, id="", kind="JSON", path="", meta=None, fingerprint="",
                     node_id="", created_at=None, size=0):
            self.id = id
            self.kind = kind
            self.path = path
            self.meta = meta or {}
            self.fingerprint = fingerprint
            self.node_id = node_id
            self.created_at = created_at
            self.size = size

    import enum as _enum

    class PortType(_enum.Enum):
        VIDEO, IMAGE, AUDIO = "VIDEO", "IMAGE", "AUDIO"
        SCRIPT, SUBTITLE, SEGMENTS, TIMELINE, JSON = "SCRIPT", "SUBTITLE", "SEGMENTS", "TIMELINE", "JSON"
        STRING, INT, FLOAT, BOOL, ANY = "STRING", "INT", "FLOAT", "BOOL", "ANY"

    class PortSpec:
        def __init__(self, name, type, label="", required=True, default=None, widget="text",
                     options=None, description=""):
            self.name, self.type, self.label = name, type, label
            self.required, self.default, self.widget = required, default, widget
            self.options, self.description = options or [], description

    class NodeSpec:
        def __init__(self, type_id="", title="", category="", description="", inputs=None,
                     outputs=None, params=None, version="1.0.0"):
            self.type_id, self.title, self.category = type_id, title, category
            self.description, self.inputs = description, inputs or []
            self.outputs, self.params, self.version = outputs or [], params or [], version

    class NodeBase:
        @classmethod
        def spec(cls):
            raise NotImplementedError
        async def setup(self, ctx):
            pass
        async def run(self, ctx, inputs, params):
            raise NotImplementedError

    assets.Asset = Asset
    nodespec.Asset = Asset
    nodespec.NodeBase = NodeBase
    nodespec.NodeSpec = NodeSpec
    nodespec.PortSpec = PortSpec
    nodespec.PortType = PortType
    app.assets = assets
    app.nodespec = nodespec
    sys.modules["app"] = app
    sys.modules["app.assets"] = assets
    sys.modules["app.nodespec"] = nodespec
    sys.modules["app.plugin"] = plugin
    sys.modules["app.plugin.deps"] = deps_mod


_install_app_stub()
Asset = sys.modules["app.assets"].Asset
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
if _DRAFT not in sys.path:
    sys.path.insert(0, _DRAFT)

from core_scene_dialogue_fuse.node import (  # noqa: E402
    normalize_scenes, normalize_subtitles, mount_dialogues, SceneDialogueFuseNode)
from core_keep_original_decision.node import (  # noqa: E402
    build_batch_prompt, parse_selection_response, apply_decisions,
    scenes_with_dialogues, KeepOriginalDecisionNode)
from core_mixed_audio_layout.node import (  # noqa: E402
    build_layout, calculate_original_clips, MixedAudioLayoutNode)
from jy_builder import (US, DraftModel, build_draft_content)  # noqa: E402

SCENES = [
    {"index": 0, "start": 0.0, "end": 10.0, "shot_type": "雨夜小巷",
     "visual_description": "雨夜小巷", "audio_content": ""},
    {"index": 1, "start": 10.0, "end": 20.0, "shot_type": "近景",
     "visual_description": "人物对峙", "audio_content": ""},
]
SUBS = [
    {"id": "s0", "start": 1.0, "end": 3.0, "text": "你终于来了。"},
    {"id": "s1", "start": 5.0, "end": 7.5, "text": "我等了很久。"},
    {"id": "s2", "start": 11.0, "end": 12.0, "text": "别过来！"},
]
NARR = [
    {"segment_index": 0, "text": "解说开场", "start": 0.0, "end": 0.8},
    {"segment_index": 1, "text": "解说过渡", "start": 7.6, "end": 10.9},
]


class S3T6ChainTest(unittest.TestCase):

    # ------------------------------------------------------------ B5
    def test_b5_mount_contract(self):
        rich = mount_dialogues(normalize_scenes(SCENES, "auto"), normalize_subtitles(SUBS))
        self.assertEqual(len(rich), 2)
        sc0 = rich[0]
        self.assertEqual(sc0["index"], 0)
        self.assertEqual(sc0["start_time"], 0.0)
        self.assertEqual(sc0["end_time"], 10.0)
        self.assertTrue(sc0["has_dialogue"])
        self.assertEqual([d["text"] for d in sc0["dialogues"]], ["你终于来了。", "我等了很久。"])
        self.assertEqual(sc0["dialogues"][0]["id"], 0)  # 场景内序号
        self.assertEqual([d["start"] for d in sc0["dialogues"]], [1.0, 5.0])
        self.assertTrue(rich[1]["has_dialogue"] and rich[1]["dialogues"][0]["text"] == "别过来！")
        # 中心点挂载验证：中心=4.0 落在场景0；中心=11.5 落在场景1
        self.assertEqual(sc0["dialogues"][1]["start"], 5.0)

    def test_b5_node_run(self):
        store = FakeStore()
        ctx = {"store": store, "progress": lambda p: None}
        node = SceneDialogueFuseNode()
        scenes_a = store.save_asset(Asset(id="", kind="SEGMENTS"), payload=SCENES)
        subs_a = store.save_asset(Asset(id="", kind="SUBTITLE"), payload=SUBS)
        import asyncio
        out = asyncio.run(node.run(ctx, {"scenes": scenes_a, "subtitles": subs_a},
                                   {"scene_time_keys": "auto"}))
        rich = store.read_json(out["rich_scenes"].id)
        self.assertEqual(rich["schema_version"], 1)
        self.assertEqual(rich["mode"], "narration")
        self.assertEqual(sum(1 for s in rich["scenes"] if s["has_dialogue"]), 2)
        report = store.read_json(out["report"].id)
        self.assertEqual(report["dialogues"], 3)
        self.assertEqual(report["unmounted"], 0)

    # ------------------------------------------------------------ B6
    def test_b6_prompt_and_parse(self):
        rich = mount_dialogues(normalize_scenes(SCENES, "auto"), normalize_subtitles(SUBS))
        prompt = build_batch_prompt(rich, "悬疑")
        self.assertIn("【场景 0】0s-10s", prompt)
        self.assertIn("你终于来了。", prompt)
        self.assertIn("keep_dialogue_ids", prompt)
        self.assertIn("情绪爆发", prompt)  # 额外标准已注入
        # list 形态（原版怪癖 7 修复）
        sel = parse_selection_response('[{"scene_index":0,"keep_dialogue_ids":[0],"reason":"关键台词"}]')
        self.assertEqual(sel[0]["scene_index"], 0)
        # dict 形态
        sel2 = parse_selection_response(
            '{"selections":[{"scene_index":1,"keep_dialogue_ids":[0],"reason":"情绪爆发"}],"summary":"ok"}')
        self.assertEqual(sel2[0]["scene_index"], 1)

    def test_b6_apply_decision_report(self):
        rich = mount_dialogues(normalize_scenes(SCENES, "auto"), normalize_subtitles(SUBS))
        warnings = []
        scenes, decisions = apply_decisions(
            rich,
            [{"scene_index": 0, "keep_dialogue_ids": [0], "reason": "关键台词"},
             {"scene_index": 9, "keep_dialogue_ids": [0], "reason": "场景不存在"}],
            warnings)
        self.assertEqual(scenes[0]["mode"], "mixed")
        self.assertEqual(scenes[0]["keep_dialogue_ids"], [0])
        self.assertTrue(scenes[0]["dialogues"][0]["keep"])
        self.assertFalse(scenes[0]["dialogues"][1]["keep"])
        self.assertEqual(scenes[1]["mode"], "narration")
        self.assertTrue(any("场景 9" in w for w in warnings))
        self.assertEqual(decisions[0]["reason"], "关键台词")  # 决策可解释
        self.assertEqual(decisions[0]["mode"], "mixed")

    def test_b6_node_run_no_key_warn(self):
        # 无 API Key + warn → 不保留原声，report 含 warn（决策可解释、不崩）
        store = FakeStore()
        ctx = {"store": store, "progress": lambda p: None}
        rich = mount_dialogues(normalize_scenes(SCENES, "auto"), normalize_subtitles(SUBS))
        rich_a = store.save_asset(Asset(id="", kind="JSON"), payload={"schema_version": 1, "scenes": rich})
        node = KeepOriginalDecisionNode()
        import asyncio
        out = asyncio.run(node.run(ctx, {"rich_scenes": rich_a},
                                   {"style": "悬疑", "api_key": "", "base_url": "",
                                    "model": "gpt-4o-mini", "max_scenes_per_batch": 50,
                                    "retries": 0, "temperature": 0.2, "on_llm_fail": "warn"}))
        report = store.read_json(out["report"].id)
        self.assertTrue(any("API Key" in w for w in report["warnings"]))
        self.assertEqual(report["llm_calls"], 0)
        self.assertEqual(report["mode"], "narration")

    # ------------------------------------------------------------ B7
    def test_b7_layout_contract(self):
        rich = mount_dialogues(normalize_scenes(SCENES, "auto"), normalize_subtitles(SUBS))
        # 手工决策：场景0 保留第0条（1.0-3.0）；场景1 保留第0条（11.0-12.0）
        rich[0]["keep_dialogue_ids"] = [0]
        rich[0]["dialogues"][0]["keep"] = True
        rich[0]["mode"] = "mixed"
        rich[1]["keep_dialogue_ids"] = [0]
        rich[1]["dialogues"][0]["keep"] = True
        rich[1]["mode"] = "mixed"
        layout = build_layout({"scenes": rich}, NARR, 0.05, 0.1, 50, 0.1)
        # 原声裁剪：1.0-0.05=0.95 → 3.0+0.1=3.1；11.0-0.05=10.95 → 12.0+0.1=12.1
        clips = layout["original_clips"]
        self.assertEqual([(c["start"], c["end"]) for c in clips],
                         [(0.95, 3.1), (10.95, 12.1)])
        # 互斥：原声播→解说轨静音；解说播→原声轨静音
        self.assertEqual(layout["narration_mute_ranges"], [[0.95, 3.1], [10.95, 12.1]])
        self.assertEqual(layout["original_mute_ranges"], [[0.0, 0.8], [7.6, 10.9]])
        self.assertEqual(layout["ramp_ms"], 50)
        self.assertEqual(layout["stats"]["narration_count"], 2)
        self.assertEqual(layout["stats"]["original_count"], 2)
        self.assertEqual(layout["stats"]["total_original_duration"], round(2.15 + 1.15, 3))

    def test_b7_node_run(self):
        store = FakeStore()
        ctx = {"store": store, "progress": lambda p: None}
        rich = mount_dialogues(normalize_scenes(SCENES, "auto"), normalize_subtitles(SUBS))
        rich[0]["keep_dialogue_ids"] = [0]
        rich[0]["dialogues"][0]["keep"] = True
        rich_a = store.save_asset(Asset(id="", kind="JSON"),
                                  payload={"schema_version": 1, "scenes": rich})
        narr_a = store.save_asset(Asset(id="", kind="JSON"), payload=NARR)
        node = MixedAudioLayoutNode()
        import asyncio
        out = asyncio.run(node.run(ctx, {"rich_scenes": rich_a, "narration_items": narr_a},
                                   {"padding_before": 0.05, "padding_after": 0.1,
                                    "ramp_ms": 50, "fade": 0.1}))
        layout = store.read_json(out["layout"].id)
        self.assertEqual(layout["schema_version"], 1)
        self.assertEqual(layout["original_clips"][0]["start"], 0.95)
        self.assertEqual(layout["ramp_ms"], 50)

    # ------------------------------------------------------------ 联调：B7 layout → draft_export
    def test_draft_export_consumes_layout_keyframes(self):
        rich = mount_dialogues(normalize_scenes(SCENES, "auto"), normalize_subtitles(SUBS))
        rich[0]["keep_dialogue_ids"] = [0]
        rich[0]["dialogues"][0]["keep"] = True
        layout = build_layout({"scenes": rich}, NARR, 0.05, 0.1, 50, 0.1)

        m = DraftModel(
            video_path=r"C:\dummy\video.mp4", video_name="video.mp4",
            video_width=1920, video_height=1080, video_duration=20.0,
            canvas={"width": 1920, "height": 1080, "ratio": "16:9"}, fps=30.0,
            export_target="jianying", draft_name="联调测试",
            draft_dir=os.path.join(tempfile.gettempdir(), "s3t6_draft_test"))
        m.layout = layout
        # 简单一条视频段 + 一条解说音频段
        from jy_builder import _video_material, _audio_material, _video_segment, _audio_segment  # noqa: F401
        vm = _video_material(r"C:\dummy\video.mp4", "video.mp4", 20 * US, 1920, 1080)
        am = _audio_material(r"C:\dummy\tts.mp3", "tts.mp3", 11 * US)
        m.entries = [{
            "segment_index": 0, "text": "解说", "is_ost": False,
            "src_start_us": 0, "src_dur_us": int(20 * US),
            "tgt_start_us": 0, "tgt_dur_us": int(20 * US),
            "video_path": r"C:\dummy\video.mp4", "speed": 1.0, "volume": 1.0,
        }]
        m.voice_items = [{"audio_path": r"C:\dummy\tts.mp3", "name": "tts.mp3",
                          "duration_us": int(11 * US), "tgt_start_us": 0}]
        content = build_draft_content(m)

        tracks = {t["name"]: t for t in content["tracks"]}
        audio_track = tracks.get("TTS解说") or tracks.get("解说")
        self.assertIsNotNone(audio_track, "解说音频轨缺失")
        audio_seg = audio_track["segments"][0]
        kfs = audio_seg.get("common_keyframes") or []
        self.assertTrue(kfs, "解说轨缺少互斥静音关键帧（narration_mute_ranges 未生效）")
        kf = kfs[0]
        self.assertEqual(kf["property_type"], "volume")
        times = [k["time"] for k in kf["keyframe_list"]]
        values = [k["value"] for k in kf["keyframe_list"]]
        # 原声区间 [0.95, 3.1]s → ±50ms 斜坡 4 帧：900000/950000/3100000/3150000 µs
        self.assertEqual(times, [900000, 950000, 3100000, 3150000], times)
        self.assertEqual(values, [1.0, 0.0, 0.0, 1.0])
        # 原声轨（视频主轨）应有 original_mute_ranges 关键帧（解说区间 [0,0.8]s + [7.6,10.9]s）
        video_track = tracks.get("主轨道")
        vkfs = video_track["segments"][0].get("common_keyframes") or []
        self.assertTrue(vkfs, "原声轨缺少互斥静音关键帧（original_mute_ranges 未生效）")
        v_times = [k["time"] for k in vkfs[0]["keyframe_list"]]
        self.assertEqual(v_times[0], 0)  # 0s-50ms 裁剪到 0
        self.assertEqual(v_times[-1], 10900000 + 50000)  # 10.9s+50ms

    def test_draft_export_node_run_with_layout(self):
        """节点级联调：DraftExportNode.run 接 layout 输入 → 草稿落盘 → draft_content 含互斥静音关键帧。"""
        from core_draft_export.node import DraftExportNode
        store = FakeStore()
        ctx = {"store": store, "progress": lambda p: None}
        # 哑视频文件（ffprobe 失败走默认参数，不影响草稿生成）
        vpath = os.path.join(tempfile.gettempdir(), "s3t6_dummy.mp4")
        with open(vpath, "wb") as f:
            f.write(b"dummy")
        video_a = store.save_asset(Asset(id="", kind="VIDEO", path=vpath, meta={}))
        segs_a = store.save_asset(Asset(id="", kind="SEGMENTS"), payload=[{
            "segment_index": 0, "text": "解说", "OST": False,
            "start": 0.0, "end": 20.0, "tts_duration": 11.0,
            "src_start": 0.0, "src_end": 20.0,
        }])
        apath = os.path.join(tempfile.gettempdir(), "s3t6_dummy.mp3")
        with open(apath, "wb") as f:
            f.write(b"dummy-audio")
        audio_a = store.save_asset(Asset(id="", kind="AUDIO", path=apath,
                                         meta={"duration": 11.0}))
        rich = mount_dialogues(normalize_scenes(SCENES, "auto"), normalize_subtitles(SUBS))
        rich[0]["keep_dialogue_ids"] = [0]
        rich[0]["dialogues"][0]["keep"] = True
        layout_a = store.save_asset(Asset(id="", kind="JSON"), payload=build_layout(
            {"scenes": rich}, NARR, 0.05, 0.1, 50, 0.1))
        node = DraftExportNode()
        import asyncio
        out = asyncio.run(node.run(ctx, {"video": video_a, "segments": segs_a,
                                         "audio": audio_a, "layout": layout_a},
                                   {"format": "jianying", "aspect": "16:9", "resolution": "1080p",
                                    "canvas_ratio": "original", "draft_name": "s3t6_联调",
                                    "export_target": "jianying", "subtitle_style": "默认",
                                    "font_size": 0.0, "enable_subtitle": False, "fps": 30,
                                    "output_dir": os.path.join(tempfile.gettempdir(), "s3t6_out")}))
        report = store.read_json(out["report"].id)
        self.assertEqual(report["layout"]["source"], "core/mixed_audio_layout")
        self.assertEqual(report["layout"]["narration_mute_ranges"], 1)
        draft_dir = store.read_json(out["draft"].id)["draft_path"]
        import json as _json
        with open(os.path.join(draft_dir, "draft_content.json"), encoding="utf-8") as f:
            content = _json.load(f)
        tracks = {t["name"]: t for t in content["tracks"]}
        audio_track = tracks.get("TTS解说") or tracks.get("解说")
        self.assertIsNotNone(audio_track)
        self.assertTrue(audio_track["segments"][0].get("common_keyframes"))

    def test_draft_export_without_layout_no_keyframes(self):
        """回归：未给 layout 时不产生关键帧（既有行为不回退）。"""
        m = DraftModel(
            video_path=r"C:\dummy\video.mp4", video_name="video.mp4",
            video_width=1920, video_height=1080, video_duration=20.0,
            canvas={"width": 1920, "height": 1080, "ratio": "16:9"}, fps=30.0,
            export_target="jianying", draft_name="回归测试",
            draft_dir=os.path.join(tempfile.gettempdir(), "s3t6_draft_regress"))
        m.entries = [{
            "segment_index": 0, "text": "解说", "is_ost": False,
            "src_start_us": 0, "src_dur_us": int(20 * US),
            "tgt_start_us": 0, "tgt_dur_us": int(20 * US),
            "video_path": r"C:\dummy\video.mp4", "speed": 1.0, "volume": 1.0,
        }]
        m.voice_items = [{"audio_path": r"C:\dummy\tts.mp3", "name": "tts.mp3",
                          "duration_us": int(11 * US), "tgt_start_us": 0}]
        content = build_draft_content(m)
        tracks = {t["name"]: t for t in content["tracks"]}
        for track in tracks.values():
            for seg in track["segments"]:
                self.assertEqual(seg.get("common_keyframes") or [], [])


def _run_all() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(S3T6ChainTest)
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
