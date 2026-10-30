"""导出 Premiere XML 节点：把片段包导成 FCP7 XML（xmeml）。

为什么选 xmeml 而不是剪映草稿：xmeml 是公开、稳定、被 Premiere / DaVinci /
Final Cut 共同支持的交换格式，结构可以照着规范写对。剪映草稿是封闭格式，
版本一变就失效——写一个「看起来像」的草稿包，用户导入失败却不知道为什么，
比不给更糟。

两套时间都要写进去，缺一不可：
  start/end —— 这段在**成片**上的位置（决定顺序）
  in/out    —— 这段从**素材**的哪一秒开始（决定取哪一截）
只给前者，剪辑软件不知道从素材哪取；只给后者，它排不出顺序。

帧率必须与源视频一致：xmeml 里所有时间都是帧号，用错 fps 会让整条时间线
按比例缩放——30fps 的素材放进 25fps 序列，一段 5 秒会变成 6 秒。
"""
from __future__ import annotations

import os
import time
import xml.sax.saxutils as sx
from typing import Any, Dict, List
from urllib.parse import quote

from app.assets import Asset
from app.nodespec import NodeBase, PortType


def _esc(v: Any) -> str:
    return sx.escape(str(v if v is not None else ""))


def _pathurl(path: str) -> str:
    p = str(path or "").replace("\\", "/")
    if not p:
        return ""
    # 冒号与斜杠必须保留：FCP7 的 pathurl 惯例是 file://localhost/F:/videos/a.mp4，
    # 把冒号编码成 %3A 后部分 NLE 会当成非法路径，导入时提示找不到素材。
    if p.startswith("/"):
        return "file://localhost" + quote(p, safe="/:")
    return "file://localhost/" + quote(p, safe="/:")


def _as_list(store: Any, asset: Any) -> List[Dict[str, Any]]:
    if asset is None:
        raise RuntimeError("缺少片段包输入")
    try:
        data = store.read_json(asset.id) if hasattr(asset, "id") else asset
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("片段包读取失败: %s" % e)
    if isinstance(data, dict):
        for key in ("segments", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not data:
        raise RuntimeError("片段包为空或格式不对（期望数组）")
    return data


def _rate(fps: float) -> str:
    tb = int(round(fps)) or 25
    ntsc = "TRUE" if abs(fps - 29.97) < 0.02 else "FALSE"
    return ("      <rate><timebase>%d</timebase><ntsc>%s</ntsc></rate>\n" % (tb, ntsc))


class ExportPremiereXmlNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        segs = _as_list(store, inputs.get("bundle"))

        name = str(params.get("output_name") or "timeline").strip() or "timeline"
        name = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "timeline"
        try:
            fps = float(params.get("fps", 25.0) or 25.0)
        except (TypeError, ValueError):
            fps = 25.0
        fps = min(max(fps, 1.0), 240.0)
        try:
            width = int(params.get("width", 1920) or 1920)
            height = int(params.get("height", 1080) or 1080)
        except (TypeError, ValueError):
            width, height = 1920, 1080

        warnings: List[str] = []
        source = ""
        for s in segs:
            if s.get("src_path") or s.get("source"):
                source = str(s.get("src_path") or s.get("source"))
                break
        src_asset = inputs.get("bundle")
        if not source:
            source = str((getattr(src_asset, "meta", {}) or {}).get("source") or "")
        if not source:
            warnings.append("片段包里没有源视频路径：导出的 XML 会缺 file 节点，"
                            "剪辑软件里需要手动重新链接素材")

        clips: List[str] = []
        cursor_out = 0.0
        total_frames = 0
        for i, s in enumerate(segs):
            try:
                out_start = float(s.get("out_start"))
                out_end = float(s.get("out_end"))
            except (TypeError, ValueError):
                # 没有成片时间就按顺序排（老片段包）
                dur = float(s.get("duration") or 0)
                out_start, out_end = cursor_out, cursor_out + dur
                warnings.append("第 %d 段没有 out_start/out_end，已按顺序重排" % i)
            try:
                src_start = float(s.get("src_start", 0) or 0)
                src_end = float(s.get("src_end", 0) or 0)
            except (TypeError, ValueError):
                src_start, src_end = 0.0, 0.0

            # 源片区间缺失时按成片时长反推，保证 in/out 至少是个合法区间
            if src_end <= src_start:
                src_end = src_start + max(out_end - out_start, 0.0)
                warnings.append("第 %d 段缺源片区间，已按成片时长反推" % i)

            f_start = int(round(out_start * fps))
            f_end = int(round(out_end * fps))
            f_in = int(round(src_start * fps))
            f_out = int(round(src_end * fps))
            if f_end <= f_start:
                f_end = f_start + 1
            if f_out <= f_in:
                f_out = f_in + 1
            dur_frames = f_end - f_start
            cursor_out = out_end
            total_frames = max(total_frames, f_end)

            file_block = ""
            if source:
                file_block = (
                    '        <file id="file-1">\n'
                    "          <name>%s</name>\n"
                    "          <pathurl>%s</pathurl>\n"
                    "%s"
                    "          <media>\n"
                    "            <video>\n"
                    "              <samplecharacteristics>\n"
                    "                <width>%d</width><height>%d</height>\n"
                    "              </samplecharacteristics>\n"
                    "            </video>\n"
                    "          </media>\n"
                    "        </file>\n"
                    % (_esc(os.path.basename(source)), _esc(_pathurl(source)),
                       _rate(fps), width, height))
            clips.append(
                '      <clipitem id="clipitem-%d">\n'
                "        <name>%s</name>\n"
                "        <duration>%d</duration>\n"
                "%s"
                "        <start>%d</start><end>%d</end>\n"
                "        <in>%d</in><out>%d</out>\n"
                "%s"
                "      </clipitem>\n"
                % (i + 1, _esc("seg%d" % (i + 1)), dur_frames, _rate(fps),
                   f_start, f_end, f_in, f_out, file_block))

        xml_text = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!DOCTYPE xmeml>\n"
            '<xmeml version="4">\n'
            '  <sequence id="sequence-1">\n'
            "    <name>%s</name>\n"
            "    <duration>%d</duration>\n"
            "%s"
            "    <media>\n"
            "      <video>\n"
            "        <format>\n"
            "          <samplecharacteristics>\n"
            "%s"
            "            <width>%d</width><height>%d</height>\n"
            "            <anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>\n"
            "          </samplecharacteristics>\n"
            "        </format>\n"
            "        <track>\n"
            "%s"
            "        </track>\n"
            "      </video>\n"
            "    </media>\n"
            "  </sequence>\n"
            "</xmeml>\n"
            % (_esc(name), total_frames, _rate(fps), _rate(fps), width, height,
               "".join(clips)))

        files_dir = getattr(store, "files_dir", None)
        if not files_dir:
            raise RuntimeError("存储后端没有 files_dir，无法写出 XML")
        os.makedirs(files_dir, exist_ok=True)
        path = os.path.join(files_dir, "%s.xml" % name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(xml_text)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass

        asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, path=path, meta={
                "format": "xmeml",
                "fps": fps,
                "width": width,
                "height": height,
                "clip_count": len(clips),
                "total_frames": total_frames,
                "total_seconds": round(total_frames / fps, 3),
                "source": source,
                "generated_at": round(time.time(), 3),
                "warnings": list(warnings),
                "degraded": bool(warnings),
            }))
        return {"xml": asset}
