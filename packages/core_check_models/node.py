"""核心插件：模型就绪检查（A10 轻节点）。

轻量只读节点：输入模型清单 → report:JSON 就绪状态（路径解析 + 存在性 + 可选 hash）。
- 路径解析与就绪判定复用平台统一依赖解析层 `app.plugin.deps.resolve_model`
  （params/env 覆盖 → FRAMEWEAVE_MODELS_DIR → DATA_DIR/models/<dir> → models_registry
  登记 → HF 缓存），与 `fw env-check` 模型检查项（app.plugin.envcheck._MODEL_CHECKS）
  同源一致，可直接对拍：6 模型默认清单与判定口径完全相同。
- 逐模型输出：ready / path / source / weight_file / size_bytes / 可选 sha256 / 下载指引。
- 缺失项给与 `fw env-check` 修复命令一致的下载指引（hf download <repo> --local-dir，
  05 复现指南 §1.2 C 节）；下载职责仍归宿主模型中心，本节点只读、不下载、不安装。

缓存语义说明：平台按 type+version+params+上游资产 计算结果缓存键，重复运行相同参数
会命中缓存（返回上次结果）。下载模型后如需重新检查，请改动任意参数（如 refresh 填当前
时间）或调用节点重置接口强制重算：
    POST /api/workflows/<wfid>/nodes/<nid>/reset   body={"force": true}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

# 默认检查清单（与 app.plugin.envcheck._MODEL_CHECKS 完全同序同名 → 可对拍）
DEFAULT_MODELS: List[str] = [
    "faster-whisper-small",
    "faster-whisper-large-v3",
    "transnetv2",
    "chinese-clip",
    "index-tts",
    "demucs",
]
MODEL_LABELS: Dict[str, str] = {
    "faster-whisper-small": "Whisper 小型转写模型",
    "faster-whisper-large-v3": "Whisper 大模型转写",
    "transnetv2": "TransNetV2 镜头切分模型",
    "chinese-clip": "Chinese-CLIP 图文匹配模型",
    "index-tts": "IndexTTS2 本地克隆配音",
    "demucs": "Demucs 人声分离模型",
}
# 默认本地目录名（DATA_DIR/models/<dir>，与 deps._MODEL_DIR_NAMES 同步维护）
DIR_NAMES: Dict[str, str] = {
    "faster-whisper-small": "faster-whisper-small",
    "faster-whisper-large-v3": "faster-whisper-large-v3",
    "transnetv2": "transnetv2",
    "chinese-clip": "chinese-clip-vit-base-patch16",
    "index-tts": "index-tts-code",
    "demucs": "demucs",
}
# 环境变量覆盖（与 deps._MODEL_ENV 同步维护）
ENV_KEYS: Dict[str, str] = {
    "faster-whisper-small": "FRAMEWEAVE_WHISPER_DIR",
    "faster-whisper-large-v3": "FRAMEWEAVE_WHISPER_DIR",
    "transnetv2": "FRAMEWEAVE_TRANSNET_DIR",
    "chinese-clip": "FRAMEWEAVE_CLIP_DIR",
    "index-tts": "INDEX_TTS_ROOT",
    "demucs": "FRAMEWEAVE_DEMUCS_DIR",
}
# 权重文件扩展名（hash/权重文件探测目标）
WEIGHT_EXTS: tuple = (".bin", ".pth", ".pt", ".safetensors", ".ckpt", ".th")


def _load_registry(registry_path: str = "") -> Dict[str, Dict[str, Any]]:
    """models_registry.json 清单（覆盖路径优先，否则平台默认注册表）。

    - 覆盖路径显式给出时必须可读、可解析（BOM 容忍 utf-8-sig）：失败直接抛错，
      不静默回退默认注册表（否则自定义清单会被悄悄替换，产出误导性报告）。
    - 覆盖路径留空 → 平台默认注册表（app.plugin.deps.load_models_registry）。
    """
    from app.plugin import deps
    if registry_path:
        if not os.path.isfile(registry_path):
            raise RuntimeError(f"invalid: registry_path 文件不存在: {registry_path}")
        try:
            with open(registry_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001 —— 给用户可读错误，不静默回退
            raise RuntimeError(f"invalid: registry_path 无法解析: {registry_path}（{e}）") from e
        raw = data.get("models") if isinstance(data, dict) and isinstance(data.get("models"), dict) else data
        if isinstance(raw, dict):
            return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        raise RuntimeError(f"invalid: registry_path 内容不是模型清单对象: {registry_path}")
    return deps.load_models_registry()


def _parse_list(raw: str) -> List[str]:
    """models 参数解析：JSON 数组 / 换行/逗号/分号分隔 / 'all'；空 → 默认 6 模型。

    BOM 容忍：Windows 记事本/PowerShell 写入的文本可能带 \ufeff 前缀。
    """
    txt = (raw or "").strip().lstrip("\ufeff").strip()
    if not txt or txt.lower() == "all":
        return list(DEFAULT_MODELS)
    if txt.startswith("["):
        try:
            arr = json.loads(txt)
            if isinstance(arr, list):
                out = []
                for x in arr:
                    if isinstance(x, str) and x.strip():
                        out.append(x.strip())
                    elif isinstance(x, dict) and x.get("id"):
                        out.append(str(x["id"]).strip())
                if out:
                    return out
        except json.JSONDecodeError:
            pass
    parts = [p.strip() for p in re.split(r"[,;\n\r]+", txt) if p.strip()]
    return parts or list(DEFAULT_MODELS)


def _sha256_of(path: str, progress: Any = None, base: float = 0.0, span: float = 1.0) -> str:
    """流式 sha256（大模型文件不落内存）；progress 为 0~1 回调（可选）。"""
    h = hashlib.sha256()
    size = 0
    try:
        size = os.path.getsize(path)
    except OSError:
        pass
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
            if progress and size:
                try:
                    progress(min(base + span * (total / size), base + span))
                except Exception:  # noqa: BLE001
                    pass
    return h.hexdigest()


def _find_weight(dirpath: str, registry_file: str = "", max_depth: int = 2) -> Optional[str]:
    """在模型目录内定位权重文件：registry.file 精确名优先，否则浅层扫描权重扩展名。"""
    if registry_file and os.path.isfile(os.path.join(dirpath, registry_file)):
        return os.path.join(dirpath, registry_file)
    if not os.path.isdir(dirpath):
        return None
    for root, dirs, files in os.walk(dirpath):
        depth = root[len(dirpath):].count(os.sep)
        if depth >= max_depth:
            dirs[:] = []
            continue
        for fn in sorted(files):
            if fn.lower().endswith(WEIGHT_EXTS):
                return os.path.join(root, fn)
    return None


def _registry_public(info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """注册表条目裁剪为报告可见字段。"""
    info = info or {}
    return {
        "repo": str(info.get("repo") or ""),
        "file": str(info.get("file") or ""),
        "size_mb": info.get("size_mb"),
        "fallback_repo": str(info.get("fallback_repo") or ""),
        "note": str(info.get("note") or "")[:200],
    }


def _download_guidance(mid: str, info: Optional[Dict[str, Any]], mdir: str) -> List[str]:
    """缺失项下载指引：与 fw env-check _repair_model 同口径（05 复现指南 §1.2）。"""
    info = info or {}
    env_key = ENV_KEYS.get(mid, "")
    repo = str(info.get("repo") or "")
    d: List[str] = []
    if repo:
        d.append(f"下载：hf download {repo} --local-dir \"{mdir}\"")
        fb = str(info.get("fallback_repo") or "")
        if fb and fb != repo:
            d.append(f"下载(备选)：hf download {fb} --local-dir \"{mdir}\"")
    else:
        d.append(f"下载：按 dep-matrix §依赖表获取 {mid} 权重文件（models_registry 未登记 repo）")
    d.append(f"放置：{mdir}（DATA_DIR/models 约定）")
    d.append(f"接线：{env_key} 指向该目录" if env_key else "接线：在节点参数 model_dir 中指定该目录")
    size = info.get("size_mb")
    if size:
        d.append(f"体积：约 {int(size)} MB")
    return d


def _check_one(mid: str, label: str, info: Optional[Dict[str, Any]],
               with_hash: bool, progress: Any = None,
               base: float = 0.0, span: float = 1.0) -> Dict[str, Any]:
    """单模型就绪检查：路径解析（deps.resolve_model）+ 存在性 + 可选 sha256。"""
    from app.plugin import deps
    r = deps.resolve_model(mid, env=os.environ)
    rec: Dict[str, Any] = {
        "id": mid,
        "name": label,
        "ready": bool(r.get("ready")),
        "source": r.get("source", ""),
        "path": r.get("path", ""),
        "warnings": list(r.get("warnings") or []),
    }
    if r.get("ready"):
        rec["status"] = "ready"
        wf = _find_weight(rec["path"], (info or {}).get("file", ""))
        if wf:
            rec["weight_file"] = os.path.basename(wf)
            try:
                rec["size_bytes"] = os.path.getsize(wf)
            except OSError:
                rec["size_bytes"] = None
            if with_hash:
                rec["sha256"] = _sha256_of(wf, progress, base, span)
        else:
            rec["weight_file"] = None
            rec["warnings"] = rec["warnings"] + ["目录就绪但未找到权重文件（registry.file 未命中）"]
        rec["download"] = []
    else:
        rec["status"] = "missing"
        mdir = os.path.join(deps.models_dir(), DIR_NAMES.get(mid, mid))
        rec["expected_dir"] = mdir
        rec["download"] = _download_guidance(mid, info, mdir)
    if info:
        rec["registry"] = _registry_public(info)
    return rec


class CheckModelsNode(NodeBase):
    """模型就绪检查（A10）。type_id: core/check_models"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/check_models",
            title="模型就绪检查",
            category="控制",
            description="按模型清单检查本地模型就绪状态（路径解析+存在性+可选 hash），report:JSON 输出；缺失项给下载指引，下载仍归宿主",
            inputs=[],
            outputs=[
                PortSpec("report", PortType.JSON, label="就绪报告", required=False,
                         description="模型就绪状态报告：路径解析 + 存在性 + 可选 hash + 缺失项下载指引"),
                PortSpec("models", PortType.JSON, label="逐项状态", required=False,
                         description="逐模型就绪明细列表（report.models 同构）"),
                PortSpec("ready", PortType.BOOL, label="全部就绪", required=False,
                         description="清单内全部模型就绪时为 true"),
            ],
            params=[
                PortSpec("models", PortType.STRING, label="模型清单", required=False,
                         default="", widget="text",
                         description="JSON 数组（id 或 {id,repo,file,dir}）或换行/逗号分隔 id；留空/all = models_registry 全部（默认 6 模型）"),
                PortSpec("registry_path", PortType.STRING, label="注册表路径", required=False,
                         default="", widget="file",
                         description="models_registry.json 覆盖路径（留空 = 平台默认注册表）"),
                PortSpec("with_hash", PortType.BOOL, label="计算 sha256", required=False,
                         default=False, widget="toggle",
                         description="对就绪模型权重文件流式计算 sha256（大文件较耗时）"),
                PortSpec("refresh", PortType.STRING, label="刷新令牌", required=False,
                         default="", widget="text",
                         description="平台按参数缓存结果；下载模型后改动此项（如填当前时间）可强制重算，或用节点重置 force=true"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        store = ctx["store"]
        from app.plugin import deps
        registry_path = str(params.get("registry_path") or "").strip()
        with_hash = bool(params.get("with_hash", False))
        raw = str(params.get("models") or "").strip()
        registry = _load_registry(registry_path)
        ids = _parse_list(raw)
        if not ids:
            ids = list(DEFAULT_MODELS)

        progress = ctx.get("progress")
        if not callable(progress):
            progress = None
        n = len(ids)
        span = 1.0 / max(n, 1)

        models: List[Dict[str, Any]] = []
        for i, mid in enumerate(ids):
            label = MODEL_LABELS.get(mid, mid)
            info = registry.get(mid)
            rec = _check_one(mid, label, info, with_hash, progress, i * span, span)
            if progress:
                try:
                    progress(min((i + 1) * span, 1.0))
                except Exception:  # noqa: BLE001
                    pass
            models.append(rec)

        ready_n = sum(1 for m in models if m.get("ready"))
        all_ready = ready_n == n
        missing = [
            {"id": m["id"], "name": m.get("name", m["id"]),
             "download": m.get("download", [])}
            for m in models if not m.get("ready")
        ]
        report = {
            "tool": "core/check_models",
            "version": "1.0.0",
            "generated_at": time.time(),
            "registry": registry_path or "<default: backend/models_registry.json>",
            "models_dir": deps.models_dir(),
            "summary": {"total": n, "ready": ready_n, "missing": n - ready_n},
            "all_ready": all_ready,
            "models": models,
            "missing": missing,
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"all_ready": all_ready, "mode": "check_models"}),
            payload=report)
        models_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"count": n}),
            payload=models)
        return {"report": report_asset, "models": models_asset, "ready": all_ready}