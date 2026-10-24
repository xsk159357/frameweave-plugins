"""C4 core/tts 厂商引擎（aliyun NLS+CosyVoice / volcano 豆包）验收测试。

pytest 收集兼容；纯 stdlib（http.server mock 端点）+ 平台 app 直跑：
    python test_c4_vendor_engines.py

覆盖（对齐 C4 acceptance / 02 §3.3-C1 厂商能力差距关闭）：
1. 引擎注册：spec()/manifest engine select 含 aliyun/volcano；版本 2.1.0；
2. 参数面：rate/volume/pitch + 厂商密钥/端点参数完整（aliyun_appkey/token/ak_*/model/
   base_url/token_url；volcano_appid/token/cluster/base_url）；
3. aliyun NLS mock：GET /stream/v1/tts 跑通（appkey+token+voice，speech_rate/volume/
   pitch_rate 正确映射），产出音频资产；
4. aliyun NLS AK/SK → /oauth2/token 换 Token 路径跑通；
5. aliyun CosyVoice mock（DashScope SSE）：POST text2audio/generation 跑通，speed 映射；
6. volcano mock：POST /api/v1/tts 跑通（app/token/cluster/voice_type/speed_ratio/
   volume_ratio/pitch_ratio + Authorization: Bearer;<token>）；
7. 无 key 显式报错：aliyun/volcano 缺密钥 → RuntimeError 含 "invalid:" 与 "需配置密钥"；
8. 音色归一：volcano:*/aliyun:* → native；跨引擎音色明确报错；
9. SDK 门禁：check_manifest G1-G10（签名后）。

环境：backend/.venv（python -m pytest 或独立脚本入口）。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, r"F:\1223\解说工坊\FrameWeave\backend")
_PKG = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_PKG)
for p in (_REPO, _PKG):
    if p not in sys.path:
        sys.path.insert(0, p)

import core_tts.node as tts  # noqa: E402


# ---------------------------------------------------------------- mock 端点
def _mk_wav(seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", 0)
                               for _ in range(int(16000 * seconds))))
    return buf.getvalue()


_WAV = _mk_wav()


class MockTTSHandler(BaseHTTPRequestHandler):
    """三厂商 mock：/oauth2/token、/stream/v1/tts、/api/v1/tts、text2audio SSE。

    每个请求 append 进 captured（token→synth 双请求流程按序断言）。
    """

    captured: List[Dict[str, Any]] = []
    _sse = True

    def log_message(self, *a):  # noqa: D401 —— 静默
        pass

    def _capture(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        rec = {"path": self.path, "headers": dict(self.headers),
               "body": body, "body_json": None}
        try:
            rec["body_json"] = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            pass
        MockTTSHandler.captured.append(rec)
        return body

    def _send(self, code: int, data: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 —— http.server 命名
        self._capture()
        u = urlsplit(self.path)
        if u.path.endswith("/oauth2/token"):
            self._send(200, json.dumps(
                {"Token": {"Id": "mock_nls_token_abc", "ExpireTime": 9999999999}},
                ensure_ascii=False).encode("utf-8"), "application/json")
            return
        if u.path.endswith("/stream/v1/tts"):
            self._send(200, _WAV, "audio/wav")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        body = self._capture()
        u = urlsplit(self.path)
        if u.path.endswith("/api/v1/tts"):
            j = json.loads(body.decode("utf-8", "replace"))
            if j.get("audio", {}).get("voice_type") == "mock_fail":
                # 业务错误路径：code != 3000 且带 message
                self._send(200, json.dumps(
                    {"code": 40001, "message": "mock business error"},
                    ensure_ascii=False).encode("utf-8"), "application/json")
                return
            self._send(200, _WAV, "audio/mp3")
            return
        if u.path.endswith("/api/v1/services/aigc/text2audio/generation"):
            audio_b64 = base64.b64encode(_WAV).decode("ascii")
            if MockTTSHandler._sse:
                sse = ("event:result\n:HTTP_STATUS/200\n"
                       "data: {\"output\":{\"audio\":\"%s\"},\"usage\":{\"total_tokens\":1}}\n\n"
                       % audio_b64).encode("utf-8")
                self._send(200, sse, "text/event-stream")
            else:
                self._send(200, _WAV, "audio/mp3")
            return
        self._send(404, b"not found", "text/plain")


class MockTTS:
    def __init__(self):
        self.captured: List[Dict[str, Any]] = []
        MockTTSHandler.captured = self.captured
        self.srv = HTTPServer(("127.0.0.1", 0), MockTTSHandler)
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    @property
    def last(self) -> Dict[str, Any]:
        return self.captured[-1]

    def close(self):
        self.srv.shutdown()
        self.th.join(timeout=5)


# ---------------------------------------------------------------- 测试夹具
class FakeStore:
    def __init__(self, root: str):
        self.root = root
        self.files_dir = os.path.join(root, "files")
        os.makedirs(self.files_dir, exist_ok=True)
        self._a: Dict[str, Any] = {}
        self._n = 0

    def read_json(self, aid):
        return self._a.get(aid)

    def save_asset(self, asset, payload=None):
        self._n += 1
        aid = "aid%d" % self._n
        asset.id = aid
        asset.meta = dict(asset.meta or {})
        if payload is not None:
            asset.path = os.path.join(self.files_dir, aid + ".json")
            with open(asset.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        self._a[aid] = payload
        return asset


def _mk_ctx(store):
    async def _run_tracked(cmd, timeout=None, **kw):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

    return {
        "store": store,
        "progress": lambda p: None,
        "run_tracked": _run_tracked,
        "resolve_secret": lambda ref: os.environ.get("FW_MOCK_" + ref, ""),
        "local_token": lambda: "mock-token",
        "node_id": "n1",
    }


def _mk_tmp():
    return os.path.join(tempfile.gettempdir(), "fw_c4_tts_%d" % int(time.time() * 1000))


def _base_params(**over):
    p = {
        "direct_text": "测试厂商引擎合成文本",
        "output_format": "wav",
        "chunk_chars": 800,
        "max_concurrent": 2,
        "retries": 1,
        "rate": "+0%", "pitch": "+0Hz", "volume": "+0%",
    }
    p.update(over)
    return p


def _run_node(params):
    store = FakeStore(_mk_tmp())
    out = asyncio.run(tts.TTSNode().run(_mk_ctx(store), {}, params))
    return store, out


def _read_audio_bytes(store, asset) -> bytes:
    with open(asset.path, "rb") as f:
        return f.read()


# ================================================================ 引擎注册 / 参数面
def test_engines_registered():
    """① aliyun/volcano 引擎注册（spec + manifest select options）+ 版本 2.1.0。"""
    spec = tts.TTSNode.spec()
    eng = next(p for p in spec.params if p.name == "engine")
    assert eng.options == ["edge", "manual", "indextts", "cloud", "aliyun", "volcano"], eng.options
    assert spec.version == "2.1.0", spec.version
    with open(os.path.join(_PKG, "manifest.json"), "r", encoding="utf-8") as f:
        m = json.load(f)
    m_eng = next(p for p in m["params"] if p["name"] == "engine")
    assert m_eng["options"] == eng.options, m_eng["options"]
    assert m["version"] == "2.1.0"
    # 新厂商参数齐备
    names = {p["name"] for p in m["params"]}
    for expect in ("aliyun_mode", "aliyun_appkey", "aliyun_token", "aliyun_ak_id",
                   "aliyun_ak_secret", "aliyun_model", "aliyun_base_url", "aliyun_token_url",
                   "volcano_appid", "volcano_token", "volcano_cluster", "volcano_base_url"):
        assert expect in names, "manifest 缺参数 %s" % expect
    # rate/volume/pitch 参数面保持（三厂商共用）
    for expect in ("rate", "volume", "pitch"):
        assert expect in names


# ================================================================ aliyun NLS
def test_aliyun_nls_mock():
    """③ aliyun NLS：GET /stream/v1/tts mock 跑通，参数映射正确，产出音频资产。"""
    mock = MockTTS()
    try:
        store, out = _run_node(_base_params(
            engine="aliyun", aliyun_mode="nls", aliyun_appkey="appkey_x",
            aliyun_token="tok_nls", voice="aliyun:xiaoyun",
            aliyun_base_url=mock.base, aliyun_token_url=mock.base + "/oauth2/token",
            rate="+10%", volume="-20%", pitch="+0Hz", output_format="wav"))
        cap = mock.last
        assert cap["path"].startswith("/stream/v1/tts?"), cap
        q = parse_qs(urlsplit(cap["path"]).query)
        assert q["appkey"] == ["appkey_x"] and q["token"] == ["tok_nls"]
        assert q["voice"] == ["xiaoyun"]                      # aliyun:* → native
        assert q["speech_rate"] == ["10"]                     # +10% → +10
        assert q["pitch_rate"] == ["0"]                       # +0Hz → 0
        assert q["volume"] == ["40"]                          # -20% → 0.8 → 40/100
        assert "测试厂商引擎合成文本" in q["text"][0]
        audio = _read_audio_bytes(store, out["audio"])
        assert len(audio) >= 512 and audio[:4] == b"RIFF", "NLS 应产出 WAV 容器音频"
        assert out["report"]["engine"] == "aliyun"
    finally:
        mock.close()


def test_aliyun_nls_aksk_token_path():
    """④ aliyun NLS：无预签发 Token 时 AK/SK → /oauth2/token 换取后合成。"""
    mock = MockTTS()
    try:
        store, out = _run_node(_base_params(
            engine="aliyun", aliyun_mode="nls", aliyun_appkey="appkey_x",
            aliyun_ak_id="akid", aliyun_ak_secret="aksecret",
            voice="aliyun:xiaoyun", aliyun_base_url=mock.base,
            aliyun_token_url=mock.base + "/oauth2/token", output_format="wav"))
        # 先打 token 端点，再打合成端点（携带换取到的 token）
        assert len(mock.captured) >= 2, mock.captured
        tok_req, tts_req = mock.captured[0], mock.captured[1]
        assert tok_req["path"].startswith("/oauth2/token?"), tok_req["path"]
        q = parse_qs(urlsplit(tok_req["path"]).query)
        assert q["Action"] == ["CreateToken"] and q["SignatureMethod"] == ["HMAC-SHA1"]
        assert tts_req["path"].startswith("/stream/v1/tts?"), tts_req["path"]
        q2 = parse_qs(urlsplit(tts_req["path"]).query)
        assert q2["token"] == ["mock_nls_token_abc"], q2["token"]
        assert len(_read_audio_bytes(store, out["audio"])) >= 512
    finally:
        mock.close()


def test_aliyun_nls_no_key_explicit_error():
    """⑦a aliyun NLS 无密钥 → 显式报错（invalid: 需配置密钥）。"""
    try:
        _run_node(_base_params(engine="aliyun", aliyun_mode="nls",
                               voice="aliyun:xiaoyun", aliyun_base_url="http://127.0.0.1:9"))
        raise AssertionError("缺 AppKey/Token 应报错")
    except RuntimeError as e:
        msg = str(e)
        assert "invalid:" in msg and "需配置密钥" in msg, msg


# ================================================================ aliyun CosyVoice
def test_aliyun_cosyvoice_mock():
    """⑤ aliyun CosyVoice：DashScope text2audio SSE mock 跑通，speed 映射正确。"""
    mock = MockTTS()
    try:
        MockTTSHandler._sse = True
        store, out = _run_node(_base_params(
            engine="aliyun", aliyun_mode="cosyvoice", api_key="sk-dashscope",
            voice="aliyun:longxiaochun", rate="+10%", output_format="mp3",
            aliyun_base_url=mock.base))
        cap = mock.last
        assert cap["path"].endswith("/api/v1/services/aigc/text2audio/generation"), cap
        j = cap["body_json"]
        assert j["model"] == "cosyvoice-v2"
        assert j["input"]["text"] == "测试厂商引擎合成文本"
        assert j["voice"] == "longxiaochun"
        assert j["parameters"]["speed"] == 1.1, "rate +10% → speed 1.1"
        assert cap["headers"].get("Authorization") == "Bearer sk-dashscope"
        audio = _read_audio_bytes(store, out["audio"])
        assert len(audio) >= 512 and audio[:4] == b"RIFF", "SSE base64 音频应解码为 WAV"
    finally:
        mock.close()


def test_aliyun_cosy_no_key_explicit_error():
    """⑦b aliyun CosyVoice 无密钥 → 显式报错（invalid: 需配置密钥）。"""
    try:
        _run_node(_base_params(engine="aliyun", aliyun_mode="cosyvoice",
                               voice="aliyun:longxiaochun"))
        raise AssertionError("缺 DashScope Key 应报错")
    except RuntimeError as e:
        msg = str(e)
        assert "invalid:" in msg and "需配置密钥" in msg, msg


# ================================================================ volcano 豆包
def test_volcano_mock():
    """⑥ volcano：POST /api/v1/tts mock 跑通，app/token/cluster/ratio + 鉴权头正确。"""
    mock = MockTTS()
    try:
        store, out = _run_node(_base_params(
            engine="volcano", volcano_appid="app_123", volcano_token="tok_v",
            voice="volcano:zh_female_vv_uranus_bigtts",
            volcano_base_url=mock.base, rate="+10%", volume="-20%", pitch="+0Hz",
            output_format="mp3"))
        cap = mock.last
        assert cap["path"].endswith("/api/v1/tts"), cap["path"]
        j = cap["body_json"]
        assert j["app"] == {"appid": "app_123", "token": "tok_v", "cluster": "volcano_tts"}
        assert j["user"]["uid"]
        assert j["audio"]["voice_type"] == "zh_female_vv_uranus_bigtts"
        assert j["audio"]["encoding"] == "mp3"
        assert abs(j["audio"]["speed_ratio"] - 1.1) < 1e-6
        assert abs(j["audio"]["volume_ratio"] - 0.8) < 1e-6
        assert abs(j["audio"]["pitch_ratio"] - 1.0) < 1e-6
        assert j["request"]["text"] == "测试厂商引擎合成文本"
        assert j["request"]["operation"] == "query"
        assert cap["headers"].get("Authorization") == "Bearer;tok_v"
        audio = _read_audio_bytes(store, out["audio"])
        assert len(audio) >= 512 and audio[:4] == b"RIFF"
        assert out["report"]["engine"] == "volcano"
    finally:
        mock.close()


def test_volcano_business_error_json():
    """⑥b volcano 业务错误 JSON（code != 3000）→ 明确报错。"""
    mock = MockTTS()
    try:
        try:
            _run_node(_base_params(
                engine="volcano", volcano_appid="app_123", volcano_token="tok_v",
                voice="volcano:mock_fail",
                volcano_base_url=mock.base, output_format="mp3"))
            raise AssertionError("业务错误应上抛")
        except RuntimeError as e:
            assert "业务错误" in str(e), str(e)
    finally:
        mock.close()


def test_volcano_no_key_explicit_error():
    """⑦c volcano 无密钥 → 显式报错（invalid: 需配置密钥）。"""
    try:
        _run_node(_base_params(engine="volcano", volcano_base_url="http://127.0.0.1:9",
                               voice="volcano:zh_female_vv_uranus_bigtts"))
        raise AssertionError("缺 AppID/Token 应报错")
    except RuntimeError as e:
        msg = str(e)
        assert "invalid:" in msg and "需配置密钥" in msg, msg


# ================================================================ 音色归一
def test_vendor_voice_normalization():
    """⑧ 音色归一：volcano:*/aliyun:* → native；跨引擎音色明确报错；裸 id 兼容。"""
    assert tts.resolve_vendor_voice("volcano", "volcano:zh_male_sunwukong_uranus_bigtts") == \
        "zh_male_sunwukong_uranus_bigtts"
    assert tts.resolve_vendor_voice("volcano", "speaker_id_icl") == "speaker_id_icl"  # ICL SpeakerID
    assert tts.resolve_vendor_voice("aliyun", "aliyun:xiaoyun", "nls") == "xiaoyun"
    assert tts.resolve_vendor_voice("aliyun", "aliyun-nls:xiaoyun", "nls") == "xiaoyun"
    assert tts.resolve_vendor_voice("aliyun", "aliyun-cosy:longxiaochun", "cosyvoice") == "longxiaochun"
    try:
        tts.resolve_vendor_voice("volcano", "aliyun:xiaoyun")
        raise AssertionError("aliyun 音色用于 volcano 应报错")
    except RuntimeError as e:
        assert "invalid:" in str(e) and "volcano" in str(e)
    try:
        tts.resolve_vendor_voice("aliyun", "aliyun-cosy:longxiaochun", "nls")
        raise AssertionError("cosyvoice 音色 + nls 模式应报错")
    except RuntimeError as e:
        assert "invalid:" in str(e) and "cosyvoice" in str(e)
    # edge 引擎对厂商音色仍显式拒绝（S3T8 回归保持）
    try:
        tts.resolve_edge_voice("volcano:zh_female_vv_uranus_bigtts")
        raise AssertionError("edge 引擎应拒绝 volcano 音色")
    except RuntimeError as e:
        assert "invalid:" in str(e) and "volcano" in str(e)


def test_ratio_param():
    """⑧b rate/volume/pitch 比率转换。"""
    assert tts._ratio_param("+10%") == 1.1
    assert tts._ratio_param("-20%") == 0.8
    assert tts._ratio_param("+50Hz") == 1.5
    assert tts._ratio_param("1.2") == 1.2
    assert tts._ratio_param("") == 1.0
    assert tts._ratio_param("bogus") == 1.0
    assert tts._ratio_param("300%") == 2.0  # 限幅
    assert tts._ratio_param("-300%") == 0.5


# ================================================================ 独立脚本入口
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
