# -*- coding: utf-8 -*-
"""项目输出打包节点：project_config.ini / split_cam / video_split_info / srt / all_des / 封面帧。

修复原版缺陷：B3（split_info 键名 start_time/end_time）、B4（scenarios 真实内容）、
B5（all_des 明文）。main.jpg 封面由 ffmpeg 抽帧（cover_time 参数）。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

# 插件包目录引导（宿主按 entrypoint 加载 node.py，不自动把包目录加入 sys.path）
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

from pack_builder import build_project_package


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def _extract_cover(ctx, video_path: str, out_jpg: str, cover_time: float) -> Optional[str]:
    """ffmpeg 抽封面帧 main.jpg。失败返回错误信息（不阻断打包）。

    输出重定向到文件而非 PIPE：ffmpeg stderr 进度输出量大，管道写满会造成
    run_tracked 死锁。
    """
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    ff = r["path"] if r.get("ready") else ""
    if not ff:
        return "ffmpeg 未找到，跳过封面抽帧"
    import tempfile
    cmd = [ff, "-y", "-ss", f"{max(cover_time, 0):.2f}", "-i", video_path,
           "-frames:v", "1", "-q:v", "2", out_jpg]
    with tempfile.TemporaryDirectory(prefix="fw_cov_") as td:
        err_f = os.path.join(td, "e.log")
        with open(err_f, "wb") as fe:
            proc = await ctx["run_tracked"](cmd, timeout=120, stdout=open(os.devnull, "wb"), stderr=fe)
        err = ""
        try:
            with open(err_f, "r", encoding="utf-8", errors="replace") as f:
                err = f.read()
        except OSError:
            pass
    if proc.returncode != 0:
        return "封面抽帧失败: " + (err[-300:] or "")
    return None


class ProjectPackageNode(NodeBase):
    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="core/project_package",
            title="项目输出打包",
            category="输出",
            description="项目输出打包：project_config.ini/split_cam/video_split_info/srt/all_des/封面帧；键名与明文修复（B3/B4/B5）。",
            inputs=[
                PortSpec(name="video", type=PortType.VIDEO, label="视频", required=True),
                PortSpec(name="scenes", type=PortType.SEGMENTS, label="片段列表", required=True),
                PortSpec(name="subtitles", type=PortType.SUBTITLE, label="字幕", required=False),
                PortSpec(name="descriptions", type=PortType.JSON, label="场景描述", required=False),
                PortSpec(name="project", type=PortType.JSON, label="项目设置", required=False),
            ],
            outputs=[
                PortSpec(name="package", type=PortType.JSON, label="打包结果"),
                PortSpec(name="report", type=PortType.JSON, label="报告", required=False),
            ],
            params=[
                PortSpec(name="output_dir", type=PortType.STRING, label="输出目录", default="",
                         widget="text", description="留空输出到资产目录"),
                PortSpec(name="cover_time", type=PortType.FLOAT, label="封面抽帧时间", default=5.0,
                         widget="number"),
                PortSpec(name="include_cover", type=PortType.BOOL, label="生成封面帧", default=True,
                         widget="toggle"),
                PortSpec(name="language_select", type=PortType.STRING, label="语言", default="中文",
                         widget="text"),
                PortSpec(name="person_select", type=PortType.STRING, label="人称", default="third",
                         widget="text"),
                PortSpec(name="story_select", type=PortType.STRING, label="故事模板", default="cjbkkt_mod",
                         widget="text"),
                PortSpec(name="audio_speed", type=PortType.FLOAT, label="语速", default=4.5, widget="number"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        video = inputs.get("video")
        scenes_asset = inputs.get("scenes")
        if video is None or not getattr(video, "path", "") or not os.path.exists(video.path):
            raise RuntimeError("需要视频输入")
        if scenes_asset is None:
            raise RuntimeError("需要片段列表输入")
        store = ctx["store"]
        progress = ctx.get("progress")
        if progress:
            progress(0.1)

        scenes = store.read_json(scenes_asset.id) or []
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError("片段列表为空")

        subs_asset = inputs.get("subtitles")
        subtitles = store.read_json(subs_asset.id) if subs_asset is not None else None
        desc_asset = inputs.get("descriptions")
        descriptions = store.read_json(desc_asset.id) if desc_asset is not None else None
        proj_asset = inputs.get("project")
        proj_settings = store.read_json(proj_asset.id) if proj_asset is not None else None

        out_dir = params.get("output_dir") or os.path.join(store.files_dir, "project")
        video_name = os.path.splitext(os.path.basename(video.path))[0]
        project_dir = os.path.join(out_dir, video_name)
        os.makedirs(project_dir, exist_ok=True)

        settings = dict(proj_settings or {})
        settings.setdefault("language_select", str(params.get("language_select", "中文") or "中文"))
        settings.setdefault("person_select", str(params.get("person_select", "third") or "third"))
        settings.setdefault("story_select", str(params.get("story_select", "cjbkkt_mod") or "cjbkkt_mod"))
        settings.setdefault("audio_speed", str(params.get("audio_speed", 4.5) or 4.5))

        if progress:
            progress(0.25)
        results = build_project_package(
            project_dir=project_dir, video_path=video.path,
            video_name=video_name, scenes=scenes,
            subtitles=subtitles, descriptions=descriptions, settings=settings)

        # 封面帧
        cover_warn = ""
        if bool(params.get("include_cover", True)):
            main_jpg = os.path.join(project_dir, "main.jpg")
            cover_warn = await _extract_cover(ctx, video.path, main_jpg,
                                              _num(params.get("cover_time", 5.0), 5.0)) or ""
            if not cover_warn:
                results["main_jpg"] = main_jpg
        if progress:
            progress(0.9)

        report = {
            "status": "ok",
            "project_dir": project_dir,
            "scenes": len(scenes),
            "subtitles": len(subtitles or []),
            "results": results,
            "warnings": [cover_warn] if cover_warn else [],
        }
        pkg_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"project_dir": project_dir, **report}),
            payload=report)
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"status": "ok"}), payload=report)
        if progress:
            progress(1.0)
        return {"package": pkg_asset, "report": report_asset}