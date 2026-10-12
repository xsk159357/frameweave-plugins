# -*- coding: utf-8 -*-
"""项目输出打包生成器（对齐 r4 §4 实测产物结构，修复三项原版缺陷）。

- B3：video_split_info 使用 start_time/end_time 键（原版用 start/end 全输出 0）；
- B4：scenarios.txt 写入真实分段内容（原版只写一行 [视频名称：…]）；
- B5：all_des.txt 默认明文（原版写 AIIA_ENC 加密，读取端不兼容）。
"""
from __future__ import annotations
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe(v, default=""):
    try:
        return str(v) if v is not None else default
    except Exception:
        return default


def _fmt_des_range(start: float, end: float) -> str:
    return f"[{start:g}s -> {end:g}s]"


def build_split_cam(scenes: List[Dict[str, Any]], video_name: str) -> List[Dict[str, Any]]:
    """构造 split_cam.json（26 键/项，与 r4 §2.17 对齐）。"""
    out: List[Dict[str, Any]] = []
    ts = int(time.time())
    for i, s in enumerate(scenes):
        start = _num(s.get("start"))
        end = _num(s.get("end"), start + 1.0)
        dur = _num(s.get("duration"), end - start)
        if dur <= 0:
            dur = max(end - start, 1.0)
        is_ost = bool(s.get("OST") or s.get("ost") or s.get("is_ost"))
        tts = _num(s.get("tts_duration"))
        speed = _num(s.get("speed"), 1.0)
        text = _safe(s.get("text"))
        out.append({
            "audio": "", "end": end, "id": str(i), "org": 0,
            "start": start, "text": text, "roast_text": _safe(s.get("roast_text"), text),
            "video": video_name, "video_pic": f"/videoPic/{ts}-{i}.jpg",
            "LAY_NUM": 1, "LAY_INDEX": i, "LAY_INDEX_INIT": i, "role": "",
            "OST": 1 if is_ost else 0,
            "clip_duration": dur, "tts_duration": tts, "speed": speed,
            "need_freeze": False,
            "src_start": _num(s.get("src_start"), start),
            "src_end": _num(s.get("src_end"), end),
            "global_start": _num(s.get("global_start"), start),
            "global_end": _num(s.get("global_end"), end),
            "video_source": {}, "clips": [], "candidate_scenes": [],
        })
    return out


def build_video_split_info(scenes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """video_split_info/<视频名>.json：键名 start_time/end_time（B3 修复）。"""
    out = []
    for s in scenes:
        st = _num(s.get("src_start", s.get("global_start", s.get("start"))))
        en = _num(s.get("src_end", s.get("global_end", s.get("end"))))
        out.append({"start_time": st, "end_time": max(en, st)})
    return out


def _descriptions_map(descriptions: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """把描述输入归一化为 {start,end,text}。兼容字段：desc/description/text/lens/audio。"""
    rows = []
    for d in (descriptions or []):
        if not isinstance(d, dict):
            continue
        st = _num(d.get("start"), _num(d.get("global_start"), 0.0))
        en = _num(d.get("end"), st + 1.0)
        text = _safe(d.get("desc") or d.get("description") or d.get("text"))
        rows.append({"start": st, "end": en, "text": text})
    return rows


def render_all_des(scenes: List[Dict[str, Any]],
                   descriptions: Optional[List[Dict[str, Any]]] = None) -> str:
    """all_des.txt 明文内容：`[s -> e] 镜头类型 | 视觉描述 | 听觉信息`。"""
    desc_map = _descriptions_map(descriptions)
    lines = []
    for i, s in enumerate(scenes):
        st = _num(s.get("start"))
        en = _num(s.get("end"), st + 1.0)
        des = ""
        if i < len(desc_map):
            des = desc_map[i].get("text") or ""
        if not des:
            des = _safe(s.get("text"))
        # 描述字段拆分：镜头/视觉/听觉信息
        lens = _safe(s.get("lens", "")).strip()
        audio_info = _safe(s.get("audio_info", "")).strip()
        vis = des if not lens else ""
        cells = [x for x in (lens, vis, audio_info) if x]
        if not cells:
            cells = [_safe(s.get("text"))]
        lines.append(f"{_fmt_des_range(st, en)} {' | '.join(cells)}")
    return "\n".join(lines)


def render_scenarios(scenes: List[Dict[str, Any]], video_name: str,
                     descriptions: Optional[List[Dict[str, Any]]] = None) -> str:
    """scenarios.txt：首行视频名 + 真实分段内容（B4 修复）。"""
    lines = [f"[视频名称：{video_name}]"]
    desc_map = _descriptions_map(descriptions)
    for i, s in enumerate(scenes):
        st = _num(s.get("start"))
        en = _num(s.get("end"), st + 1.0)
        des = (desc_map[i].get("text") if i < len(desc_map) else "") or _safe(s.get("text"))
        text = _safe(s.get("text"))
        lines.append(f"{_fmt_des_range(st, en)} {des or text}")
    return "\n".join(lines)


def render_project_ini(project_dir: str, video_path: str, created_at: str,
                       settings: Optional[Dict[str, Any]] = None) -> str:
    """project_config.ini（[projectDate] + [settings] + [paths]）。"""
    settings = settings or {}
    name = os.path.basename(project_dir.rstrip("/\\")) or "project"
    rel = os.path.relpath(project_dir, os.path.dirname(project_dir)).replace("\\", "/")
    return (
        "[projectDate]\n"
        f"projectname = {name}\n"
        f"projectpath = {rel}\n"
        "projecttype = commentary-cut-slim\n"
        f"video_path = {json.dumps([video_path], ensure_ascii=False)}\n"
        f"created_at = {created_at}\n"
        "\n"
        "[settings]\n"
        f"scenariosforit = {_safe(settings.get('scenariosforit'), 'on')}\n"
        f"mod_select = {_safe(settings.get('mod_select'), 'mod_ultra')}\n"
        f"split_mod = {_safe(settings.get('split_mod'), 'split_three')}\n"
        f"video_language = {_safe(settings.get('video_language'), 'auto')}\n"
        f"is_org = {_safe(settings.get('is_org'), 'yesOrg')}\n"
        f"org_num = {_safe(settings.get('org_num'), '30')}\n"
        f"language_select = {_safe(settings.get('language_select'), '中文')}\n"
        f"person_select = {_safe(settings.get('person_select'), 'third')}\n"
        f"story_select = {_safe(settings.get('story_select'), 'cjbkkt_mod')}\n"
        f"numb_select = {_safe(settings.get('numb_select'), 'auto')}\n"
        f"audio_speed = {_safe(settings.get('audio_speed'), '4.5')}\n"
        "\n"
        "[paths]\n"
        "audio_path = audio/\n"
        "tts_path = videoBucket/tts/\n"
        "srt_path = srt/\n"
    )


def build_project_package(*, project_dir: str, video_path: str, video_name: str,
                          scenes: List[Dict[str, Any]],
                          subtitles: Optional[List[Dict[str, Any]]] = None,
                          descriptions: Optional[List[Dict[str, Any]]] = None,
                          settings: Optional[Dict[str, Any]] = None,
                          write_split_files: bool = True) -> Dict[str, Any]:
    """落盘项目包。返回全部产物路径。"""
    os.makedirs(project_dir, exist_ok=True)
    results: Dict[str, str] = {}
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # 1) project_config.ini
    ini_path = os.path.join(project_dir, "project_config.ini")
    with open(ini_path, "w", encoding="utf-8") as f:
        f.write(render_project_ini(project_dir, video_path, created_at, settings))
    results["project_config"] = ini_path

    # 2) split_cam.json（根 + videoBucket 副本）
    split_data = build_split_cam(scenes, video_name)
    sc_root = os.path.join(project_dir, "split_cam.json")
    with open(sc_root, "w", encoding="utf-8") as f:
        json.dump(split_data, f, ensure_ascii=False, indent=2)
    results["split_cam"] = sc_root

    video_bucket = os.path.join(project_dir, "videoBucket")
    os.makedirs(video_bucket, exist_ok=True)
    if write_split_files:
        sc_bucket = os.path.join(video_bucket, "split_cam.json")
        with open(sc_bucket, "w", encoding="utf-8") as f:
            json.dump(split_data, f, ensure_ascii=False, indent=2)
        results["video_bucket_split_cam"] = sc_bucket

    # 3) video_split_info/<视频名>.json（B3 键名）
    vsi_dir = os.path.join(project_dir, "video_split_info")
    os.makedirs(vsi_dir, exist_ok=True)
    vsi_path = os.path.join(vsi_dir, video_name + ".json")
    with open(vsi_path, "w", encoding="utf-8") as f:
        json.dump(build_video_split_info(scenes), f, ensure_ascii=False, indent=2)
    results["video_split_info"] = vsi_path

    # 4) videoBucketList.json
    vbl = [{"video": video_path.replace("\\", "/"),
            "videoDir": f"videoBucket/{video_name}", "videoList": []}]
    vbl_path = os.path.join(project_dir, "videoBucketList.json")
    with open(vbl_path, "w", encoding="utf-8") as f:
        json.dump(vbl, f, ensure_ascii=False, indent=2)
    results["video_bucket_list"] = vbl_path

    # 5) srt 三件套 + scenarios.txt
    srt_dir = os.path.join(project_dir, "srt")
    os.makedirs(srt_dir, exist_ok=True)
    rows = [dict(s) for s in (subtitles or []) if isinstance(s, dict) and s.get("text")]
    if rows:
        srt_path = os.path.join(srt_dir, video_name + ".srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(_build_srt_text(rows))
        results["srt_file"] = srt_path
        full_text = "".join(r.get("text", "") for r in rows)
        sjson = {"key": video_name, "text": full_text,
                 "sentence_info": [{"start": _num(r.get("start")), "end": _num(r.get("end")),
                                    "text": r.get("text", "")} for r in rows]}
        sjson_path = os.path.join(srt_dir, video_name + ".json")
        with open(sjson_path, "w", encoding="utf-8") as f:
            json.dump(sjson, f, ensure_ascii=False, indent=2)
        results["srt_json"] = sjson_path
        tc = [{"text": r.get("text", ""), "start": _num(r.get("start")),
               "end": _num(r.get("end")), "speaker": 0} for r in rows]
        tc_path = os.path.join(srt_dir, video_name + "_time_content.json")
        with open(tc_path, "w", encoding="utf-8") as f:
            json.dump(tc, f, ensure_ascii=False, indent=2)
        results["time_content"] = tc_path

    # 6) scenarios.txt（B4：真实分段内容）
    sc_text = render_scenarios(scenes, video_name, descriptions)
    scen_path = os.path.join(srt_dir, "scenarios.txt")
    with open(scen_path, "w", encoding="utf-8") as f:
        f.write(sc_text)
    results["scenarios"] = scen_path

    # 7) all_des.txt（B5：明文）
    all_des = render_all_des(scenes, descriptions)
    all_des_path = os.path.join(video_bucket, "all_des.txt")
    with open(all_des_path, "w", encoding="utf-8") as f:
        f.write(all_des)
    results["all_des"] = all_des_path

    return results


def _build_srt_text(rows: List[Dict[str, Any]]) -> str:
    def fmt(t: float) -> str:
        t = max(float(t), 0.0)
        h = int(t // 3600)
        m = int(t % 3600 // 60)
        s = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    out = []
    for i, r in enumerate(rows, 1):
        st = _num(r.get("start"))
        en = _num(r.get("end"), st + 0.5)
        out.append(str(i))
        out.append(f"{fmt(st)} --> {fmt(en)}")
        out.append(str(r.get("text", "")).strip())
        out.append("")
    return "\n".join(out)