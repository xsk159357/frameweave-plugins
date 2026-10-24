# user/jsm_remake_draft — D4 精选复刻草稿节点

> 依据：[07_实施任务拆解_阶段二.md](../../../docs/reports/项目方向审计/07_实施任务拆解_阶段二.md) S2-T2
> + [r2-match-analysis.md](../../../docs/reports/jieshuomao解说工坊/r2-match-analysis.md) §3（curated_remake_draft 侦察）
> + `_recon/r4_generators_curated_remake_draft_introspect.txt`（模块常量）与
>   `_recon/r4_run/drafts/r4_curated/draft_content.json`（实测产物结构）。

## 功能契约（R-节点-1 后半）

输入「S2-T1 匹配表 + 原片 + 解说视频 + 解说 wav」→ 输出可导入剪映的**精选复刻草稿**
（第 4 模式，jy_builder 新增分支，向后兼容既有 3 模式）。

```
S2-T1 matches ─┐
原片 VIDEO     ├─→ normalize_remake_matches（S2-T1→D4 条目）
解说 VIDEO     ├─→ ffprobe 宽高/时长（解说 + 原片 + wav）
解说 wav      ─┘
                   ↓
build_remake_content(m)：compare / remake_only
                   ↓
write_remake_draft → 8 文件草稿目录 → draft_validator 校验 → report（含 R7 断言）
```

## 两种子模式

| 模式 | 画布 | 轨道 | LABEL |
|---|---|---|---|
| **compare**（默认） | `(cW+rW) x cH`（左右宽和：左=解说 cW，右=原片缩放到高 cH 后宽 rW） | 主轨=整段解说(左半)、覆盖轨=逐匹配原片段(右半，按原片时间轴 1x)、audio=解说wav、2×text=LABEL | 「原解说」(左 render_index 14001) /「复刻后」(右 14004)，黄字 `#ffde00` + 黑描边，scale=`0.8413899931146691`、y=0.72、x 对齐各自视频半区（与 r4_curated 实测产物一致） |
| **remake_only** | 原片 W x H | 单视频轨=逐镜头原片段按解说节奏铺满（target=解说时间，source=ref 时间）、audio=解说wav | 无 |

剪映 transform.x 单位 = 半画布宽：左素材 x = `-rW/canvasW`，右素材 x = `cW/canvasW`（等高各居其半、互不遮挡）。

## R7 修复（不复发）

原版缺陷：commentary wav 作为 video material 写入且 duration=0、无音频轨 → 草稿无声。
本实现：wav 经 ffprobe **实测真实时长**（>0）写入 `materials.audios` + audio 轨 segment，
`report.r7.assertion_ok` 回归断言（wav_duration_sec>0 ∧ material_duration_ok ∧ segment_duration_ok）。
R7 专项测试在 `test_d4.py --node`（合成 wav → ffprobe 3s → 断言 duration==3_000_000US）。

## 依赖

- **core/draft_export**（含扩展后 jy_builder）：D4 分支在 `core_draft_export/jy_builder.py`
  （`build_remake_content` / `write_remake_draft` / `normalize_remake_matches` / D4 常量区），
  既有 `build_draft_content` / 3 模式结构不变（回归：core_mixed_audio_layout 链测 10 OK）。
- 本节点经 `declarative.USER_NODES_DIR`（打包态）→ 插件仓库兄弟目录（源码态）解析 core_draft_export。

## 验证

```bash
# SDK 门禁（G1-G10）
python ../sdk/check_manifest.py .

# 全部测试（纯结构 5 项 + 节点级集成 R7/剪映校验/LABEL）
python test_d4.py --all
#  → test_normalize_matches / compare/remake_only 结构 / 3 模式向后兼容 /
#    reference_conformance（对齐 r4_curated 产物）✅ + node: compare_r7=True remake_only_r7=True label_ok=True

# 依赖链回归（jy_builder 变更不破坏既有消费者）
python ../../core_mixed_audio_layout/test_s3t6_chain.py   # 10 OK
```

## 文件

- `manifest.json` — 插件契约（v2；dependencies: core/draft_export）
- `node.py` — 平台节点入口（资产解析 / ffprobe 探测 / R7 断言 / 双模式编排）
- `test_d4.py` — 验证（纯结构 + 节点集成 + 参考产物对齐 + R7 专项）
- `core_draft_export/jy_builder.py` — 第 4 模式分支（本任务扩展，向后兼容）