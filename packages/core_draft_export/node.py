"""剪映草稿导出节点：生成剪映草稿目录（draft_content.json）+ Pr XML + EDL。

剪映草稿结构（2024+ 版本）：
  Drafts/<name>/draft_content.json
  关键字段：tracks（视频/音频/字幕轨）、materials（素材库）、canvas_config
"""
from __future__ import annotations
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app.registry import register


def _seg_to_asset(store, seg: Dict[str, Any], base_dir: str) -> str:
    """为片段建立素材（M0 简化：指向源视频 + 区间）。返回素材 id。"""
    return str(seg.get("index", 0))


class DraftExportNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/draft_export",
            title="剪映草稿导出",
            category="输出",
            description="生成剪映草稿 / Premiere XML / EDL 工程文件",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="视频", required=True),
                PortSpec(name="segments", type=PortType.SEGMENTS, label="片段列表", required=True),
                PortSpec(name="audio", type=PortType.AUDIO, label="配音音频", required=False),
                PortSpec(name="styled_subtitle", type=PortType.SUBTITLE, label="样式字幕", required=False),
            ],
            outputs=[
                PortSpec(name="draft", type=PortType.JSON, label="剪映草稿信息"),
                PortSpec(name="xml", type=PortType.STRING, label="Pr XML"),
                PortSpec(name="edl", type=PortType.STRING, label="EDL"),
                PortSpec(name="fcpxml", type=PortType.STRING, label="FCPXML"),
            ],
            params=[
                PortSpec(name="format", type=PortType.STRING, label="导出格式", default="jianying",
                         widget="select", options=["jianying", "prxml", "edl", "fcpxml", "all"]),
                PortSpec(name="aspect", type=PortType.STRING, label="画幅", default="16:9",
                         widget="select", options=["16:9", "9:16", "1:1"]),
                PortSpec(name="resolution", type=PortType.STRING, label="分辨率", default="1080p",
                         widget="select", options=["1080p", "720p", "4k"]),
                PortSpec(name="draft_name", type=PortType.STRING, label="草稿名",
                         default="FrameWeave_导出", widget="text"),
                PortSpec(name="transition", type=PortType.STRING, label="转场", default="cut",
                         widget="select", options=["cut", "fade", "fadeblack", "dissolve", "slide", "wipe", "zoom"]),
                PortSpec(name="transition_duration", type=PortType.FLOAT, label="转场时长(秒)", default=0.5,
                         widget="number"),
                PortSpec(name="output_dir", type=PortType.STRING, label="输出目录",
                         default="", widget="text",
                         description="留空输出到资产目录"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video_asset = inputs.get("video")
        seg_asset = inputs.get("segments")
        audio_asset = inputs.get("audio")
        sub_asset = inputs.get("styled_subtitle")
        if video_asset is None or seg_asset is None:
            raise RuntimeError("需要视频与片段列表输入")

        store = ctx["store"]
        segs = store.read_json(seg_asset.id) or []
        subs = store.read_json(sub_asset.id) if sub_asset else []
        aspect = params.get("aspect", "16:9")
        res = params.get("resolution", "1080p")
        format_ = params.get("format", "jianying")
        draft_name = str(params.get("draft_name", "FrameWeave_导出"))

        width, height = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}[aspect]
        if res == "720p":
            width, height = (1280, 720) if aspect == "16:9" else ((720, 1280) if aspect == "9:16" else (720, 720))
        elif res == "4k":
            width, height = (3840, 2160) if aspect == "16:9" else ((2160, 3840) if aspect == "9:16" else (2160, 2160))

        video_path = video_asset.path
        vname = os.path.basename(video_path)
        # meta/JSON 读出后 duration 可能是字符串，统一转 float
        _raw_dur = video_asset.meta.get("duration")
        if _raw_dur is None:
            _raw_dur = segs[-1]["end"] if segs else 60.0
        try:
            duration = float(_raw_dur)
        except (TypeError, ValueError):
            duration = 60.0

        # ---------- 剪映草稿 JSON ----------
        transition = str(params.get("transition", "cut") or "cut")
        trans_dur = float(params.get("transition_duration", 0.5) or 0.5)
        jy_draft = _build_jianying_draft(segs, video_path, vname, duration,
                                         audio_path=audio_asset.path if audio_asset else None,
                                         subs=subs, width=width, height=height, draft_name=draft_name,
                                         transition=transition, transition_duration=trans_dur)

        # Pr XML
        xml = _build_pr_xml(segs, video_path, vname, width, height, duration)
        # EDL
        edl = _build_edl(segs, video_path, vname, fps=25.0)
        # FCPXML
        fcpxml = _build_fcpxml(segs, video_path, vname, width, height, duration, fps=25.0,
                               audio_path=audio_asset.path if audio_asset else None, subs=subs)

        out_dir = params.get("output_dir") or os.path.join(store.files_dir, "drafts")
        os.makedirs(out_dir, exist_ok=True)

        results = {}
        if format_ in ("jianying", "all"):
            draft_dir = os.path.join(out_dir, draft_name)
            os.makedirs(draft_dir, exist_ok=True)
            jp = os.path.join(draft_dir, "draft_content.json")
            with open(jp, "w", encoding="utf-8") as f:
                json.dump(jy_draft, f, ensure_ascii=False, indent=2)
            results["draft_path"] = jp
        if format_ in ("prxml", "all"):
            xp = os.path.join(out_dir, draft_name + ".xml")
            with open(xp, "w", encoding="utf-8") as f:
                f.write(xml)
            results["xml_path"] = xp
        if format_ in ("edl", "all"):
            ep = os.path.join(out_dir, draft_name + ".edl")
            with open(ep, "w", encoding="utf-8") as f:
                f.write(edl)
            results["edl_path"] = ep
        if format_ in ("fcpxml", "all"):
            fp2 = os.path.join(out_dir, draft_name + ".fcpxml")
            with open(fp2, "w", encoding="utf-8") as f:
                f.write(fcpxml)
            results["fcpxml_path"] = fp2

        draft_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta=results),
            payload={"format": format_, "aspect": aspect, "resolution": res,
                     "tracks": len(segs), "results": results})
        xml_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "prxml"}),
            payload={"text": xml})
        edl_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "edl"}),
            payload={"text": edl})
        fcpxml_asset = store.save_asset(
            Asset(id="", kind=PortType.STRING.value, meta={"format": "fcpxml"}),
            payload={"text": fcpxml})
        return {"draft": draft_asset, "xml": xml_asset, "edl": edl_asset, "fcpxml": fcpxml_asset}



# 剪映内置转场 id（draft 兼容字段）
_JIANYING_TRANSITION_IDS = {
    "cut": "",
    "fade": "00000100",
    "fadeblack": "00000102",
    "dissolve": "00000104",
    "slide": "00000108",
    "wipe": "00000110",
    "zoom": "00000112",
}

def _build_jianying_draft(segs, video_path, vname, duration, audio_path, subs, width, height, draft_name,
                           transition="cut", transition_duration=0.5):
    """构造剪映 draft_content.json（兼容新版结构）。"""
    now = int(time.time() * 1000 * 1000)  # 微秒
    vid = str(uuid.uuid4()).upper()
    vid_seg = str(uuid.uuid4()).upper()

    # 视频轨片段
    video_segments = []
    for i, s in enumerate(segs):
        st = int(s["start"] * 1_000_000)
        du = int(s["duration"] * 1_000_000)
        video_segments.append({
            "id": str(uuid.uuid4()).upper(),
            "material_id": vid,
            "source_timestamp": st,
            "target_timestamp": st,
            "duration": du,
            "source_duration": du,
            "speed": 1.0,
            "reverse": False,
            "visible": True,
            "render_index": i,
            "clip": {"alpha": 1.0, "flip_h": False, "flip_v": False},
            "extra_material_refs": [],
            "transition": _jianying_transition(transition, transition_duration, i, len(segs)),
            "volume": 1.0,
            "audio_fade": {"fade_in_duration": 0, "fade_out_duration": 0},
        })

    # 音频轨
    audio_segments = []
    if audio_path and os.path.exists(audio_path):
        audio_segments.append({
            "id": str(uuid.uuid4()).upper(),
            "material_id": vid_seg,
            "source_timestamp": 0,
            "target_timestamp": 0,
            "duration": int(duration * 1_000_000),
            "source_duration": int(duration * 1_000_000),
            "speed": 1.0,
            "visible": True,
            "render_index": 0,
            "volume": 1.0,
            "audio_fade": {"fade_in_duration": 0, "fade_out_duration": 0},
        })

    # 字幕轨（material 注册到 materials.texts，避免悬空引用）
    sub_segments = []
    text_materials = []
    for i, sub in enumerate(subs or []):
        st = int(sub.get("start", 0) * 1_000_000)
        du = int((sub.get("end", sub.get("start", 0)) - sub.get("start", 0)) * 1_000_000)
        style = _jianying_subtitle_style(sub.get("style", {}))
        text_mid = str(uuid.uuid4()).upper()
        text_materials.append({
            "id": text_mid,
            "content": sub.get("text", ""),
            "type": "text",
            "style": style,
            "duration": du,
            "material_name": "字幕",
        })
        sub_segments.append({
            "id": str(uuid.uuid4()).upper(),
            "material_id": text_mid,
            "source_timestamp": 0,
            "target_timestamp": st,
            "duration": du,
            "source_duration": du,
            "visible": True,
            "render_index": i,
            "content": sub.get("text", ""),
            "style": style,
            "transform": {"scale": 1.0, "rotation": 0, "position": {"x": 0, "y": 0}},
            "adjust": {"alpha": 1.0},
        })

    return {
        "canvas_config": {
            "canvas_size": {"width": width, "height": height},
            "canvas_color": "#000000",
        },
        "tracks": [
            {
                "type": "video", "id": str(uuid.uuid4()).upper(),
                "is_default_name": True, "segment_count": len(video_segments),
                "segments": video_segments,
            },
            {
                "type": "audio", "id": str(uuid.uuid4()).upper(),
                "is_default_name": True, "segment_count": len(audio_segments),
                "segments": audio_segments,
            },
            {
                "type": "text", "id": str(uuid.uuid4()).upper(),
                "is_default_name": True, "segment_count": len(sub_segments),
                "segments": sub_segments,
            },
        ],
        "materials": {
            "videos": [{
                "id": vid, "path": video_path, "duration": int(duration * 1_000_000),
                "width": width, "height": height, "material_name": vname,
                "type": "video",
            }],
            "audios": [{
                "id": vid_seg, "path": audio_path or "", "duration": int(duration * 1_000_000),
                "material_name": "配音", "type": "audio",
            }] if audio_path else [],
            "texts": text_materials,
            "stickers": [], "effects": [],
            "transitions": _jianying_transitions(transition, transition_duration),
        },
        "draft_fold_path": "",
        "draft_name": draft_name,
        "create_time": now,
        "modify_time": now,
        "draft_root_path": "",
    }


def _build_pr_xml(segs, video_path, vname, width, height, duration):
    """Premiere Pro XML（粗略版）。M5 修复：名称/路径做 XML 转义，防 & < > 破坏工程。"""
    import xml.sax.saxutils as sax

    def esc(t):
        return sax.escape(str(t))

    vname_esc = esc(vname)
    path_esc = esc(video_path.replace(os.sep, "/"))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<xmeml version="4">',
        '<project id="frameweave">',
        f'<name>FrameWeave {vname_esc}</name>',
        '<sequence id="seq1">',
        f'<duration>{int(duration * 25)}</duration>',
        '<rate><timebase>25</timebase></rate>',
        '<media><video><format><samplecharacteristics>',
        f'<width>{width}</width><height>{height}</height>',
        '</samplecharacteristics></format></video></media>',
        '<track>',
    ]
    for s in segs:
        st = int(s["start"] * 25)
        du = int(s["duration"] * 25)
        lines.append(f'<clipitem id="c{st}"><name>{vname_esc}</name>'
                     f'<start>{st}</start><end>{st + du}</end>'
                     f'<in>{st}</in><out>{st + du}</out>'
                     f'<file id="f{st}"><pathurl>file://localhost/{path_esc}</pathurl></file>'
                     f'</clipitem>')
    lines += ['</track>', '</sequence>', '</project>', '</xmeml>']
    return "\n".join(lines)


def _build_edl(segs, video_path, vname, fps=25.0):
    """CMX 3600 EDL。"""
    lines = ["TITLE: FrameWeave Export", "FCM: NON-DROP FRAME", ""]
    for i, s in enumerate(segs, 1):
        st_f = s["start"] * fps
        en_f = s["end"] * fps
        def tc(frames):
            h = int(frames // (3600 * fps)); m = int(frames % (3600 * fps) // (60 * fps))
            sec = int(frames % (60 * fps) // fps); fr = int(frames % fps)
            return f"{h:02d}:{m:02d}:{sec:02d}:{fr:02d}"
        lines.append(f"{i:03d}  AX       V     C        {tc(st_f)} {tc(en_f)} {tc(st_f)} {tc(en_f)}")
        lines.append(f"* FROM CLIP NAME: {vname}")
        lines.append("")
    return "\n".join(lines)


def _build_fcpxml(segs, video_path, vname, width, height, duration, fps=25.0,
                  audio_path=None, subs=None):
    """构造 FCPXML 1.9 工程文件（Final Cut Pro 交换格式）。

    结构：resources(素材) + library/event/project/sequence + spine 时间线。
    每个片段为 asset-clip，支持视频/音频/字幕三层轨道，offset 累加。
    """
    import xml.sax.saxutils as sax

    def esc(t):
        return sax.escape(str(t))

    def frame_of(seconds):
        return int(round(seconds * fps))

    def fr(seconds):
        return "%d/%ds" % (frame_of(seconds), fps)

    fmt_name = "FFVideoFormat%s" % ("1080p25" if height <= 1080 and width <= 1920 else "2160p25")
    total_frames = max(frame_of(duration), 1)
    asset_id = "r1"

    # 资源
    res_parts = []
    res_parts.append('    <format id="fmt1" name="%s" frameDuration="1/%ds" width="%d" height="%d"/>'
                     % (fmt_name, fps, width, height))
    res_parts.append('    <asset id="%s" name="%s" uid="%s" start="0s" duration="%s" hasVideo="1" hasAudio="1" '
                     'format="fmt1"/>' % (asset_id, esc(vname), asset_id, fr(duration)))
    if audio_path:
        aname = os.path.basename(audio_path)
        res_parts.append('    <asset id="aud1" name="%s" uid="aud1" start="0s" duration="%s" hasVideo="0" '
                         'hasAudio="1"/>' % (esc(aname), fr(duration)))

    # 视频轨（spine）
    spine_parts = []
    offset_frames = 0
    for i, s in enumerate(segs):
        st = frame_of(s.get("start", 0))
        du = frame_of(s.get("duration", s.get("end", 0) - s.get("start", 0)))
        if du <= 0:
            continue
        spine_parts.append(
            '      <asset-clip name="%s" ref="%s" lane="1" offset="%d/%ds" start="%d/%ds" duration="%d/%ds" '
            'tcFormat="NDF"/>' % (esc(vname + "_%d" % i), asset_id, offset_frames, fps, st, fps, du, fps))
        offset_frames += du

    # 音频轨（次级轨道）
    audio_parts = []
    if audio_path:
        audio_parts.append(
            '      <asset-clip name="%s" ref="aud1" lane="2" offset="0s" start="0s" duration="%s" tcFormat="NDF"/>'
            % (esc(os.path.basename(audio_path)), fr(duration)))
    elif segs:
        # 无外部配音时保留片段原声
        off2 = 0
        for i, s in enumerate(segs):
            st = frame_of(s.get("start", 0))
            du = frame_of(s.get("duration", s.get("end", 0) - s.get("start", 0)))
            if du <= 0:
                continue
            audio_parts.append(
                '      <asset-clip name="%s" ref="%s" lane="2" offset="%d/%ds" start="%d/%ds" duration="%d/%ds" '
                'tcFormat="NDF"/>' % (esc(vname + "_a%d" % i), asset_id, off2, fps, st, fps, du, fps))
            off2 += du

    # 字幕轨（title）
    title_parts = []
    for i, sub in enumerate(subs or []):
        st = frame_of(sub.get("start", 0))
        du = frame_of(sub.get("end", sub.get("start", 0)) - sub.get("start", 0))
        if du <= 0:
            continue
        txt = esc(sub.get("text", ""))
        title_parts.append(
            '      <title name="sub_%d" lane="3" offset="%d/%ds" start="0s" duration="%d/%ds">'
            '<text><text-style ref="ts1">%s</text-style></text></title>'
            % (i, st, fps, du, fps, txt))

    timeline = []
    timeline.append('    <spine>')
    if spine_parts:
        timeline.extend(spine_parts)
    else:
        # 空工程占位
        timeline.append('      <gap name="空" duration="%s"/>' % fr(duration))
    timeline.append('    </spine>')
    if audio_parts:
        timeline.append('    <audio lane="2">')
        timeline.extend(audio_parts)
        timeline.append('    </audio>')
    if title_parts:
        timeline.append('    <title lane="3">')
        timeline.extend(title_parts)
        timeline.append('    </title>')

    doc = []
    doc.append('<?xml version="1.0" encoding="UTF-8"?>')
    doc.append('<!DOCTYPE fcpxml>')
    doc.append('')
    doc.append('<fcpxml version="1.9">')
    doc.append('  <resources>')
    doc.extend(res_parts)
    doc.append('  </resources>')
    doc.append('  <library>')
    doc.append('    <event name="FrameWeave">')
    doc.append('      <project name="%s">' % esc("FrameWeave项目"))
    doc.append('        <sequence format="fmt1" duration="%s" tcStart="0s" tcFormat="NDF">' % fr(duration))
    doc.extend(timeline)
    doc.append('        </sequence>')
    doc.append('      </project>')
    doc.append('    </event>')
    doc.append('  </library>')
    doc.append('</fcpxml>')
    doc.append('')
    return "\n".join(doc)


def _jianying_transition(transition, duration, idx, total):
    """剪映片段转场字段：首段无转场，其余段应用。"""
    if idx == 0 or total <= 1 or transition in ("cut", ""):
        return {"duration": 0, "is_apply": False, "id": ""}
    tid = _JIANYING_TRANSITION_IDS.get(transition, "")
    if not tid:
        return {"duration": 0, "is_apply": False, "id": ""}
    return {
        "duration": int(max(float(duration), 0.1) * 1_000_000),
        "is_apply": True,
        "id": tid,
        "transition_effect": {"type": transition, "is_custom": False},
    }


def _jianying_transitions(transition, duration):
    """materials.transitions 素材注册。"""
    tid = _JIANYING_TRANSITION_IDS.get(transition, "")
    if not tid:
        return []
    return [{
        "id": tid,
        "type": "transition",
        "name": transition,
        "duration": int(max(duration, 0.1) * 1_000_000),
        "category": "common",
    }]


def _jianying_subtitle_style(style):
    """字幕花字样式 → 剪映 text 样式字段。"""
    style = style or {}
    color = str(style.get("color", "#FFFFFF"))
    stroke = str(style.get("stroke", "#000000"))
    stroke_w = int(style.get("stroke_width", 2) or 2)
    bold = bool(style.get("bold", True))
    karaoke = bool(style.get("karaoke", True))
    animation = str(style.get("animation", "none"))
    anim_map = {
        "none": "",
        "typewriter": "typewriter",
        "bounce": "bounce",
        "fade": "fade_in",
        "fadeup": "fade_up",
    }
    return {
        "color": color,
        "stroke_color": stroke,
        "stroke_width": stroke_w,
        "bold": bold,
        "font_size": int(style.get("size", 48) or 48),
        "font": str(style.get("font", "微软雅黑")),
        "alignment": "center",
        "karaoke": karaoke,
        "animation": anim_map.get(animation, ""),
    }
