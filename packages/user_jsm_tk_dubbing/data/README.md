# -*- coding: utf-8 -*-
"""C4 TK 配音流水线 · TK prompt 资源草稿（S2-T0 收集）。

用途：供 S2-T3 `user/jsm_tk_dubbing` 编排节点加载的口播/洗稿/翻译 prompt 资源。
来源：jieshuomao 侦察产物 `_recon/r3_tts_introspect.txt`（core.tk_dubbing.text_processor
MODCONST 常量），干净重写为插件资源（红线：不复刻 pyd 受限逻辑，只迁移 prompt 素材。

三份 prompt 均已入此目录草稿：
  tk_rewrite.txt        —— DEFAULT_REWRITE_PROMPT（字幕洗稿/重写润色）
  tk_translate.txt      —— DEFAULT_TRANSLATE_PROMPT（逐条翻译为目标语言）
  cantonese_to_mandarin.txt —— DEFAULT_CANTONESE_NORMALIZE_PROMPT（粤转普）

本目录属 packages/user_jsm_tk_dubbing/data/（插件包资源，随包分发）。
S2-T3 节点实现时：node.py 从包内 data/ 读取（os.path.join(os.path.dirname(__file__), 'data')）。
"""