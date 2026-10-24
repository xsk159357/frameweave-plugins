# -*- coding: utf-8 -*-
"""剪映草稿生成器（纯 Python 重建，对齐 r4 §2 实测结构）。

生成目录（8 文件，与真实产物一致）：
    draft_content.json            # 27 顶层键 / materials 41 类 / tracks 列表
    draft_meta_info.json          # 25 键 + draft_materials 素材索引
    draft_settings                # 空文件
    attachment_draft_enterprise_info.json  # {}
    attachment_pc_common.json     # {}
    draft_agency_config.json      # {}
    draft_virtual_store.json      # {}

时间单位：微秒；UUID 全部 UUID4。轨道模型与实测一致：
    无配音: [video 主轨道][text]
    TTS:    [video 主轨道][video 原片插播(OST)][audio TTS解说][text]  (+BGM 轨可选)
字幕富文本 content 为 JSON 字符串（styles 数组）；音量互斥经段级 volume 0 或
common_keyframes volume 关键帧（±50ms 斜坡）。
"""
from __future__ import annotations
import json
import os
import platform as _plat
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

US = 1_000_000  # 秒 → 微秒


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def _now_us() -> int:
    return int(time.time() * US)


def _os_version() -> str:
    try:
        if sys.platform == "win32":
            try:
                import ctypes
                v = ctypes.windll.ntdll.RtlGetVersion(ctypes.byref(_OSVERSIONINFO()))
                if v == 0:
                    return f"{_OSVERSIONINFO().dwMajorVersion}.{_OSVERSIONINFO().dwMinorVersion}.{_OSVERSIONINFO().dwBuildNumber}"
            except Exception:
                pass
            try:
                w = sys.getwindowsversion()
                return f"{w.major}.{w.minor}.{w.build}"
            except Exception:
                pass
        return _plat.version()
    except Exception:
        return "10.0.0"


class _OSVERSIONINFO:
    pass


# ---------------------------------------------------------------- 平台标识
_PLATFORM_PROFILES = {
    "jianying": {"app_id": 3704, "app_source": "lv", "app_version": "5.9.0", "new_version": "89.0.0"},
    "capcut": {"app_id": 359289, "app_source": "cc", "app_version": "6.4.0", "new_version": "137.0.0"},
}


def _platform_block(export_target: str) -> Dict[str, Any]:
    prof = _PLATFORM_PROFILES.get(export_target, _PLATFORM_PROFILES["jianying"])
    return {
        "app_id": prof["app_id"],
        "app_source": prof["app_source"],
        "app_version": prof["app_version"],
        "device_id": "auto_generated",
        "hard_disk_id": "",
        "mac_address": "",
        "os": "windows",
        "os_version": _os_version(),
    }


# ---------------------------------------------------------------- 字幕样式
def _resolve_style(style_name: str) -> Dict[str, Any]:
    """字幕样式 → (content 样式, 材料级字段)。修复原版"黄字红边"映射缺失（B6）。"""
    s = str(style_name or "经典-白字黑边")
    if s in ("黑底白字", "black-bg"):
        return {
            "kind": "blackbg",
            "fill_rgb": [1.0, 1.0, 1.0],
            "stroke_rgb": None,
            "stroke_w": 0.0,
            "background_alpha": 0.85,
            "background_color": "#000000",
            "text_color": "#FFFFFF",
            "border_color": "",
            "border_width": 0.0,
        }
    if s in ("黄字红边", "yellow-red"):
        # 原版映射缺失输出与默认一致——此处按样式名真实生效
        return {
            "kind": "yellowred",
            "fill_rgb": [1.0, 0.8706, 0.0],
            "stroke_rgb": [1.0, 0.0, 0.0],
            "stroke_w": 0.08,
            "background_alpha": 0.0,
            "background_color": "",
            "text_color": "#FFDE00",
            "border_color": "#FF0000",
            "border_width": 0.08,
        }
    # 经典-白字黑边 / 默认 / 阅读模式
    return {
        "kind": "classic",
        "fill_rgb": [1.0, 1.0, 1.0],
        "stroke_rgb": [0.0, 0.0, 0.0],
        "stroke_w": 0.08,
        "background_alpha": 0.0,
        "background_color": "",
        "text_color": "#FFFFFF",
        "border_color": "#000000",
        "border_width": 0.08,
    }


def _rich_content(text: str, style: Dict[str, Any], size: float) -> str:
    """富文本 content（styles 数组 JSON 字符串）。"""
    fill = {
        "content": {"render_type": "solid", "solid": {"alpha": 1.0, "color": style["fill_rgb"]}},
    }
    style_obj: Dict[str, Any] = {
        "fill": fill,
        "font": {"id": "", "path": ""},
        "range": [0, len(text)],
        "size": size,
        "useLetterColor": True,
    }
    if style.get("stroke_rgb") is not None and style.get("stroke_w", 0) > 0:
        style_obj["strokes"] = [{
            "alpha": 1.0,
            "content": {"render_type": "solid", "solid": {"alpha": 1.0, "color": style["stroke_rgb"]}},
            "width": style["stroke_w"],
        }]
    obj = {"text": text, "styles": [style_obj]}
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------- materials
def _video_material(path: str, name: str, duration_us: int, w: int, h: int) -> Dict[str, Any]:
    p = path.replace("\\", "/")
    return {
        "audio_fade": None, "category_id": "", "category_name": "local", "check_flag": 63487,
        "crop": {"lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
                 "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
                 "upper_right_x": 1.0, "upper_right_y": 0.0},
        "crop_ratio": "free", "crop_scale": 1.0, "duration": duration_us, "extra_type_option": 0,
        "formula_id": "", "freeze": None, "has_audio": True, "height": h, "id": _uuid(),
        "intensifies_audio_path": "", "intensifies_path": "", "is_ai_generate_content": False,
        "is_unified_beauty_mode": False, "local_id": "", "local_material_id": "", "material_id": "",
        "material_name": name, "material_url": "", "matting": {
            "flag": 0, "has_use_quick_brush": False, "has_use_quick_eraser": False,
            "interactiveTime": [], "path": "", "strokes": []},
        "media_path": "", "name": name, "path": p, "picture_from": "none",
        "picture_set_category_id": "", "picture_set_category_name": "", "request_id": "",
        "reverse_intensifies_path": "", "reverse_path": "", "smart_motion": None, "source": 0,
        "source_platform": 0, "stable": None, "team_id": "", "type": "video",
        "video_algorithm": {"algorithms": [], "deflicker": None, "motion_blur_config": None,
                            "noise_reduction": None, "path": "", "quality_enhance": None,
                            "time_range": None},
        "width": w,
    }


def _speed_material(speed: float) -> Dict[str, Any]:
    return {"curve_speed": None, "id": _uuid(), "mode": 0, "speed": float(speed), "type": "speed"}


def _audio_material(path: str, name: str, duration_us: int) -> Dict[str, Any]:
    return {
        "app_id": 0, "category_id": "", "category_name": "local", "check_flag": 1,
        "duration": duration_us, "effect_id": "", "formula_id": "", "id": _uuid(),
        "intensifies_path": "", "local_material_id": "", "music_id": "", "name": name,
        "path": path.replace("\\", "/"), "query": "", "request_id": "", "resource_id": "",
        "search_id": "", "source_platform": 0, "team_id": "", "text_id": "",
        "tone_category_id": "", "tone_category_name": "", "tone_effect_id": "",
        "tone_effect_name": "", "tone_speaker": "", "tone_type": "", "type": "extract_music",
        "video_id": "", "wave_points": [],
    }


def _text_material(text: str, style: Dict[str, Any], size: float, is_portrait: bool) -> Dict[str, Any]:
    return {
        "add_type": 0, "alignment": 1, "background_alpha": style["background_alpha"],
        "background_color": style["background_color"], "background_height": 0.14,
        "background_horizontal_offset": 0.0, "background_round_radius": 0.0,
        "background_style": 0, "background_vertical_offset": 0.0, "background_width": 0.14,
        "base_content": "", "bold_width": 0.0, "border_alpha": 1.0,
        "border_color": style["border_color"], "border_width": style["border_width"],
        "caption_template_info": {"category_id": "", "category_name": "", "effect_id": "",
                                  "is_new": False, "path": "", "request_id": "",
                                  "resource_id": "", "resource_name": "", "source_platform": 0},
        "check_flag": 7, "combo_info": {"text_templates": []},
        "content": _rich_content(text, style, size),
        "fixed_height": -1.0, "fixed_width": -1.0, "font_category_id": "", "font_category_name": "",
        "font_id": "", "font_name": "", "font_path": "", "font_resource_id": "",
        "font_size": size, "font_source_platform": 0, "font_team_id": "", "font_title": "none",
        "font_url": "", "fonts": [], "force_apply_line_max_width": False, "global_alpha": 1.0,
        "group_id": "", "has_shadow": False, "id": _uuid(), "initial_scale": 1.0,
        "inner_padding": -1.0, "is_rich_text": False, "italic_degree": 0, "ktv_color": "",
        "language": "", "layer_weight": 1, "letter_spacing": 0.0, "line_feed": 1,
        "line_max_width": 0.82, "line_spacing": 0.02, "multi_language_current": "none",
        "name": "", "original_size": [], "preset_category": "", "preset_category_id": "",
        "preset_has_set_alignment": False, "preset_id": "", "preset_index": 0, "preset_name": "",
        "recognize_task_id": "", "recognize_type": 0, "relevance_segment": [],
        "shadow_alpha": 0.9, "shadow_angle": -45.0, "shadow_color": "",
        "shadow_distance": 5.0, "shadow_point": {"x": 1.0182337649086284, "y": -1.0182337649086284},
        "shadow_smoothing": 0.45, "shape_clip_x": False, "shape_clip_y": False,
        "source_from": "", "style_name": "", "sub_type": 0, "subtitle_keywords": None,
        "text_alpha": 1.0, "text_color": style["text_color"], "text_curve": None,
        "text_preset_resource_id": "", "text_size": 30, "text_to_audio_ids": [],
        "tts_auto_update": False, "type": "subtitle", "typesetting": 0, "underline": False,
        "underline_offset": 0.22, "underline_width": 0.05, "use_effect_default_color": False,
        "words": {"end_time": [], "start_time": [], "text": []},
    }


def _anim_material() -> Dict[str, Any]:
    return {"animations": [], "id": _uuid(), "multi_language_current": "none",
            "type": "sticker_animation"}


# ---------------------------------------------------------------- segments
def _base_segment() -> Dict[str, Any]:
    return {
        "audio_fade": None, "cartoon": False,
        "clip": {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
                 "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0},
                 "transform": {"x": 0.0, "y": 0.0}},
        "common_keyframes": [], "enable_adjust": False, "enable_color_curves": True,
        "enable_color_match_adjust": False, "enable_color_wheels": True, "enable_lut": False,
        "enable_smart_color_adjust": False, "extra_material_refs": [], "group_id": "",
        "hdr_settings": None, "id": _uuid(), "intensifies_audio": False, "is_placeholder": False,
        "is_tone_modify": False, "keyframe_refs": [], "last_nonzero_volume": 1.0,
        "material_id": "", "render_index": 0,
        "responsive_layout": {"enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
                              "target_follow": "", "vertical_pos_layout": 0},
        "reverse": False, "source_timerange": None, "speed": 1.0, "target_timerange": None,
        "template_id": "", "template_scene": "default", "track_attribute": 0,
        "track_render_index": 0, "uniform_scale": None, "visible": True, "volume": 1.0,
    }


def _video_segment(mat_id: str, src_start_us: int, src_dur_us: int,
                   tgt_start_us: int, tgt_dur_us: int, speed: float,
                   volume: float = 1.0, clip_transform_y: float = 0.0,
                   render_index: int = 0) -> Dict[str, Any]:
    seg = _base_segment()
    seg["clip"]["transform"]["y"] = clip_transform_y
    seg["material_id"] = mat_id
    seg["render_index"] = render_index
    seg["source_timerange"] = {"duration": src_dur_us, "start": src_start_us}
    seg["target_timerange"] = {"duration": tgt_dur_us, "start": tgt_start_us}
    seg["speed"] = float(speed)
    seg["volume"] = float(volume)
    if volume == 0.0:
        seg["last_nonzero_volume"] = 1.0
    return seg


def _audio_segment(mat_id: str, duration_us: int, tgt_start_us: int,
                   fade_us: int = 50000) -> Dict[str, Any]:
    seg = _base_segment()
    seg["material_id"] = mat_id
    seg["audio_fade"] = {"fade_in": {"duration": fade_us}, "fade_out": {"duration": fade_us}}
    seg["source_timerange"] = {"duration": duration_us, "start": 0}
    seg["target_timerange"] = {"duration": duration_us, "start": tgt_start_us}
    return seg


def _text_segment(mat_id: str, anim_mat_id: str, tgt_start_us: int, tgt_dur_us: int,
                  is_portrait: bool) -> Dict[str, Any]:
    seg = _base_segment()
    seg["material_id"] = mat_id
    seg["render_index"] = 11000
    seg["extra_material_refs"] = [anim_mat_id] if anim_mat_id else []
    seg["source_timerange"] = None
    seg["target_timerange"] = {"duration": tgt_dur_us, "start": tgt_start_us}
    seg["clip"]["transform"]["y"] = 0.8 if is_portrait else -0.8
    return seg


def _volume_keyframes(mute_ranges_us: List[tuple]) -> Dict[str, Any]:
    """互斥静音 volume 关键帧：每区间 4 帧（±50ms 斜坡），对齐 r4 §2.13。"""
    kf = []
    for st, en in mute_ranges_us:
        st, en = int(st), int(en)
        ramp = 50000
        kf += [
            {"time": max(st - ramp, 0), "value": 1.0},
            {"time": st, "value": 0.0},
            {"time": en, "value": 0.0},
            {"time": en + ramp, "value": 1.0},
        ]
    kf.sort(key=lambda x: x["time"])
    return {"id": _uuid(), "property_type": "volume", "keyframe_list": kf}


def _layout_ranges_us(layout_ranges: Any) -> List[tuple]:
    """B7 布局区间 [[start,end]...]（秒）→ µs 元组；空/非法容错。"""
    if not isinstance(layout_ranges, list):
        return []
    out = []
    for r in layout_ranges:
        try:
            st, en = float(r[0]), float(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        if en > st:
            out.append((int(st * US), int(en * US)))
    return out


def _apply_mute_keyframes(segments: List[Dict[str, Any]], mute_ranges_us: List[tuple]) -> None:
    """为片段挂互斥静音 volume 关键帧（绝对时间 µs，逐段裁剪到片段窗口）。

    对齐 r4 §2.13：common_keyframes=[{id, property_type:'volume',
    keyframe_list:[{time,value}]}]，每静音区间 4 帧 ±50ms 斜坡（_volume_keyframes）。
    """
    if not mute_ranges_us:
        return
    for seg in segments:
        tt = seg.get("target_timerange") or {}
        ts = int(tt.get("start") or 0)
        te = ts + int(tt.get("duration") or 0)
        clipped = []
        for st, en in mute_ranges_us:
            lo, hi = max(st, ts), min(en, te)
            if hi - lo > 0:
                clipped.append((lo, hi))
        if clipped:
            seg.setdefault("common_keyframes", []).append(_volume_keyframes(clipped))


# ---------------------------------------------------------------- 字幕拆句（与 SRT/草稿共用规则）
def split_text_to_subtitles(text: str, total_dur: float, min_dur: float = 0.6,
                            max_dur: float = 4.0) -> List[Dict[str, Any]]:
    """按中文标点拆句，再按字符比例分时长（r4 §3.4 实测：0.89s 细分按 len(text) 比例）。"""
    import re
    text = (text or "").strip()
    if not text or total_dur <= 0:
        return []
    # 先按强标点切，再按弱标点补，超长按字符预算继续拆
    parts: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？；":
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    # 弱标点细分超长句
    refined: List[str] = []
    for p in parts:
        if len(p) <= 12:
            refined.append(p)
            continue
        sub = ""
        for ch in p:
            sub += ch
            if ch in "，、：" and len(sub) >= 4:
                refined.append(sub.strip())
                sub = ""
        if sub.strip():
            refined.append(sub.strip())
    if not refined:
        refined = [text]
    chars = [len(r) for r in refined]
    total_chars = max(sum(chars), 1)
    out = []
    acc = 0.0
    for i, (part, c) in enumerate(zip(refined, chars)):
        d = total_dur * c / total_chars
        if i == len(refined) - 1:
            d = max(total_dur - acc, 0.05)
        d = max(min(d, max_dur), 0.2)
        out.append({"start": round(acc, 3), "end": round(min(acc + d, total_dur), 3), "text": part})
        acc += d
    return out


# ---------------------------------------------------------------- 主构建
class DraftModel:
    """剪映草稿构建模型（节点层归一化后传入）。"""

    def __init__(self, *, video_path: str, video_name: str, video_width: int, video_height: int,
                 video_duration: float, canvas: Dict[str, Any], fps: float,
                 export_target: str = "jianying", draft_name: str, draft_dir: str,
                 subtitle_style_name: str = "经典-白字黑边", font_size: Optional[float] = None,
                 enable_subtitle: bool = True):
        self.video_path = video_path
        self.video_name = video_name
        self.video_width = video_width
        self.video_height = video_height
        self.video_duration = video_duration
        self.canvas = canvas
        self.fps = fps
        self.export_target = export_target
        self.draft_name = draft_name
        self.draft_dir = draft_dir
        self.subtitle_style_name = subtitle_style_name
        self.font_size = font_size
        self.enable_subtitle = enable_subtitle
        # 时间线条目：{segment_index, text, is_ost, src_start_us, src_dur_us,
        #             tgt_start_us, tgt_dur_us, video_path, volume, speed}
        self.entries: List[Dict[str, Any]] = []
        # 配音条目：{audio_path, name, duration_us, tgt_start_us, fade_us}
        self.voice_items: List[Dict[str, Any]] = []
        # 字幕条目：{start, end, text}（秒）
        self.subtitles: List[Dict[str, Any]] = []
        # 花字条目（G3）：{start, end, text}（秒；写入文本轨，样式与字幕区分）
        self.flower_items: List[Dict[str, Any]] = []
        # 遮盖原字幕（G10/TK）：底部字幕区叠黑条
        self.cover_original_subtitle: bool = False
        # BGM：{path, name, volume}（整段铺满）
        self.bgm_items: List[Dict[str, Any]] = []
        self.mute_ranges_us: List[tuple] = []  # 合并音轨上的静音区间
        # B7 混音布局（core/mixed_audio_layout 输出）：{narration_mute_ranges, original_mute_ranges, ramp_ms}
        self.layout: Optional[Dict[str, Any]] = None


def build_draft_content(m: DraftModel) -> Dict[str, Any]:
    """构造 draft_content.json（27 顶层键）。"""
    mats_videos: List[Dict[str, Any]] = []
    mats_speeds: List[Dict[str, Any]] = []
    mats_audios: List[Dict[str, Any]] = []
    mats_texts: List[Dict[str, Any]] = []
    mats_anims: List[Dict[str, Any]] = []

    video_track: List[Dict[str, Any]] = []
    ost_track: List[Dict[str, Any]] = []
    audio_track: List[Dict[str, Any]] = []
    text_track: List[Dict[str, Any]] = []
    bgm_track: List[Dict[str, Any]] = []

    video_dur_us = int(m.video_duration * US)
    is_portrait = m.video_height > m.video_width

    # ---- 视频轨（主轨道 + 原片插播） ----
    for e in m.entries:
        vpath = e.get("video_path") or m.video_path
        vname = os.path.basename(vpath)
        src_dur = int(e["src_dur_us"])
        tgt_dur = int(e["tgt_dur_us"])
        speed = float(e.get("speed") or 0.0)
        if speed <= 0:
            speed = src_dur / tgt_dur if tgt_dur > 0 else 1.0
        vm = _video_material(vpath, vname, max(src_dur, video_dur_us), m.video_width, m.video_height)
        mats_videos.append(vm)
        sm = _speed_material(speed)
        mats_speeds.append(sm)
        seg = _video_segment(vm["id"], int(e["src_start_us"]), src_dur,
                             int(e["tgt_start_us"]), tgt_dur, speed,
                             volume=float(e.get("volume", 1.0)))
        video_track.append(seg)
        if e.get("is_ost"):
            # 原片插播轨：同一素材 + 原声（若主轨该段已静音）
            ost_seg = _video_segment(vm["id"], int(e["src_start_us"]), src_dur,
                                     int(e["tgt_start_us"]), tgt_dur, speed,
                                     volume=1.0)
            ost_track.append(ost_seg)

    # ---- 配音音频轨 ----
    for a in m.voice_items:
        am = _audio_material(a["audio_path"], a["name"], int(a["duration_us"]))
        mats_audios.append(am)
        audio_track.append(_audio_segment(am["id"], int(a["duration_us"]),
                                          int(a["tgt_start_us"]), int(a.get("fade_us", 50000))))

    # ---- BGM 轨 ----
    for b in m.bgm_items:
        bm = _audio_material(b["path"], b["name"], int(b.get("duration_us", 0)))
        mats_audios.append(bm)
        bgm_seg = _audio_segment(bm["id"], int(b["duration_us"]), 0,
                                 fade_us=int(b.get("fade_us", 80000)))
        bgm_seg["volume"] = float(b.get("volume", 0.38))
        bgm_track.append(bgm_seg)

    # ---- B7 混音布局：互斥静音 volume 关键帧（±50ms 斜坡，r4 §2.13） ----
    if isinstance(m.layout, dict):
        # 原声播 → 解说轨静音（narration_mute_ranges 落在解说音频段上）
        n_mutes = _layout_ranges_us(m.layout.get("narration_mute_ranges"))
        if n_mutes:
            _apply_mute_keyframes(audio_track, n_mutes)
        # 解说播 → 原声轨静音（original_mute_ranges 落在视频段内嵌原声上）
        o_mutes = _layout_ranges_us(m.layout.get("original_mute_ranges"))
        if o_mutes:
            _apply_mute_keyframes(video_track, o_mutes)
            _apply_mute_keyframes(ost_track, o_mutes)

    # ---- 字幕轨 ----
    style = _resolve_style(m.subtitle_style_name)
    size = float(m.font_size or (9.0 if is_portrait else 6.0))
    # G10：遮盖原字幕黑条（先于字幕写入，字幕渲染在其上层）
    if m.cover_original_subtitle:
        _total_us = int(m.video_duration * US)
        cover_style = {
            "kind": "blackbg",
            "fill_rgb": [0.0, 0.0, 0.0],
            "stroke_rgb": None, "stroke_w": 0.0,
            "background_alpha": 0.95, "background_color": "#000000",
            "text_color": "#000000", "border_color": "", "border_width": 0.0,
        }
        ctm = _text_material("", cover_style, 1.0, is_portrait)
        ctm["background_alpha"] = 0.95
        ctm["background_height"] = 0.16
        ctm["background_width"] = 0.9
        ctm["background_horizontal_offset"] = 0.0
        ctm["background_vertical_offset"] = 0.0
        canim = _anim_material()
        mats_texts.append(ctm)
        mats_anims.append(canim)
        cseg = _text_segment(ctm["id"], canim["id"], 0, _total_us, is_portrait)
        cseg["clip"]["transform"]["y"] = 0.8 if is_portrait else -0.8
        cseg["render_index"] = 10900  # 低于字幕(11000)，字幕在上层
        text_track.append(cseg)
    for sub in m.subtitles:
        st = int(float(sub["start"]) * US)
        du = int((float(sub["end"]) - float(sub["start"])) * US)
        if du <= 0:
            continue
        tm = _text_material(sub["text"], style, size, is_portrait)
        mats_texts.append(tm)
        anim = _anim_material()
        mats_anims.append(anim)
        text_track.append(_text_segment(tm["id"], anim["id"], st, du, is_portrait))

    # ---- G3：花字轨（黄色高亮样式，位置偏中上） ----
    for fl in m.flower_items:
        st = int(float(fl["start"]) * US)
        du = int((float(fl["end"]) - float(fl["start"])) * US)
        if du <= 0:
            continue
        fstyle = dict(style)
        fstyle.update({"fill_rgb": [1.0, 0.8706, 0.0], "text_color": "#FFDE00",
                       "stroke_rgb": [0.0, 0.0, 0.0], "stroke_w": 0.08,
                       "border_color": "#000000", "border_width": 0.08})
        ftm = _text_material(fl["text"], fstyle, size + 2.0, is_portrait)
        ftm["background_alpha"] = 0.0
        mats_texts.append(ftm)
        fanim = _anim_material()
        mats_anims.append(fanim)
        fseg = _text_segment(ftm["id"], fanim["id"], st, du, is_portrait)
        fseg["clip"]["transform"]["y"] = 0.2 if is_portrait else -0.4
        text_track.append(fseg)

    # ---- materials（41 类） ----
    materials: Dict[str, Any] = {
        k: [] for k in (
            "audio_balances", "audio_effects", "audio_fades", "audios", "beats", "canvases",
            "chromas", "color_curves", "digital_humans", "drafts", "effects", "flowers",
            "green_screens", "handwrites", "hsl", "images", "log_color_wheels", "loudnesses",
            "manual_deformations", "masks", "material_animations", "material_colors",
            "placeholders", "plugin_effects", "primary_color_wheels", "realtime_denoises",
            "shapes", "smart_crops", "smart_relights", "sound_channel_mappings", "speeds",
            "stickers", "tail_leaders", "text_templates", "texts", "transitions",
            "video_effects", "video_trackings", "videos", "vocal_beautifys", "vocal_separations")
    }
    materials["videos"] = mats_videos
    materials["speeds"] = mats_speeds
    materials["audios"] = mats_audios
    materials["texts"] = mats_texts
    materials["material_animations"] = mats_anims

    # ---- tracks ----
    tracks: List[Dict[str, Any]] = []
    if video_track:
        tracks.append({"attribute": 0, "flag": 0, "id": _uuid(), "is_default_name": True,
                       "name": "主轨道" if (m.voice_items or m.bgm_items) else "", "segments": video_track,
                       "type": "video"})
    if ost_track:
        tracks.append({"attribute": 0, "flag": 0, "id": _uuid(), "is_default_name": True,
                       "name": "原片插播", "segments": ost_track, "type": "video"})
    if audio_track:
        tracks.append({"attribute": 0, "flag": 0, "id": _uuid(), "is_default_name": True,
                       "name": "TTS解说" if not m.bgm_items else "解说", "segments": audio_track,
                       "type": "audio"})
    if bgm_track:
        tracks.append({"attribute": 0, "flag": 0, "id": _uuid(), "is_default_name": True,
                       "name": "BGM", "segments": bgm_track, "type": "audio"})
    if text_track:
        tracks.append({"attribute": 0, "flag": 1, "id": _uuid(), "is_default_name": True,
                       "name": "", "segments": text_track, "type": "text"})

    total_us = max(int(m.video_duration * US), 0)
    now = _now_us()
    prof = _PLATFORM_PROFILES.get(m.export_target, _PLATFORM_PROFILES["jianying"])
    plat = _platform_block(m.export_target)
    return {
        "canvas_config": m.canvas,
        "color_space": 0,
        "config": {
            "adjust_max_index": 1, "attachment_info": [], "combination_max_index": 1,
            "export_range": None, "extract_audio_last_index": 1, "lyrics_recognition_id": "",
            "lyrics_sync": True, "lyrics_taskinfo": [], "maintrack_adsorb": True,
            "material_save_mode": 0, "original_sound_last_index": 1,
            "record_audio_last_index": 1, "sticker_max_index": 1,
            "subtitle_recognition_id": "", "subtitle_sync": True, "subtitle_taskinfo": [],
            "system_font_list": [], "video_mute": False, "zoom_info_params": None,
        },
        "cover": None,
        "create_time": now,
        "duration": total_us,
        "extra_info": None,
        "fps": float(m.fps),
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": _uuid(),
        "keyframe_graph_list": [],
        "keyframes": {"adjusts": [], "audios": [], "effects": [], "filters": [],
                      "handwrites": [], "stickers": [], "texts": [], "videos": []},
        "last_modified_platform": plat,
        "materials": materials,
        "mutable_config": None,
        "name": "",
        "new_version": prof["new_version"],
        "platform": plat,
        "relationships": [],
        "render_index_track_mode_on": False,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "tracks": tracks,
        "update_time": now,
        "version": 360000,
    }


def build_draft_meta_info(m: DraftModel) -> Dict[str, Any]:
    """构造 draft_meta_info.json（draft_materials 素材索引）。"""
    now = _now_us()
    prof = _PLATFORM_PROFILES.get(m.export_target, _PLATFORM_PROFILES["jianying"])
    seen: Dict[str, bool] = {}
    dm_values: List[Dict[str, Any]] = []
    for e in m.entries:
        vpath = e.get("video_path") or m.video_path
        key = vpath.replace("\\", "/")
        if key in seen:
            continue
        seen[key] = True
        src_dur = int(e["src_dur_us"])
        dm_values.append({
            "create_time": int(time.time()), "duration": max(src_dur, int(m.video_duration * US)),
            "extra_info": m.video_name, "file_Path": key,
            "height": m.video_height, "id": _uuid(), "import_time": int(time.time()),
            "import_time_ms": int(time.time() * US), "item_source": 1, "md5": "",
            "metetype": "video",
            "roughcut_time_range": {"duration": max(src_dur, int(m.video_duration * US)), "start": 0},
            "sub_time_range": {"duration": -1, "start": -1}, "type": 0, "width": m.video_width,
        })
    groups = []
    for t in (0, 1, 2, 3, 6, 7, 8):
        groups.append({"type": t, "value": dm_values if t == 0 else []})
    draft_fold = m.draft_dir.replace("\\", "/")
    draft_root = os.path.dirname(m.draft_dir).replace("\\", "/")
    return {
        "cloud_package_completed_time": "", "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False, "draft_cloud_materials": [],
        "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "", "draft_deeplink_url": "",
        "draft_enterprise_info": {"draft_enterprise_extra": "", "draft_enterprise_id": "",
                                  "draft_enterprise_name": "", "enterprise_material": []},
        "draft_fold_path": draft_fold, "draft_id": _uuid(),
        "draft_is_article_video_draft": False, "draft_is_from_deeplink": "false",
        "draft_materials": groups, "draft_materials_copied_info": [],
        "draft_name": m.draft_name, "draft_new_version": prof["new_version"],
        "draft_removable_storage_device": "", "draft_root_path": draft_root,
        "draft_segment_extra_info": [], "draft_timeline_materials_size_": 0,
        "tm_draft_cloud_completed": "", "tm_draft_cloud_modified": 0,
        "tm_draft_create": now, "tm_draft_modified": now, "tm_draft_removed": 0,
        "tm_duration": int(m.video_duration * US),
    }


def write_draft_dir(m: DraftModel) -> str:
    """写盘草稿目录（8 文件）。返回草稿目录绝对路径。"""
    os.makedirs(m.draft_dir, exist_ok=True)
    content = build_draft_content(m)
    meta = build_draft_meta_info(m)
    with open(os.path.join(m.draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    with open(os.path.join(m.draft_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    # 占位文件
    empties = {
        "draft_settings": b"",
        "attachment_draft_enterprise_info.json": b"{}",
        "attachment_pc_common.json": b"{}",
        "draft_agency_config.json": b"{}",
        "draft_virtual_store.json": b"{}",
    }
    for fn, content_bytes in empties.items():
        with open(os.path.join(m.draft_dir, fn), "wb") as f:
            f.write(content_bytes)
    return m.draft_dir


# ---------------------------------------------------------------- 便捷归一化（供节点复用）
def normalize_timeline_entries(segments: List[Dict[str, Any]], video_path: str,
                               voice_map: Optional[Dict[int, Dict[str, Any]]] = None,
                               total_dur: Optional[float] = None) -> List[Dict[str, Any]]:
    """把 split_cam/时间线输入归一化为 DraftModel.entries。

    segments 每项支持：{index, segment_index, text, OST, start, end, duration,
      tts_duration, speed, src_start, src_end, global_start, global_end, video, ...}
    voice_map: {segment_index: {path, duration}} 配音项（目标时长优先取 tts）。
    """
    entries: List[Dict[str, Any]] = []
    tgt_cursor = 0.0
    for raw in segments:
        seg = dict(raw)
        idx = int(seg.get("segment_index", seg.get("index", len(entries))))
        is_ost = bool(seg.get("OST") or seg.get("ost") or seg.get("is_ost"))
        src_start = float(seg.get("src_start", seg.get("start", 0.0)))
        src_end = float(seg.get("src_end", seg.get("end", src_start + 1.0)))
        src_dur = float(seg.get("clip_duration", src_end - src_start))
        if src_dur <= 0:
            src_dur = max(src_end - src_start, 1.0)
        tts_dur = float(seg.get("tts_duration", 0.0) or 0.0)
        target_dur = 0.0
        if is_ost:
            target_dur = src_dur
        elif voice_map and idx in voice_map:
            target_dur = float(voice_map[idx].get("duration", 0.0) or 0.0)
        elif tts_dur > 0:
            target_dur = tts_dur
        elif seg.get("duration") is not None:
            target_dur = float(seg.get("duration"))
        else:
            target_dur = max(src_dur, 1.0)
        if target_dur <= 0:
            target_dur = max(src_dur, 1.0)
        seg_video = str(seg.get("video") or seg.get("video_path") or video_path)
        entries.append({
            "segment_index": idx,
            "text": str(seg.get("text") or seg.get("roast_text") or ""),
            "is_ost": is_ost,
            "src_start_us": int(src_start * US),
            "src_dur_us": int(src_dur * US),
            "tgt_start_us": int(tgt_cursor * US),
            "tgt_dur_us": int(target_dur * US),
            "video_path": seg_video,
            "speed": float(seg.get("speed", 0.0) or 0.0),
            "volume": 0.0 if (not is_ost and voice_map is not None) else 1.0,
        })
        tgt_cursor += target_dur
    if total_dur is not None and entries:
        # 末段对齐总时长（剪映 duration 语义）
        diff = total_dur - tgt_cursor
        if diff > 0.05:
            last = entries[-1]
            last["tgt_dur_us"] += int(diff * US)
    return entries


def build_voice_items(voice_map: Dict[int, Dict[str, Any]],
                      entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """配音项：{audio_path, name, duration_us, tgt_start_us}。"""
    items = []
    for e in entries:
        if e["is_ost"]:
            continue
        v = voice_map.get(e["segment_index"])
        if not v or not v.get("path"):
            continue
        dur_us = int(float(v.get("duration", 0.0) or 0.0) * US) or e["tgt_dur_us"]
        items.append({
            "audio_path": v["path"],
            "name": os.path.basename(v["path"]),
            "duration_us": dur_us,
            "tgt_start_us": e["tgt_start_us"],
        })
    return items


# ============================================================================
# 第 4 模式：精选复刻（D4）—— 干净重实现 generators.curated_remake_draft
#
# 依据：_recon/r4_generators_curated_remake_draft_introspect.txt（模块常量与文档语义）
#   + _recon/r4_run/drafts/r4_curated/draft_content.json（真实产物结构）。
#   保留既有 3 模式（普通/TTS/BGM）不动，仅新增 compare / remake_only 两个子模式。
#
# mode="compare"（默认，对比效果）：
#   画布 (cW+rW) x cH：左半放整段解说(宽 cW)，右半放匹配原片(缩放到高 cH 后宽 rW)，
#   两者等高各居其半、互不遮挡；另加两条文字轨「原解说」(左)/「复刻后」(右)，
#   黄字(#ffde00)+黑描边 LABEL 常量，置于顶部。
# mode="remake_only"（仅复刻效果）：
#   画布 W x H（原片分辨率）；单视频轨 = 逐镜头匹配到的原片片段，按解说节奏铺满。
# 剪映 transform.x 单位 = 半画布宽，故 ±0.5 把各 W 宽素材居中放在左/右半屏。
# R7 修复：commentary wav 一律作为 audio 轨素材写入，duration 取真实时长（>0），
#   绝不把 wav 当 video material（原版 duration=0 无音频轨缺陷）。
# ============================================================================
_REMAKE_FILL_RGB = [1.0, 0.8705882430076599, 0.0]        # #ffde00 黄字
_REMAKE_STROKE_RGB = [0.0, 0.0, 0.0]                      # 黑描边
_REMAKE_STROKE_W = 0.07999999821186066
_REMAKE_LABEL_LEFT_POS = (-0.5135135135135135, 0.72)      # (x, y)
_REMAKE_LABEL_RIGHT_POS = (0.5011261261261262, 0.72)
_REMAKE_LABEL_SCALE = 0.8413899931146691
_REMAKE_SUSPECT_POS = (0.88, 0.86)
_REMAKE_SUSPECT_SCALE = 0.5
_REMAKE_TEXT_SIZE = 15


def normalize_remake_matches(matches: List[Dict[str, Any]],
                             original_path: str = "") -> List[Dict[str, Any]]:
    """S2-T1 匹配表 → D4 内部条目（兼容 S2-T1 user/jsm_remake_match 输出与探针字典）。

    输入每项支持：{start,end(解说时间), ref_start,ref_end, ref_path,ref_name,ref_source,
    speed, suspect,...}（S2-T1 输出）或 {q_start,q_end,src_start,src_end,ref_*,...}（探针）。
    输出每项：{commentary_start_us, commentary_dur_us, ref_start_us, ref_dur_us,
    ref_path, ref_name, ref_source, speed, suspect}
    """
    out: List[Dict[str, Any]] = []
    for m in matches or []:
        if not isinstance(m, dict):
            continue
        # 解说时间：S2-T1 → start/end；探针 → q_start/q_end
        qs = float(m.get("start", m.get("q_start", 0.0)) or 0.0)
        qe = float(m.get("end", m.get("q_end", qs + 1.0)) or (qs + 1.0))
        # 原片时间：S2-T1 → ref_start/ref_end；探针 → src_start/src_end 或 ref_start/ref_end
        rs = float(m.get("ref_start", m.get("src_start", m.get("start", 0.0))) or 0.0)
        re_ = float(m.get("ref_end", m.get("src_end", m.get("end", rs + 1.0))) or (rs + 1.0))
        if re_ < rs:
            rs, re_ = re_, rs
        if qe <= qs or re_ <= rs:
            continue
        rp = str(m.get("ref_path") or m.get("original_path") or original_path or "")
        rn = str(m.get("ref_name") or os.path.basename(rp) if rp else "")
        spd = float(m.get("speed", 0.0) or 0.0)
        out.append({
            "commentary_start_us": int(qs * US),
            "commentary_dur_us": int((qe - qs) * US),
            "ref_start_us": int(rs * US),
            "ref_dur_us": int((re_ - rs) * US),
            "ref_path": rp,
            "ref_name": rn,
            "ref_source": str(m.get("ref_source", "") or ""),
            "speed": spd,
            "suspect": bool(m.get("suspect")),
            "text": str(m.get("text", "") or ""),
        })
    return out


def _remake_label_material(text: str) -> Dict[str, Any]:
    """黄字 #ffde00 + 黑描边 LABEL 文字材料（对齐 r4_curated 产物 + 模块常量）。"""
    content = _rich_content(text, {
        "fill_rgb": _REMAKE_FILL_RGB, "stroke_rgb": _REMAKE_STROKE_RGB,
        "stroke_w": _REMAKE_STROKE_W,
    }, _REMAKE_TEXT_SIZE)
    mat = _text_material(text, {
        "fill_rgb": _REMAKE_FILL_RGB, "stroke_rgb": _REMAKE_STROKE_RGB,
        "stroke_w": _REMAKE_STROKE_W, "text_color": "#ffde00",
        "border_color": "#000000", "border_width": _REMAKE_STROKE_W,
        "background_alpha": 0.0, "background_color": "",
    }, _REMAKE_TEXT_SIZE, is_portrait=False)
    mat["type"] = "text"
    mat["text_color"] = "#ffde00"
    mat["border_color"] = "#000000"
    mat["border_width"] = _REMAKE_STROKE_W
    mat["font_size"] = float(_REMAKE_TEXT_SIZE)
    mat["text_size"] = 30
    mat["check_flag"] = 15
    mat["content"] = content
    mat["use_effect_default_color"] = True
    return mat


def _remake_label_segment(mat_id: str, pos: tuple, scale: float,
                          tgt_start_us: int, tgt_dur_us: int,
                          render_index: int) -> Dict[str, Any]:
    """LABEL 文字段：pos=(x,y)、scale 常量、渲染层 render_index（14001/14004）。"""
    seg = _base_segment()
    seg["material_id"] = mat_id
    seg["render_index"] = render_index
    seg["source_timerange"] = None
    seg["target_timerange"] = {"duration": tgt_dur_us, "start": tgt_start_us}
    seg["clip"]["transform"]["x"] = float(pos[0])
    seg["clip"]["transform"]["y"] = float(pos[1])
    seg["clip"]["scale"] = {"x": scale, "y": scale}
    seg["uniform_scale"] = {"on": True, "value": scale}
    seg["enable_adjust"] = False
    seg["enable_color_curves"] = True
    seg["hdr_settings"] = None
    seg["template_scene"] = "default"
    return seg


def build_remake_content(m: DraftModel) -> Dict[str, Any]:
    """第 4 模式 draft_content（compare / remake_only）。

    m 需附加 D4 字段（user_jsm_remake_draft 节点填充，既有 3 模式不受影响）：
      remake_mode: "compare" | "remake_only"
      remake_matches: normalize_remake_matches() 输出
      remake_commentary_w: int, remake_commentary_h: int   # 解说视频宽高
      remake_original_w: int, remake_original_h: int       # 原片宽高
      remake_original_path: str
      remake_commentary_audio: {path, name, duration_us}   # R7：真实时长 >0
      remake_labels: (str, str) = ("原解说", "复刻后")
      remake_orig_volume: float = 0.0
      remake_total_us: int                                 # 画布总时长
    """
    import math
    mode = str(getattr(m, "remake_mode", "compare") or "compare")
    # 调用方（user_jsm_remake_draft 节点）负责把匹配表归一化为 D4 条目；
    # 此处直接消费已归一化条目（避免二次归一化污染时间值）
    matches = list(getattr(m, "remake_matches", None) or [])
    cW = int(getattr(m, "remake_commentary_w", 1920) or 1920)
    cH = int(getattr(m, "remake_commentary_h", 1080) or 1080)
    oW = int(getattr(m, "remake_original_w", 1920) or 1920)
    oH = int(getattr(m, "remake_original_h", 1080) or 1080)
    orig_path = str(getattr(m, "remake_original_path", "") or "")
    commentary_audio = getattr(m, "remake_commentary_audio", None) or {}
    labels = tuple(getattr(m, "remake_labels", None) or ("原解说", "复刻后"))
    orig_volume = float(getattr(m, "remake_orig_volume", 0.0) or 0.0)
    total_us = int(getattr(m, "remake_total_us", 0) or 0)

    mats_videos: List[Dict[str, Any]] = []
    mats_audios: List[Dict[str, Any]] = []
    mats_texts: List[Dict[str, Any]] = []
    mats_anims: List[Dict[str, Any]] = []
    video_track: List[Dict[str, Any]] = []
    overlay_track: List[Dict[str, Any]] = []
    audio_track: List[Dict[str, Any]] = []
    text_track: List[Dict[str, Any]] = []

    if mode == "compare":
        # ---- 画布：左半解说(cW) + 右半原片(缩放到高 cH 后宽 rW)，等高各居其半 ----
        rW = int(round(oW * cH / max(oH, 1)))
        canvas_w = cW + rW
        canvas_h = cH
        left_x = -rW / canvas_w          # 左素材中心：-rW/canvasW（见 r4_curated 实测）
        right_x = cW / canvas_w          # 右素材中心：cW/canvasW

        # 主轨（左半）：整段解说视频
        comm_dur = max(total_us, int(getattr(m, "video_duration", 0.0) * US))
        vm = _video_material(getattr(m, "video_path", ""),
                             getattr(m, "video_name", "解说"), comm_dur, cW, cH)
        mats_videos.append(vm)
        seg = _video_segment(vm["id"], 0, comm_dur, 0, comm_dur, 1.0, volume=1.0)
        seg["clip"]["transform"]["x"] = left_x
        video_track.append(seg)

        # 覆盖轨（右半）：逐匹配原片片段（按原片自身时间轴 1x，target=ref 时间）
        ovm = None
        if orig_path:
            orig_dur = max(int(getattr(m, "remake_total_us", 0) or 0),
                           int(float(getattr(m, "remake_original_dur", 0.0) or 0.0) * US))
            ovm = _video_material(orig_path, os.path.basename(orig_path),
                                  orig_dur, oW, oH)
            mats_videos.append(ovm)
        for mc in matches:
            if ovm is None:
                continue
            ref_src = int(mc["ref_start_us"])
            ref_du = int(mc["ref_dur_us"])
            seg = _video_segment(ovm["id"], ref_src, ref_du, ref_src, ref_du, 1.0,
                                 volume=orig_volume)
            seg["clip"]["transform"]["x"] = right_x
            seg["speed"] = 1.0
            if float(mc.get("speed") or 0.0) > 0:
                seg["speed"] = float(mc["speed"])
            overlay_track.append(seg)

        # 两条 LABEL 文字轨（黄字黑边常量）：x 对齐各自视频半区（对齐 r4_curated 实测产物），
        # scale=_LABEL_SCALE、y=0.72 为常量；render_index 14001/14004
        for i, (label_txt, pos, r_idx) in enumerate((
                (labels[0], (left_x, _REMAKE_LABEL_LEFT_POS[1]), 14001),
                (labels[1], (right_x, _REMAKE_LABEL_RIGHT_POS[1]), 14004))):
            tm = _remake_label_material(label_txt)
            mats_texts.append(tm)
            anim = _anim_material()
            mats_anims.append(anim)
            text_track.append(_remake_label_segment(
                tm["id"], pos, _REMAKE_LABEL_SCALE, 0, comm_dur, r_idx))
    else:  # remake_only：画布 = 原片 WxH；单视频轨 = 逐镜头原片片段按解说节奏铺满
        canvas_w, canvas_h = oW, oH
        ovm = None
        if orig_path:
            ovm = _video_material(orig_path, os.path.basename(orig_path),
                                  total_us, oW, oH)
            mats_videos.append(ovm)
        for mc in matches:
            if ovm is None:
                continue
            tgt_st = int(mc["commentary_start_us"])
            tgt_du = int(mc["commentary_dur_us"])
            ref_src = int(mc["ref_start_us"])
            ref_du = int(mc["ref_dur_us"])
            spd = float(mc.get("speed") or 0.0)
            if spd <= 0:
                spd = ref_du / tgt_du if tgt_du > 0 else 1.0
            seg = _video_segment(ovm["id"], ref_src, ref_du, tgt_st, tgt_du, spd,
                                 volume=orig_volume if orig_volume else 1.0)
            seg["clip"]["transform"]["x"] = 0.0
            video_track.append(seg)
        comm_dur = total_us

    # ---- R7：commentary wav 作为 audio 轨（真实时长 >0，绝不写 0） ----
    if commentary_audio and commentary_audio.get("path"):
        du = int(commentary_audio.get("duration_us", 0) or 0)
        if du <= 0:
            du = comm_dur  # 占位保底：仍 >0（节点层已用 ffprobe 实测）
        am = _audio_material(str(commentary_audio["path"]),
                             str(commentary_audio.get("name") or "解说音频"), du)
        mats_audios.append(am)
        audio_track.append(_audio_segment(am["id"], du, 0, fade_us=50000))

    # ---- 组装（对齐 build_draft_content 骨架） ----
    materials: Dict[str, Any] = {
        k: [] for k in (
            "audio_balances", "audio_effects", "audio_fades", "audios", "beats", "canvases",
            "chromas", "color_curves", "digital_humans", "drafts", "effects", "flowers",
            "green_screens", "handwrites", "hsl", "images", "log_color_wheels", "loudnesses",
            "manual_deformations", "masks", "material_animations", "material_colors",
            "placeholders", "plugin_effects", "primary_color_wheels", "realtime_denoises",
            "shapes", "smart_crops", "smart_relights", "sound_channel_mappings", "speeds",
            "stickers", "tail_leaders", "text_templates", "texts", "transitions",
            "video_effects", "video_trackings", "videos", "vocal_beautifys", "vocal_separations")
    }
    materials["videos"] = mats_videos
    materials["audios"] = mats_audios
    materials["texts"] = mats_texts
    materials["material_animations"] = mats_anims

    tracks: List[Dict[str, Any]] = []
    if video_track:
        tracks.append({"attribute": 0, "flag": 0, "id": _uuid(), "is_default_name": True,
                       "name": "", "segments": video_track, "type": "video"})
    if overlay_track:
        tracks.append({"attribute": 0, "flag": 0, "id": _uuid(), "is_default_name": True,
                       "name": "覆盖视频", "segments": overlay_track, "type": "video"})
    if audio_track:
        tracks.append({"attribute": 0, "flag": 0, "id": _uuid(), "is_default_name": True,
                       "name": "解说音频", "segments": audio_track, "type": "audio"})
    if text_track:
        # compare 两条 LABEL 分属两个文字轨（render_index 14001/14004，对齐产物）
        for tseg in text_track:
            tracks.append({"attribute": 0, "flag": 1, "id": _uuid(),
                           "is_default_name": True, "name": "",
                           "segments": [tseg], "type": "text"})

    now = _now_us()
    prof = _PLATFORM_PROFILES.get(m.export_target, _PLATFORM_PROFILES["jianying"])
    plat = _platform_block(m.export_target)
    # 平台校验器字段补齐（draft_name 顶层 + 片段 duration=目标时长；Jianying 容忍附加键）
    for tr in tracks:
        for s in tr.get("segments") or []:
            tt = s.get("target_timerange") or {}
            if s.get("duration") is None:
                s["duration"] = tt.get("duration", 0)
    return {
        "canvas_config": {"height": canvas_h, "ratio": "custom", "width": canvas_w},
        "color_space": 0,
        "config": {
            "adjust_max_index": 1, "attachment_info": [], "combination_max_index": 1,
            "export_range": None, "extract_audio_last_index": 1, "lyrics_recognition_id": "",
            "lyrics_sync": True, "lyrics_taskinfo": [], "maintrack_adsorb": True,
            "material_save_mode": 0, "original_sound_last_index": 1,
            "record_audio_last_index": 1, "sticker_max_index": 1,
            "subtitle_recognition_id": "", "subtitle_sync": True, "subtitle_taskinfo": [],
            "system_font_list": [], "video_mute": False, "zoom_info_params": None,
        },
        "cover": None,
        "create_time": now,
        "duration": comm_dur,
        "draft_name": str(getattr(m, "draft_name", "") or ""),
        "extra_info": None,
        "fps": float(getattr(m, "fps", 30.0)),
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": _uuid(),
        "keyframe_graph_list": [],
        "keyframes": {"adjusts": [], "audios": [], "effects": [], "filters": [],
                      "handwrites": [], "stickers": [], "texts": [], "videos": []},
        "last_modified_platform": plat,
        "materials": materials,
        "mutable_config": None,
        "name": "",
        "new_version": prof["new_version"],
        "platform": plat,
        "relationships": [],
        "render_index_track_mode_on": False,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "tracks": tracks,
        "update_time": now,
        "version": 360000,
    }


def write_remake_draft(m: DraftModel) -> str:
    """第 4 模式草稿写盘（同一 8 文件结构）。返回草稿目录绝对路径。"""
    os.makedirs(m.draft_dir, exist_ok=True)
    content = build_remake_content(m)
    # meta：首素材按主视频（compare 左轨/remake_only 主轨）登记
    meta = build_draft_meta_info(m)
    with open(os.path.join(m.draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    with open(os.path.join(m.draft_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    empties = {
        "draft_settings": b"",
        "attachment_draft_enterprise_info.json": b"{}",
        "attachment_pc_common.json": b"{}",
        "draft_agency_config.json": b"{}",
        "draft_virtual_store.json": b"{}",
    }
    for fn, content_bytes in empties.items():
        with open(os.path.join(m.draft_dir, fn), "wb") as f:
            f.write(content_bytes)
    return m.draft_dir
