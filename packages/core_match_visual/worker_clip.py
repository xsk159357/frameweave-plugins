# -*- coding: utf-8 -*-
"""Chinese-CLIP 侧车 worker（外置解释器进程）。

平台 `app.sidecar` 起这个脚本，按 JSON-lines 收发。**torch/transformers 只在
这台外置解释器里 import**——冻结版后端里没有它们。

请求：
  {"id":1,"action":"ping","payload":{"model_dir":"..."}}
  {"id":1,"action":"text_features","payload":{"model_dir":"...","texts":["一只猫"]}}
  {"id":1,"action":"image_features","payload":{"model_dir":"...","paths":["C:/a.jpg"]}}
响应：
  {"id":1,"ok":true,"result":{"embeddings":[[...]],"dim":512}}
"""
import json
import os
import sys

os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# worker 的 env 是白名单（不含 USERNAME），huggingface_hub 在 Windows 上拿它
# 拼缓存路径会直接报 "No username set in the environment"。显式给 HF_HOME，
# 不让库去猜用户目录。离线模式下这个目录只用于放锁文件，不会真下载。
if not os.environ.get("HF_HOME"):
    _hf = (os.environ.get("TEMP") or os.environ.get("TMP")
           or os.path.join(os.path.expanduser("~"), ".cache"))
    os.environ["HF_HOME"] = os.path.join(_hf, "fw_hf_cache")

_MODEL = None
_PROC = None
_DEVICE = "cpu"
_DIR = ""


def _arch(model_dir):
    p = os.path.join(model_dir, "config.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return str((json.load(f).get("architectures") or [""])[0])
    except Exception:
        return ""


def _load(model_dir):
    """按 config.architectures 选 ChineseCLIPModel / CLIPModel（旧实现用通用类会大量不匹配）。"""
    global _MODEL, _PROC, _DEVICE, _DIR
    if _MODEL is not None and _DIR == model_dir:
        return
    import torch
    if "ChineseCLIP" in _arch(model_dir) or _arch(model_dir).startswith("chinese_clip"):
        from transformers import ChineseCLIPModel as M, ChineseCLIPProcessor as P
    else:
        from transformers import CLIPModel as M, CLIPProcessor as P
    _MODEL = M.from_pretrained(model_dir)
    _PROC = P.from_pretrained(model_dir)
    _DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    _MODEL = _MODEL.to(_DEVICE).eval()
    _DIR = model_dir


def _flat(emb):
    if hasattr(emb, "pooler_output"):
        emb = emb.pooler_output
    return [v.tolist() for v in emb]


def _need(payload):
    d = str(payload.get("model_dir") or os.environ.get("FRAMEWEAVE_CLIP_DIR") or "").strip()
    if not d or not os.path.isdir(d):
        raise RuntimeError("未指定本地 CLIP 模型目录（model_dir / FRAMEWEAVE_CLIP_DIR）")
    return d


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
        payload = req.get("payload") or {}
        try:
            if action == "ping":
                d = str(payload.get("model_dir") or os.environ.get("FRAMEWEAVE_CLIP_DIR") or "")
                if d and os.path.isdir(d):
                    _load(d)
                import torch
                print(json.dumps({"id": rid, "ok": True, "result": {
                    "python": "%d.%d.%d" % sys.version_info[:3],
                    "torch": torch.__version__,
                    "cuda": torch.cuda.is_available(),
                    "device": _DEVICE,
                    "model_loaded": _MODEL is not None,
                    "model_dir": _DIR,
                }}, ensure_ascii=False), flush=True)
                continue

            if action in ("text_features", "image_features"):
                d = _need(payload)
                _load(d)
                import torch
                if action == "text_features":
                    texts = [str(t) for t in (payload.get("texts") or [])]
                    if not texts:
                        raise RuntimeError("texts 为空")
                    inputs = _PROC(text=texts, return_tensors="pt", padding=True, truncation=True)
                    with torch.no_grad():
                        emb = _MODEL.get_text_features(**{k: v.to(_DEVICE) for k, v in inputs.items()})
                else:
                    from PIL import Image
                    paths = [str(t) for t in (payload.get("paths") or [])]
                    if not paths:
                        raise RuntimeError("paths 为空")
                    imgs = [Image.open(p).convert("RGB") for p in paths]
                    inputs = _PROC(images=imgs, return_tensors="pt")
                    with torch.no_grad():
                        emb = _MODEL.get_image_features(**{k: v.to(_DEVICE) for k, v in inputs.items()})
                vecs = _flat(emb)
                print(json.dumps({"id": rid, "ok": True, "result": {
                    "embeddings": vecs, "dim": len(vecs[0]) if vecs else 0, "device": _DEVICE,
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
