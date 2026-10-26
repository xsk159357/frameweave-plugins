# 报告：workflows meta 元数据治理（2026-10-26）

> 执行：`plugins-dev`（团队 `fw-split-20261026`，任务 **t7**）
> 范围：`frameweave-plugins/workflows/*/meta.json` + `sdk/`（新检查器与串接）+ `README.md` + 本报告。
> 一句话结论：**选方向 A（修好 + 装门禁）** —— 25 条 meta 的 `workflow_sha256` 与 `updated_at`
> 全部刷新为可复核的真实值（实测 16 条哈希过期、25 条时间戳与 git 不符），新增只读检查器
> `sdk/check_workflow_meta.py`（M1–M5）并串进 `sdk/check_all.py --with-workflows`，实跑 **exit 0**。

---

## 0. 结论速览

| 项 | 治理前 | 治理后 |
|---|---|---|
| `workflow_sha256` 与真实 `workflow.json` 一致 | 9 / 25（**16 条过期**） | **25 / 25** |
| `updated_at` 与 `workflow.json` 最后提交时间一致 | 0 / 25 | **25 / 25** |
| `meta` ↔ `workflow.json` 基本字段（id/name/version/kind）不一致 | 0（4 条按旧口径误报，见 §2） | **0** |
| 校验入口 | 无（字段是死元数据） | `sdk/check_workflow_meta.py`（M1–M5，只读）+ `check_all.py --with-workflows` |
| 自检实跑 | — | `[wf-meta] 校验 25 个工作流：25 OK / 0 FAILED`，**exit 0** |
| `workflow.json` 语义内容改动 | — | **0 字节**（`git diff -- 'workflows/*/workflow.json'` 为空） |

> **事实更正**：任务背景引用的「15/25 过期」实测为 **16/25**（当时 `jsm_video_narration` 也已过期，
> t6 的 15 少算 1 条）。方向与工作量不受影响，本报告按实测 16 记账。

---

## 1. 盘点方法与命令

```bash
# 逐目录：真实 sha256 = workflow.json 原样字节的 sha256；真实 updated_at = 该文件的最后一次提交（committer）
python .tmp/t7-metagov/inventory.py            # 打印全量盘点表，--json 落盘快照
# 内部等价命令（单条）：
sha256sum frameweave-plugins/workflows/<id>/workflow.json
git -C frameweave-plugins log -1 --format='%ct%x09%cI' -- workflows/<id>/workflow.json
```

- 一致性口径：`int(meta.updated_at) == git committer epoch`（精确相等，不留容差）。
- 目录完整性：`workflows/` 下 **25 个目录全部齐备** `workflow.json` + `meta.json`，无缺件（按任务要求只核对，未补建）。
- 25 条 meta 字段集合完全一致：`aligned_to | id | kind | name | source | updated_at | version | workflow_sha256`。

---

## 2. 全量盘点表（治理前，25 行）

「workflow.json 最后提交」列全部是 `2026-10-24T20:14:18+08:00 (1792844058)`——即本仓首次入库提交，
只有 `jsm_video_narration` 例外（t6 回灌后的 `2026-10-26 19:23` 提交）。因此「updated_at 不一致 25/25」
不是 25 个独立事故，而是**同一个根因**：meta 里的时间戳来自旧机器上生成器 `time.time()`（2026-10-17），
从未与 git 对齐过。

| id | 记录 sha256 | 实际 sha256 | 一致 | updated_at 记录 | workflow.json 最后提交(committer) | updated_at 一致 |
|---|---|---|---|---|---|---|
| `265a8c12a785` | `3b70933a8df0c46c` | `2472d3969622fd9d` | ❌ | 1792262478.2186859 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `bd493c2a9ab6` | `dd6f2b334ea16031` | `8487e0f984fa601c` | ❌ | 1792262478.2201817 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `dc6590feb7dc` | `03ed91bb32f18608` | `a31dbf43d131571d` | ❌ | 1792262478.2194157 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `f2400f44fe2b` | `fb403ccb21351061` | `a7118ba52de39b78` | ❌ | 1792262478.2209995 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `fw_batch_mixcut` | `0084a2310628ab42` | `9cda8048453f3355` | ❌ | 1792266701.097808 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `fw_bilingual` | `7bedc68afe2e84b1` | `457852476a9c6a92` | ❌ | 1792266701.093063 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `fw_jsm_narration_pipeline` | `f20b6dfbf5c0b5d3` | `b3484109cef8c64a` | ❌ | 1792262478.2246726 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `fw_oneclick` | `986cdbb66f9c9a48` | `ac169817896796ea` | ❌ | 1792262478.2256265 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `jsm_batch_pipeline` | `66833538e09e4e9b` | `66833538e09e4e9b` | ✅ | 1792265081.4016874 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `jsm_curated_remake` | `ced47d63a1a37d44` | `ced47d63a1a37d44` | ✅ | 1792265081.4000115 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `jsm_highlight_mixcut` | `22b9cf280b494316` | `22b9cf280b494316` | ✅ | 1792265081.3995192 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `jsm_image_studio` | `0672b426b41f6b61` | `0672b426b41f6b61` | ✅ | 1792265081.4008715 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `jsm_koubo` | `e0cfb69cc278cdf6` | `e0cfb69cc278cdf6` | ✅ | 1792265081.3989077 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `jsm_script_doctor` | `7c37ed18889ae68b` | `7c37ed18889ae68b` | ✅ | 1792265081.4004571 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `jsm_tk_dubbing` | `e0adc1bfa01a9acd` | `e0adc1bfa01a9acd` | ✅ | 1792265081.3992178 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `jsm_video_narration` | `0e6b22913c441b52` | `3c737283c10de9e9` | ❌ | 1792265081.3984878 | 2026-10-26T19:23:42+08:00 (1793013822) | ❌ |
| `jsm_video_tools` | `c84e912e3a3ec591` | `c84e912e3a3ec591` | ✅ | 1792265081.401401 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `jsm_vocal_separate` | `cd2b519753465107` | `cd2b519753465107` | ✅ | 1792265081.4011369 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `minimal` | `0aa7b741d1ec8142` | `ffe033a8390f2e59` | ❌ | 1792262478.2178814 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `oneclick` | `e93d2121ea4ff972` | `bbc5e16a97ee785a` | ❌ | 1792262478.2169065 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `wf_batch_narration` | `437ebc40234764b0` | `e8147cdfada70bd4` | ❌ | 1792266701.099749 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `wf_highlight_mix` | `8831b0ed8fd6ae92` | `0415a67f79d0851c` | ❌ | 1792262478.2276607 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `wf_narration_standard` | `7820338b63137773` | `208a73cf087e4908` | ❌ | 1792262478.2284892 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `wf_subtitle_polish` | `c0456f7d50e40f2a` | `59bd4d186d68dcd8` | ❌ | 1792266701.0948548 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |
| `wf_vertical_talk` | `d6da804b762a5a4d` | `f74750ef484792c1` | ❌ | 1792266701.0962865 | 2026-10-24T20:14:18+08:00 (1792844058) | ❌ |

合计 25 个工作流：sha256 不一致 **16**，updated_at 不一致 **25**

**基本字段核对（M5）**：25 条里 `id/name` 全部一致；4 条「`version`/`kind` 缺对侧」的是**实例型工作流**
（`265a8c12a785`、`bd493c2a9ab6`、`dc6590feb7dc`、`f2400f44fe2b`）——它们是 `backend/data/workflows`
清洗出来的运行实例，顶层只有 `id/name/nodes/edges`（+ `created_at/updated_at/pipeline_*`），
既无 `tpl_id` 也无 `version/kind`。检查器按「模板型看 `tpl_id`、实例型看 `id`；workflow 未声明 `version`/`kind`
时跳过对应项」处理，**不把这些判成失败，也不去猜值**（用 `meta.version=1.0.0` 反推实例版本就是猜）。

---

## 3. 选定方向：A（修好 + 门禁），以及为什么不选 B

**先摆事实**（工作区 grep 全量，排除 `_archive/` 与团队状态目录）：

```
grep -rn "workflow_sha256" --include=*.py --include=*.ps1 --include=*.js --include=*.ts --include=*.json --include=*.md .
→ 命中只有 3 类：
  1) frameweave-plugins/workflows/*/meta.json        （数据本身）
  2) .tmp/build_artifacts_t5.py:56                   （写入方：生成 workflows 目录时写 hash）
  3) .tmp/repair_server_wf_t8.py:182                 （写入方：修复后刷 hash）
平台后端 `FrameWeave/backend/app/**`、`FrameWeave/scripts/**` 零命中；
工作流导出 zip（t5/t8 脚本）只装 `workflow.json`，不带 meta.json。
```

所以：**该字段没有任何运行时/发布链消费者**，按任务给的 B 条件（grep 不到读取方）本来成立。
仍然选 A，理由按重要性排：

1. **它是目录级内容锚，正好补 git 补不上的那一层**。`workflows/<id>/` 会被整目录复制/导出/交给第三方
   （本仓 25 个目录里有 24 个不是内置模板），离开 git 后就没有任何东西能证明「这份 meta 描述的就是这份
   `workflow.json`」。删掉字段等于放弃这层能力，且要改 25 个文件 + README 约定。
2. **半死状态的真正病根是"没有闸门"，不是"有这个字段"**。删字段能止血，但同样的病会在别处复发
   （本仓刚在 t2 修过"只存在于被忽略目录的修复"）。修好 + 装只读门禁，是把一类问题一次性关掉。
3. **成本极低且可验证**：25 条刷新是一次性脚本（`meta` 行级 diff，41 insert / 41 delete，不动
   `workflow.json` 一字节），检查器纯标准库 + 只读、25 个目录 0.3 s 跑完。
4. **B 的收益是"少维护一个字段"，A 的收益是"多一条可复核的不变量"**。在一个由多会话/多 agent 共同维护
   的仓里，后者价值更高——README 写的约定没有闸门就会腐烂，这次 16/25 就是实证。

**为了让 A 不只是"自证自洽"，检查器特意多查了 M5**（meta 的 id/name/version/kind ↔ workflow 顶层）：
这是能抓到真错误的交叉校验（例：workflow 升了 `version` 而 meta 没跟，导出/上架时就会带着旧版本号走）。

---

## 4. 落地内容（最小 diff）

| 改动 | 数量 | 说明 |
|---|---|---|
| `meta.json` 的 `workflow_sha256` 刷新 | **16** 条 | 例：`jsm_video_narration` `0e6b2291…` → `3c737283…`（t6 回灌后的真实字节）；`minimal` `0aa7b741…` → `ffe033a8…` |
| `meta.json` 的 `updated_at` 对齐 | **25** 条 | 取值 = `git log -1 --format=%ct -- workflows/<id>/workflow.json`：24 条 → `1792844058`（2026-10-24T20:14:18+08:00），`jsm_video_narration` → `1793013822`（2026-10-26T19:23:42+08:00） |
| 合计改动 meta 文件 | **25** 个 | 每个文件只改 1–2 行；`git diff --stat` = 25 files / **+41 −41** |
| `workflow.json` | **0** | `git diff -- 'workflows/*/workflow.json'` 为空 |
| 其他字段（id/name/version/kind/source/aligned_to） | 0 | 未改（已核对一致或不适用） |

写入纪律：一次性刷值脚本（`.tmp/t7-metagov/refresh_meta.py`，未入库）**保留原文件 EOL 与结尾换行**
（本仓 meta 是 CRLF、无结尾换行），否则会变成整文件 diff；复跑幂等（第二次跑显示"已一致"）。
刷新工具**没有放进 `sdk/`**——门禁自己改绿等于没有门禁；`sdk/` 只提供 `--print-desired` 打印应有值。

**`updated_at` 的语义变更（重要，且是有意为之）**：本仓历史从 2026-10-24 的首次入库开始，
旧的 2026-10-17 时间戳无法从 git 验证，属不可复核来源。治理后约定
「`updated_at` = 该 `workflow.json` 最后提交时刻」。原值已在本报告 §2 全表留档，信息不丢失。
若团队需要保留"创作时间"语义，正解是**新增** `authored_at`，而不是让 `updated_at` 兼任两种含义（建议项，见 §9）。

---

## 5. 新增门禁：`sdk/check_workflow_meta.py`（只读，M1–M5）

| 门 | 检查 |
|---|---|
| M1 | `workflows/<id>/` 下 `workflow.json` 与 `meta.json` 都存在 |
| M2 | meta 具备 `id / name / version / kind / source / updated_at / workflow_sha256` 全部字段 |
| M3 | `meta.workflow_sha256 == sha256(workflow.json 原样字节)` |
| M4 | `int(meta.updated_at) == git` 最后提交（committer）时间；非 git 环境（裸目录）自动跳过并告警，或显式 `--no-git` |
| M5 | `meta.id/name/version/kind` ↔ `workflow.json` 顶层（`tpl_id` 优先、其次 `id`；未声明 `version/kind` 时跳过） |

用法与退出码：

```bash
python sdk/check_workflow_meta.py                 # 0 = 全过 / 1 = 有不一致 / 2 = 环境缺失（目录不存在/非 git 且未 --no-git）
python sdk/check_workflow_meta.py --json          # 机器可读（含 desired/actual，供 CI 直接取）
python sdk/check_workflow_meta.py --print-desired  # 打印每个 meta 的应有值（不写盘）
python sdk/check_workflow_meta.py --no-git         # 跳过 M4
```

**串接 `check_all.py`（`--with-workflows` 显式开关）**：

```bash
python sdk/check_all.py                    # 只校验包（保持旧语义，末尾提示可加开关）
python sdk/check_all.py --with-workflows   # 包 + workflows meta；任一门失败 exit 1
python sdk/check_all.py --only-workflows   # 只校验 workflows meta
```

为什么是显式开关而不是改默认：`check_all.py` 的既有调用方（含本仓 README、历史报告、他人脚本）
都按「只校验包」理解它，把默认行为悄悄扩大 = 把他人的绿门变红门/或反之产生假红；
任务给的两条路（串进 check_all / 写进 README 自检清单）**两条都做了**：
README「典型提交前自检」已改为 `python sdk/check_all.py --with-workflows`，并明确「动了 workflows 就必须带开关」。

---

## 6. 治理后复核

```bash
$ python sdk/check_workflow_meta.py
[wf-meta] 校验 25 个工作流：25 OK / 0 FAILED        # exit 0

$ python sdk/check_all.py --with-workflows
[check_all] 校验 96 个包：96 OK / 0 FAILED
[wf-meta] 校验 25 个工作流：25 OK / 0 FAILED
[check_all] 门禁汇总：包=OK / workflows-meta=OK → PASS   # exit 0

$ python .tmp/t7-metagov/inventory.py                  # 全量盘点（与 §2 同口径）
合计 25 个工作流目录：sha256 不一致 0，updated_at 不一致 0，meta↔workflow 基本字段不一致 0

$ python FrameWeave/scripts/sync-builtin-templates.py --check
[tpl-sync] jsm_video_narration: ✅ 一致（nodes=17 edges=35）   # exit 0（meta 改动不影响模板闸门）
```

| id | 记录 sha256 | 实际 sha256 | 一致 | updated_at 记录 | workflow.json 最后提交(committer) | updated_at 一致 |
|---|---|---|---|---|---|---|
| `265a8c12a785` | `2472d3969622fd9d` | `2472d3969622fd9d` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `bd493c2a9ab6` | `8487e0f984fa601c` | `8487e0f984fa601c` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `dc6590feb7dc` | `a31dbf43d131571d` | `a31dbf43d131571d` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `f2400f44fe2b` | `a7118ba52de39b78` | `a7118ba52de39b78` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `fw_batch_mixcut` | `9cda8048453f3355` | `9cda8048453f3355` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `fw_bilingual` | `457852476a9c6a92` | `457852476a9c6a92` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `fw_jsm_narration_pipeline` | `b3484109cef8c64a` | `b3484109cef8c64a` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `fw_oneclick` | `ac169817896796ea` | `ac169817896796ea` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `jsm_batch_pipeline` | `66833538e09e4e9b` | `66833538e09e4e9b` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `jsm_curated_remake` | `ced47d63a1a37d44` | `ced47d63a1a37d44` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `jsm_highlight_mixcut` | `22b9cf280b494316` | `22b9cf280b494316` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `jsm_image_studio` | `0672b426b41f6b61` | `0672b426b41f6b61` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `jsm_koubo` | `e0cfb69cc278cdf6` | `e0cfb69cc278cdf6` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `jsm_script_doctor` | `7c37ed18889ae68b` | `7c37ed18889ae68b` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `jsm_tk_dubbing` | `e0adc1bfa01a9acd` | `e0adc1bfa01a9acd` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `jsm_video_narration` | `3c737283c10de9e9` | `3c737283c10de9e9` | ✅ | 1793013822 | 2026-10-26T19:23:42+08:00 (1793013822) | ✅ |
| `jsm_video_tools` | `c84e912e3a3ec591` | `c84e912e3a3ec591` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `jsm_vocal_separate` | `cd2b519753465107` | `cd2b519753465107` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `minimal` | `ffe033a8390f2e59` | `ffe033a8390f2e59` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `oneclick` | `bbc5e16a97ee785a` | `bbc5e16a97ee785a` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `wf_batch_narration` | `e8147cdfada70bd4` | `e8147cdfada70bd4` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `wf_highlight_mix` | `0415a67f79d0851c` | `0415a67f79d0851c` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `wf_narration_standard` | `208a73cf087e4908` | `208a73cf087e4908` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `wf_subtitle_polish` | `59bd4d186d68dcd8` | `59bd4d186d68dcd8` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |
| `wf_vertical_talk` | `f74750ef484792c1` | `f74750ef484792c1` | ✅ | 1792844058 | 2026-10-24T20:14:18+08:00 (1792844058) | ✅ |

合计 25 个工作流目录：sha256 不一致 **0**，updated_at 不一致 **0**，meta↔workflow 基本字段不一致 **0**
meta 字段集合（去重）: aligned_to|id|kind|name|source|updated_at|version|workflow_sha256

---

## 7. 负向测试（证明闸门真的会红，不是恒绿）

1. **M3 触发**（在 `workflows/` 的临时副本里把 `minimal` 的 `workflow_sha256` 改成 64 个 0）：

```
$ python frameweave-plugins/sdk/check_workflow_meta.py --workflows .tmp/t7-metagov/wf_copy
[wf-meta] 警告：...\.tmp\t7-metagov 不是 git 工作区，自动跳过 M4（等价 --no-git）
  FAIL minimal
       - M3 workflow_sha256 过期：meta=0000000000000000… / 实际=ffe033a8390f2e59…
[wf-meta] 校验 25 个工作流：24 OK / 1 FAILED（已跳过 M4 时间戳）      # exit 1
```

2. **M4 触发**（临时把真实 `minimal/meta.json` 的 `updated_at` 改成 `1700000000`，测后用 t7 刷值脚本恢复）：

```
$ python frameweave-plugins/sdk/check_workflow_meta.py
  FAIL minimal
       - M4 updated_at 与最后提交时间不符：meta=1700000000 / git=1792844058
[wf-meta] 校验 25 个工作流：24 OK / 1 FAILED                          # exit 1
```

3. **非 git 目录**：自动跳过 M4 并打印告警（不是静默通过），其余门照常判。

---

## 8. 边界与"没做什么"

- **未改 `workflow.json` 任何字节**（`git diff -- 'workflows/*/workflow.json'` 为空）；只碰 meta 元数据、`sdk/`、`README.md`、本报告。
- **未改 `FrameWeave/**`**；**未运行** `sync-builtin-templates.py --write`（只跑过 `--check` 验证 meta 改动不影响模板闸门，仍 exit 0）。
- **未补建任何文件**：25 个目录本来就齐（没有缺 meta.json / 缺 workflow.json 的情况）。
- **未把刷值脚本入库**：`sdk/` 里只有只读检查器（防止"门禁自己改绿"）；刷值脚本在 `.tmp/t7-metagov/refresh_meta.py`，可复现。
- **未动 4 个实例型工作流的语义**：它们的 meta 里 `version=1.0.0` 与 workflow 无 `version` 字段的"不对称"保留原样，只做"不比对"处理（§2 已说明理由）。

---

## 9. 建议（本轮不做，供后续排单）

1. **把 `check_all.py --with-workflows` 钉进 CI / pre-commit**：目前它靠人记得带开关。平台侧 `build-installer.ps1`
   已在调 `sync-builtin-templates.py --check`，可顺手加一行本仓自检（跨仓调用需注意路径）。不钉死的话，
   下一次"改完 workflow.json 忘了刷 meta"仍会漏网——只是这次有 `--print-desired` 能一秒给出应写值。
2. **`authored_at`（可选）**：若要保留 2026-10-17 那批创作时间语义，新增字段而不是复用 `updated_at`。
3. **`meta.source` 值域**：现状有 `official` / `builtin-template` / `backend-data-workflow(cleaned)` 三种写法
   （第三种像备注而非枚举值），建议收敛成枚举并写进 README，检查器再加一道 M6。
4. **meta 字段集与 `workflow.json` 的 `tags/description`**：`meta` 不含 `tags`，检查器未比对；若以后用 meta 做商城卡片，
   需要把 `tags/description` 也纳入 M5。
