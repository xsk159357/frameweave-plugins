"""片段包导出节点：把时间线与字幕整理成剪辑软件能直接用的片段包。

输出的是**逐段清单**，不是成品视频：每段给出「取源片哪一段」与「放在成片哪一段」。
两个区间必须都带上——只给源片区间，剪辑软件排不出顺序；只给成片区间，
它不知道该从素材里哪个位置取。

为什么不在这里渲染：渲染是另一件事（解码、缩放、转场、编码），
一个节点干两件事，出问题时说不清是排片错了还是编码错了。

SRT 写到资产目录（store.files_dir），不写到任意路径：
节点没有「随便写盘」的权限，也不该要。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from app.assets import Asset
from app.nodespec import NodeBase, PortType


def _as_list(store: Any, asset: Any, what: str, required: bool = True) -> List[Dict[str, Any]]:
    if asset is None:
        if required:
            raise RuntimeError("缺少%s输入" % what)
        return []
    try:
        data = store.read_json(asset.id) if hasattr(asset, "id") else asset
    except Exception as e:  # noqa: BLE001
        if required:
            raise RuntimeError("%s读取失败: %s" % (what, e))
        return []
    if isinstance(data, dict):
        for key in ("items", "segments", "timeline", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        if required:
            raise RuntimeError("%s格式不对（期望数组）" % what)
        return []
    return data


def _srt_time(t: float) -> str:
    ms = max(int(round(float(t) * 1000)), 0)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _to_srt(rows: List[Dict[str, Any]]) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        out.append(str(i))
        out.append("%s --> %s" % (_srt_time(r.get("start", 0)), _srt_time(r.get("end", 0))))
        text = str(r.get("text") or "")
        lines = r.get("lines") or [text]
        out.append("\n".join(str(x) for x in lines))
        out.append("")
    return "\n".join(out)


class ExportBundleNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        timeline = _as_list(store, inputs.get("timeline"), "时间线")
        if not timeline:
            raise RuntimeError("时间线为空，没有可导出的片段")
        subtitle = _as_list(store, inputs.get("subtitle"), "字幕", required=False)
        video = inputs.get("video")
        video_path = str(getattr(video, "path", "") or "") if video is not None else ""

        name = str(params.get("output_name") or "bundle").strip() or "bundle"
        name = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "bundle"
        aspect = str(params.get("aspect") or "16:9").strip() or "16:9"

        # 字幕按成片时间挂到对应片段上；跨段的字幕挂给它起始所在的段
        by_seg: Dict[int, List[Dict[str, Any]]] = {}
        for s in subtitle:
            try:
                st = float(s.get("start"))
            except (TypeError, ValueError):
                continue
            for seg in timeline:
                try:
                    if float(seg.get("out_start")) <= st < float(seg.get("out_end")):
                        by_seg.setdefault(int(seg.get("index", 0)), []).append(s)
                        break
                except (TypeError, ValueError):
                    continue

        warnings: List[str] = []
        segs: List[Dict[str, Any]] = []
        for seg in timeline:
            idx = int(seg.get("index", len(segs)))
            item = {
                "index": idx,
                "src_start": seg.get("start"),
                "src_end": seg.get("end"),
                "out_start": seg.get("out_start"),
                "out_end": seg.get("out_end"),
                "duration": seg.get("duration"),
                "scene_index": seg.get("scene_index"),
                "text": seg.get("text") or "",
                "subtitle": by_seg.get(idx, []),
            }
            if item["out_start"] is None:
                warnings.append("片段 %d 没有 out_start（时间线来自旧版本），已回落到源片时间" % idx)
                item["out_start"] = seg.get("start")
                item["out_end"] = seg.get("end")
            segs.append(item)

        try:
            total = float(segs[-1].get("out_end") or 0) - float(segs[0].get("out_start") or 0)
        except (TypeError, ValueError):
            total = 0.0

        srt_path = ""
        if subtitle and bool(params.get("write_srt", True)):
            files_dir = getattr(store, "files_dir", None)
            if files_dir:
                try:
                    os.makedirs(files_dir, exist_ok=True)
                    srt_path = os.path.join(files_dir, "%s.srt" % name)
                    with open(srt_path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(_to_srt(subtitle))
                    os.chmod(srt_path, 0o644)
                except OSError as e:
                    srt_path = ""
                    warnings.append("SRT 写出失败（%s），片段包仍然可用" % e)
            else:
                warnings.append("存储后端没有 files_dir，跳过 SRT 写出")

        meta = {
            "count": len(segs),
            "total_duration": round(max(total, 0.0), 3),
            "aspect": aspect,
            "source": video_path,
            "subtitle_count": len(subtitle),
            "srt_path": srt_path,
            "bundle_name": name,
            "generated_at": round(time.time(), 3),
            "warnings": list(warnings),
            "degraded": bool(warnings),
        }
        asset = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value, meta=meta), payload=segs)
        return {"bundle": asset}
