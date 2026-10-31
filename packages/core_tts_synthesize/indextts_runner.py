"""IndexTTS 合成 runner——由 core/tts_synthesize 用独立运行时调用。

为什么是独立进程：
    IndexTTS 2.5 要 torch 2.8 / transformers 4.52.1，平台 venv 是 torch 2.11 /
    transformers 5.x（`cache_utils.OffloadedCache` 已被移除），两套装不到一起。
    所以模型跑在自己的 `.venv` 里，节点通过子进程调用它。

为什么一次进程合成整段：
    模型约 9.5 GB（gpt 3.1G + s2mel 396M + codec 580M + hf_cache 5.2G），
    每句重加载一次不可接受。一次加载、逐句合成、逐句落盘。

为什么不用 sys.path 依赖 .pth：
    editable 安装会写 `_editable_impl_indextts.pth`，内容是安装目录的绝对路径。
    装在含中文的路径下时，site 模块按 locale（GBK）解码这个 UTF-8 的 .pth，
    直接 UnicodeDecodeError，连解释器都起不来。这里一律显式 sys.path.insert，
    路径里有什么字符都不影响。

退出码：0 全部成功，stdout 末行是 JSON {"ok": true, "files": [...]}；
非 0 失败，stderr 写明卡在哪一步。
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="IndexTTS 批量合成")
    ap.add_argument("--base", required=True, help="index-tts-code 所在目录")
    ap.add_argument("--lines", required=True, help="JSON 文件：待合成的字符串数组")
    ap.add_argument("--out-dir", required=True, help="逐句 wav 输出目录")
    ap.add_argument("--ref", default="", help="音色参考音频；空则用模型默认音色")
    ap.add_argument("--lang", default="ZH")
    ap.add_argument("--no-bf16", action="store_true", help="关闭 bf16（老卡用）")
    a = ap.parse_args()

    base = os.path.abspath(a.base)
    if not os.path.isdir(base):
        print("模型目录不存在: %s" % base, file=sys.stderr)
        return 2
    sys.path.insert(0, base)

    try:
        with open(a.lines, "r", encoding="utf-8") as f:
            lines = json.load(f)
    except (OSError, ValueError) as e:
        print("读取文案失败: %s" % e, file=sys.stderr)
        return 2
    if not isinstance(lines, list) or not lines:
        print("文案为空", file=sys.stderr)
        return 2
    lines = [str(x) for x in lines]
    os.makedirs(a.out_dir, exist_ok=True)

    try:
        from indextts.infer_v2_5 import IndexTTS2  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        print("导入 IndexTTS 失败: %s" % e, file=sys.stderr)
        return 3

    try:
        tts = IndexTTS2(cfg_path=os.path.join(base, "checkpoints", "config.yaml"),
                        model_dir=os.path.join(base, "checkpoints"),
                        use_bf16=not a.no_bf16)
    except Exception as e:  # noqa: BLE001
        print("加载模型失败: %s" % e, file=sys.stderr)
        return 4

    ref = a.ref if (a.ref and os.path.isfile(a.ref)) else ""
    # IndexTTS 的 spk_audio_prompt 是**必需**的：infer_generator 会把它交给
    # _load_and_cut_audio() 读音频，传 None 直接炸在文件打开那一步
    # （实测报 "Invalid file: None"）。use_random 是控制情绪随机的，
    # 不能替代音色参考——这两个概念在 API 里是分开的。
    #
    # 所以没有参考音频就别装作能跑：明确告诉用户要什么，而不是让他等
    # 模型加载完 20 秒再看到一个原生异常。
    if not ref:
        print("IndexTTS 需要一段音色参考音频（几秒的人声 wav 即可）。"
              "请设置「音色参考音频」参数，或改用 engine=sapi / 云端 TTS。",
              file=sys.stderr)
        return 7
    files = []
    for i, text in enumerate(lines):
        out = os.path.join(a.out_dir, "l%03d.wav" % i)
        kwargs = {"text": text, "lang": a.lang, "output_path": out, "verbose": False,
                  "spk_audio_prompt": ref}
        try:
            tts.infer(**kwargs)
        except Exception as e:  # noqa: BLE001
            print("第 %d 句合成失败: %s" % (i, e), file=sys.stderr)
            return 5
        if not os.path.isfile(out) or os.path.getsize(out) < 1024:
            print("第 %d 句没有产出有效音频: %s" % (i, out), file=sys.stderr)
            return 6
        files.append(os.path.basename(out))
        print("DONE %d/%d" % (i + 1, len(lines)), flush=True)

    print(json.dumps({"ok": True, "files": files}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
