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


def _is_runtime_work_dir(path: str) -> bool:
    """路径是否落在 worker 的运行期工作目录里。

    依据：supervisor 把每个 job 的 scratch 建在 `<store>/runtime/work/<job_id>`，
    `sweep_stale_scratch` 会回收 24 小时前的目录。而进程内执行时 files_dir 是
    `<store>/files`，不会被回收。两个位置必须区别对待——否则用户今天拿到的
    srt_path，明天就是个不存在的文件，而片段包里没有任何提示。
    """
    norm = os.path.abspath(path).replace("\\", "/").lower()
    return "/runtime/work/" in norm


class ExportBundleNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        timeline = _as_list(store, inputs.get("timeline"), "时间线", required=False)
        subtitle = _as_list(store, inputs.get("subtitle"), "字幕", required=False)
        video = inputs.get("video")
        # 素材路径优先取 meta.source。worker 模式下 video.path 指向该次 job 的
        # in/ 临时目录，任务结束就删了——把它写进片段包，等于给用户一个必然失效的
        # 路径，剪辑软件导入后素材全离线。meta.source 是 media_import 记下的用户
        # 选的原始文件路径，才是能拿去重链接的那个。
        video_path = ""
        if video is not None:
            vm = getattr(video, "meta", None) or {}
            video_path = str(vm.get("source") or getattr(video, "path", "") or "")

        # 整段直出：没有时间线时，成片就是原片本身（转写加字幕这类场景不重剪画面）。
        # 不硬造一条假的「排片结果」：这一段如实标注 out 时间等于源片时间，
        # 并在 warnings 里说明画面未重剪，免得下游以为做过排片。
        straight_through = False
        if not timeline:
            dur = 0.0
            try:
                dur = float((getattr(video, "meta", None) or {}).get("duration") or 0.0)
            except (TypeError, ValueError):
                dur = 0.0
            if video is None or dur <= 0:
                raise RuntimeError(
                    "既没有时间线，也没有带时长的源视频，没有可导出的片段"
                    "（整段直出需要 video 且其 meta.duration > 0）")
            d = round(dur, 3)
            timeline = [{
                "index": 0, "start": 0.0, "end": d,
                "out_start": 0.0, "out_end": d,
                "duration": d, "scene_index": None, "text": "",
            }]
            straight_through = True

        name = str(params.get("output_name") or "bundle").strip() or "bundle"
        name = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "bundle"
        aspect = str(params.get("aspect") or "16:9").strip() or "16:9"

        # 字幕按成片时间挂到对应片段上；跨段的字幕挂给它起始所在的段
        by_seg: Dict[int, List[Dict[str, Any]]] = {}
        orphan = 0
        for s in subtitle:
            try:
                st = float(s.get("start"))
            except (TypeError, ValueError):
                orphan += 1
                continue
            placed = False
            for seg in timeline:
                try:
                    if float(seg.get("out_start")) <= st < float(seg.get("out_end")):
                        by_seg.setdefault(int(seg.get("index", 0)), []).append(s)
                        placed = True
                        break
                except (TypeError, ValueError):
                    continue
            if not placed:
                orphan += 1

        warnings: List[str] = []
        if straight_through:
            warnings.append(
                "没有时间线输入，按整段直出：画面未重剪，成片时间等于源片时间")
        if orphan:
            warnings.append(
                "%d 条字幕的起始时间不落在任何片段的成片区间内，未挂进片段包（仍写进 SRT）" % orphan)
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

        # 兜底：正常情况下走不到这里（有 timeline 就有段，没有就整段直出，两样都没有已在上面 raise）。
        # 留这一条是因为反证时发现，这里要是被改坏，报出来的是 IndexError，
        # 看不出「没片段」这层意思。
        if not segs:
            raise RuntimeError("没有可导出的片段（时间线与源视频都为空）")

        try:
            total = float(segs[-1].get("out_end") or 0) - float(segs[0].get("out_start") or 0)
        except (TypeError, ValueError):
            total = 0.0

        srt_path = ""
        srt_text = ""
        if subtitle and bool(params.get("write_srt", True)):
            srt_text = _to_srt(subtitle)
            files_dir = getattr(store, "files_dir", None)
            if files_dir:
                try:
                    os.makedirs(files_dir, exist_ok=True)
                    srt_path = os.path.join(files_dir, "%s.srt" % name)
                    with open(srt_path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(srt_text)
                    os.chmod(srt_path, 0o644)
                except OSError as e:
                    srt_path = ""
                    warnings.append("SRT 写出失败（%s），片段包仍然可用" % e)
            else:
                warnings.append("存储后端没有 files_dir，跳过 SRT 写出")
        srt_temp = bool(srt_path) and _is_runtime_work_dir(srt_path)
        if srt_temp:
            warnings.append(
                "SRT 落在运行期工作目录（%s），该目录会被定时回收；"
                "要文件请取 srt 端口的全文自行落盘" % os.path.dirname(srt_path))

        meta = {
            "count": len(segs),
            "total_duration": round(max(total, 0.0), 3),
            "aspect": aspect,
            "source": video_path,
            "subtitle_count": len(subtitle),
            "srt_path": srt_path,
            # srt_path 指向运行期目录时为 True——那个路径会失效，
            # 权威产物是 srt 端口里的全文。
            "srt_path_is_temp": srt_temp,
            "bundle_name": name,
            "straight_through": straight_through,
            "generated_at": round(time.time(), 3),
            "warnings": list(warnings),
            "degraded": bool(warnings),
        }
        asset = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value, meta=meta), payload=segs)
        out: Dict[str, Any] = {"bundle": asset}
        if srt_text:
            out["srt"] = srt_text
        return out
