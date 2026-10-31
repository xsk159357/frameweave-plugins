# -*- coding: utf-8 -*-
"""TransNetV2 侧车 worker（外置解释器；冻结包里没有 torch）。

平台 `app.sidecar` 起它，JSON-lines 收发；torch 只活在这台解释器里。

请求：
  {"id":1,"action":"ping","payload":{"model_dir":"..."}}
  {"id":2,"action":"predict","payload":{"model_dir":"...","npy":"C:/.../frames.npy"}}
响应：
  {"id":2,"ok":true,"result":{"probs":[...],"device":"cpu"}}

为什么要 npy：27x48x3 的抽帧数组动辄几十万帧，走 JSON 列表会膨胀十倍且解析慢，
所以由节点侧把 np.ndarray 写成 .npy，worker 读文件。
"""
import json
import os
import sys

os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

_STATE = {"model": None, "dir": "", "device": "cpu"}


def _load(model_dir):
    import importlib.util
    import torch

    d = model_dir
    if d and os.path.isfile(d):
        d = os.path.dirname(d)
    if not d or not os.path.isdir(d):
        raise RuntimeError(f"模型目录不存在: {d}")
    if _STATE["model"] is not None and _STATE["dir"] == d:
        return
    module_file = os.path.join(d, "transnetv2_pytorch.py")
    if not os.path.isfile(module_file):
        raise RuntimeError(f"缺少 transnetv2_pytorch.py: {d}")
    weights = ""
    for name in ("transnetv2-pytorch-weights.pth", "transnetv2_weights.pth", "weights.pth"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            weights = p
            break
    if not weights:
        cands = sorted(f for f in os.listdir(d) if f.lower().endswith(".pth"))
        if not cands:
            raise RuntimeError(f"未找到 *.pth 权重: {d}")
        weights = os.path.join(d, cands[0])

    spec = importlib.util.spec_from_file_location("fw_transnetv2_pytorch", module_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fw_transnetv2_pytorch"] = mod
    spec.loader.exec_module(mod)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = mod.TransNetV2()
    try:
        sd = torch.load(weights, map_location=device, weights_only=True)
    except TypeError:
        sd = torch.load(weights, map_location=device)
    if isinstance(sd, dict) and isinstance(sd.get("state_dict"), dict):
        sd = sd["state_dict"]
    model.load_state_dict(sd)
    _STATE["model"] = model.to(device).eval()
    _STATE["dir"] = d
    _STATE["device"] = device


def _predict(frames):
    import numpy as np
    import torch

    model = _STATE["model"]
    dev = next(model.parameters()).device
    n = int(frames.shape[0])
    if n == 0:
        return np.zeros((0,), dtype=np.float32)
    win, stride = 100, 50
    starts, windows = [], []
    start = 0
    while start < n:
        w = frames[start:start + win]
        if w.shape[0] < win:
            w = np.concatenate([w, np.repeat(w[-1:], win - w.shape[0], axis=0)], axis=0)
        windows.append(w)
        starts.append(start)
        start += stride
    preds = []
    with torch.no_grad():
        for w in windows:
            t = torch.from_numpy(np.ascontiguousarray(w)).unsqueeze(0).to(dev)
            out = model(t)
            one_hot = out[0] if isinstance(out, tuple) else out
            preds.append(torch.sigmoid(one_hot).squeeze(0).squeeze(-1).cpu().numpy())
    acc = np.zeros(n, dtype=np.float64)
    cnt = np.zeros(n, dtype=np.float64)
    for s, pred in zip(starts, preds):
        for j in range(len(pred)):
            idx = s + j
            if idx < n:
                acc[idx] += pred[j]
                cnt[idx] += 1
    cnt[cnt == 0] = 1.0
    return (acc / cnt).astype(np.float32)


def handle(action, payload):
    if action == "ping":
        _load(payload.get("model_dir") or "")
        import torch
        return {"ready": True, "device": _STATE["device"], "torch": torch.__version__,
                "backend": "pytorch"}
    if action == "predict":
        import numpy as np
        _load(payload.get("model_dir") or "")
        npy = payload.get("npy") or ""
        frames = np.load(npy, allow_pickle=False)
        probs = _predict(frames)
        return {"probs": [float(x) for x in probs], "device": _STATE["device"]}
    raise RuntimeError(f"未知 action: {action}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        rid = req.get("id")
        try:
            res = handle(req.get("action") or "", req.get("payload") or {})
            out = {"id": rid, "ok": True, "result": res}
        except Exception as e:  # noqa: BLE001
            out = {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
