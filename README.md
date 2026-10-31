# frameweave-plugins — 节点与工作流的源码正本仓

> FrameWeave 平台与节点/工作流**分仓开发**（2026-10-26 收口）。
> 本仓 = **节点包源码 + 工作流源码 + 开发 SDK + 发布产物**；
> 平台仓 `FrameWeave/` = 软件本体（后端 / 桌面 / 前端 / 契约规范）。
> 两边各自独立提交、独立发布；平台侧不再保存任何节点源码的唯一副本。
>
> **⚠ 2026-10-29 整仓重做（v3）**：旧 96 个 v2 包已全部移出，按 v3 契约重建为
> 现役 18 包；旧版完整快照保存在 tag `pre-remake-20261029`，旧源码与旧 dist
> 归档在 `_remake_archive/`（已 gitignore，不入库，以 tag 为准）。本 README 已
> 重写对齐 v3 现状（2026-10-31）；文末「v2 历史基线」一节仅作存档。

## 1. 仓库边界（谁放什么）

| 内容 | 本仓（`frameweave-plugins/`） | 平台仓（`FrameWeave/`） |
|---|---|---|
| 节点包源码（`manifest.json` + `node.py` + 资源） | ✅ **唯一正本** `packages/<pkg>/` | ❌ 不存源码；`backend/user_nodes/` 仅是**运行时落地目录**（被平台 `.gitignore:27` 整目录忽略） |
| 工作流定义（`workflow.json` + `meta.json`） | ✅ **唯一正本** `workflows/<id>/` | ❌ 平台侧只放内置/出厂模板的构建产物（`app/builtin_templates/`，由 `sync-builtin-templates.py --write` 单向生成） |
| 插件 / 节点开发文档 | ✅ `docs/`（规范、第三方指南、迁移报告） | 只保留**平台契约规范** `docs/specs/` |
| 开发 SDK、打包、签名、卫生工具 | ✅ `sdk/` | — |
| 发布产物（zip + `.sig`） | ✅ `dist/` | ❌ 由本仓产出后被平台/商城消费 |
| 后端代码、桌面端、前端、安装器、更新体系 | ❌ | ✅ `backend/` `web/` `desktop/` `scripts/` … |
| 官方公钥锚 | 消费方：`sdk/*.py` 从平台仓读取 | ✅ `backend/app/fw_official_pubkey.json`（`key_id=a8998646b062c170`） |
| 发布私钥 | ❌ 绝不放本仓 | ✅ `backend/data/sign-keys/signing.key`（签名只在平台 venv 内执行） |

**硬约束**：`core/` 前缀只给官方签名包；社区/第三方包用 `user/`（v3 起 `user/` 前缀
是**命名空间归属**，官方身份由官方锚签名判定，A1 决议——见
`sdk/check_manifest_v3.py` 头部）。签名一律用平台发布私钥 + 官方锚校验，
**私钥不进本仓、不进聊天、不硬编码**。

## 2. 目录结构

```
frameweave-plugins/
├── packages/                 # 节点包源码正本（v3 现役 18 包，全部 core_*）
├── workflows/                # 工作流源码正本（4 条，每条含 workflow.json + meta.json）
├── dist/                     # 发布产物快照：18 个 <pkg>.zip + .sig + hygiene_report.json
├── docs/                     # 节点/工作流文档（索引见 docs/README.md）
├── sdk/                      # 校验 / 打包 / 签名 / 卫生工具（含 check_manifest_v3.py）
├── scripts/                  # 一次性运维脚本（模型拉取、包内单测）
├── _remake_archive/          # 旧 v2 时代归档（gitignored；权威旧版 = tag pre-remake-20261029）
└── .tmp/                     # 本地草稿（已 gitignore）
```

### 2.1 v3 现役 18 包（2026-10-31）

全部 `core/` 前缀、全部官方锚验签通过。一条「视频解说出片」链的完整能力集：

| 梯队 | 包（type_id / 版本） | 说明 |
|---|---|---|
| AI 核心 | `core/ai_narrate` 3.0.0（1097 行） | 六模式解说词：上帝视角/扩写/配画面/仿写/神回复/透传；OpenAI+Gemini 双协议 |
| | `core/llm_call` 2.0.0 | 统一 LLM 调用通道 |
| | `core/analyze_chunks` 2.0.0 | 分块多模态视觉分析 |
| | `core/match_visual` 2.0.0 | Chinese-CLIP 视觉配画面 |
| 媒体 | `core/media_import` 1.0.0 ｜ `core/scene_detect` 1.1.0（TransNetV2）｜ `core/smart_chunk` 3.0.0 ｜ `core/asr_whisper` 1.0.0（faster-whisper） | 导入 → 切镜 → 分块 → 转写 |
| 音频 | `core/tts_synthesize` 1.3.0 | 多引擎配音；`voice_ref` 端口可自动从视频抽参考音 |
| 剪辑/输出 | `core/script_input` ｜ `core/script_extract` ｜ `core/match_clip` ｜ `core/timeline_build` ｜ `core/subtitle_build` ｜ `core/subtitle_compose` ｜ `core/export_bundle` ｜ `core/export_premiere_xml` ｜ `core/video_render` 1.1.0 | 文案 → 排片 → 字幕 → 片段包 / Pr XML / MP4 |

### 2.2 `packages/<pkg>/` 目录约定（v3 契约）

一个包 = 一个目录，**顶层直接放 `manifest.json`**（平台 `declarative.scan()` 按 `<dir>/manifest.json` 扫描）：

| 文件 | 必需 | 说明 |
|---|---|---|
| `manifest.json` | ✅ | 唯一契约。**v3 现行**（`manifest_version: 3`）：端口 6 键、参数 `[{value,label}]`、`perm` 五作用域结构化、`exposable` 向向导暴露；校验器 `sdk/check_manifest_v3.py`（V1–V13）。v2 兼容通道仍可用（`check_manifest.py`），新包一律写 v3 |
| `node.py` | `kind=python` 必需 | 导出 manifest `class_name` 指定的类，继承 `NodeBase`，提供 `spec()` 与 `run()` |
| `data/` `models/` `ui/` `web/` | 可选 | 资源子目录；`ui.entry` 必须在 `ui/` 子树，`web.entry` 必须在 `web/` 子树 |
| `__pycache__/` `*.pyc` | 🚫 | 卫生红线：`package_plugin.py` / `hygiene_dist.py` 会拒绝/清理 |

命名规则：目录名 `snake_case`；`manifest.id` 用 `前缀/名字`（如 `core/video_render`），
前缀与目录前缀一致；版本 `semver`，任何影响行为的改动必须升版本。

### 2.3 `workflows/<id>/` 工作流正本（现役 4 条）

| 工作流 | 节点/边 | 用户路径（05 审查实测） |
|---|---|---|
| `local_ai_narrate_001` | 10 / 20 | AI 写文案出片——填视频 + API Key 可跑，最完整 |
| `local_no_model_001` | 9 / 13 | ✅ 填视频 + 文案两个框就能出片 |
| `local_transcribe_001` | 5 / 6 | 原片加字幕；需先下 faster-whisper 模型（139MB） |
| `local_voice_001` | 10 / 17 | 文案 → 机器配音 → 按实测时长排片 → 渲染 |

| 文件 | 内容 |
|---|---|
| `workflow.json` | 干净的模板式工作流定义（v3 节点用 `type` 引用 type_id；边 `source/target/sourceHandle/targetHandle`；无运行时状态、无绝对路径、无资产 id） |
| `meta.json` | 版本受控元数据：`id` / `name` / `version` / `kind` / `source` / `updated_at` / `aligned_to` / `workflow_sha256` |

**`meta.json` 两条硬不变量**（由 `sdk/check_workflow_meta.py` 强制，M1–M5）：

- `workflow_sha256` = 同目录 `workflow.json` **原样字节**的 sha256（不是解析后重新序列化的哈希）；
- `updated_at` = 该 `workflow.json` 最后一次提交的 committer 时间（`git log -1 --format=%ct -- workflows/<id>/workflow.json`）。

改完 `workflow.json` 就同步这两项（`python sdk/check_workflow_meta.py --print-desired`
只打印应有值、不写盘）。`meta.id/name/version/kind` 必须与 `workflow.json` 顶层一致。

### 2.4 `dist/` 是发布产物，不是源码

现覆盖 **18 个包**（18 zip + 18 `.sig` + `hygiene_report.json`；parity 18/18 零 drift，
归档验签 18/18 ARCH_SIG_OK）。zip 顶层目录 = 包名；`.sig` 是对 zip 本体的 archive
签名（`over=archive`）。dist 里的文件**不得手工编辑**，一切重新生成。

## 3. 构建与发布入口（`sdk/*.py`）

| 命令 | 用途 | 退出码 |
|---|---|---|
| `python sdk/check_manifest_v3.py <pkg_dir>` | v3 单包 V1–V13 校验（新包必过） | 0 = 过 |
| `python sdk/check_manifest.py <pkg_dir>` | v2 单包 G1-G10（兼容旧包） | 0 = 过 |
| `python sdk/check_all.py [dir]` | 全仓逐包（默认只查包；`signed=True` **不代表验签通过**） | 0 = 全过 |
| `python sdk/check_all.py --with-workflows --with-signature --with-dist` | **提交前必跑**：包 + meta（M1-M5）+ 官方锚验签 + dist 归档验签 | 0 = 全过 |
| `python sdk/check_signature.py [--pkg NAME] [--only-dist]` | 官方锚 ed25519 验签；`NO_SIG` 不豁免；allowlist 只减不增 | 0/1/2 三态 |
| `python sdk/check_workflow_meta.py` | `meta.json` ↔ `workflow.json` 对拍 | 0/1/2 三态 |
| `python sdk/check_dist_parity.py` | dist zip ↔ packages 正本逐文件 sha256 | 0/1/2 三态 |
| `python sdk/check_port_consistency.py [dir]` | manifest 端口声明 ↔ `node.py spec()`（需平台 venv） | 0 = 全一致 |
| `python sdk/package_plugin.py <pkg_dir> [-o dist]` | 校验通过后打 zip（**按 manifest_version 分发校验器**） | 0 = 成功 |
| `sdk/sign_core_manifests.py [pkg...]` | **仅授权时**：平台私钥签 manifest（锚匹配 + 签后自验） | 非 0 = 拒绝 |
| `sdk/hygiene_dist.py` | 清缓存 → 全量重打 → 重签 → 逐文件对拍 + 报告 | 0 = 全 OK |

**签名纪律**：私钥只在平台仓、只在平台 venv 里用；验签只用公钥锚。日常开发**不要**
为了让门禁变绿去重签——先确认 manifest 内容是否本来就该改。
`signature_allowlist.json` 当前 `entries: []`；新增条目须用户裁定。

**典型提交前自检**（已封成 `.githooks/pre-commit`，本机已启用）：

```bash
cd frameweave-plugins
python sdk/check_all.py --with-workflows --with-signature --with-dist   # 四门全绿
```

一键入口启用/关闭：`git config core.hooksPath .githooks` / `--unset`；
紧急放行 `git commit --no-verify`；跳过 dist 门 `SKIP_DIST=1 git commit`。
行尾约定见 `.gitattributes`（`.githooks/**` 强制 LF、`*.cmd` 强制 CRLF，t12 实测）。

## 4. 与平台仓的拉取约定

平台**只从本仓拉取**，不在平台仓库内改节点源码：

1. **出厂基础节点种子**（随安装包内置）：
   ```bash
   cd FrameWeave/backend
   .venv/Scripts/python.exe packaging/build_seed.py
   ```
   - 默认节点源 = 同级本仓 `packages/`（`--source` > `FW_PLUGIN_SOURCE` > 同级约定，缺源 exit 5）。
   - 包的取舍由平台 `build_seed.py:BASIC_NODE_DIRS`（**现役 18 项**）决定，不按目录全量播种。
   - 每包过平台 manifest 校验（内置 v3 分支）+ 官方锚验签（exit 4 缺包 / exit 5 验签失败）；
     与 `packaging/seed_manifest.lock.json` 逐包逐文件比对（不一致 exit 6）；
     改基线需人工批准后 `--write-lock`。
   - **2026-10-31 实测**：白名单 18 包 + 锁基线 12→18 重定（`4351a64`），
     `build_seed` exit 0、锁比对逐文件一致、分离守卫 c-runtime-cache-parity 18/18。
2. **运行时安装**：平台把 zip 解到 `backend/user_nodes/<pkg>/`（或用户数据目录），
   该目录**只是落地位置**。装完不要在该目录里改代码——改了就等于在版本控制之外开发。
3. **商城/分发**：`dist/<pkg>.zip` + `<pkg>.zip.sig` 由本仓产出，平台侧校验
   `over=archive` 签名后安装。
4. **出厂模板**：平台 `app/builtin_templates/<id>.json` 是构建产物，正本在本仓
   `workflows/<id>/workflow.json`；平台 `scripts/sync-builtin-templates.py --check/--write`
   单向同步（改模板先改本仓，再 `--write`，禁止手改平台侧）。

## 5. 现状基线（2026-10-31，v3）

- `packages/` **18 包**：全 `core_*`；`check_all --with-workflows --with-signature --with-dist`
  **四门全绿**（18 OK / 4 工作流 meta OK / 18 SIG_OK / 18 zip 零 drift）。
- `workflows/` **4 条**：`meta.json` 的 `workflow_sha256` / `updated_at` 4/4 一致。
- `dist/` **18 zip + 18 sig**；`hygiene_report.json` 18 条全 consistent。
- 官方锚 `key_id=a8998646b062c170`；allowlist `entries: []`。
- 运行时缓存 `FrameWeave/backend/user_nodes/` 18 包与正本逐文件一致（分离守卫 PASS）。

## 6. 参考

- 节点开发：`docs/plugin-development-spec.md`（v2 规范正本）+ `docs/specs/节点开发规范_20261026.md`（内部契约：平台校验门编号 / 槽位 / 缓存键）
- v3 manifest 规格：`FrameWeave/docs/reports/节点重做_20261029/manifest_v3规格.md` + `sdk/check_manifest_v3.py` 头注（A1 决议）
- 工作流开发：`docs/工作流开发指南_第三方_20261026.md`
- 平台内部契约：平台仓 `FrameWeave/docs/specs/`

---

## 附：v2 历史基线（2026-10-26 存档，已被 v3 重做取代）

<details>
<summary>旧 96 包时代口径（点开查看；勿作为现行依据）</summary>

- `packages/` 96 包：67 `core_*` + 28 `user_*` + `hello`；G1-G10 96/96；验签 92 OK / 4 失效（allowlist 豁免后 exit 0）。
- `workflows/` 25 条：meta 治理 25/25 一致（`docs/元数据治理报告_workflows_meta_20261026.md`）。
- `dist/` 75 zip；证据三件套：`docs/迁移报告_平台独占节点入库_20261026.md`、`docs/验签门报告_20261026.md`、`docs/重签报告_4个user包_20261026.md`。
- 2026-10-29 重做决策与 v3 规格：平台仓 `FrameWeave/docs/reports/节点重做_20261029/`（13 份）。

</details>
