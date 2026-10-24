"""C2/A5：core/asr 对话角色（Gemini input_audio 直听 60s 窗 + role）验收测试。

pytest 收集（qa 统一 CI 会用）；纯 stdlib 环境亦可直跑：
    python test_a5_roles.py

覆盖（对齐 02 §3.1-A5 差距 / 任务契约）：
1. Whisper 句级 + 启发式 role 标注（narrator/speaker/unknown），确定性、零 API；
2. dialogues 契约 [{start,end,role,text}]（role ∈ narrator/speaker/unknown）；
3. Gemini 直听引擎：mock generateContent 返回 `[mm:ss - mm:ss] 角色 | 台词` →
   parse_gemini_dialogue 正确解析 + node.run(engine=gemini) 输出含 role 的 dialogue；
4. 有 key 时模拟音频输入（gemini 引擎）+ 真实音频 local（Whisper 句级）不崩、带 role；
5. 本地零 key 兜底不崩（engine=auto 无 api_key → local 成功）；
6. engine=gemini 且无 key → 明确 contract 错误（不静默）；
7. base_url 指向 Gemini 且无 key → auto 本地优先不崩。

音频夹具：真实 wav 走 local 转写（faster-whisper 缓存命中离线可用）；Gemini 引擎
用 mock（本机无 key；有 key 时同一代码路径真实调用）。
"""
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
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
_TMP_ROOT = os.path.join(_WORK, ".tmp", "a5_test_scratch")
os.makedirs(_TMP_ROOT, exist_ok=True)
atexit.register(lambda: shutil.rmtree(_TMP_ROOT, ignore_errors=True))


class FakeStore:
    """最小 AssetStore：read_json / save_asset 走内存。"""

    def __init__(self):
        self._assets = {}
        self._n = 0
        self.root = _TMP_ROOT
        self.files_dir = _TMP_ROOT

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

        def resolve_model(self, mid, env=None, params=None):
            return {"ready": False, "path": ""}

        def coded_message(self, code, id="", path="", hint=""):
            return f"[{code}] {id}: {hint}"

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

from core_asr import node as asr_mod  # noqa: E402

_orig_local = asr_mod.ASRNode._transcribe_local


def _base_ctx(store):
    d = os.path.join(_TMP_ROOT, "run_" + uuid.uuid4().hex[:8])
    os.makedirs(d, exist_ok=True)
    return {"store": store, "progress": lambda p: None, "workdir": d}


def _fake_audio(store, name="a5_fake.wav", size=16000 * 2):
    """最小 wav 夹具（44B 头 + 静音 PCM；真实转写可用真实 wav 替代）。"""
    ap = os.path.join(_TMP_ROOT, name)
    with open(ap, "wb") as f:
        f.write(b"RIFF" + (36 + size).to_bytes(4, "little"))
        f.write(b"WAVEfmt " + (16).to_bytes(4, "little"))
        f.write((1).to_bytes(2, "little") + (1).to_bytes(2, "little"))
        f.write((16000).to_bytes(4, "little") + (16000 * 2).to_bytes(4, "little"))
        f.write((2).to_bytes(2, "little") + (16).to_bytes(2, "little"))
        f.write(b"data" + size.to_bytes(4, "little"))
        f.write(b"\x00\x00" * (size // 2))
    return store.save_asset(Asset(id="", kind="AUDIO", path=ap))


GEMMI_RESP = """[00:00 - 00:03] 旁白 | 故事发生在一个雨夜
[00:03 - 00:07] 男子 | 你到底是谁？
[00:07 - 00:10] 女子 | 我只是路过，别紧张。
"""


class RoleHeuristicTest(unittest.TestCase):
    """A5：Whisper 句级 + 启发式 role（确定性、零 API）。"""

    def test_speaker_marks(self):
        for t in ("他说，快走", "她喊道：别过来", "我问道，你是谁？",
                  "男子说道，这不可能", "各位观众，请看这里"):
            self.assertEqual(asr_mod.heuristic_role(t), "speaker", t)
        # 末尾叹/问/称呼
        self.assertEqual(asr_mod.heuristic_role("真的假的？"), "speaker")
        self.assertEqual(asr_mod.heuristic_role("快跑！"), "speaker")

    def test_narrator_marks(self):
        for t in ("旁白：故事开始", "解说：这是一段传奇", "此时镜头拉远", "这就是他的结局"):
            self.assertEqual(asr_mod.heuristic_role(t), "narrator", t)

    def test_unknown_fallback(self):
        self.assertEqual(asr_mod.heuristic_role("今天天气不错"), "unknown")
        # 无标记 → unknown；近距离同角色延续仅当 prev 已知
        self.assertEqual(asr_mod.heuristic_role("今天天气不错", "narrator", 0.2), "narrator")
        self.assertEqual(asr_mod.heuristic_role("今天天气不错", "unknown", 0.2), "unknown")

    def test_annotate_and_dialogues_contract(self):
        rows = [
            {"index": 0, "start": 0.0, "end": 3.0, "text": "旁白：故事开始"},
            {"index": 1, "start": 3.2, "end": 6.0, "text": "他喊道，快走！"},
            {"index": 2, "start": 6.2, "end": 9.0, "text": "此时镜头拉远，夜色渐深"},  # 叙述性开篇 → narrator
        ]
        out = asr_mod.annotate_roles(rows, with_role=True)
        roles = [r["role"] for r in out]
        self.assertEqual(roles, ["narrator", "speaker", "narrator"])
        dlg = asr_mod.to_dialogues(out, source="whisper")
        self.assertEqual(len(dlg), 3)
        for d in dlg:
            self.assertIn("start", d)
            self.assertIn("end", d)
            self.assertIn("role", d)
            self.assertIn("text", d)
            self.assertIn(d["role"], ("narrator", "speaker", "unknown"))
            self.assertEqual(d["source"], "whisper")
        self.assertEqual(dlg[0]["role"], "narrator")
        self.assertEqual(dlg[1]["role"], "speaker")
        self.assertAlmostEqual(dlg[0]["start"], 0.0)


class GeminiParseTest(unittest.TestCase):
    """A5：Gemini input_audio 直听解析（[mm:ss - mm:ss] 角色 | 台词）。"""

    def test_parse_basic(self):
        out = asr_mod.parse_gemini_dialogue(GEMMI_RESP, window_start=0.0, window_end=60.0)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["role"], "narrator")
        self.assertEqual(out[1]["role"], "speaker")
        self.assertEqual(out[2]["role"], "speaker")  # 女子 → speaker
        self.assertEqual(out[1]["text"], "你到底是谁？")
        self.assertAlmostEqual(out[1]["start"], 3.0)
        self.assertAlmostEqual(out[1]["end"], 7.0)
        self.assertEqual(out[0]["source"], "gemini")

    def test_parse_window_offset(self):
        out = asr_mod.parse_gemini_dialogue(GEMMI_RESP, window_start=60.0, window_end=120.0)
        self.assertAlmostEqual(out[0]["start"], 60.0)
        self.assertAlmostEqual(out[1]["start"], 63.0)

    def test_parse_no_time_fallback_uniform(self):
        txt = "旁白 | 雨夜\n男子 | 快走\n"
        out = asr_mod.parse_gemini_dialogue(txt, window_start=0.0, window_end=60.0)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0]["start"], 0.0)
        self.assertAlmostEqual(out[1]["start"], 30.0)  # 均匀插值

    def test_parse_empty(self):
        self.assertEqual(asr_mod.parse_gemini_dialogue("无语音", 0.0), [])
        self.assertEqual(asr_mod.parse_gemini_dialogue("", 0.0), [])


class GeminiEngineTest(unittest.TestCase):
    """A5：有 key 时 Gemini 直听引擎（模拟音频 + mock generateContent）。"""

    def setUp(self):
        self.store = FakeStore()
        # mock 网络与 ffmpeg（不真调外部/子进程）
        asr_mod._chat_gemini_audio = lambda *a, **k: GEMMI_RESP
        asr_mod._ffmpeg_probe_duration = lambda _p: 30.0
        asr_mod._ffmpeg_cut_window = lambda _p, s, e, _d, i: os.path.join(
            _TMP_ROOT, f"win_{i}.wav")
        # 兜底：win_N.wav 不存在时生成最小可读 wav（mock 直听不读内容）
        for i in range(4):
            p = os.path.join(_TMP_ROOT, f"win_{i}.wav")
            if not os.path.isfile(p):
                with open(p, "wb") as f:
                    f.write(b"RIFF" + (36 + 1600).to_bytes(4, "little"))
                    f.write(b"WAVEfmt " + (16).to_bytes(4, "little"))
                    f.write((1).to_bytes(2, "little") + (1).to_bytes(2, "little"))
                    f.write((16000).to_bytes(4, "little") + (32000).to_bytes(4, "little"))
                    f.write((2).to_bytes(2, "little") + (16).to_bytes(2, "little"))
                    f.write(b"data" + (1600).to_bytes(4, "little"))
                    f.write(b"\x00\x00" * 800)
        asr_mod.ASRNode._transcribe_local = _orig_local  # 恢复原实现

    def test_gemini_engine_dialogue_with_role(self):
        audio = _fake_audio(self.store)
        node = asr_mod.ASRNode()
        out = asyncio.run(node.run(
            _base_ctx(self.store), {"audio": audio},
            {"engine": "gemini", "api_key": "test-key",
             "base_url": "https://generativelanguage.googleapis.com/v1beta",
             "gemini_model": "gemini-1.5-flash", "window_seconds": 60.0}))
        dlg = self.store.read_json(out["dialogues"].id)
        self.assertEqual(len(dlg), 3)
        self.assertEqual(dlg[0]["role"], "narrator")
        self.assertEqual(dlg[1]["role"], "speaker")
        self.assertEqual(dlg[1]["text"], "你到底是谁？")
        # 报告
        report = self.store.read_json(out["report"].id)
        self.assertEqual(report["engine_used"], "gemini")
        self.assertEqual(report["role_stats"], {"narrator": 1, "speaker": 2})
        # 字幕行也带 role
        subs = self.store.read_json(out["subtitles"].id)
        self.assertEqual(subs[0]["role"], "narrator")

    def test_gemini_no_key_contract(self):
        audio = _fake_audio(self.store)
        node = asr_mod.ASRNode()
        with self.assertRaisesRegex(RuntimeError, "contract"):
            asyncio.run(node.run(
                _base_ctx(self.store), {"audio": audio},
                {"engine": "gemini", "api_key": "",
                 "base_url": "https://generativelanguage.googleapis.com/v1beta",
                 "gemini_model": "gemini-1.5-flash"}))

    def test_auto_no_key_local_fallback(self):
        """零 key 兜底：engine=auto 无 key → local（Whisper）不崩。"""
        audio = _fake_audio(self.store)

        async def fake_local(src_path, model, language, device, compute_type, vad, word_ts, progress):
            return ([
                {"index": 0, "start": 0.0, "end": 3.0, "text": "旁白：故事开始",
                 "confidence": -0.1},
                {"index": 1, "start": 3.2, "end": 6.0, "text": "他喊道，快走！",
                 "confidence": -0.2},
            ], {"language": "zh", "duration": 6.0}, [])

        node = asr_mod.ASRNode()
        # 实例属性 patch（避免类属性绑定 self 顶掉 src_path）
        node._transcribe_local = fake_local
        out = asyncio.run(node.run(
            _base_ctx(self.store), {"audio": audio},
            {"engine": "auto", "api_key": "", "base_url": "https://generativelanguage.googleapis.com/v1beta"}))
        dlg = self.store.read_json(out["dialogues"].id)
        self.assertEqual(len(dlg), 2)
        self.assertEqual(dlg[0]["role"], "narrator")
        self.assertEqual(dlg[1]["role"], "speaker")
        report = self.store.read_json(out["report"].id)
        self.assertEqual(report["engine_used"], "local")

    def test_gemini_base_url_auto_with_key_uses_gemini(self):
        audio = _fake_audio(self.store)
        node = asr_mod.ASRNode()
        out = asyncio.run(node.run(
            _base_ctx(self.store), {"audio": audio},
            {"engine": "gemini", "api_key": "k",
             "base_url": "https://api.openai.com/v1",  # 非 gemini 根 → _gemini_base 归一
             "gemini_model": "gemini-1.5-flash"}))
        report = self.store.read_json(out["report"].id)
        self.assertEqual(report["engine_used"], "gemini")

    def test_local_annotate_not_crash(self):
        """真实路径：Whisper 句级 + 启发式 role（faster-whisper 本地，缓存命中离线）。"""
        wav = os.path.join(_TMP_ROOT, "a5_real.wav")
        # 用现成真实 wav（tts 产物）作夹具，验证 annotate 不崩
        candidates = [
            r"F:\1223\解说工坊\FrameWeave\backend\data\assets\files\tts\clone_1790619185807.wav",
            r"F:\1223\解说工坊\FrameWeave\backend\data\assets\files\tts\clone_1790626549826.wav",
        ]
        src = next((p for p in candidates if os.path.isfile(p)), None)
        if not src:
            self.skipTest("无真实 wav 夹具（backend tts 资产缺失）")
        shutil.copy(src, wav)
        audio = self.store.save_asset(Asset(id="", kind="AUDIO", path=wav))
        node = asr_mod.ASRNode()
        out = asyncio.run(node.run(
            _base_ctx(self.store), {"audio": audio},
            {"engine": "local", "model": "small", "language": "zh", "device": "cpu",
             "compute_type": "int8", "vad": True, "word_timestamps": False, "api_key": ""}))
        subs = self.store.read_json(out["subtitles"].id)
        dlg = self.store.read_json(out["dialogues"].id)
        self.assertGreater(len(subs), 0)
        self.assertEqual(len(dlg), len(subs))
        for d in dlg:
            self.assertIn(d["role"], ("narrator", "speaker", "unknown"))
            self.assertIn("start", d)
            self.assertIn("end", d)
        report = self.store.read_json(out["report"].id)
        self.assertEqual(report["engine_used"], "local")


if __name__ == "__main__":
    unittest.main(verbosity=2)
