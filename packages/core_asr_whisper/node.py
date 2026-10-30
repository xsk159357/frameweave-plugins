"""语音转写节点：用本地 faster-whisper 把音轨转成带时间的字幕。

模型不随包：首次用某个档位前要先下载。这里区分两种「没有结果」，不能混为一谈：
  · **模型缺失** —— 配置问题，直接报错并说清缺哪个、去哪下；
  · **跑完了但没识别到文字** —— 数据问题（全静音、纯音乐），返回空字幕但
    degraded=True 并在 warnings 里写明。

为什么不能对前者也返回空字幕：空字幕会让下游以为「转写成功但这段没人说话」，
用户拿到一堆空条目还在四处找原因。配置问题必须以异常表达。

也不给「无模型时降级成固定间隔字幕」：转写的产物就是文字，没有文字就没有产物。
降级只能降精度（换更小的模型），不能降到「没有文字」。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Tuple

import app as _app
from app.assets import Asset
from app.nodespec import NodeBase, PortType

_TIERS = ("base", "small", "medium")


def _models_root() -> str:
    """backend/data/models —— 用 app 包位置推，不依赖 node.py 被放在哪。"""
    backend = os.path.dirname(os.path.dirname(os.path.abspath(_app.__file__)))
    return os.path.join(backend, "data", "models")


def _find_model(tier: str) -> Tuple[str, str]:
    """返回 (模型目录 或 "", 说明)。只认 model.bin 存在的目录。"""
    root = _models_root()
    cand = os.path.join(root, "faster-whisper-%s" % tier)
    if os.path.isfile(os.path.join(cand, "model.bin")):
        return cand, ""
    exists = os.path.isdir(cand)
    why = ("目录存在但缺 model.bin（下载未完成）" if exists else "目录不存在")
    return "", "%s（期望位置 %s）" % (why, cand)


def _transcribe(path: str, model_dir: str, language: str, vad: bool,
                beam: int, prompt: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from faster_whisper import WhisperModel  # 延迟导入：没装也不该让平台起不来

    model = WhisperModel(model_dir, device="cpu", compute_type="int8")
    segs, info = model.transcribe(
        path,
        language=(language or None),
        vad_filter=bool(vad),
        beam_size=int(beam),
        initial_prompt=(prompt or None),
    )
    out: List[Dict[str, Any]] = []
    for seg in segs:
        text = str(getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        start = round(float(getattr(seg, "start", 0.0) or 0.0), 3)
        end = round(float(getattr(seg, "end", 0.0) or 0.0), 3)
        if end <= start:
            end = round(start + 0.2, 3)
        out.append({"index": len(out), "start": start, "end": end,
                    "duration": round(end - start, 3),
                    "text": text, "lines": [text]})
    meta = {
        "language": str(getattr(info, "language", "") or ""),
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
    }
    return out, meta


class AsrWhisperNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        audio = inputs.get("audio")
        path = str(getattr(audio, "path", "") or "") if audio is not None else ""
        if not path:
            raise RuntimeError("缺少音频输入：audio 端口未连接，或资产没有文件路径")
        if not os.path.isfile(path):
            raise RuntimeError("音频文件不存在: %s" % path)

        tier = str(params.get("model") or "base").strip().lower()
        if tier not in _TIERS:
            tier = "base"
        model_dir, why = _find_model(tier)
        if not model_dir:
            raise RuntimeError(
                "找不到本地模型 faster-whisper-%s：%s。请先下载该模型再重试"
                "（模型不随软件包分发）" % (tier, why))

        language = str(params.get("language") or "").strip()
        vad = bool(params.get("vad_filter", True))
        try:
            beam = int(params.get("beam_size", 5) or 5)
        except (TypeError, ValueError):
            beam = 5
        beam = min(max(beam, 1), 10)
        prompt = str(params.get("initial_prompt") or "")
        # 中文音频实测：base 模型默认输出繁体（「這是一段測試語音」）。
        # 用一句提示把它拉回简体，实测有效且不需要额外依赖：
        #   prompt=None                  -> 這是一段測試語音…
        #   prompt='以下是简体中文普通话。' -> 这是一段测试语音…
        if bool(params.get("simplified", True)) and (not language or language.startswith("zh")):
            prompt = "以下是简体中文普通话。" + prompt

        warnings: List[str] = []
        try:
            rows, info = await asyncio.to_thread(
                _transcribe, path, model_dir, language, vad, beam, prompt)
        except ImportError as e:
            raise RuntimeError(
                "本机没有安装 faster-whisper 依赖（%s）。装好后再重试" % e)
        except Exception as e:  # noqa: BLE001 —— 模型损坏/内存不足都会走到这
            raise RuntimeError("转写失败（模型 %s）：%s: %s"
                               % (tier, type(e).__name__, e))

        if not rows:
            warnings.append(
                "整段没有识别到文字（可能全是静音或纯音乐）；输出为空字幕")
        if language and info.get("language") and info["language"] != language:
            warnings.append("指定语言 %s，模型判定 %s" % (language, info["language"]))

        meta = {
            "count": len(rows),
            "model": "faster-whisper-%s" % tier,
            "language": info.get("language", ""),
            "requested_language": language,
            "audio_duration": round(float(info.get("duration") or 0), 3),
            "total_speech": round(sum(r["duration"] for r in rows), 3),
            "vad_filter": vad,
            "beam_size": beam,
            "warnings": warnings,
            "degraded": bool(warnings),
        }
        out = ctx["store"].save_asset(
            Asset(id="", kind=PortType.SUBTITLE.value, meta=meta), payload=rows)
        return {"subtitle": out}
