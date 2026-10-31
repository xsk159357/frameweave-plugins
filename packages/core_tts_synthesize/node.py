"""语音合成节点：文案 → 配音，并给出每句的真实时长。

为什么逐句合成，不是整段一次说完：
  1. 下游要按配音时长排片（timeline_build 现在按字数估），只有逐句才有边界；
  2. 整段合成之后想在中间改一句、重读一句，做不到；逐句可以只重跑那一句。

引擎：三条可用路 + 一条兜底：
- indextts: IndexTTS 2.5 的权重在本机是齐的（约 9.5 GB：gpt 3.1G / sffmel 396M / codec 580M
  / hf_cache 5.2G），但它依赖的 transformers 版本与平台 venv 冲突
  （`OffloadedCache` 已被移除），需要给模型单独准备运行时才能用。
- edge:     edge-tts 免费在线配音（v1.4.0 从归档 core_tts 移植，FW-DEV-36 同源）。
  零下载、无需 Key、只需联网；endpoint speech.platform.bing.com 已声明进 perm.net。
  失败分流沿用归档口径：403/Sec-MS-GEC = 签名类（edge-tts<7.3.1 发 v1 令牌，
  重试无效，直接报「请升级依赖」）；连接类 = network（指数退避重试）。
- sapi:     Windows 系统语音（秒出，但部分机器没有可用中文语音）。

所以 engine=auto 的降级链是 indextts → edge → sapi，每一步降级都写进 warnings
——**不假装用了高优先级引擎**。显式选 indextts/edge 而环境不满足时直接报错，
不是静默降级：用户选了它，就该知道为什么没用上。

时长口径：以 ffprobe 读到的实际音频为准，不用字数估算。这是本节点在整条链里
的核心价值——它是唯一知道「这句话读出来到底几秒」的地方。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Tuple

from app.assets import Asset
from app.media_tools import ffmpeg_path, ffprobe_path

# 句末标点（后向零宽切分）：一行里有多句时要拆开，否则逐句时长只剩一句。
_SENT_END = re.compile(r"(?<=[。！？!?；;])")
from app.nodespec import NodeBase, PortType

# 句末标点：遇到这些就断句
SENT_END = "。！？!?；;…"
# 长句再切时的软断点
SOFT = "，,、：:"

# 统一采样口径：SAPI 输出的 wav 采样率随音色/系统版本变，
# 不统一的话 concat 在混合来源上会直接报错或产出噪声
AR = "22050"

# ---- edge 引擎（v1.4.0，从归档 core_tts 移植，口径与 FW-DEV-36 一致）----
# edge-tts 的 SentenceBoundary 用 100ns tick 记时间（offset=1000000 即 0.1 s），不是秒。
_EDGE_100NS = 10_000_000.0
_EDGE_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
# endpoint 已声明进 manifest.perm.net.hosts；写在这里仅供报错提示与单测对账
_EDGE_HOST = "speech.platform.bing.com"
_EDGE_HINT_SIGNATURE = (
    "配音服务签名已过期，请升级到新版本（需要 edge-tts>=7.3.1）。"
    "这是配音服务端的安全令牌更新，重试无效。")
_EDGE_HINT_NETWORK = "网络不通，无法连接配音服务。请检查网络/代理后重试。"


def _edge_version() -> str:
    try:
        import edge_tts
        return str(getattr(edge_tts, "__version__", "?"))
    except Exception:  # noqa: BLE001 —— 版本只用于提示，取不到不影响分流
        return "未安装"


def _edge_http_status(exc: BaseException):
    """尽力取 HTTP 状态：aiohttp/websockets 两族的字段名不同，两条都试。"""
    for attr in ("status", "status_code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    if resp is not None:
        for attr in ("status_code", "status"):
            v = getattr(resp, attr, None)
            if isinstance(v, int):
                return v
    return None


def _edge_failure_kind(exc: BaseException) -> str:
    """edge 失败分类（FW-DEV-36）：signature（令牌/403，不可重试）/ network（可重试）
    / dependency / other。403 属于端点侧令牌版本问题，重试只是让用户多等几秒再看到
    同一个「配音失败」，所以 signature/dependency 立刻失败并说清原因。"""
    if _edge_http_status(exc) == 403:
        return "signature"
    text = "%s: %s" % (type(exc).__name__, exc)
    if "Sec-MS-GEC" in text:
        return "signature"
    if isinstance(exc, (ImportError, AttributeError)) and "edge_tts" in text:
        return "dependency"
    low = type(exc).__name__.lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return "network"
    if any(k in low for k in ("connector", "clientconnection", "servertimeout",
                              "serverdisconnected", "clientoserror", "clientpayloaderror")):
        return "network"
    if "getaddrinfo" in text or "name resolution" in text.lower():
        return "network"
    return "other"


def _edge_failure_message(kind: str, exc: BaseException, attempts: int) -> str:
    if kind == "signature":
        return "%s（当前 edge-tts %s）" % (_EDGE_HINT_SIGNATURE, _edge_version())
    if kind == "dependency":
        return ("配音依赖异常，请重新安装或升级 edge-tts（>=7.3.1）：%s（当前 %s）"
                % (exc, _edge_version()))
    if kind == "network":
        return "%s（%s）" % (_EDGE_HINT_NETWORK, exc)
    return "edge-tts 第 %d 次失败: %s" % (attempts, exc)


def _edge_resolve_voice(voice: str) -> str:
    """edge 引擎音色归一：edge:{native} → native；裸 id 按 edge 兼容处理；
    其它引擎前缀明确报错（音色属于谁就该用谁的引擎）。空 → 默认晓晓。"""
    v = str(voice or "").strip()
    if not v:
        return _EDGE_DEFAULT_VOICE
    if ":" in v:
        provider, native = v.split(":", 1)
        if provider != "edge":
            raise RuntimeError(
                "invalid: 音色 %r 属于 %s 引擎，edge 引擎仅支持 edge:* 音色——"
                "请切换到对应引擎" % (v, provider))
        return native
    return v


async def _edge_one(text: str, out_path: str, voice: str, rate: int,
                    volume: int, retries: int = 3) -> None:
    """edge-tts 合成一句（不含分块——v3 逐句切分已由 _to_lines 完成）。

    不能用 comm.save()：它只落音频，把服务端每次都在发的 SentenceBoundary 丢掉。
    v3 逐句口径下单句边界暂不外抛（segments 以句为单位由 ffprobe 实测），但保留
    读取路径，句子内多子句的时间轴后续要用时不再踩 100ns tick 这个坑。
    """
    import edge_tts
    # SAPI 的 rate 是 -10~10 档位，edge 语法是 +N%/-N%：按档位×5% 线性映射
    # （0 档=正常，±10 档=±50%，与 SAPI 档位感知大致对齐）。
    rate_pct = "+%d%%" % (int(rate) * 5) if rate > 0 else \
        ("%d%%" % (int(rate) * 5) if rate < 0 else "+0%")
    # volume 是 0-100 的绝对值，edge 语法同样带符号：50 即 "+50%"（相对正常音量）。
    vol_pct = "+%d%%" % int(volume) if volume > 0 else "-0%"
    attempt = 0
    while True:
        attempt += 1
        try:
            comm = edge_tts.Communicate(text, voice=voice, rate=rate_pct,
                                        volume=vol_pct)
            with open(out_path, "wb") as f:
                async for chunk in comm.stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        f.write(chunk["data"])
            if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
                raise RuntimeError("edge-tts 输出为空")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 按 FW-DEV-36 分流
            kind = _edge_failure_kind(e)
            if kind in ("signature", "dependency") or attempt > retries:
                raise RuntimeError(_edge_failure_message(kind, e, attempt)) from e
            await asyncio.sleep(min(30, 0.25 * (2 ** attempt)))


def _app_root() -> str:
    """backend 根目录——用 app 包位置推，不依赖 node.py 被放在哪。"""
    import app as _app
    return os.path.dirname(os.path.dirname(os.path.abspath(_app.__file__)))


def _split_long(piece: str, max_chars: int) -> List[str]:
    """只处理超长句：按软断点切成若干段，尽量每段接近 max_chars 的一半以上。"""
    piece = str(piece or "").strip()
    if not piece:
        return []
    if len(piece) <= max_chars:
        return [piece]
    out: List[str] = []
    buf = ""
    for ch in piece:
        buf += ch
        if ch in SOFT and len(buf) >= max(4, max_chars // 2):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    # 仍然超长的（整段没有软断点），硬切
    final: List[str] = []
    for one in out:
        while len(one) > max_chars * 2:
            final.append(one[:max_chars])
            one = one[max_chars:]
        if one:
            final.append(one)
    return final


def _extract_ref_audio(src: str, workdir: str, seconds: float = 8.0) -> str:
    """从视频里抽一段音频当音色参考。失败返回空串。

    为什么需要：IndexTTS 内部用 librosa.load() 读参考音频，它读不了 mp4。
    而 voice_ref 端口最自然的来源就是 media_import 的 audio——那是个 mp4 路径。
    不转一下，用户接上端口也会失败。
    """
    exe = ffmpeg_path()
    if not exe or not os.path.isfile(src):
        return ""
    out_dir = os.path.join(workdir, "voice_ref")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "ref.wav")
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-y", "-i", src, "-t", str(seconds),
             "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", out],
            capture_output=True, text=True, timeout=120, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) < 1024:
        return ""
    return out


def _to_lines(payload: Any, max_chars: int) -> List[str]:
    """SCRIPT 载荷 → 逐句。兼容 {text, lines}、纯字符串、字符串数组三种形态。

    按句末标点再切一次：本节点的核心价值是「逐句真实时长」，一行里塞两句话
    却合成成一段，下游排片就拿不到句子边界，等于白做。上游 script_input /
    ai_narrate 产出的 lines 本来就是按句切好的，这里兜的是用户直接粘一段
    文案进 text 的情况。
    """
    lines: List[str] = []
    if isinstance(payload, dict):
        raw = payload.get("lines")
        if isinstance(raw, list) and raw:
            lines = [str(x) for x in raw if str(x).strip()]
        else:
            txt = str(payload.get("text") or "")
            lines = [x for x in txt.splitlines() if x.strip()]
    elif isinstance(payload, list):
        lines = [str(x) for x in payload if str(x).strip()]
    elif isinstance(payload, str):
        lines = [x for x in payload.splitlines() if x.strip()]

    out: List[str] = []
    for one in lines:
        out.extend(_split_long(one, max_chars))

    # 按句末标点再切：同一行里的「第一句。第二句。」要拆成两句，
    # 否则逐句时长只剩一句，下游按它排片就是错的。
    resplit: List[str] = []
    for one in out:
        parts = [p for p in _SENT_END.split(one) if p.strip()]
        resplit.extend(parts or [one])

    # 只剩标点的碎片并到下一句，避免合成出一堆 0.3 秒的气口。
    # 判断用 _is_fragment（按去标点后的字数），不是按原始长度——
    # 按长度会把「走吧。」这类正常短句一并并掉。
    merged: List[str] = []
    for one in resplit:
        if merged and _is_fragment(merged[-1]):
            merged[-1] = merged[-1] + one
        else:
            merged.append(one)
    return [x for x in merged if x.strip()]


_PUNCT = "，。！？；：、,.;:!?…—～~\"'“”‘’（）()【】[]《》<>"


def _is_fragment(s: str) -> bool:
    """是不是「只剩标点」那种碎片。

    按去掉标点后的字数判断，不按原始长度：`走吧。` 原长 3、去标点 2，是一句
    正常的话；`甲。` 去标点只有 1，才是该并进下一句的碎片。用原长度判断会把
    「走吧。」这种短句也并掉，配音听起来就没有停顿了。
    """
    return len([c for c in str(s) if c not in _PUNCT]) < 2


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字符串：内部的 ' 要写成 ''。"""
    return "'" + str(s).replace("'", "''") + "'"


def _sapi_one(text: str, out_wav: str, voice: str, rate: int, volume: int, work: str) -> None:
    """用系统 SAPI 合成一句。文本直接嵌进脚本，不走临时文件——少一层编码坑。"""
    body = [
        "Add-Type -AssemblyName System.Speech",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
    ]
    if voice:
        body.append("$s.SelectVoice(%s)" % _ps_quote(voice))
    body += [
        "$s.Rate = %d" % int(rate),
        "$s.Volume = %d" % int(volume),
        "$s.SetOutputToWaveFile(%s)" % _ps_quote(out_wav),
        "$s.Speak(%s)" % _ps_quote(text),
        "$s.Dispose()",
    ]
    ps = os.path.join(work, "_sapi.ps1")
    # PowerShell 5.1 读**无 BOM** 的 .ps1 会按系统 ANSI 解码，中文直接变乱码
    with open(ps, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write("\n".join(body) + "\n")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps],
                       capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.isfile(out_wav) or os.path.getsize(out_wav) < 128:
        raw = (r.stderr or r.stdout or "").strip()
        # PowerShell 的输出是 GBK，经 utf-8 解码后是乱码——直接抛给用户等于没报错。
        # 按已知的失败形态翻成人话，剩下的才原样附上供排查。
        low = raw.lower()
        if "no voice installed" in low or "未安装语音" in raw or "安全设置" in raw \
                or "speak" in low and "exception" in low:
            hint = ("系统语音不可用：这台机器上找不到可用的中文语音，或当前权限读不到它。"
                    "改用 engine=indextts（需先配好模型），或在能联网的机器上用云端 TTS。")
        elif "cannot find path" in low or "找不到路径" in raw:
            hint = "系统语音合成脚本没有跑起来（PowerShell 脚本路径问题），请检查系统环境。"
        else:
            hint = "系统语音合成失败，原因见下方原始输出。"
        raise RuntimeError("系统语音合成失败（%s…）：%s\n原始输出：%s"
                           % (text[:16], hint, raw[:300]))


def _run(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def _norm(src: str, dst: str, ff: str) -> None:
    r = _run([ff, "-y", "-v", "error", "-i", src, "-ar", AR, "-ac", "1",
              "-c:a", "pcm_s16le", dst], 180)
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError("音频格式统一失败: %s" % (r.stderr or "").strip()[:300])


def _silence(dst: str, seconds: float, ff: str) -> None:
    _run([ff, "-y", "-v", "error", "-f", "lavfi", "-i",
          "anullsrc=r=%s:cl=mono" % AR, "-t", "%.3f" % seconds,
          "-c:a", "pcm_s16le", dst], 60)


def _concat(list_file: str, dst: str, ff: str) -> None:
    r = _run([ff, "-y", "-v", "error", "-f", "concat", "-safe", "0",
              "-i", list_file, "-c", "copy", dst], 300)
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError("配音拼接失败: %s" % (r.stderr or "").strip()[:300])


def _dur(path: str, fp: str) -> float:
    r = _run([fp, "-v", "error", "-show_entries", "format=duration",
              "-of", "csv=p=0", path], 60)
    try:
        return float((r.stdout or "").strip())
    except (TypeError, ValueError):
        return 0.0


def _indextts_base(params: Optional[Dict[str, Any]] = None) -> str:
    """IndexTTS 模型根。三级来源，优先级从高到低：

    1. 参数 `indextts_dir`（worker 环境下唯一可靠的一条——worker 的 env 是白名单，
       宿主设的 FRAMEWEAVE_INDEXTTS_DIR 传不进去）；
    2. 环境变量 FRAMEWEAVE_INDEXTTS_DIR（直跑/宿主进程内有效）；
    3. backend/data/models/index-tts-code（默认安装位）。

    为什么需要能改：模型目录必须纯 ASCII（wetext 的 kaldifst 用窄字符 API），
    而工作区路径常含中文。给一条显式出口，用户把模型放到英文路径后指过来即可，
    不必为此搬家整个安装目录。
    """
    if isinstance(params, dict):
        p = str(params.get("indextts_dir") or "").strip()
        if p:
            return os.path.abspath(p)
    override = (os.environ.get("FRAMEWEAVE_INDEXTTS_DIR") or "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(_app_root(), "data", "models", "index-tts-code")


def _indextts_ready(params: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """IndexTTS 能不能用。只做轻量检查，不加载 9.5 GB 权重。

    三条都是「跑起来必炸」的前置条件。与其让用户点完播放等了半天再崩，
    不如在入口就说清楚缺什么：
      1. 模型目录与权重在不在；
      2. 独立运行时（.venv）在不在——IndexTTS 要 torch 2.8 / transformers 4.52.1，
         平台 venv 是 torch 2.11 / transformers 5.x，两套装不到一起；
      3. 路径是不是纯 ASCII——wetext 里的 kaldifst 是 C++ 扩展，用窄字符 API
         打开 .fst 词典，中文路径直接打不开。
    """
    base = _indextts_base(params)
    if not os.path.isdir(base):
        return False, "模型目录不存在：%s" % base
    for rel in ("checkpoints/gpt.pth", "checkpoints/config.yaml"):
        p = os.path.join(base, *rel.split("/"))
        if not os.path.isfile(p):
            return False, "权重缺失：%s" % p
    venv_py = os.path.join(base, ".venv", "Scripts", "python.exe")
    if not os.path.isfile(venv_py):
        return False, "独立运行时缺失：%s" % venv_py
    if not base.isascii():
        return False, ("模型目录路径含非 ASCII 字符（%s）。IndexTTS 依赖的 wetext "
                       "用窄字符 API 打开词典文件，中文路径打不开；"
                       "请把模型放到纯英文路径下" % base)
    return True, ""


def _indextts_speak_all(base: str, lines: List[str], out_dir: str,
                        ref: str, lang: str) -> List[str]:
    """调独立运行时一次把整段合成完，返回逐句 wav 路径。

    为什么一次进程：模型加载约 9.5 GB，每句重加载不可接受。runner 里一次加载、
    逐句合成、逐句落盘，父进程只负责读时长与拼接。
    """
    venv_py = os.path.join(base, ".venv", "Scripts", "python.exe")
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indextts_runner.py")
    if not os.path.isfile(runner):
        raise RuntimeError("缺少 IndexTTS runner：%s" % runner)
    args_file = os.path.join(out_dir, "_lines.json")
    with open(args_file, "w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False)

    cmd = [venv_py, runner, "--base", base, "--lines", args_file,
           "--out-dir", out_dir, "--lang", lang or "ZH"]
    if ref and os.path.isfile(ref):
        cmd += ["--ref", ref]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                           encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("IndexTTS 合成超时（30 分钟）——文案过长或显存不足")

    if r.returncode != 0:
        tail = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-600:]
        raise RuntimeError("IndexTTS 合成失败（退出码 %d）：%s" % (r.returncode, tail))

    out: List[str] = []
    for i in range(len(lines)):
        p = os.path.join(out_dir, "l%03d.wav" % i)
        if not os.path.isfile(p) or os.path.getsize(p) < 1024:
            raise RuntimeError("IndexTTS 没有产出第 %d 句的有效音频（%s）" % (i, p))
        out.append(p)
    return out


class TtsSynthesizeNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        script = inputs.get("script")
        if script is None:
            raise RuntimeError("缺少文案输入：请连接一个文案节点，或在「文案」里填写内容")
        try:
            payload = store.read_json(script.id) if hasattr(script, "id") else script
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("文案读取失败: %s" % e)

        try:
            max_chars = int(params.get("max_chars") or 40)
        except (TypeError, ValueError):
            max_chars = 40
        lines = _to_lines(payload, max_chars)
        if not lines:
            raise RuntimeError("文案为空，没有可合成的文字")

        engine = str(params.get("engine") or "auto").strip().lower()
        voice = str(params.get("voice") or "").strip()
        try:
            rate = int(params.get("rate") or 0)
            volume = int(params.get("volume") or 100)
            gap = float(params.get("gap") or 0.0)
        except (TypeError, ValueError):
            rate, volume, gap = 0, 100, 0.15
        gap = max(0.0, min(2.0, gap))
        lang = str(params.get("lang") or "ZH").strip().upper() or "ZH"
        warnings: List[str] = []
        # 音色参考：端口优先（把 media_import 的 audio 直接接过来，从原片截一段
        # 就是最自然的音色），参数 ref_audio 作为覆盖项。
        ref_audio = str(params.get("ref_audio") or "").strip()
        ref_port = inputs.get("voice_ref")
        if ref_port is not None:
            _rp = str(getattr(ref_port, "path", "") or "")
            if _rp and os.path.isfile(_rp):
                ref_audio = _rp
        if ref_audio and not os.path.isfile(ref_audio):
            ref_audio = ""   # 路径失效时按「没有参考」处理，由下面按引擎判定
        # 参考音频要是纯音频文件：IndexTTS 内部用 librosa.load()，它读不了 mp4。
        # 而 voice_ref 最自然的来源是 media_import.audio——那是个 mp4 路径。
        # 所以非 wav/mp3/flac 的先抽成 wav，抽不出来就当作没有参考。
        if ref_audio and os.path.splitext(ref_audio)[1].lower() \
                not in (".wav", ".mp3", ".flac", ".m4a"):
            _ref_wav = _extract_ref_audio(ref_audio, ctx.get("workdir", "."))
            ref_audio = _ref_wav or ""
            if not ref_audio:
                warnings.append("音色参考是视频文件，抽音频失败；本次不使用音色克隆")

        idx_base = ""
        if engine in ("auto", "indextts"):
            ready, why = _indextts_ready(params)
            if ready:
                idx_base = _indextts_base(params)
            elif engine == "indextts":
                raise RuntimeError("IndexTTS 不可用：%s" % why)
            else:
                warnings.append("IndexTTS 不可用（%s），退 Edge 在线配音" % why)
                engine = "edge"

        edge_voice = ""
        if engine in ("auto", "edge"):
            # auto 链第二级：edge 依赖检查（装没装、版本对不对）。真正的连通性
            # 到合成时才知道——第一句失败时按 FW-DEV-36 分流，network 类才降 sapi。
            try:
                edge_voice = _edge_resolve_voice(voice)
            except RuntimeError as e:
                if engine == "edge":
                    raise
                warnings.append(str(e))
                engine = "sapi" if engine == "auto" else engine
            if engine == "auto" or engine == "edge":
                try:
                    import edge_tts  # noqa: F401 —— 依赖探测
                except ImportError as e:
                    if engine == "edge":
                        raise RuntimeError(
                            "Edge 配音依赖缺失：%s。请安装 edge-tts>=7.3.1" % e)
                    warnings.append("edge-tts 未安装，退系统语音合成")
                    engine = "sapi"

        ff = ffmpeg_path()
        fp = ffprobe_path()
        if not ff or not fp:
            raise RuntimeError("找不到 ffmpeg/ffprobe：请先在首启引导里下载工具链")

        files_dir = getattr(store, "files_dir", None)
        if not files_dir:
            raise RuntimeError("存储后端没有 files_dir，无法写出配音")
        os.makedirs(files_dir, exist_ok=True)

        work = os.path.join(files_dir, "_tts_%d" % int(time.time() * 1000))
        os.makedirs(work, exist_ok=True)
        try:
            raw: List[str] = []
            if engine == "indextts":
                raw = await asyncio.to_thread(_indextts_speak_all, idx_base, lines, work,
                                              ref_audio, lang)
            elif engine == "edge":
                # 逐句合成（与 SAPI/IndexTTS 同构）；首句就摊上 network 类失败时
                # 降 sapi（仅 auto；显式选 edge 则直接抛）。
                import edge_tts  # noqa: F401 —— 此处确认已可用
                sapi_fallback: Exception | None = None
                try:
                    for i, one in enumerate(lines):
                        p = os.path.join(work, "l%03d.wav" % i)
                        await _edge_one(one, p, edge_voice, rate, volume)
                        raw.append(p)
                except RuntimeError as e:
                    if engine == "edge" or _edge_failure_kind(
                            RuntimeError(str(e))) not in ("network", "other") \
                            and "网络不通" not in str(e):
                        raise
                    # auto 链第三级：edge 网络不通 → sapi。signature/dependency
                    # 类错误不属于"网络不通"，不降级——升级依赖才能解决。
                    if "网络不通" not in str(e):
                        raise
                    sapi_fallback = e
                    warnings.append("Edge 配音网络不通（%s），退系统语音合成" % str(e)[:80])
                if sapi_fallback is not None:
                    engine = "sapi"
                    for i, one in enumerate(lines):
                        p = os.path.join(work, "l%03d.wav" % i)
                        await asyncio.to_thread(_sapi_one, one, p, voice, rate, volume, work)
                        raw.append(p)
            else:
                for i, one in enumerate(lines):
                    p = os.path.join(work, "l%03d.wav" % i)
                    await asyncio.to_thread(_sapi_one, one, p, voice, rate, volume, work)
                    raw.append(p)

            norm: List[str] = []
            for i, p in enumerate(raw):
                q = os.path.join(work, "n%03d.wav" % i)
                await asyncio.to_thread(_norm, p, q, ff)
                norm.append(q)

            durs = [await asyncio.to_thread(_dur, p, fp) for p in norm]

            sil = ""
            if gap > 0 and len(norm) > 1:
                sil = os.path.join(work, "gap.wav")
                await asyncio.to_thread(_silence, sil, gap, ff)

            parts: List[str] = []
            for i, p in enumerate(norm):
                if i and sil:
                    parts.append(sil)
                parts.append(p)

            lst = os.path.join(work, "list.txt")
            with open(lst, "w", encoding="utf-8", newline="\n") as f:
                for p in parts:
                    # concat demuxer 用正斜杠最稳；单引号在列表文件里要写成 '\''
                    f.write("file '%s'\n" % p.replace("\\", "/").replace("'", "'\\''"))

            final = os.path.join(files_dir, "voice_%d.wav" % int(time.time() * 1000))
            await asyncio.to_thread(_concat, lst, final, ff)

            segs: List[Dict[str, Any]] = []
            cursor = 0.0
            for i, (one, d) in enumerate(zip(lines, durs)):
                d = round(float(d), 3)
                segs.append({"index": i, "text": one,
                             "start": round(cursor, 3),
                             "end": round(cursor + d, 3),
                             "duration": d})
                if i < len(lines) - 1:
                    cursor += d + gap
                else:
                    cursor += d

            summed = round(cursor, 3)
            actual = await asyncio.to_thread(_dur, final, fp)
            total = round(float(actual or summed), 3)
            if actual and abs(actual - summed) > 0.5:
                warnings.append("拼接后实测时长 %.2fs 与逐句累加 %.2fs 不符，以实测为准"
                                % (actual, summed))

            meta = {
                "engine": engine,
                "voice": (edge_voice if engine == "edge"
                          else voice or "(系统默认)"),
                "rate": rate,
                "volume": volume,
                "gap": gap,
                "line_count": len(lines),
                "duration": total,
                "summed_duration": summed,
                "generated_at": round(time.time(), 3),
                "warnings": list(warnings),
                "degraded": bool(warnings),
            }
            audio = store.save_asset(Asset(id="", kind=PortType.AUDIO.value,
                                           path=final, meta=dict(meta, source=final)))
            seg_meta = {
                "count": len(segs),
                "total_duration": total,
                "engine": engine,
                "warnings": list(warnings),
                "degraded": bool(warnings),
            }
            segments = store.save_asset(Asset(id="", kind=PortType.SEGMENTS.value,
                                              meta=seg_meta), payload=segs)
            return {"audio": audio, "segments": segments}
        finally:
            # 中间文件不进资产库；final 在 files_dir，不在 work 里，删 work 不影响它
            shutil.rmtree(work, ignore_errors=True)
