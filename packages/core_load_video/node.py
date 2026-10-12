"""核心插件：视频导入 + 元数据探测（增强版 v2）。

能力：
- 单文件导入 `path` → 输出 VIDEO 资产 + 完整 ffprobe 元数据（容器/视频流/音频流/指纹）。
- 批量导入 `paths`（JSON 数组 / 换行或分号分隔 / glob 通配符）→ 标准批次信封 `batch` + `report`，
  逐条错误隔离、稳定 item_id、内容派生 batch_id（batchlib 契约）。
- 不覆盖源文件；默认复制进平台素材库；同一内容可经 fingerprint 识别重复。

增强点（相对解说猫 A1）：
- 元数据全量：container/format、video stream（codec/profile/尺寸/像素格式/SAR/DAR/旋转/色彩空间/码率）、
  audio stream（采样率/声道）、预估帧数、文件指纹（内容去重）与校验警告。
- 批次导入支持通配符展开与逐条失败隔离（错误归类永久/可重试），并保留原始相对信息。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from app.assets import Asset, file_fingerprint
from app.nodespec import NodeBase, PortType

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".webm", ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".ogv", ".rmvb"}
MEDIA_EXTS = VIDEO_EXTS | {".srt", ".ass", ".vtt", ".lrc", ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".wma", ".opus", ".aiff", ".amr", ".ape"}


def _parse_fps(rate: Any) -> float:
    """ffprobe '24000/1001' -> 23.952；无法解析返回 0.0。"""
    if rate is None:
        return 0.0
    s = str(rate)
    try:
        if "/" in s:
            n, d = s.split("/", 1)
            if float(d) == 0:
                return 0.0
            return round(float(n) / float(d), 4)
        return round(float(s), 4)
    except (ValueError, ZeroDivisionError):
        return 0.0


async def _run_cmd(ctx: Any, cmd: List[str], timeout: float, text: bool = True) -> Any:
    """执行子进程并捕获输出。

    优先平台托管执行（可取消/杀进程树）——但平台 run_tracked 是 wait 后读管道，
    在部分 ffmpeg 构建上会因管道持有而死锁；故先用短预算试探，失败立即回退
    subprocess.run(capture_output=True)（communicate 并发读，不会死锁，已实测）。
    """
    import asyncio
    import subprocess as _sp
    rt = ctx.get("run_tracked") if isinstance(ctx, dict) else None
    if rt is not None:
        try:
            return await rt(cmd, timeout=min(float(timeout), 6.0), text=text,
                            stdout=_sp.PIPE, stderr=_sp.PIPE)
        except Exception:  # noqa: BLE001 —— 超时/死锁/失败 → 回退
            pass
    return await asyncio.to_thread(
        lambda: _sp.run(cmd, capture_output=True, timeout=timeout, text=text))


def _ffprobe_candidates() -> List[str]:
    """ffprobe 候选链（统一 resolver；保持返回列表契约以兼容 probe_video 循环）。"""
    import shutil
    from app.plugin import deps
    out: List[str] = []
    env = os.environ.get("FRAMEWEAVE_FFPROBE", "").strip()
    if env:
        out.append(env)
    env2 = os.environ.get("FFPROBE_BIN", "").strip()
    if env2:
        out.append(env2)
    for name in ("ffprobe", "ffprobe.exe"):
        p = shutil.which(name)
        if p:
            out.append(p)
    # 统一 resolver：优先法（可能包含 params/env/imageio/默认位）
    r = deps.resolve_binary("ffprobe", env=os.environ, params={})
    if r["ready"] and r["path"] not in out:
        out.insert(0, r["path"])
    # 兜底保留裸命令
    return out or ["ffprobe"]


def _rotation_of(stream: Dict[str, Any]) -> float:
    for sd in stream.get("side_data_list") or []:
        r = sd.get("rotation")
        if r is not None:
            try:
                return float(r)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


async def probe_video(path: str, ctx: Any = None, timeout: float = 30.0) -> Dict[str, Any]:
    """ffprobe 全量探测 → 结构化元数据（含 warnings）。

    失败时不抛异常（返回 exists=False / warnings 带原因）；调用方据此给明确错误。
    """
    meta: Dict[str, Any] = {
        "path": os.path.abspath(path),
        "name": os.path.basename(path),
        "exists": os.path.isfile(path),
        "fingerprint": "",
        "container": {},
        "video": None,
        "audio": None,
        "streams": 0,
        "warnings": [],
        "probed_at": time.time(),
    }
    if not meta["exists"]:
        meta["warnings"].append("文件不存在")
        return meta
    try:
        meta["size_bytes"] = os.path.getsize(path)
        meta["fingerprint"] = file_fingerprint(path)
    except OSError as e:
        meta["warnings"].append(f"stat/指纹失败: {e}")
    data: Optional[Dict[str, Any]] = None
    last_err = ""
    for probe in _ffprobe_candidates():
        try:
            proc = await _run_cmd(
                ctx, [probe, "-v", "quiet", "-print_format", "json",
                      "-show_format", "-show_streams", path], timeout, text=True)
        except Exception as e:  # noqa: BLE001
            last_err = f"{probe}: {e}"
            continue
        if proc.returncode == 0 and proc.stdout:
            try:
                data = json.loads(proc.stdout)
                break
            except json.JSONDecodeError as e:
                last_err = f"{probe}: stdout 非 JSON ({e})"
        else:
            last_err = f"{probe}: 退出码 {proc.returncode}"
    if data is None:
        meta["warnings"].append(f"ffprobe 探测失败: {last_err or '无可用 ffprobe'}")
        return meta

    fmt = data.get("format") or {}
    meta["container"] = {
        "format_name": fmt.get("format_name", ""),
        "format_long_name": fmt.get("format_long_name", ""),
        "duration": None,
        "bit_rate": None,
        "size_bytes": fmt.get("size"),
        "tags": fmt.get("tags") or {},
    }
    try:
        d = float(fmt.get("duration") or 0)
        meta["container"]["duration"] = round(d, 3) if d > 0 else None
    except (TypeError, ValueError):
        pass
    try:
        br = int(fmt.get("bit_rate") or 0)
        meta["container"]["bit_rate"] = br if br > 0 else None
    except (TypeError, ValueError):
        pass

    streams = data.get("streams") or []
    meta["streams"] = len(streams)
    vstreams = [s for s in streams if s.get("codec_type") == "video"]
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    if not vstreams:
        meta["warnings"].append("未发现视频流（不是有效的视频文件）")

    if vstreams:
        vs = vstreams[0]
        fps = _parse_fps(vs.get("avg_frame_rate") or vs.get("r_frame_rate"))
        meta["video"] = {
            "index": vs.get("index"),
            "codec": vs.get("codec_name", ""),
            "codec_long": vs.get("codec_long_name", ""),
            "profile": vs.get("profile", ""),
            "width": int(vs.get("width") or 0) or None,
            "height": int(vs.get("height") or 0) or None,
            "pix_fmt": vs.get("pix_fmt", ""),
            "fps": fps,
            "avg_frame_rate": vs.get("avg_frame_rate", ""),
            "sample_aspect_ratio": vs.get("sample_aspect_ratio", ""),
            "display_aspect_ratio": vs.get("display_aspect_ratio", ""),
            "rotation": _rotation_of(vs),
            "color_space": vs.get("color_space", ""),
            "bit_rate": None,
            "nb_frames": vs.get("nb_frames"),
            "level": vs.get("level"),
        }
        try:
            br = int(vs.get("bit_rate") or 0)
            meta["video"]["bit_rate"] = br if br > 0 else None
        except (TypeError, ValueError):
            pass
        if vs.get("nb_frames"):
            try:
                meta["video"]["frame_count"] = int(vs["nb_frames"])
            except (TypeError, ValueError):
                pass
        dur = meta["container"].get("duration")
        if dur and fps:
            meta["video"]["estimated_frames"] = int(round(dur * fps))
        if len(vstreams) > 1:
            meta["warnings"].append(f"存在 {len(vstreams)} 条视频流，仅取第 1 条")

    if astreams:
        a0 = astreams[0]
        meta["audio"] = {
            "index": a0.get("index"),
            "codec": a0.get("codec_name", ""),
            "codec_long": a0.get("codec_long_name", ""),
            "sample_rate": int(a0.get("sample_rate") or 0) or None,
            "channels": int(a0.get("channels") or 0) or None,
            "channel_layout": a0.get("channel_layout", ""),
            "bit_rate": None,
        }
        try:
            br = int(a0.get("bit_rate") or 0)
            meta["audio"]["bit_rate"] = br if br > 0 else None
        except (TypeError, ValueError):
            pass
        if len(astreams) > 1:
            meta["warnings"].append(f"存在 {len(astreams)} 条音频流，仅取第 1 条")
    return meta


def expand_paths(raw: str) -> List[str]:
    """paths 参数解析：JSON 数组 / 换行或分号分隔 / glob（** 递归）→ 文件列表（保序去重）。"""
    txt = (raw or "").strip()
    if not txt:
        return []
    if txt.startswith("["):
        try:
            arr = json.loads(txt)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except json.JSONDecodeError:
            pass
    parts = [p.strip() for p in txt.replace(";", "\n").split("\n")]
    out: List[str] = []
    seen = set()
    for p in parts:
        if not p:
            continue
        if glob.has_magic(p):
            for hit in sorted(glob.glob(p, recursive=True)):
                if os.path.isfile(hit) and os.path.abspath(hit) not in seen:
                    seen.add(os.path.abspath(hit))
                    out.append(hit)
        else:
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap)
                out.append(p)
    return out


class LoadVideoNode(NodeBase):
    """视频导入（单文件或批量）。type_id: core/load_video"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="core/load_video",
            title="导入视频",
            category="输入",
            description="导入本地视频并解析完整元数据；paths 支持 JSON/换行/通配符批量导入为批次信封",
            inputs=[],
            outputs=[
                PortSpec("video", PortType.VIDEO, label="视频", required=False),
                PortSpec("meta", PortType.JSON, label="元数据", required=False),
                PortSpec("batch", PortType.JSON, label="批次信封", required=False),
                PortSpec("report", PortType.JSON, label="导入报告", required=False),
            ],
            params=[
                PortSpec("path", PortType.STRING, label="视频路径", required=False,
                         default=None, widget="file"),
                PortSpec("paths", PortType.STRING, label="批量路径/通配符", required=False,
                         default="", widget="text",
                         description="JSON 数组 / 换行或分号分隔 / glob 通配；启用后输出 batch/report"),
                PortSpec("copy_into_store", PortType.BOOL, label="复制到素材库", required=False,
                         default=True, widget="toggle"),
                PortSpec("probe_timeout", PortType.INT, label="探测超时(秒)", required=False,
                         default=30, widget="number"),
            ],
            version="2.0.0",
        )

    async def _load_single(self, ctx: dict, path: str, copy: bool) -> Asset:
        ap = os.path.abspath(path)
        if not os.path.isfile(ap):
            raise RuntimeError(f"invalid: 视频文件不存在: {path}")
        ext = os.path.splitext(ap)[1].lower()
        if ext not in MEDIA_EXTS:
            raise RuntimeError(f"invalid: 不支持的文件类型: {ext or '(无扩展名)'}（支持 {sorted(VIDEO_EXTS)}）")
        meta = await probe_video(path, ctx, float(ctx.get("_probe_timeout", 30)))
        if not meta["video"]:
            raise RuntimeError(f"invalid: 不是有效视频（未发现视频流）: {path}（{'; '.join(meta['warnings'])}）")
        store = ctx["store"]
        dest = ap
        if copy:
            dest_dir = os.path.join(store.files_dir, "videos")
            os.makedirs(dest_dir, exist_ok=True)
            base = os.path.basename(ap)
            dest = os.path.join(dest_dir, base)
            if os.path.abspath(dest) != ap:
                n = 0
                while os.path.exists(dest):
                    stem, ext = os.path.splitext(base)
                    n += 1
                    dest = os.path.join(dest_dir, f"{stem}_{n}{ext}")
                shutil.copy2(ap, dest)
            meta["stored_path"] = dest
        asset = Asset(id="", kind=PortType.VIDEO.value, path=dest, meta=meta,
                      fingerprint=meta.get("fingerprint", ""), node_id=ctx.get("node_id", ""))
        return store.save_asset(asset)

    async def _import_batch(self, ctx: dict, paths: List[str], copy: bool,
                            timeouts: float) -> Dict[str, Any]:
        from app import batchlib
        items = [{"path": p, "name": os.path.basename(p)} for p in paths]
        batch = batchlib.new_batch(items, source="core/load_video",
                                   options={"copy_into_store": copy})
        cp = os.path.join(ctx.get("workdir", "."), "batch_checkpoints",
                          f"load_{ctx.get('node_id', 'load_video')}.json")

        async def worker(item: dict) -> dict:
            payload = item.get("input") or {}
            p = str(payload.get("path") or "")
            meta = await probe_video(p, ctx, timeouts)
            if not meta["video"]:
                raise RuntimeError(
                    f"invalid: 不是有效视频: {p}（{'; '.join(meta['warnings']) or '文件不存在'}）")
            asset = await self._load_single(ctx, p, copy)
            w = list(meta.get("warnings") or [])
            return {
                "video_id": asset.id,
                "path": asset.path,
                "meta": asset.meta,
                "name": payload.get("name", ""),
                "fingerprint": asset.fingerprint,
                "warnings": w,
            }

        batch = await batchlib.run_items(batch, worker, concurrency=2,
                                         checkpoint_path=cp, progress=ctx.get("progress"),
                                         retries=0, resume=True)
        report = batchlib.summarize(batch)
        clean = []
        for it in batch["items"]:
            r = it.get("result")
            rec = {
                "item_id": it.get("item_id"), "index": it.get("index"),
                "status": it.get("status"), "name": (it.get("input") or {}).get("name"),
                "error": it.get("error"), "warnings": it.get("warnings"),
                "attempt": it.get("attempt"),
            }
            if isinstance(r, dict):
                rec.update({k: r.get(k) for k in ("video_id", "path", "fingerprint")})
            clean.append(rec)
        return {"batch": {"batch_id": batch.get("batch_id"), "items": clean},
                "report": report}

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        path = str(params.get("path") or "").strip()
        paths_raw = str(params.get("paths") or "").strip()
        copy = bool(params.get("copy_into_store", True))
        try:
            timeout = float(params.get("probe_timeout", 30) or 30)
        except (TypeError, ValueError):
            timeout = 30.0
        ctx = dict(ctx or {})
        ctx["_probe_timeout"] = timeout

        if paths_raw:
            paths = expand_paths(paths_raw)
            if not paths:
                raise RuntimeError("invalid: paths 未匹配到任何文件（检查路径/通配符）")
            result = await self._import_batch(ctx, paths, copy, timeout)
            store = ctx["store"]
            batch_asset = store.save_asset(
                Asset(id="", kind=PortType.JSON.value, meta={"count": len(result["batch"]["items"])}),
                payload=result["batch"])
            report_asset = store.save_asset(
                Asset(id="", kind=PortType.JSON.value, meta={"mode": "batch"}),
                payload=result["report"])
            return {"video": None, "meta": None, "batch": batch_asset, "report": report_asset}

        if not path:
            raise RuntimeError("contract: 请提供 path（单文件）或 paths（批量导入）")
        asset = await self._load_single(ctx, path, copy)
        store = ctx["store"]
        meta_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value,
                  meta={"name": asset.meta.get("name", ""), "fingerprint": asset.fingerprint}),
            payload=asset.meta)
        report = {
            "mode": "single", "ok": True,
            "video_id": asset.id, "path": asset.path,
            "fingerprint": asset.fingerprint,
            "duration": asset.meta.get("container", {}).get("duration"),
            "warnings": asset.meta.get("warnings") or [],
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": "single"}), payload=report)
        return {"video": asset, "meta": meta_asset, "batch": None, "report": report_asset}