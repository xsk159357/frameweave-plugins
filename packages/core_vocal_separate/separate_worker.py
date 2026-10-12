"""人声分离 worker：Demucs 推理（由节点以 tracked 子进程启动）。

用法：python separate_worker.py '<json spec>'
spec: {"input","out_dir","model","stems","device","format"}
输出：out_dir/vocals.<fmt> 与 out_dir/accompaniment.<fmt>（stems=2）
     或 out_dir/<源名>.<fmt>（stems=4，模型原生分轨）。
以非零退出码 + stderr 报告错误。
"""
from __future__ import annotations
import json
import os
import sys
import traceback


def _main() -> int:
    if len(sys.argv) < 2:
        print("usage: separate_worker.py '<spec json>'", file=sys.stderr)
        return 2
    spec = json.loads(sys.argv[1])
    inp = spec["input"]
    out_dir = spec["out_dir"]
    model_name = str(spec.get("model", "htdemucs"))
    stems = int(spec.get("stems", 2))
    device = str(spec.get("device", "cpu"))
    fmt = str(spec.get("format", "wav")).lstrip(".")

    import torch  # noqa: F401  (demucs 依赖，提前失败暴露)
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    model = get_model(model_name)
    model.eval()
    model.to(device)

    import torchaudio
    wav, sr = torchaudio.load(inp)
    if wav.dim() == 1:
        wav = wav.unsqueeze(0)
    mix = wav.mean(0)
    if device.startswith("cuda"):
        wav = wav.to(device)
    ref = mix.to(device) if device.startswith("cuda") else mix

    with torch.no_grad():
        sources = apply_model(model, wav[None], device=device, shifts=1,
                              split=True, overlap=0.25, progress=True)[0]
    # sources: (num_sources, channels, samples)
    if stems >= 4:
        for i, name in enumerate(model.sources):
            src = sources[i]
            if device.startswith("cuda"):
                src = src.cpu()
            torchaudio.save(os.path.join(out_dir, f"{name}.{fmt}"), src, sr)
    else:
        vocals = sources[model.sources.index("vocals")]
        others = [sources[i] for i, n in enumerate(model.sources) if n != "vocals"]
        if device.startswith("cuda"):
            vocals = vocals.cpu()
            others = [o.cpu() for o in others]
        accomp = sum(others[1:], others[0]) if others else vocals.clone()
        torchaudio.save(os.path.join(out_dir, f"vocals.{fmt}"), vocals, sr)
        torchaudio.save(os.path.join(out_dir, f"accompaniment.{fmt}"), accomp, sr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)