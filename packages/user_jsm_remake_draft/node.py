# -*- coding: utf-8 -*-
"""D4 精选复刻草稿节点 —— user/jsm_remake_draft（匹配表 + 原片 + 解说 → 剪映草稿）。

干净重实现 jieshuomao generators.curated_remake_draft（第 4 模式，消费 S2-T1 匹配表）：
  mode="compare"（默认）：画布 (cW+rW) x cH —— 左半整段解说 + 右半匹配原片段
     （缩放到等高，互不遮挡）；两条 LABEL 文字轨「原解说」/「复刻后」黄字#ffde00+黑描边
     （pos/scale 常量 _LABEL_LEFT_POS/_LABEL_RIGHT_POS/_LABEL_SCALE）。
  mode="remake_only"：画布 = 原片 WxH；单视频轨 = 逐镜头原片片段按解说节奏铺满。

R7 修复（不复发）：commentary wav 一律经 ffprobe 实测时长写入 audio 轨素材
  （duration_us>0），绝不把 wav 当 video material 或写 0 时长。

jy_builder 第 4 模式扩展在 core_draft_export 包内（build_remake_content / write_remake_draft），
既有 3 模式（普通/TTS/BGM）保持向后兼容。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

# 插件包目录引导（同 user_jsm_remake_match：宿主按 entrypoint 加载 node.py，
# 不自动把包目录加入 sys.path——先插入本包目录，再做扁平导入）
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)


def _core_draft_pkg_dir() -> str:
    """定位 core_draft_export 包目录（jy_builder 宿主）。

    优先级：已安装用户节点目录（打包/隔离测试）→ 插件仓库兄弟目录（源码开发）→ 空。
    """
    candidates = []
    try:
        from app import declarative
        candidates.append(os.path.join(declarative.USER_NODES_DIR, "core_draft_export"))
    except Exception:  # noqa: BLE001
        pass
    candidates.append(os.path.join(os.path.dirname(_PKG_DIR), "core_draft_export"))
    for c in candidates:
        if os.path.isdir(c) and os.path.isfile(os.path.join(c, "jy_builder.py")):
            return c
    return ""


_CORE_DRAFT_PKG = _core_draft_pkg_dir()
if _CORE_DRAFT_PKG and _CORE_DRAFT_PKG not in sys.path:
    sys.path.insert(0, _CORE_DRAFT_PKG)

from jy_builder import (  # noqa: E402
    US, DraftModel, _REMAKE_LABEL_LEFT_POS, _REMAKE_LABEL_RIGHT_POS,
    _REMAKE_LABEL_SCALE, _REMAKE_TEXT_SIZE, build_remake_content,
    normalize_remake_matches, write_remake_draft,
)


def _asset_path(store: Any, asset: Any) -> str:
    """解析资产本地路径（Asset / @id 引用）。"""
    if asset is None:
        return ""
    if isinstance(asset, Asset):
        return asset.path or ""
    if isinstance(asset, str):
        if asset.startswith("@"):
            a = store.get(asset[1:])
            return a.path if a else ""
        return asset
    return ""


async def _probe_media(ctx: Dict[str, Any], path: str) -> Dict[str, Any]:
    """ffprobe 宽高/时长（视频或音频通用）。失败返回空 dict（调用方兜底）。"""
    import subprocess
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    if not r.get("ready"):
        return {}
    cmd = [r["path"], "-v", "error", "-show_entries",
           "stream=codec_type,width,height,duration", "-show_entries",
           "format=duration", "-of", "json", path]
    rt = ctx.get("run_tracked")
    try:
        proc = await rt(cmd, timeout=60, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True) if rt else None
        out = proc
    except Exception:  # noqa: BLE001
        out = None
    raw = ""
    if out is not None and getattr(out, "returncode", 1) == 0:
        raw = getattr(out, "stdout", "") or ""
    if not raw:
        # 回退子进程探测（无 run_tracked 环境）
        try:
            import asyncio
            res = await asyncio.to_thread(
                lambda: subprocess.run(cmd, capture_output=True, timeout=60, text=True))
            raw = res.stdout or ""
        except Exception:  # noqa: BLE001
            return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = 0.0
    try:
        dur = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    info = {"duration": dur}
    if v:
        info.update({"width": int(v.get("width") or 0), "height": int(v.get("height") or 0)})
    if a:
        info["audio_duration"] = dur or float(a.get("duration") or 0.0)
    return info


def _safe_name(name: str, default: str = "精选复刻") -> str:
    """草稿目录名安全化（仅 [A-Za-z0-9_-] + 中文等保留，路径分隔符剥离）。"""
    s = str(name or "").strip().replace("\\", "/").split("/")[-1]
    s = re.sub(r'[<>:"|?*\x00-\x1f]', "", s).strip()
    return s or default


class RemakeDraftNode(NodeBase):
    """D4 精选复刻草稿节点。type_id: user/jsm_remake_draft"""

    @classmethod
    def spec(cls) -> NodeSpec:
        return NodeSpec(
            type_id="user/jsm_remake_draft",
            title="精选复刻草稿",
            category="输出",
            description=("D4：消费 S2-T1 匹配表 → 精剪映草稿（compare 分屏黄字 LABEL / "
                         "remake_only 原片满铺）；commentary wav 真实时长音频轨（R7）。"),
            inputs=[
                PortSpec(name="matches", type=PortType.JSON, label="匹配表", required=True),
                PortSpec(name="original", type=PortType.VIDEO, label="原片", required=True),
                PortSpec(name="commentary", type=PortType.VIDEO, label="解说视频", required=True),
                PortSpec(name="commentary_wav", type=PortType.AUDIO, label="解说音频(WAV)",
                         required=True),
            ],
            outputs=[
                PortSpec(name="draft_dir", type=PortType.STRING, label="草稿目录"),
                PortSpec(name="report", type=PortType.JSON, label="导出报告"),
                PortSpec(name="draft_content", type=PortType.JSON, label="草稿内容",
                         required=False),
            ],
            params=[
                PortSpec(name="mode", type=PortType.STRING, label="复刻模式", default="compare",
                         widget="select", options=["compare", "remake_only"]),
                PortSpec(name="labels", type=PortType.STRING, label="LABEL 文案 JSON",
                         default='["原解说", "复刻后"]', required=False, widget="text"),
                PortSpec(name="orig_volume", type=PortType.FLOAT, label="原片音量",
                         default=0.0, widget="number"),
                PortSpec(name="output_name", type=PortType.STRING, label="草稿名",
                         default="精选复刻", required=False, widget="text"),
                PortSpec(name="output_dir", type=PortType.STRING, label="输出目录",
                         default="", required=False, widget="text"),
                PortSpec(name="write_suspect_list", type=PortType.BOOL, label="写存疑清单",
                         default=True, widget="toggle",
                         description="G14：把低置信匹配段写入草稿目录 存疑清单.txt（解说猫语义）"),
            ],
            version="1.1.0",
        )

    async def run(self, ctx: Dict[str, Any], inputs: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        store = ctx["store"]
        t0 = time.time()
        if not _CORE_DRAFT_PKG:
            raise RuntimeError("找不到 core_draft_export 包（jy_builder）：请先安装官方 core/draft_export 插件")

        modes_ok = ("compare", "remake_only")
        mode = str(params.get("mode", "compare") or "compare")
        if mode not in modes_ok:
            raise ValueError(f"mode 必须是 compare|remake_only，当前: {mode!r}")

        # ---------- 输入解析 ----------
        matches_asset = inputs.get("matches")
        original_asset = inputs.get("original")
        commentary_asset = inputs.get("commentary")
        wav_asset = inputs.get("commentary_wav")

        original_path = _asset_path(store, original_asset)
        commentary_path = _asset_path(store, commentary_asset)
        wav_path = _asset_path(store, wav_asset)
        for label, p in (("原片", original_path), ("解说视频", commentary_path),
                         ("解说音频", wav_path)):
            if not p or not os.path.isfile(p):
                raise ValueError(f"{label} 文件不存在: {p!r}")

        if matches_asset is None:
            raise ValueError("缺少匹配表输入（请连接 user/jsm_remake_match 输出）")
        try:
            raw_matches = store.read_json(matches_asset.id) if isinstance(matches_asset, Asset) \
                else matches_asset
        except Exception:  # noqa: BLE001
            raw_matches = None
        if isinstance(raw_matches, str):
            try:
                raw_matches = json.loads(raw_matches)
            except json.JSONDecodeError:
                raw_matches = None
        matches = normalize_remake_matches(raw_matches, original_path=original_path)
        if not matches:
            raise ValueError("匹配表为空或无法解析（请确认 S2-T1 输出结构）")

        labels_raw = str(params.get("labels", "") or '["原解说", "复刻后"]')
        try:
            labels = json.loads(labels_raw)
        except json.JSONDecodeError:
            labels = ["原解说", "复刻后"]
        if not isinstance(labels, list) or len(labels) < 2:
            labels = ["原解说", "复刻后"]
        try:
            orig_volume = float(params.get("orig_volume", 0.0) or 0.0)
        except (TypeError, ValueError):
            orig_volume = 0.0

        # ---------- 媒体探测（宽高/时长；R7 关键：wav 真实时长 >0） ----------
        orig_info = await _probe_media(ctx, original_path)
        comm_info = await _probe_media(ctx, commentary_path)
        wav_info = await _probe_media(ctx, wav_path)
        orig_w = int(orig_info.get("width") or 1920)
        orig_h = int(orig_info.get("height") or 1080)
        comm_w = int(comm_info.get("width") or 1920)
        comm_h = int(comm_info.get("height") or 1080)
        wav_dur = float(wav_info.get("duration") or 0.0)

        # 匹配段覆盖的最大解说时间（remake_only 铺满语义）与最大原片时间
        max_comm_us = max((m["commentary_start_us"] + m["commentary_dur_us"] for m in matches),
                          default=0)
        max_ref_us = max((m["ref_start_us"] + m["ref_dur_us"] for m in matches), default=0)
        comm_dur = max(float(comm_info.get("duration") or 0.0) * US, max_comm_us)
        total_us = int(max(comm_dur, max_comm_us, 1))

        # ---------- R7 断言：commentary wav 真实时长（ffprobe）>0 ----------
        r7_ok = wav_dur > 0.0
        wav_dur_us = int(wav_dur * US) if r7_ok else total_us  # 兜底>0（但报告标记告警）

        # ---------- 构建第 4 模式 DraftModel ----------
        out_dir = str(params.get("output_dir") or "").strip() or \
            os.path.join(ctx.get("workdir", "."), "drafts")
        draft_name = _safe_name(params.get("output_name"), "精选复刻") + "_" + time.strftime("%m%d_%H%M%S")
        os.makedirs(out_dir, exist_ok=True)
        m = DraftModel(
            video_path=commentary_path,
            video_name=os.path.basename(commentary_path),
            video_width=comm_w, video_height=comm_h,
            video_duration=max(comm_dur / US, float(comm_info.get("duration") or 0.0)),
            canvas={"width": comm_w, "height": comm_h, "ratio": "16:9"},
            fps=30.0, export_target="jianying",
            draft_name=draft_name,
            draft_dir=os.path.join(out_dir, draft_name),
        )
        m.remake_mode = mode
        m.remake_matches = matches
        m.remake_commentary_w = comm_w
        m.remake_commentary_h = comm_h
        m.remake_original_w = orig_w
        m.remake_original_h = orig_h
        m.remake_original_path = original_path
        m.remake_original_dur = float(orig_info.get("duration") or 0.0)
        m.remake_commentary_audio = {
            "path": wav_path,
            "name": os.path.basename(wav_path),
            "duration_us": wav_dur_us,
        }
        m.remake_labels = (str(labels[0]), str(labels[1]))
        m.remake_orig_volume = orig_volume
        m.remake_total_us = total_us
        # meta 用：主视频 + 原片素材登记（供剪映素材库识别；同老 3 模式语义）
        m.entries = [
            {"video_path": commentary_path, "src_start_us": 0,
             "src_dur_us": max(int(comm_dur), 1), "tgt_start_us": 0,
             "tgt_dur_us": max(int(comm_dur), 1), "is_ost": False,
             "segment_index": 0, "text": "", "speed": 1.0, "volume": 1.0},
            {"video_path": original_path, "src_start_us": 0,
             "src_dur_us": max(int(float(orig_info.get("duration") or 1.0) * US), 1),
             "tgt_start_us": 0, "tgt_dur_us": total_us, "is_ost": True,
             "segment_index": 1, "text": "", "speed": 1.0, "volume": 0.0},
        ]

        draft_dir = write_remake_draft(m)

        # ---------- G14：存疑清单.txt（低置信匹配段落盘，解说猫语义） ----------
        suspect_path = ""
        if bool(params.get("write_suspect_list", True)):
            suspect_rows = [mm for mm in matches if mm.get("suspect")]
            if suspect_rows:
                try:
                    suspect_path = os.path.join(draft_dir, "存疑清单.txt")
                    lines = ["低置信匹配段存疑清单（user/jsm_remake_draft）", ""]
                    for mm in suspect_rows:
                        lines.append(
                            f"解说 {mm.get('commentary_start', 0):.2f}-{mm.get('commentary_end', 0):.2f}s → "
                            f"原片 {mm.get('ref_start', 0):.2f}-{mm.get('ref_end', 0):.2f}s "
                            f"(similarity={mm.get('similarity')}, strategy={mm.get('strategy', '')})")
                    with open(suspect_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
                except OSError as e:
                    warnings = list(validation.get("warnings") or [])
                    warnings.append(f"存疑清单写入失败: {e}")
                    suspect_path = ""

        # ---------- 校验（剪映结构） ----------
        content_path = os.path.join(draft_dir, "draft_content.json")
        validation: Dict[str, Any] = {"ok": True, "errors": [], "warnings": []}
        try:
            from app.draft_validator import validate_jianying_draft
            validation = validate_jianying_draft(content_path) or validation
        except Exception as e:  # noqa: BLE001
            validation = {"ok": False, "errors": [f"draft_validator 不可用: {e}"],
                          "warnings": []}

        with open(content_path, "r", encoding="utf-8") as f:
            content = json.load(f)
        canvas = content.get("canvas_config", {})
        tracks = content.get("tracks") or []

        # LABEL 断言（compare）：黄字#ffde00 + 黑描边 + 常量 pos/scale
        label_ok = True
        if mode == "compare":
            text_contents = []
            for tm in (content.get("materials", {}).get("texts") or []):
                txt = str(tm.get("content", "") or "")
                if tm.get("text_color", "").upper() == "#FFDE00" or \
                        '"color": [1.0, 0.8705882430076599, 0.0]' in txt:
                    text_contents.append(tm)
            # 至少 2 条黄字材料，且含黑描边（strokes color [0,0,0]）
            black_stroke_ok = all(
                '"color": [0.0, 0.0, 0.0]' in str(tm.get("content", "") or "")
                for tm in text_contents)
            label_ok = len(text_contents) >= 2 and black_stroke_ok

        # R7 回归断言（专项）：audio 轨素材 duration>0 且存在于 materials.audios
        audios = content.get("materials", {}).get("audios") or []
        audio_tracks = [t for t in tracks if t.get("type") == "audio"]
        r7_material_ok = any(
            a.get("path", "").replace("\\", "/") == wav_path.replace("\\", "/")
            and int(a.get("duration", 0) or 0) > 0 for a in audios)
        r7_segment_ok = any(
            int((s.get("target_timerange") or {}).get("duration", 0) or 0) > 0
            for t in audio_tracks for s in (t.get("segments") or []))

        report = {
            "draft_name": draft_name,
            "draft_dir": draft_dir,
            "mode": mode,
            "suspect_list_path": suspect_path,
            "suspect_count": sum(1 for mm in matches if mm.get("suspect")),
            "canvas": canvas,
            "tracks": [{"type": t.get("type"), "name": t.get("name", ""),
                        "segments": len(t.get("segments") or [])} for t in tracks],
            "matches_consumed": len(matches),
            "labels": list(labels),
            "label_constants": {
                "left_pos": list(_REMAKE_LABEL_LEFT_POS),
                "right_pos": list(_REMAKE_LABEL_RIGHT_POS),
                "scale": _REMAKE_LABEL_SCALE,
                "text_size": _REMAKE_TEXT_SIZE,
            },
            "label_ok": label_ok,
            "r7": {
                "wav_path": wav_path,
                "wav_duration_sec": round(wav_dur, 3),
                "wav_duration_us": wav_dur_us,
                "material_duration_ok": r7_material_ok,
                "segment_duration_ok": r7_segment_ok,
                "assertion_ok": bool(r7_ok and r7_material_ok and r7_segment_ok),
            },
            "media": {
                "original": {"w": orig_w, "h": orig_h,
                             "dur": round(float(orig_info.get("duration") or 0), 3)},
                "commentary": {"w": comm_w, "h": comm_h,
                               "dur": round(float(comm_info.get("duration") or 0), 3)},
            },
            "validation": validation,
            "elapsed_sec": round(time.time() - t0, 3),
            "warnings": (validation.get("warnings") or []) or
                        (["R7 音频时长回退为总时长（wav 探测失败）"] if not r7_ok else []),
        }

        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": mode, "draft_name": draft_name}),
            payload=report)
        content_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"mode": mode}),
            payload=content)
        return {"draft_dir": draft_dir, "report": report_asset, "draft_content": content_asset}