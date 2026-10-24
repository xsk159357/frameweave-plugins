"""配音对齐 Whisper worker：词级转写（由节点以 tracked 子进程启动，S3T15 加固）。

用法：python align_worker.py '<json spec>'
spec: {"audio_path","out_path","model_dir","model_name","language",
       "device"(可选,"auto") ,"compute_type"(可选,"int8")}
行为：
- 在子进程内加载 faster-whisper 并转写（CUDA/onnxruntime 原生崩溃只杀本进程，不拖垮主进程）；
- 结果词列表写 out_path JSON：[{"word","start","end"}, ...]；
- 失败以非零退出码 + stderr 报告（节点据此走 fallback 分切，主进程不受影响）。
"""
from __future__ import annotations
import json
import os
import sys
import traceback


def _main() -> int:
    if len(sys.argv) < 2:
        print("usage: align_worker.py '<spec json>'", file=sys.stderr)
        return 2
    spec = json.loads(sys.argv[1])
    audio_path = spec["audio_path"]
    out_path = spec["out_path"]
    model_dir = str(spec.get("model_dir") or "")
    model_name = str(spec.get("model_name") or "large-v3")
    language = str(spec.get("language") or "")
    device = str(spec.get("device") or "auto")
    compute_type = str(spec.get("compute_type") or "int8")

    from faster_whisper import WhisperModel  # noqa: F401  提前失败暴露依赖缺失

    model_path = model_dir or model_name
    handle = WhisperModel(model_path, device=device, compute_type=compute_type)
    lang = None if language in ("auto", "", "None", "none") else language
    segments, _info = handle.transcribe(audio_path, language=lang, vad_filter=True,
                                        word_timestamps=True)
    words: list = []
    for seg in segments:
        for w in (seg.words or []):
            words.append({"word": w.word.strip(), "start": float(w.start),
                          "end": float(w.end)})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False)
    if not words:
        print("Whisper 未产出词级时间戳（无匹配语音？）", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)