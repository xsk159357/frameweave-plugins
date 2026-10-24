# -*- coding: utf-8 -*-
"""B3 特征管线模型下载指引脚本（S2-T0，插件资源草稿）。

用途：下载 DINOv2-Small / DINOv2-Base 权重到 DATA_DIR/models/ 约定目录
（或 --target 指定目录）；幂等（已存在且非空即跳过）；日志输出放置与接线指引。

依据：
- docs/reports/项目方向审计/07_实施任务拆解_阶段二.md S2-T0（验证：下载指引脚本可执行）
- FrameWeave/backend/models_registry.json（dinov2-small / dinov2-base 登记项）
- 平台模型目录约定：DATA_DIR/models/<dir_name>（deps.models_dir()）

用法：
  # 使用 backend venv 的 python 运行（无第三方依赖，纯 urllib）：
  python -m pip 无依赖；直接：
  python scripts/fetch_b3_models.py                      # DINOv2-Small + Base 到 DATA_DIR/models
  python scripts/fetch_b3_models.py --model dinov2-small # 仅 Small
  python scripts/fetch_b3_models.py --target D:/models   # 指定目标目录
  python scripts/fetch_b3_models.py --modelscope         # 主源切 ModelScope（默认 HF）

降级路径（模型缺失不阻断 B3）：fast 档（灰度投票+全局DP）零模型可跑；
balanced/precise 缺模型时回退 chinese-clip（本机 HF 缓存已就绪）→ 灰度，并记录 report.degraded。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Optional

# 模型 id → (repo, file, 目录名) —— 与 backend/models_registry.json 保持一致
_REGISTRY: Dict[str, Dict[str, str]] = {
    "dinov2-small": {
        "repo": "facebook/dinov2-small", "file": "pytorch_model.bin",
        "dir_name": "dinov2-small", "size_mb": "≈89MB",
    },
    "dinov2-base": {
        "repo": "facebook/dinov2-base", "file": "pytorch_model.bin",
        "dir_name": "dinov2-base", "size_mb": "≈344MB",
    },
}


def default_models_dir() -> str:
    """与 deps.models_dir() 一致：frozen → %APPDATA%/FrameWeave/models；源码 → backend/data/models。"""
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "FrameWeave", "models")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # frameweave-plugins/
    backend = os.path.join(here, "..", "FrameWeave", "backend")
    return os.path.join(backend, "data", "models")


def _ms_url(repo: str, file: str) -> str:
    return f"https://modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={file}"


def _hf_url(repo: str, file: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{file}"


def _download(url: str, dest: str) -> None:
    """断点续传下载（复用 model_download 语义）。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    headers = {}
    if os.path.exists(dest):
        headers["Range"] = f"bytes={os.path.getsize(dest)}-"
    req = urllib.request.Request(url, headers=headers)
    mode = "ab" if "Range" in headers else "wb"
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, mode) as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def fetch(model: str, target: str, prefer: str) -> None:
    info = _REGISTRY[model]
    mdir = os.path.join(target, info["dir_name"])
    dest = os.path.join(mdir, info["file"])
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        print(f"[skip] {model} 已存在: {dest}（{os.path.getsize(dest)} bytes）")
        return
    sources: List[tuple] = []
    if prefer == "modelscope":
        sources.append(("ModelScope", _ms_url(info["repo"], info["file"])))
        sources.append(("HuggingFace", _hf_url(info["repo"], info["file"])))
    else:
        sources.append(("HuggingFace", _hf_url(info["repo"], info["file"])))
        sources.append(("ModelScope", _ms_url(info["repo"], info["file"])))
    last_err = ""
    for name, url in sources:
        try:
            print(f"[download] {model} via {name}: {url}")
            _download(url, dest)
            print(f"[ok] {model} -> {dest}（{os.path.getsize(dest)} bytes）")
            return
        except Exception as e:  # noqa: BLE001
            last_err = f"{name}: {e}"
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                # 部分下载保留给断点续传
                print(f"[warn] {model} {last_err}（保留部分文件，可重试续传）")
                return
    raise SystemExit(f"[fail] {model} 下载失败: {last_err}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="B3 特征管线模型下载脚本（DINOv2-Small/Base）")
    ap.add_argument("--model", choices=sorted(_REGISTRY), default="dinov2-small",
                    help="要下载的模型（默认 dinov2-small）")
    ap.add_argument("--target", default="", help="目标目录（默认 DATA_DIR/models）")
    ap.add_argument("--modelscope", action="store_true", help="主源用 ModelScope")
    args = ap.parse_args(argv)
    target = args.target.strip() or default_models_dir()
    print("=" * 72)
    print(f"B3 模型下载：{args.model}（{_REGISTRY[args.model]['size_mb']}）")
    print(f"目标目录：{target}")
    print("=" * 72)
    fetch(args.model, target, "modelscope" if args.modelscope else "huggingface")
    print(f"\n放置：{os.path.join(target, _REGISTRY[args.model]['dir_name'], _REGISTRY[args.model]['file'])}")
    print("接线：$env:FRAMEWEAVE_DINO_DIR = 上述模型目录（或 B3 节点 model_dir 参数直指）")
    print("降级：语义回退链 dinov2_base → dinov2_small → chinese-clip → 灰度投票；fast 档零模型可跑")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())