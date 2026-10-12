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
        # BGM：{path, name, volume}（整段铺满）
        self.bgm_items: List[Dict[str, Any]] = []
        self.mute_ranges_us: List[tuple] = []  # 合并音轨上的静音区间


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

    # ---- 字幕轨 ----
    style = _resolve_style(m.subtitle_style_name)
    size = float(m.font_size or (9.0 if is_portrait else 6.0))
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
