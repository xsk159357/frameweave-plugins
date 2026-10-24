"""S3-T3 core/analyze_chunks（A4）+ core/analyze_basic（A9）验收测试。

pytest 收集（qa 统一 CI 会用）；纯 stdlib 环境亦可直跑：
    python test_s3t3_chain.py

覆盖（对齐 08 §S3-T3/S3-T4 契约）：
1. A4：chunks+video → descriptions JSON（shot_type/visual_description/audio_content），
   并发硬上限 ≤5、解析重试 3、JSON schema 校验（失败 → 占位条目 + 警告）。
2. A4 → ai_narrate 接入：descriptions 输出直读进 AINarrateNode 上帝视角（零关键帧），
   场景 scene_indices 隶属回退不产生占位符。
3. 省 Token 路径对比：report.path_compare.ratio ≥ 3（显著低于关键帧路径估算）。
4. A9：输出 description_items；批 15/并发 5 语义参数化（frame_batch/frames_per_group/
   concurrency 生效且有 max_concurrent 实测）；与 A4 区分（无分块输入端口）。
"""
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import types as _types
import unittest
import uuid

_PKG = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_PKG)
_WORK = os.path.abspath(os.path.join(_REPO, "..", ".."))  # 工作区根（沙箱可写根）
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

# 测试 scratch 落工作区 .tmp（沙箱 temp 重定向目录拒绝嵌套目录创建）；
# 进程退出自动清理，不留残留
_TMP_ROOT = os.path.join(_WORK, ".tmp", "s3t3_test_scratch")
os.makedirs(_TMP_ROOT, exist_ok=True)
atexit.register(lambda: shutil.rmtree(_TMP_ROOT, ignore_errors=True))


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

    class _Deps:
        def resolve_binary(self, name, env=None):
            return {"ready": False, "path": ""}
        def resolve_model(self, mid, env=None):
            return {"ready": False, "path": ""}

    assets.Asset = Asset
    nodespec.Asset = Asset
    nodespec.NodeBase = NodeBase
    nodespec.NodeSpec = NodeSpec
    nodespec.PortSpec = PortSpec
    nodespec.PortType = PortType
    plugin.deps = _Deps()
    app.assets = assets
    app.nodespec = nodespec
    app.plugin = plugin
    sys.modules["app"] = app
    sys.modules["app.assets"] = assets
    sys.modules["app.nodespec"] = nodespec
    sys.modules["app.plugin"] = plugin
    sys.modules["app.plugin.deps"] = plugin.deps


_install_app_stub()
Asset = sys.modules["app.assets"].Asset

from core_analyze_chunks import node as ac_mod            # noqa: E402
from core_analyze_basic import node as ab_mod             # noqa: E402
from core_ai_narrate import node as narrate_mod           # noqa: E402

SCENES = [
    {"index": 0, "start": 0.0, "end": 30.0, "shot_type": "wide",
     "visual_description": "雨夜小巷", "audio_content": ""},
    {"index": 1, "start": 30.0, "end": 60.0, "shot_type": "medium",
     "visual_description": "人物对峙", "audio_content": ""},
]
CHUNKS = {
    "schema_version": 1,
    "chunks": [
        {"chunk_index": 0, "start": 0.0, "end": 30.0, "duration": 30.0,
         "scene_indices": [0], "video_time_offset": 0.0},
        {"chunk_index": 1, "start": 30.0, "end": 60.0, "duration": 30.0,
         "scene_indices": [1], "video_time_offset": 30.0},
    ],
}


async def _fake_frames(_ctx, video_path, times, out_dir, scale):
    """假抽帧：为每个时间点生成一个最小 jpg 文件（内容占位）。"""
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i, t in enumerate(times):
        p = os.path.join(out_dir, f"f_{i:03d}_{int(t * 1000)}.jpg")
        with open(p, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fakejpeg")
        paths.append(p)
    return paths


def make_vision_tracker(good_json: str = None, fail_times: int = 0, bad_schema: bool = False,
                        delay: float = 0.02):
    """返回 (fake_vision, tracker)；fake 为同步函数（节点经 to_thread 调用），
    tracker 记录 max_active/calls（跨线程用锁保护）。"""
    import threading
    import time as _time
    lock = threading.Lock()
    state = {"max_active": 0, "calls": 0, "active": 0,
             "fail_left": fail_times, "bad_schema": bad_schema}

    def fake_vision(images_b64, prompt, api_key, base_url, model, temperature, max_tokens):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["calls"] += 1
        _time.sleep(delay)  # 制造真实重叠窗口（跨 to_thread 线程）
        try:
            with lock:
                if state["fail_left"] > 0:
                    state["fail_left"] -= 1
                else:
                    if state["bad_schema"]:
                        return '{"shot_type":"INVALID","visual_description":"x","audio_content":""}'
                    if good_json is not None:
                        return good_json
                    return ('{"shot_type":"closeup","visual_description":"雨夜中男子撑伞回头，'
                            '霓虹倒映在水面，镜头缓慢推进。","audio_content":"雨声与远处汽车鸣笛"}')
            return "{not-json"
        finally:
            with lock:
                state["active"] -= 1

    return fake_vision, state


def _base_ctx(store):
    # 注意：沙箱中 tempfile.mkdtemp 生成的目录带拒绝写 ACL，改用普通 os.makedirs
    d = os.path.join(_TMP_ROOT, "run_" + uuid.uuid4().hex[:8])
    os.makedirs(d, exist_ok=True)
    return {"store": store, "progress": lambda p: None, "workdir": d}


def _fake_video(store, duration=60.0):
    vp = os.path.join(_TMP_ROOT, "s3t3_fake_video.mp4")
    with open(vp, "wb") as f:
        f.write(b"00000018fake-mp4")
    return store.save_asset(
        Asset(id="", kind="VIDEO", path=vp,
              meta={"container": {"duration": duration}}))


class AnalyzeChunksTest(unittest.TestCase):
    """A4：chunks+video → descriptions（契约字段/并发/重试/schema 校验/Token 对比）。"""

    def setUp(self):
        self.store = FakeStore()
        ac_mod._extract_frames = _fake_frames
        ac_mod._BACKOFF = 0.02  # 加速重试退避（不影响重试次数语义）

    def test_a4_happy_path_contract(self):
        fake_vision, tracker = make_vision_tracker()
        ac_mod._chat_gemini_vision = fake_vision
        video = _fake_video(self.store)
        chunks_a = self.store.save_asset(Asset(id="", kind="JSON"), payload=CHUNKS)
        node = ac_mod.AnalyzeChunksNode()
        out = asyncio.run(node.run(_base_ctx(self.store), {"video": video, "chunks": chunks_a},
                                   {"provider": "gemini", "api_key": "test",
                                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                                    "model": "gemini-1.5-flash", "concurrency": 3,
                                    "retries": 3, "frames_per_chunk": 3}))
        payload = self.store.read_json(out["descriptions"].id)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["items"]), 2)
        it0 = payload["items"][0]
        for key in ("chunk_index", "index", "scene_indices", "start", "end",
                    "shot_type", "visual_description", "audio_content"):
            self.assertIn(key, it0)
        self.assertEqual(it0["chunk_index"], 0)
        self.assertEqual(it0["index"], 0)
        self.assertEqual(it0["scene_indices"], [0])
        self.assertEqual(it0["shot_type"], "closeup")
        self.assertGreater(len(it0["visual_description"]), 0)
        report = self.store.read_json(out["report"].id)
        self.assertEqual(report["chunk_count"], 2)
        self.assertEqual(report["failed_chunks"], [])
        self.assertEqual(report["params"]["concurrency"], 3)
        self.assertEqual(tracker["calls"], 2)

    def test_a4_concurrency_capped_at_5(self):
        fake_vision, tracker = make_vision_tracker()
        ac_mod._chat_openai_vision = fake_vision
        ac_mod._chat_gemini_vision = fake_vision
        video = _fake_video(self.store)
        many = {"schema_version": 1, "chunks": [
            {"chunk_index": i, "start": i * 10.0, "end": (i + 1) * 10.0,
             "scene_indices": [i], "video_time_offset": i * 10.0} for i in range(8)]}
        chunks_a = self.store.save_asset(Asset(id="", kind="JSON"), payload=many)
        node = ac_mod.AnalyzeChunksNode()
        out = asyncio.run(node.run(_base_ctx(self.store), {"video": video, "chunks": chunks_a},
                                   {"provider": "openai", "api_key": "test",
                                    "base_url": "http://localhost:1/v1", "model": "gpt-4o",
                                    "concurrency": 100, "retries": 3, "frames_per_chunk": 2}))
        self.assertEqual(len(self.store.read_json(out["descriptions"].id)["items"]), 8)
        self.assertLessEqual(tracker["max_active"], 5)  # 硬上限 5
        self.assertGreaterEqual(tracker["max_active"], 2)  # 确实并发执行
        report = self.store.read_json(out["report"].id)
        self.assertEqual(report["params"]["concurrency"], 5)  # 请求 100 → 生效 5

    def test_a4_retry_invalid_json_then_ok(self):
        fake_vision, tracker = make_vision_tracker(fail_times=2)
        ac_mod._chat_gemini_vision = fake_vision
        video = _fake_video(self.store)
        chunks_a = self.store.save_asset(Asset(id="", kind="JSON"), payload=CHUNKS)
        node = ac_mod.AnalyzeChunksNode()
        out = asyncio.run(node.run(_base_ctx(self.store), {"video": video, "chunks": chunks_a},
                                   {"provider": "gemini", "api_key": "test",
                                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                                    "concurrency": 1, "retries": 3, "frames_per_chunk": 2}))
        payload = self.store.read_json(out["descriptions"].id)
        for it in payload["items"]:
            self.assertIn("shot_type", it)          # 重试后成功，无占位
            self.assertNotIn("分块分析失败", it["visual_description"])
        # 串行（并发 1）确定性：块 0 先消费 2 次坏 JSON → 第 3 次成功（calls=3）；
        # 块 1 首次即成功（calls=1）；总调用 4
        self.assertEqual(payload["items"][0]["meta"]["calls"], 3)
        self.assertEqual(payload["items"][1]["meta"]["calls"], 1)
        self.assertEqual(tracker["calls"], 4)  # 2 次失败 + 2 次成功

    def test_a4_schema_validation_exhausted_placeholder(self):
        fake_vision, tracker = make_vision_tracker(bad_schema=True)
        ac_mod._chat_gemini_vision = fake_vision
        video = _fake_video(self.store)
        chunks_a = self.store.save_asset(Asset(id="", kind="JSON"), payload=CHUNKS)
        node = ac_mod.AnalyzeChunksNode()
        out = asyncio.run(node.run(_base_ctx(self.store), {"video": video, "chunks": chunks_a},
                                   {"provider": "gemini", "api_key": "test",
                                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                                    "concurrency": 1, "retries": 3, "frames_per_chunk": 2}))
        payload = self.store.read_json(out["descriptions"].id)
        self.assertEqual(payload["items"][0]["shot_type"], "other")  # schema 全失败 → 占位
        report = self.store.read_json(out["report"].id)
        self.assertEqual(len(report["failed_chunks"]), 2)
        self.assertTrue(any("分析失败" in w for w in report["warnings"]))
        self.assertEqual(tracker["calls"], 8)  # 2 块 × 4 次尝试（重试 3）全失败

    def test_a4_token_path_compare(self):
        fake_vision, tracker = make_vision_tracker()
        ac_mod._chat_openai_vision = fake_vision
        ac_mod._chat_gemini_vision = fake_vision
        video = _fake_video(self.store)
        chunks_a = self.store.save_asset(Asset(id="", kind="JSON"), payload=CHUNKS)
        node = ac_mod.AnalyzeChunksNode()
        out = asyncio.run(node.run(_base_ctx(self.store), {"video": video, "chunks": chunks_a},
                                   {"provider": "openai", "api_key": "test",
                                    "base_url": "http://localhost:1/v1", "model": "gpt-4o",
                                    "concurrency": 2, "retries": 3, "frames_per_chunk": 2}))
        report = self.store.read_json(out["report"].id)
        pc = report["path_compare"]
        self.assertGreaterEqual(pc["ratio"], 3)  # 省 Token 路径显著低于关键帧路径
        self.assertIn(pc["verdict"], ("省Token显著", "省Token"))
        self.assertGreater(pc["keyframe_path_est_tokens"], pc["description_path_est_tokens"])


class AnalyzeBasicTest(unittest.TestCase):
    """A9：中间帧批/并发参数化 + description_items + 与 A4 语义区分。"""

    def setUp(self):
        self.store = FakeStore()
        ab_mod._extract_frames = _fake_frames
        ab_mod._BACKOFF = 0.02

    def test_a9_description_items_batch_semantics(self):
        fake_vision, tracker = make_vision_tracker()
        ab_mod._chat_gemini_vision = fake_vision
        video = _fake_video(self.store, duration=120.0)
        node = ab_mod.AnalyzeBasicNode()
        out = asyncio.run(node.run(_base_ctx(self.store), {"video": video},
                                   {"provider": "gemini", "api_key": "test",
                                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                                    "frame_batch": 6, "frames_per_group": 2,
                                    "concurrency": 5, "retries": 2}))
        payload = self.store.read_json(out["description_items"].id)
        items = payload["description_items"]
        self.assertEqual(len(items), 3)  # 6 帧 / 每组 2 → 3 组
        it0 = items[0]
        for key in ("index", "time_start", "time_end", "shot_type",
                    "visual_description", "audio_content"):
            self.assertIn(key, it0)
        self.assertEqual(it0["index"], 0)
        self.assertGreater(it0["time_end"], it0["time_start"])
        report = self.store.read_json(out["report"].id)
        self.assertEqual(report["params"]["frame_batch"], 6)
        self.assertEqual(report["params"]["frames_per_group"], 2)
        self.assertEqual(report["params"]["concurrency"], 5)
        self.assertEqual(report["summary"]["vision_calls"], 3)
        self.assertEqual(tracker["max_active"], 3)  # 组数 < 并发 → 实际并发 3

    def test_a9_concurrency_parameter_effective(self):
        fake_vision, tracker = make_vision_tracker()
        ab_mod._chat_openai_vision = fake_vision
        ab_mod._chat_gemini_vision = fake_vision
        video = _fake_video(self.store, duration=120.0)
        node = ab_mod.AnalyzeBasicNode()
        out = asyncio.run(node.run(_base_ctx(self.store), {"video": video},
                                   {"provider": "openai", "api_key": "test",
                                    "base_url": "http://localhost:1/v1",
                                    "frame_batch": 8, "frames_per_group": 1,
                                    "concurrency": 2, "retries": 2}))
        report = self.store.read_json(out["report"].id)
        self.assertEqual(report["params"]["concurrency"], 2)
        self.assertLessEqual(tracker["max_active"], 2)  # 并发 2 生效
        self.assertEqual(tracker["max_active"], 2)
        self.assertEqual(report["item_count"], 8)

    def test_a9_distinct_from_a4(self):
        # 语义区分：A9 无 chunks 输入、无分块输出；输出端口名 description_items
        spec = ab_mod.AnalyzeBasicNode.spec()
        self.assertEqual(spec.type_id, "core/analyze_basic")
        in_names = {p.name for p in spec.inputs}
        self.assertEqual(in_names, {"video"})
        out_names = {p.name for p in spec.outputs}
        self.assertIn("description_items", out_names)
        self.assertNotIn("chunks", in_names)
        spec4 = ac_mod.AnalyzeChunksNode.spec()
        self.assertEqual(spec4.type_id, "core/analyze_chunks")
        self.assertIn("chunks", {p.name for p in spec4.inputs})


class NarrateIntegrationTest(unittest.TestCase):
    """A4 → ai_narrate 接入：descriptions 直读 + scene_indices 隶属回退。"""

    def test_scene_lines_scene_indices_fallback(self):
        desc = [{"index": 0, "scene_indices": [0, 1], "shot_type": "wide",
                 "visual_description": "整块雨夜画面描述", "audio_content": "背景音乐"}]
        lines = narrate_mod.scene_lines(SCENES, desc, "中文")
        self.assertIn("整块雨夜画面描述", lines)
        self.assertNotIn("【占位画面：无描述】", lines)  # 块内场景不占位

    def test_analyze_chunks_to_ai_narrate_direct(self):
        # A4 产出 → 直接接入 ai_narrate descriptions 输入（零关键帧）
        self.store = FakeStore()
        ac_mod._extract_frames = _fake_frames
        fake_vision, _t = make_vision_tracker()
        ac_mod._chat_gemini_vision = fake_vision
        video = _fake_video(self.store)
        chunks_a = self.store.save_asset(Asset(id="", kind="JSON"), payload=CHUNKS)
        a4 = ac_mod.AnalyzeChunksNode()
        a4out = asyncio.run(a4.run(_base_ctx(self.store), {"video": video, "chunks": chunks_a},
                                   {"provider": "gemini", "api_key": "test",
                                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                                    "concurrency": 2, "retries": 3, "frames_per_chunk": 2}))

        scenes_a = self.store.save_asset(Asset(id="", kind="SEGMENTS"), payload=SCENES)
        narrate_mod._chat_openai = lambda *a, **k: (
            '[{"text":"雨夜，男人撑着伞走进小巷。","start":0.0,"end":6.7},'
            '{"text":"巷口出现另一道人影。","start":30.0,"end":36.0}]')
        node = narrate_mod.AINarrateNode()
        out = asyncio.run(node.run(_base_ctx(self.store),
                                   {"video": None, "scenes": scenes_a,
                                    "descriptions": a4out["descriptions"], "draft": None},
                                   {"mode": "上帝视角", "style": "悬疑解说", "language": "中文",
                                    "provider": "openai", "api_key": "test",
                                    "base_url": "http://localhost:1/v1", "model": "gpt-4o",
                                    "json_mode": True}))
        script_a = out["script"]
        script = self.store.read_json(script_a.id)
        self.assertGreater(len(script["text"]), 10)  # 文案真实生成
        report = self.store.read_json(out["report"].id)
        self.assertEqual(report["strategy_used"], "llm")
        narration = self.store.read_json(out["narration"].id)
        self.assertGreaterEqual(len(narration["segments"]), 2)  # 基于 descriptions 直读


if __name__ == "__main__":
    unittest.main(verbosity=2)