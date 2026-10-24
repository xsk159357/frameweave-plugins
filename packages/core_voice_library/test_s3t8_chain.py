"""S3-T8 core/voice_library（C3 214 音色库）+ core/tts 动态加载 验收测试。

pytest 收集兼容；纯 stdlib + 平台 app 直跑：
    python test_s3t8_chain.py

覆盖（对齐 08 §S3-T8 acceptance/verify）：
1. 资源加载断言：214 条全量加载（edge 83/volcano 94/aliyun 31/index 6）、id 唯一且统一模型
   {provider}:{native_id}；
2. keywords 搜索：单/多关键词（空格 AND）、provider/lang/category 筛选、limit；
3. core/voice_library 节点 run → VoiceProfile 列表（可搜索）；
4. core/tts 动态加载：voice_options()/spec() 音色下拉 ≥39 且含 volcano:/edge: 统一 id；
5. T6 双份数据不复发：全仓库仅 1 份 tts_library.json（core_voice_library/data），
   core_tts 无内嵌第二份；resolve_edge_voice 统一模型归一 + 非 edge 提供商明确报错。
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import sys

sys.path.insert(0, r"F:\1223\解说工坊\FrameWeave\backend")

_PKG = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_PKG)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

import core_voice_library.node as vl  # noqa: E402
import core_tts.node as tts  # noqa: E402

import app.assets as A  # noqa: E402


class FakeStore:
    def __init__(self):
        self._a = {}
        self._n = 0
        self.root = "."
        self.files_dir = "."

    def read_json(self, aid):
        return self._a.get(aid)

    def save_asset(self, asset, payload=None):
        self._n += 1
        aid = f"aid{self._n}"
        self._a[aid] = payload
        asset.id = aid
        asset.meta = dict(asset.meta or {})
        return asset


def test_resource_full_load():
    """① 214 条全量加载 + 提供商分布 + id 统一模型唯一。"""
    data = vl.load_library()
    voices = data["voices"]
    assert len(voices) == 214, len(voices)
    by_provider = {}
    for v in voices:
        p = v["provider"]
        by_provider[p] = by_provider.get(p, 0) + 1
        assert v["id"] == "%s:%s" % (p, v["native_id"]), v["id"]
        assert v["name"] and v["lang"]
    assert by_provider == {"edge": 83, "volcano": 94, "aliyun": 31, "index": 6}, by_provider
    ids = [v["id"] for v in voices]
    assert len(set(ids)) == 214, "id 重复"


def test_kw_search_and_filters():
    """② keywords 搜索（单/多词 AND）+ provider/lang/category 筛选 + limit。"""
    r = vl.search_voices("晓晓")
    assert r["filtered"] >= 1 and all("晓晓" in v["name"] for v in r["voices"]), r
    r2 = vl.search_voices("温柔 女")
    assert r2["filtered"] >= 1
    for v in r2["voices"]:
        text = (v["name"] + " " + " ".join(v.get("tags") or [])).lower()
        assert "温柔" in text and "女" in text, v["name"]
    rv = vl.search_voices(provider="volcano")
    assert rv["total"] == 94 and all(v["provider"] == "volcano" for v in rv["voices"]), rv["total"]
    ra = vl.search_voices(provider="aliyun")
    assert ra["total"] == 31 and ra["filtered"] == 31
    ri = vl.search_voices(provider="index")
    assert ri["total"] == 6
    re_ = vl.search_voices(provider="edge")
    assert re_["total"] == 83
    rl = vl.search_voices(lang="zh-CN")
    assert rl["total"] >= 1 and all(str(v["lang"]).startswith("zh-CN") for v in rl["voices"])
    rc = vl.search_voices(category="通用")
    assert rc["total"] >= 1 and all("通用" in [str(x) for x in v.get("categories") or []]
                                    for v in rc["voices"])
    rlim = vl.search_voices(limit=10, provider="all")
    assert rlim["returned"] == 10
    rall = vl.search_voices()
    assert rall["total"] == 214 and rall["filtered"] == 214


def test_voice_library_node_run():
    """③ 节点 run → VoiceProfile 列表 + 检索报告。"""
    store = FakeStore()
    node = vl.VoiceLibraryNode()
    out = asyncio.run(node.run({"store": store, "progress": lambda p: None},
                               {}, {"query": "", "provider": "all",
                                    "lang": "", "category": "", "limit": 0}))
    voices = store.read_json(out["voices"].id)
    assert isinstance(voices, list) and len(voices) == 214, "limit=0 应返回全量 214"
    report = store.read_json(out["report"].id)
    assert report["ok"] is True and report["total_matching_filter"] == 214
    assert report["returned"] == 214
    assert set(report["by_provider"].keys()) == {"edge", "volcano", "aliyun", "index"}
    assert report["by_provider"] == {"edge": 83, "volcano": 94, "aliyun": 31, "index": 6}, \
        report["by_provider"]
    # 显式 limit 生效
    out3 = asyncio.run(node.run({"store": store, "progress": lambda p: None},
                                {}, {"query": "", "provider": "all",
                                     "lang": "", "category": "", "limit": 200}))
    assert len(store.read_json(out3["voices"].id)) == 200
    # 关键词搜索经节点 run 真实命中
    out2 = asyncio.run(node.run({"store": store, "progress": lambda p: None},
                                {}, {"query": "Vivi", "provider": "all",
                                     "lang": "", "category": "", "limit": 200}))
    hits = store.read_json(out2["voices"].id)
    assert 1 <= len(hits) <= 214
    assert any("vivi" in v["name"].lower() or "vivi" in v["id"].lower() for v in hits)


def test_tts_dynamic_load():
    """④ core/tts 音色下拉动态加载（不再仅 39 内置）。"""
    opts = tts.voice_options()
    assert len(opts) > 39, len(opts)
    assert any(o.startswith("volcano:") for o in opts), "应含 volcano 统一 id"
    assert any(o.startswith("edge:") for o in opts), "应含 edge 统一 id"
    assert any(o.startswith("aliyun:") for o in opts)
    # spec() 层面：voice 参数 options 动态展开（前端 /object_info 下拉真实可用）
    spec = tts.TTSNode.spec()
    voice_port = next(p for p in spec.params if p.name == "voice")
    assert len(voice_port.options) > 39 and "volcano:zh_female_vv_uranus_bigtts" in voice_port.options
    # 39 裸 edge id 仍保留（向后兼容）
    assert "zh-CN-XiaoxiaoNeural" in voice_port.options


def test_t6_single_source():
    """⑤ T6 双份数据不复发：全仓库仅 1 份 tts_library.json；统一模型解析。"""
    hits = glob.glob(os.path.join(_REPO, "..", "packages", "*", "**", "tts_library.json"),
                     recursive=True)
    hits = [h for h in hits if "user_nodes_data_backup" not in h]
    assert len(hits) == 1, "tts_library.json 应只有 core_voice_library/data 一份: %s" % hits
    assert hits[0].endswith(os.path.join("core_voice_library", "data", "tts_library.json")), hits
    # 统一模型解析：edge: 前缀 → native_id；裸 id 兼容；非 edge 提供商明确报错
    assert tts.resolve_edge_voice("edge:zh-CN-XiaoxiaoNeural") == "zh-CN-XiaoxiaoNeural"
    assert tts.resolve_edge_voice("zh-CN-XiaoxiaoNeural") == "zh-CN-XiaoxiaoNeural"
    try:
        tts.resolve_edge_voice("volcano:zh_female_vv_uranus_bigtts")
        raise AssertionError("volcano 音色应明确报错")
    except RuntimeError as e:
        assert "invalid:" in str(e) and "volcano" in str(e)


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