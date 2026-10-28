# -*- coding: utf-8 -*-
"""DINOv2 / Chinese-CLIP 图像特征侧车 worker（外置解释器进程）。

平台 `app.sidecar` 起它，JSON-lines 收发；torch/transformers 只活在这台解释器里。

请求：
  {"id":1,"action":"ping","payload":{"model_dir":"...","kind":"dino|clip"}}
  {"id":1,"action":"frame_features","payload":{"model_dir":"...","kind":"dino","paths":[...]}}
响应：
  {"id":1,"ok":true,"result":{"vectors":[[...]],"dim":768,"device":"cuda"}}
"""
import json
import os
import sys

os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

_STATE = {"model": None, "proc": None, "dir": "", "kind": "", "device": "cpu"}


def _dino_tensor(path):
    """无 torchvision 的 DINOv2 预处理：短边 256 → 中心裁 224 → ImageNet 归一化。"""
    import numpy as np
    import torch
    from PIL import Image

    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = 256.0 / max(1, min(w, h))
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    img = img.resize((nw, nh), Image.BICUBIC)
    left, top = max(0, (nw - 224) // 2), max(0, (nh - 224) // 2)
    img = img.crop((left, top, left + 224, top + 224))
    arr = np.asarray(img, dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    return torch.from_numpy(arr.transpose(2, 0, 1).copy()).float()


def _load(model_dir, kind):
    import torch

    if _STATE["model"] is not None and _STATE["dir"] == model_dir and _STATE["kind"] == kind:
        return
    if kind == "clip":
        from transformers import ChineseCLIPModel as M, ChineseCLIPProcessor as P
        _STATE["proc"] = P.from_pretrained(model_dir)
        model = M.from_pretrained(model_dir)
    else:
        from transformers import AutoModel
        _STATE["proc"] = None
        model = AutoModel.from_pretrained(model_dir)
    _STATE["device"] = "cuda" if torch.cuda.is_available() else "cpu"
    _STATE["model"] = model.to(_STATE["device"]).eval()
    _STATE["dir"], _STATE["kind"] = model_dir, kind


def _l2(rows):
    out = []
    for v in rows:
        n = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / n for x in v])
    return out


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        action = str(req.get("action") or "")
        p = req.get("payload") or {}
        try:
            model_dir = str(p.get("model_dir") or "").strip()
            kind = str(p.get("kind") or "dino").strip()
            if action == "ping":
                if model_dir and os.path.isdir(model_dir):
                    _load(model_dir, kind)
                import torch
                print(json.dumps({"id": rid, "ok": True, "result": {
                    "python": "%d.%d.%d" % sys.version_info[:3], "torch": torch.__version__,
                    "cuda": torch.cuda.is_available(), "device": _STATE["device"],
                    "kind": _STATE["kind"], "model_loaded": _STATE["model"] is not None,
                }}, ensure_ascii=False), flush=True)
                continue

            if action == "frame_features":
                if not model_dir or not os.path.isdir(model_dir):
                    raise RuntimeError("模型目录不存在: %r" % model_dir)
                _load(model_dir, kind)
                import numpy as np
                import torch

                paths = [str(x) for x in (p.get("paths") or [])]
                if not paths:
                    raise RuntimeError("paths 为空")
                rows = []
                batch = 8
                for i in range(0, len(paths), batch):
                    chunk = paths[i:i + batch]
                    if kind == "clip":
                        inp = _STATE["proc"](images=chunk, return_tensors="pt")
                        inp = {k: v.to(_STATE["device"]) for k, v in inp.items()}
                        with torch.no_grad():
                            emb = _STATE["model"].get_image_features(**inp)
                        if hasattr(emb, "pooler_output"):
                            emb = emb.pooler_output
                        rows.extend(emb.detach().cpu().numpy().tolist())
                    else:
                        tensors = [_dino_tensor(x) for x in chunk]
                        inp = {"pixel_values": torch.stack(tensors).to(_STATE["device"])}
                        with torch.no_grad():
                            out = _STATE["model"](**inp)
                        rows.extend(out.last_hidden_state[:, 0, :].detach().cpu().numpy().tolist())
                vecs = _l2(np.asarray(rows, dtype="float32").tolist())
                print(json.dumps({"id": rid, "ok": True, "result": {
                    "vectors": vecs, "dim": len(vecs[0]) if vecs else 0, "device": _STATE["device"],
                }}, ensure_ascii=False), flush=True)
                continue

            raise RuntimeError("未知 action: %s" % action)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(json.dumps({"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e),
                              "traceback": traceback.format_exc()[-2000:]},
                             ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
