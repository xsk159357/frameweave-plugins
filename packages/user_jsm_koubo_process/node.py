# -*- coding: utf-8 -*-
"""口播成片编排节点（user/jsm_koubo_process，E1，对齐解说猫 koubo 全链语义）。

一键出片链（单节点触发）：
  1. 字幕：subtitles 输入直用；缺省 + enable_asr → core/asr 识别；
  2. 剪静音：抽音轨（ffmpeg -vn）→ core/audio_silence_trim；
  3. 响度：core/audio_master（loudnorm 目标可参数化）；
  4. BGM 混音：bgm 输入存在时 amix（narration/bgm 音量参数，自身 ffmpeg 实现）；
  5. 随机放大：ffmpeg zoompan——按 scenes 分段（缺省整片单段），约 zoom_ratio（默认 50%）
     的段放大，**相邻段不重复放大（S10 修复）**：choose_zoom 从偶位插槽随机选段，
     恒满足不相邻约束；zoompan 表达式按段帧数预计算步进；
  6. 字幕烧录：user/jsm_subtitle_burn（六套样式透传）；
  7. 出片：音画 mux（-c:v copy -c:a aac）→ 成片视频；
  8. 导出：core/draft_export（剪映草稿）。

错误码报告（S13）：逐阶段 status + code（STAGE_*），阶段失败不阻断后续（warn+code 透出），
致命输入错误才 raise（invalid: 前缀）；report.stages 全程可审计。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from app.assets import Asset
from app.nodespec import NodeBase, PortType

_SUB_STYLES = [
    "经典-白字黑边", "综艺-黄字黑边", "影视-红字白边",
    "霓虹-青蓝光", "阅读-黑底白字", "沉稳-黑字白边",
]


def _num(v: Any, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _rows(raw: Any) -> List[Dict[str, Any]]:
    """字幕/分段归一：dict{items|rows|subtitles|scenes|chunks} 或 list。"""
    if isinstance(raw, dict):
        for k in ("items", "rows", "subtitles", "scenes", "chunks"):
            if isinstance(raw.get(k), list):
                return raw[k]
        return []
    return raw if isinstance(raw, list) else []


# ---------------------------------------------------------------- S10：随机放大决策（纯函数）
def choose_zoom(n_segments: int, ratio: float = 0.5, seed: Optional[int] = None) -> List[bool]:
    """约 ratio（默认 50%）的段放大，**相邻段绝不重复放大（S10）**。

    实现：候选位 = 偶位插槽（任意两个偶位都不相邻 → 恒满足约束），先随机洗牌后取
    target=round(n*ratio) 个；约束保证与随机化兼得，确定性由 seed 控制（测试可复现）。
    """
    n = max(0, int(n_segments))
    if n == 0:
        return []
    target = max(0, min(n, int(round(n * max(0.0, min(1.0, ratio))))))
    rng = random.Random(seed)
    evens = list(range(0, n, 2))
    rng.shuffle(evens)
    chosen = set(evens[:target])
    return [i in chosen for i in range(n)]


def zoom_step(amount: float, frames: int) -> float:
    """zoompan 每帧增量：保证段尾达到 amount（frames≤0 退化为 0 增量）。"""
    frames = max(1, int(frames))
    return round((max(1.0, amount) - 1.0) / frames, 6)


# ---------------------------------------------------------------- ffmpeg 基底（子进程托管）
async def _run_proc(ctx: dict, argv: List[str], timeout: float = 120.0) -> Tuple[int, str, str]:
    """执行命令（run_tracked 优先，回退 to_thread subprocess）。返回 (code, stdout, stderr)。"""
    import subprocess as _sp
    rt = ctx.get("run_tracked")
    if rt is not None:
        try:
            proc = await rt(argv, timeout=min(timeout, 30.0), text=True,
                            stdout=_sp.PIPE, stderr=_sp.PIPE)
            return (int(proc.returncode or 0),
                    str(getattr(proc, "stdout", "") or ""),
                    str(getattr(proc, "stderr", "") or ""))
        except Exception as e:  # noqa: BLE001
            return -1, "", str(e)
    try:
        proc = await asyncio.to_thread(
            lambda: _sp.run(argv, capture_output=True, timeout=timeout))
        return (int(proc.returncode or 0),
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


async def _ffmpeg(ctx: dict, argv: List[str], timeout: float = 180.0) -> str:
    """执行 ffmpeg，失败抛 RuntimeError（stderr 截断）。"""
    ff = _ffmpeg_bin()
    if not ff:
        raise RuntimeError("ffmpeg 不可用（deps.resolve_binary 未就绪）")
    code, _out, err = await _run_proc(ctx, [ff] + argv, timeout=timeout)
    if code != 0:
        raise RuntimeError(f"ffmpeg 失败(code={code}): {err[-400:]}")
    return err


def _ffmpeg_bin() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffmpeg", env=os.environ)
    return r["path"] if r.get("ready") else ""


def _ffprobe_bin() -> str:
    from app.plugin import deps
    r = deps.resolve_binary("ffprobe", env=os.environ)
    return r["path"] if r.get("ready") else ""


async def _probe_duration(ctx: dict, path: str) -> float:
    fp = _ffprobe_bin()
    if not fp or not os.path.isfile(path):
        return 0.0
    code, out, _err = await _run_proc(
        ctx, [fp, "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", path], timeout=30.0)
    if code != 0 or not out.strip():
        return 0.0
    try:
        return float(out.strip().splitlines()[-1])
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- 链式节点调用（可测桩）
async def _invoke(type_id: str, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
    """平台注册表取节点类并进程内执行（对齐 user/jsm_tk_dubbing 编排模式）。"""
    from app.registry import get_class
    node = get_class(type_id)()
    return await node.run(ctx, inputs, params)


async def _probe_dims(ctx: dict, path: str) -> Tuple[int, int]:
    """ffprobe 输入视频宽高（失败回退 16:9 假设）。"""
    fp = _ffprobe_bin()
    if fp and os.path.isfile(path):
        code, out, _err = await _run_proc(
            ctx, [fp, "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=width,height",
                  "-of", "default=noprint_wrappers=1:nokey=1", path], timeout=30.0)
        if code == 0:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            if len(lines) >= 2:
                try:
                    return int(lines[0]), int(lines[1])
                except ValueError:
                    pass
    return 1280, 720


# ---------------------------------------------------------------- zoompan 分段实现
def zoom_encode_args(video: str, t0: float, dur: float, amount: float,
                     width: int, height: int, fps: int, out: str) -> List[str]:
    """单个放大段的 ffmpeg 参数（trim + scale + zoompan；ffmpeg 可执行由 _ffmpeg 前置注入）。"""
    frames = max(1, int(dur * fps))
    step = zoom_step(amount, frames)
    vf = (f"scale={width}:-2,"
          f"zoompan=z='min(zoom+{step},{amount})':d=1:"
          f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps}")
    return ["-v", "error", "-y", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
            "-i", video, "-an", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(fps), out]


def flat_encode_args(video: str, t0: float, dur: float,
                     width: int, fps: int, out: str) -> List[str]:
    """非放大段的统一参数编码（与放大段同编码，保证 concat 兼容）。"""
    return ["-v", "error", "-y", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
            "-i", video, "-an", "-vf", f"scale={width}:-2",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(fps), out]


async def render_zoom(ctx: dict, video: str, scenes: List[Dict[str, Any]],
                      choices: List[bool], amount: float, width: int, fps: int,
                      out_mp4: str, workdir: str) -> Dict[str, Any]:
    """按分段渲染 zoompan 出片：放大段 zoompan、非放大段统一重编码、concat 合成。

    返回 {ok, output, segments_used, zoom_count, error}。
    """
    ff = _ffmpeg_bin()
    if not ff:
        return {"ok": False, "error": "ffmpeg 不可用"}
    segs: List[Dict[str, Any]] = []
    for i, sc in enumerate(scenes):
        s = _num(sc.get("start"), _num(sc.get("start_time"), 0.0))
        e = _num(sc.get("end"), _num(sc.get("end_time"), s))
        segs.append({"start": s, "end": max(e, s)})
    if not segs:
        # 整片单段（无 scenes）：整段 zoompan
        dur = await _probe_duration(ctx, video)
        segs = [{"start": 0.0, "end": dur if dur > 0 else 60.0}]
        choices = [True]
    iw, ih = await _probe_dims(ctx, video)
    height = max(2, 2 * round(width * ih / max(1, iw) / 2))
    parts: List[str] = []
    try:
        for i, seg in enumerate(segs):
            dur = seg["end"] - seg["start"]
            if dur <= 0:
                continue
            out = os.path.join(workdir, f"zoom_seg_{i:03d}.mp4")
            argv = (zoom_encode_args(video, seg["start"], dur, amount, width, height, fps, out)
                    if choices[i] else
                    flat_encode_args(video, seg["start"], dur, width, fps, out))
            await _ffmpeg(ctx, argv, timeout=300.0)
            parts.append(out)
        if not parts:
            return {"ok": False, "error": "无可渲染分段"}
        if len(parts) == 1:
            os.replace(parts[0], out_mp4)
        else:
            lst = os.path.join(workdir, "zoom_concat.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for p in parts:
                    f.write("file '%s'\n" % p.replace("\\", "/"))
            await _ffmpeg(ctx, ["-v", "error", "-y", "-f", "concat", "-safe", "0",
                                "-i", lst, "-c:v", "libx264", "-preset", "veryfast",
                                "-pix_fmt", "yuv420p", "-r", str(fps), out_mp4], timeout=300.0)
        return {"ok": True, "output": out_mp4, "segments_used": len(segs),
                "zoom_count": sum(1 for c in choices if c), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}


# ---------------------------------------------------------------- 音轨处理（自身实现）
async def extract_audio(ctx: dict, video: str, workdir: str) -> str:
    """抽取单声道 wav 音轨。"""
    out = os.path.join(workdir, "narration_raw.wav")
    await _ffmpeg(ctx, ["-v", "error", "-y", "-i", video, "-vn",
                        "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", out], timeout=120.0)
    return out


async def mix_bgm(ctx: dict, narration: str, bgm: str, narration_vol: float,
                  bgm_vol: float, workdir: str) -> str:
    """amix 混音：解说轨 + BGM 轨（duration=first）。"""
    out = os.path.join(workdir, "mixed.wav")
    await _ffmpeg(ctx, ["-v", "error", "-y", "-i", narration, "-i", bgm,
                        "-filter_complex",
                        f"[0:a]volume={narration_vol}[a];[1:a]volume={bgm_vol}[b];"
                        "[a][b]amix=inputs=2:duration=first:dropout_transition=0[out]",
                        "-map", "[out]", "-c:a", "pcm_s16le", out], timeout=120.0)
    return out


async def mux_final(ctx: dict, video: str, audio: str, out: str) -> str:
    """音画合成：-c:v copy + aac 音轨。

    不用 -shortest：口播链剪静音后音轨可能短于视频，视频时长才是一键出片的权威
    （-shortest 会错误地把成片裁到音轨长度）。
    """
    await _ffmpeg(ctx, ["-v", "error", "-y", "-i", video, "-i", audio,
                        "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                        "-c:a", "aac", out], timeout=180.0)
    return out


# ---------------------------------------------------------------- 编排节点
class JsmKouboProcessNode(NodeBase):
    """口播成片编排。type_id: user/jsm_koubo_process"""

    @classmethod
    def spec(cls):
        from app.nodespec import NodeSpec, PortSpec
        return NodeSpec(
            type_id="user/jsm_koubo_process",
            title="口播成片编排",
            category="剪辑",
            description="E1 口播一键出片：剪静音→响度→BGM 混音→随机放大(zoompan 50% 不相邻)→字幕烧录→导出剪映草稿；逐阶段错误码报告(S13)",
            inputs=[
                PortSpec("video", PortType.VIDEO, label="口播视频", required=True),
                PortSpec("subtitles", PortType.SUBTITLE, label="字幕", required=False),
                PortSpec("scenes", PortType.SEGMENTS, label="分段", required=False),
                PortSpec("bgm", PortType.AUDIO, label="BGM 素材", required=False),
            ],
            outputs=[
                PortSpec("video_out", PortType.VIDEO, label="成片视频", required=True),
                PortSpec("audio", PortType.AUDIO, label="成片音轨", required=True),
                PortSpec("segments", PortType.SEGMENTS, label="放大决策段", required=False),
                PortSpec("report", PortType.JSON, label="编排报告", required=True),
            ],
            params=[
                PortSpec("enable_asr", PortType.BOOL, label="无字幕时 ASR", default=False, widget="toggle"),
                PortSpec("enable_trim", PortType.BOOL, label="剪静音", default=True, widget="toggle"),
                PortSpec("trim_threshold_db", PortType.FLOAT, label="静音阈值(dB)", required=False, default=0.0, widget="number"),
                PortSpec("trim_min_silence_ms", PortType.INT, label="最小静音(ms)", required=False, default=200, widget="number"),
                PortSpec("enable_master", PortType.BOOL, label="响度统一", default=True, widget="toggle"),
                PortSpec("master_lufs", PortType.FLOAT, label="响度目标(LUFS)", required=False, default=-16.0, widget="number",
                         description="YouTube 口播标准 -16 LUFS（对齐解说猫口播页默认）"),
                PortSpec("enable_bgm", PortType.BOOL, label="BGM 混音", default=True, widget="toggle"),
                PortSpec("narration_volume", PortType.FLOAT, label="解说音量", required=False, default=1.0, widget="number"),
                PortSpec("bgm_volume", PortType.FLOAT, label="BGM 音量", required=False, default=0.3, widget="number"),
                PortSpec("enable_zoom", PortType.BOOL, label="随机放大", default=True, widget="toggle"),
                PortSpec("zoom_ratio", PortType.FLOAT, label="放大段比例", default=0.5, widget="number"),
                PortSpec("zoom_amount", PortType.FLOAT, label="放大倍率", default=1.12, widget="number"),
                PortSpec("zoom_seed", PortType.INT, label="随机种子(0=随机)", required=False, default=0, widget="number"),
                PortSpec("zoom_width", PortType.INT, label="放大输出宽(px)", required=False, default=1280, widget="number"),
                PortSpec("subtitle_style", PortType.STRING, label="字幕样式", default=_SUB_STYLES[0],
                         widget="select", options=list(_SUB_STYLES)),
                PortSpec("enable_burn", PortType.BOOL, label="字幕烧录", default=True, widget="toggle"),
                PortSpec("enable_export", PortType.BOOL, label="导出剪映草稿", default=True, widget="toggle"),
                PortSpec("draft_name", PortType.STRING, label="草稿名", required=False, default="口播成片", widget="text"),
                PortSpec("output_name", PortType.STRING, label="输出文件名", required=False, default="koubo_out", widget="text"),
            ],
            version="1.0.0",
        )

    async def run(self, ctx: dict, inputs: dict, params: dict) -> Dict[str, Any]:
        store = ctx["store"]
        progress = ctx.get("progress") or (lambda p: None)
        video_asset = inputs.get("video")
        if video_asset is None or not getattr(video_asset, "path", "") or not os.path.exists(video_asset.path):
            raise RuntimeError("invalid: 需要可读的口播视频输入")

        workdir = os.path.join(ctx.get("workdir", "."), "koubo_work")
        os.makedirs(workdir, exist_ok=True)
        fps = 30
        zoom_width = max(64, int(params.get("zoom_width", 1280) or 1280))
        zoom_amount = max(1.0, _num(params.get("zoom_amount", 1.12), 1.12))
        zoom_ratio = _num(params.get("zoom_ratio", 0.5), 0.5)
        zoom_seed = int(params.get("zoom_seed", 0) or 0)

        stages: List[Dict[str, Any]] = []
        warnings: List[str] = []

        def _stage(name: str) -> None:
            stages.append({"name": name, "status": "running"})

        def _finish(name: str, code: str, ok: bool, detail: Any = None) -> None:
            for st in stages:
                if st["name"] == name:
                    st["status"] = "succeeded" if ok else "failed"
                    st["code"] = code
                    if detail is not None:
                        st["detail"] = detail
                    return
            stages.append({"name": name, "status": "succeeded" if ok else "failed",
                           "code": code, "detail": detail})

        async def _guard(name: str, code: str, fn_async):
            """阶段守卫：失败记录 code（S13）不阻断后续；返回 (ok, result)。"""
            try:
                r = await fn_async()
                _finish(name, code, True)
                return True, r
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:300]
                warnings.append(f"{name} 失败[{code}]: {msg}")
                _finish(name, code, False, {"error": msg})
                return False, None

        # ---- 字幕（输入直用 / ASR）
        _stage("subtitles")
        subtitle_rows: List[Dict[str, Any]] = []
        sub_asset = inputs.get("subtitles")
        if sub_asset is not None:
            subtitle_rows = _rows(store.read_json(sub_asset.id))
            _finish("subtitles", "SUB_INPUT", True, {"rows": len(subtitle_rows)})
        elif bool(params.get("enable_asr", False)):
            asr_ok, asr_out = await _guard("subtitles", "STAGE_ASR_FAILED", lambda: _invoke(
                "core/asr", ctx, {"video": video_asset},
                {"engine": "local", "language": "zh", "model": "small"}))
            if asr_ok and asr_out and asr_out.get("subtitles") is not None:
                subtitle_rows = _rows(store.read_json(asr_out["subtitles"].id))
            _finish("subtitles", "SUB_ASR", True, {"rows": len(subtitle_rows)})
        else:
            _finish("subtitles", "SUB_SKIP", True, {"rows": 0, "note": "无字幕输入且 enable_asr=关，跳过烧录"})
        progress(0.1)

        # ---- 音轨：抽音轨 → 剪静音 → 响度 → BGM 混音
        _stage("audio_extract")
        audio_path = ""
        audio_ok, audio_path = await _guard("audio_extract", "STAGE_EXTRACT_FAILED",
                                             lambda: extract_audio(ctx, video_asset.path, workdir))
        if not audio_ok:
            audio_path = ""
        final_audio_asset = None
        if audio_ok:
            _stage("trim")
            trimmed_asset = None
            if bool(params.get("enable_trim", True)):
                trim_ok, trim_out = await _guard("trim", "STAGE_TRIM_FAILED", lambda: _invoke(
                    "core/audio_silence_trim", ctx,
                    {"audio": store.save_asset(Asset(id="", kind=PortType.AUDIO.value, path=audio_path), payload=None)},
                    {"threshold_db": _num(params.get("trim_threshold_db", 0.0), 0.0),
                     "min_silence_ms": int(params.get("trim_min_silence_ms", 200) or 200),
                     "output_format": "wav"}))
                if trim_ok and trim_out and trim_out.get("trimmed") is not None:
                    trimmed_asset = trim_out["trimmed"]
            else:
                _finish("trim", "TRIM_SKIP", True, {"note": "enable_trim=关"})
            _stage("master")
            mastered_asset = trimmed_asset or store.save_asset(
                Asset(id="", kind=PortType.AUDIO.value, path=audio_path), payload=None)
            if bool(params.get("enable_master", True)):
                master_ok, master_out = await _guard("master", "STAGE_MASTER_FAILED", lambda: _invoke(
                    "core/audio_master", ctx, {"audio": mastered_asset},
                    {"enable_loudnorm": True, "loudnorm_target_lufs": _num(params.get("master_lufs", -16.0), -16.0),
                     "enable_volume_boost": False, "output_format": "wav"}))
                if master_ok and master_out and master_out.get("mastered") is not None:
                    mastered_asset = master_out["mastered"]
            else:
                _finish("master", "MASTER_SKIP", True, {"note": "enable_master=关"})
            _stage("bgm")
            mixed_asset = mastered_asset
            bgm_asset = inputs.get("bgm")
            if bool(params.get("enable_bgm", True)) and bgm_asset is not None \
                    and getattr(bgm_asset, "path", ""):
                bgm_ok, bgm_out = await _guard("bgm", "STAGE_BGM_FAILED", lambda: mix_bgm(
                    ctx, mastered_asset.path, bgm_asset.path,
                    _num(params.get("narration_volume", 1.0), 1.0),
                    _num(params.get("bgm_volume", 0.3), 0.3), workdir))
                if bgm_ok and bgm_out:
                    mixed_asset = store.save_asset(
                        Asset(id="", kind=PortType.AUDIO.value, path=bgm_out), payload=None)
            else:
                _finish("bgm", "BGM_SKIP", True, {"note": "无 bgm 输入或 enable_bgm=关"})
            final_audio_asset = mixed_asset
        progress(0.35)

        # ---- 随机放大（zoompan，50% 不相邻 S10）
        _stage("zoom")
        zoom_asset = None
        zoom_choices: List[bool] = []
        if bool(params.get("enable_zoom", True)):
            scenes = _rows(store.read_json(inputs.get("scenes").id)) if inputs.get("scenes") is not None else []
            n = len(scenes)
            if n > 0:
                zoom_choices = choose_zoom(n, zoom_ratio, seed=(zoom_seed or None))
            else:
                zoom_choices = [True]
            z_out = os.path.join(workdir, "zoomed.mp4")
            zok, zres = await _guard("zoom", "STAGE_ZOOM_FAILED", lambda: render_zoom(
                ctx, video_asset.path, scenes, zoom_choices, zoom_amount, zoom_width, fps, z_out, workdir))
            if zok and zres and zres.get("ok") and os.path.isfile(zres["output"]):
                zoom_asset = store.save_asset(
                    Asset(id="", kind=PortType.VIDEO.value,
                          path=zres["output"],
                          meta={"zoom_count": zres.get("zoom_count", 0),
                                "segments": zres.get("segments_used", 0)}), payload=None)
                _finish("zoom", "ZOOM_OK", True, {"zoom_count": zres.get("zoom_count", 0),
                                                  "segments": zres.get("segments_used", 0)})
        else:
            _finish("zoom", "ZOOM_SKIP", True, {"note": "enable_zoom=关"})
        progress(0.55)

        # ---- 字幕烧录
        _stage("burn")
        burn_asset = None
        base_video = zoom_asset if zoom_asset is not None else video_asset
        if subtitle_rows and bool(params.get("enable_burn", True)):
            burn_ok, burn_out = await _guard("burn", "STAGE_BURN_FAILED", lambda: _invoke(
                "user/jsm_subtitle_burn", ctx,
                {"video": base_video, "subtitles": inputs.get("subtitles") or store.save_asset(
                    Asset(id="", kind=PortType.SUBTITLE.value), payload=subtitle_rows)},
                {"style": str(params.get("subtitle_style", _SUB_STYLES[0]) or _SUB_STYLES[0]),
                 "output_name": "koubo_burn"}))
            if burn_ok and burn_out and burn_out.get("video_out") is not None:
                burn_asset = burn_out["video_out"]
        else:
            _finish("burn", "BURN_SKIP", True, {"note": "无字幕或 enable_burn=关"})
        progress(0.72)

        # ---- 音画合成出片
        _stage("finalize")
        final_video_asset = burn_asset or base_video
        if final_audio_asset is not None and final_audio_asset.path:
            fin_path = os.path.join(workdir, f"{params.get('output_name', 'koubo_out') or 'koubo_out'}.mp4")
            fin_ok, fin_res = await _guard("finalize", "STAGE_MUX_FAILED", lambda: mux_final(
                ctx, final_video_asset.path, final_audio_asset.path, fin_path))
            if fin_ok and fin_res and os.path.isfile(fin_res):
                final_video_asset = store.save_asset(
                    Asset(id="", kind=PortType.VIDEO.value, path=fin_res), payload=None)
        else:
            _finish("finalize", "MUX_SKIP", True, {"note": "无成片音轨，直出视频"})
        progress(0.85)

        # ---- 剪映草稿导出
        _stage("export")
        draft_asset = None
        if bool(params.get("enable_export", True)):
            exp_ok, exp_out = await _guard("export", "STAGE_EXPORT_FAILED", lambda: _invoke(
                "core/draft_export", ctx,
                {"video": final_video_asset,
                 "audio": final_audio_asset,
                 "segments": (inputs.get("scenes") or store.save_asset(
                     Asset(id="", kind=PortType.SEGMENTS.value), payload=[{
                         "index": 0, "start": 0.0, "end": 0.0}])),
                 "styled_subtitle": inputs.get("subtitles")},
                {"draft_name": str(params.get("draft_name", "口播成片") or "口播成片"),
                 "enable_subtitle": bool(subtitle_rows)}))
            if exp_ok and exp_out and exp_out.get("draft") is not None:
                draft_asset = exp_out["draft"]
        else:
            _finish("export", "EXPORT_SKIP", True, {"note": "enable_export=关"})
        progress(0.95)

        # ---- 输出
        seg_rows = []
        if inputs.get("scenes") is not None:
            for i, sc in enumerate(_rows(store.read_json(inputs.get("scenes").id))):
                seg_rows.append({"index": i, "start": _num(sc.get("start"), 0.0),
                                 "end": _num(sc.get("end"), _num(sc.get("start"), 0.0)),
                                 "zoom": bool(zoom_choices[i]) if i < len(zoom_choices) else False})
        else:
            seg_rows = [{"index": 0, "start": 0.0, "end": 0.0,
                         "zoom": bool(zoom_choices[0]) if zoom_choices else False}]
        seg_asset = store.save_asset(
            Asset(id="", kind=PortType.SEGMENTS.value,
                  meta={"zoom_count": sum(1 for r in seg_rows if r["zoom"]),
                        "segments": len(seg_rows)}), payload=seg_rows)
        report = {
            "ok": True,
            "tool": "user/jsm_koubo_process",
            "stages": stages,
            "zoom": {"segments": len(seg_rows),
                     "zoomed": sum(1 for r in seg_rows if r["zoom"]),
                     "adjacent_zoom": any(seg_rows[i]["zoom"] and seg_rows[i + 1]["zoom"]
                                          for i in range(len(seg_rows) - 1)),
                     "amount": zoom_amount, "ratio": zoom_ratio, "seed": zoom_seed},
            "counts": {"subtitles": len(subtitle_rows), "video_duration": 0.0},
            "warnings": warnings,
            "finished_at": time.time(),
        }
        report_asset = store.save_asset(
            Asset(id="", kind=PortType.JSON.value, meta={"stages": len(stages)}), payload=report)
        progress(1.0)
        return {"video_out": final_video_asset, "audio": final_audio_asset,
                "segments": seg_asset, "report": report_asset}