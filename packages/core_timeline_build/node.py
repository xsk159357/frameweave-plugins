"""时间线生成节点：把「句子→场景」的匹配结果变成带时间的画面切片。

时长是一条贯穿链：目标时长 → 目标字数 → 分段数 → 每段配音时长 → 场景时长适配。
本节点只负责最后一步：**按字数推每句配音时长，再把它套到匹配到的场景上**。

chars_per_second 默认 4.5 是实测值：jieshuomao 的 estimate_tts_duration 默认参数
就是 4.5，而且它没有固定开销项（10 个字 = 2.22 秒）。不同风格档位不同：
纪录片 3.5 / 电视剧 4.2 / 其余 4.5 —— 所以这是个参数，不是常量。

**不排序。** 输出数组的顺序就是叙事顺序，start/end 是原片时间，两者不是一回事。
（jieshuomao 的 _align_timeline 明确不排序：排序会让画面按时间排回去，
 文案却还按叙事走，两边就错位了。这是它踩过并写进设计的坑。）
"""
from __future__ import annotations

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
        for key in ("items", "segments", "scenes", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        if required:
            raise RuntimeError("%s为空或格式不对（期望数组）" % what)
        return []
    if required and not data:
        raise RuntimeError("%s为空或格式不对（期望数组）" % what)
    return data


_PUNCT = "，。！？；：、,.;:!?…—～~\"'“”‘’（）()【】[]《》<>"


def _norm(s: Any) -> str:
    """去掉标点与空白，只留实义字符。用于跨节点比对文本。"""
    return "".join(c for c in str(s or "") if c not in _PUNCT and not c.isspace())


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _voice_durations(texts: List[str], voice_segs: List[Dict[str, Any]]) -> List[Any]:
    """把配音的逐句时长映射到文案句上，返回与 texts 等长、元素为 float 或 None。

    为什么需要对齐：配音节点会把长句按逗号再切一刀、把只剩标点的碎片并进下一句，
    所以它的句数和文案句数不一定相同。按 index 硬对会在切碎时整体错位。

    **前提是先确认两边说的是同一段话**（去标点后拼接相同）。只判句数是不够的：
    接错一个不相关的配音、句数碰巧一样，就会静默按 index 错位，时间轴全歪而
    没有任何告警——宁可退回估算并报出来，也不要静默对错。

    文本序列确认一致后，分两种切分：
      1. 句数相同 → 直接按 index
      2. 句数不同（配音把长句切碎了）→ 按字符位置把时长切分到文案句上
    都对不上 → 返回全 None，调用方退回按字数估算并记 warning。
    """
    n = len(texts)
    if not voice_segs:
        return [None] * n

    vt = [_norm(v.get("text")) for v in voice_segs]
    tt = [_norm(t) for t in texts]
    joined_v = "".join(vt)
    joined_t = "".join(tt)
    if not joined_t or joined_v != joined_t:
        return [None] * n

    if len(voice_segs) == n:
        ds = [_num(v.get("duration")) for v in voice_segs]
        if all(d > 0 for d in ds):
            return ds

    out: List[Any] = []
    j = 0          # 配音句游标
    offset = 0     # 当前配音句已被用掉的字符数
    for want in tt:
        need = len(want)
        got = 0.0
        while need > 0 and j < len(vt):
            cur = vt[j]
            avail = len(cur) - offset
            if avail <= 0:
                j += 1
                offset = 0
                continue
            take = min(avail, need)
            dur = _num(voice_segs[j].get("duration"))
            got += dur * (take / max(len(cur), 1))
            need -= take
            offset += take
            if offset >= len(cur):
                j += 1
                offset = 0
        out.append(round(got, 3) if got > 0 else None)
    if all(d and d > 0 for d in out):
        return out
    return [None] * n


class TimelineBuildNode(NodeBase):

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        store = ctx["store"]
        matches = _as_list(store, inputs.get("match"), "匹配结果")
        scenes = _as_list(store, inputs.get("scenes"), "场景段")
        voice_segs = _as_list(store, inputs.get("voice_segments"), "配音逐句", required=False)

        try:
            cps = float(params.get("chars_per_second", 4.5) or 4.5)
        except (TypeError, ValueError):
            cps = 4.5
        cps = min(max(cps, 1.0), 20.0)
        try:
            min_sec = float(params.get("min_clip_sec", 1.0) or 1.0)
        except (TypeError, ValueError):
            min_sec = 1.0
        try:
            max_sec = float(params.get("max_clip_sec", 12.0) or 12.0)
        except (TypeError, ValueError):
            max_sec = 12.0
        max_sec = max(max_sec, min_sec)
        fit = str(params.get("fit_mode", "truncate") or "truncate").strip().lower()
        if fit not in ("truncate", "extend"):
            fit = "truncate"

        n_scenes = len(scenes)
        # 片段总长（用于 extend 模式的越界判断）
        total_end = 0.0
        for s in scenes:
            try:
                total_end = max(total_end, float(s.get("end") or 0))
            except (TypeError, ValueError):
                continue

        # 配音逐句时长：有就按它排片，没有就退回按字数估。
        # 对齐必须整批成功或整批退回——一半用配音一半用估算，时间轴会在中间跳一下。
        texts = [str(it.get("text") or "") for it in matches]
        voice_durs = _voice_durations(texts, voice_segs)
        voice_used = sum(1 for d in voice_durs if d)
        duration_source = ("voice" if voice_used == len(matches)
                           else "mixed" if voice_used else "estimate")

        warnings: List[str] = []
        if voice_segs and not voice_used:
            warnings.append(
                "配音逐句（%d 句）与文案句（%d 句）对不上，本次退回按字数估算；"
                "时间轴与配音会不同步，请检查两边的切句"
                % (len(voice_segs), len(matches)))
        out: List[Dict[str, Any]] = []
        short = 0
        voice_cut = 0
        used_scenes: List[int] = []
        reused = 0
        out_cursor = 0.0          # 成片时间游标：与源片位置无关，永远连续

        for i, it in enumerate(matches):
            text = str(it.get("text") or "")
            chars = int(it.get("chars") or len(text) or 0)
            vd = voice_durs[i] if i < len(voice_durs) else None
            from_voice = bool(vd and vd > 0)
            if from_voice:
                # 有配音时，内容时长就是配音时长，**不能**再按 max_sec 截断——
                # 截了就是音画不同步。画面不够长是另一件事，按 fit_mode 处理并单独告警。
                need = float(vd)
            else:
                need = max(chars / cps, min_sec)
                need = min(need, max_sec)

            picks = it.get("scenes") or []
            try:
                si = int(picks[0].get("index", 0)) if picks else 0
            except (AttributeError, TypeError, ValueError):
                si = 0
            if si < 0 or si >= n_scenes:
                warnings.append("第 %d 句的场景索引 %d 越界，已夹到范围 0..%d"
                                % (i + 1, si, n_scenes - 1))
                si = min(max(si, 0), n_scenes - 1)

            scene = scenes[si]
            try:
                s_start = float(scene.get("start") or 0.0)
                s_end = float(scene.get("end") or (s_start + need))
            except (TypeError, ValueError):
                s_start, s_end = 0.0, need
            avail = max(s_end - s_start, 0.0)

            start = s_start
            if fit == "extend":
                end = min(start + need, total_end or (start + need))
            else:
                end = min(s_end, start + need)
            if end - start < min_sec and s_end > start:
                end = min(s_end, start + min_sec)

            dur = round(max(end - start, 0.0), 3)
            fits = avail + 1e-6 >= need
            if not fits:
                short += 1
                if from_voice:
                    voice_cut += 1
            # 源片时间与成片时间是两回事，必须分开存：
            #   start/end         —— 这段素材在原片里的位置（同一段用两次就会重叠）
            #   out_start/out_end —— 它在成片上的位置（永远连续、单调递增）
            # 下游排片只能用后者：用前者时，第 3 句会跳回片头与第 1 句重叠。
            if si in used_scenes:
                reused += 1
            used_scenes.append(si)
            out_start = out_cursor
            out_cursor = round(out_cursor + dur, 3)
            out.append({
                "index": i,
                "scene_index": si,
                "text": text,
                "chars": chars,
                "start": round(start, 3),
                "end": round(end, 3),
                "out_start": round(out_start, 3),
                "out_end": out_cursor,
                "duration": dur,
                "need": round(need, 3),
                "fits": fits,
                "duration_source": "voice" if from_voice else "estimate",
            })

        if short:
            warnings.append(
                "%d 句的画面不够长（fit_mode=%s）：%s。截断更稳，延长会把下一段画面带进来"
                % (short, fit, "已按场景末尾截断" if fit == "truncate" else "已越过场景边界"))

        if voice_cut:
            warnings.append(
                "%d 句的配音比可用画面长，按 fit_mode=%s 截断后配音会被切断（音画不同步）。"
                "要么放宽 max_clip_sec、用 fit_mode=extend，要么给这句换更长的场景"
                % (voice_cut, fit))

        if reused:
            warnings.append(
                "%d 句复用了已用过的场景（场景 %d 段 < 句子 %d 句）。"
                "源片时间因此会重叠，成片时间不会——排片请用 out_start/out_end，"
                "不要用 start/end" % (reused, n_scenes, len(matches)))
        total_dur = round(sum(x["duration"] for x in out), 3)
        timeline = store.save_asset(
            Asset(id="", kind=PortType.TIMELINE.value,
                  meta={"count": len(out), "total_duration": total_dur,
                        "chars_per_second": cps, "fit_mode": fit,
                        "duration_source": duration_source,
                        "voice_lines": voice_used,
                        "short_count": short, "voice_cut_count": voice_cut,
                        "reused_count": reused,
                        "warnings": list(warnings),
                        "degraded": bool(short or reused or (voice_segs and not voice_used))}),
            payload=out)
        return {"timeline": timeline}
