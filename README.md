# frameweave-plugins — 节点与工作流的源码正本仓

> FrameWeave 平台与节点/工作流**分仓开发**（2026-10-26 收口）。
> 本仓 = **节点包源码 + 工作流源码 + 开发 SDK + 发布产物**；
> 平台仓 `FrameWeave/` = 软件本体（后端 / 桌面 / 前端 / 契约规范）。
> 两边各自独立提交、独立发布；平台侧不再保存任何节点源码的唯一副本。

## 1. 仓库边界（谁放什么）

| 内容 | 本仓（`frameweave-plugins/`） | 平台仓（`FrameWeave/`） |
|---|---|---|
| 节点包源码（`manifest.json` + `node.py` + 资源） | ✅ **唯一正本** `packages/<pkg>/` | ❌ 不存源码；`backend/user_nodes/` 仅是**运行时落地目录**（被 `.gitignore:27` 整目录忽略） |
| 工作流定义（`workflow.json` + `meta.json`） | ✅ **唯一正本** `workflows/<id>/` | ❌ 平台侧只放内置/出厂模板的构建输入 |
| 插件 / 节点开发文档 | ✅ `docs/`（规范、第三方指南、迁移报告） | 只保留**平台契约规范** `docs/specs/` |
| 开发 SDK、打包、签名、卫生工具 | ✅ `sdk/` | — |
| 发布产物（zip + `.sig`） | ✅ `dist/` | ❌ 由本仓产出后被平台/商城消费 |
| 后端代码、桌面端、前端、安装器、更新体系 | ❌ | ✅ `backend/` `desktop/` `apps/` `scripts/` … |
| 官方公钥锚 | 消费方：`sdk/*.py` 从平台仓读取 | ✅ `backend/app/fw_official_pubkey.json`（`key_id=a8998646b062c170`） |
| 发布私钥 | ❌ 绝不放本仓 | ✅ `backend/data/sign-keys/signing.key`（签名只在平台 venv 内执行） |

**硬约束**：`core/` 前缀只给官方签名包；社区/第三方包用 `user/`（或已登记前缀）。签名一律用平台发布私钥 + 官方锚校验，**私钥不进本仓、不进聊天、不硬编码**。

## 2. 目录结构

```
frameweave-plugins/
├── packages/                 # 节点包源码正本（96 包：67 个 core_* + 28 个 user_* + hello）
│   ├── core_<name>/          # 官方包：manifest.json + node.py (+ data/ ui/ web/ 资源)
│   ├── user_<name>/          # 社区/业务包：同一目录约定，type_id 前缀 user/
│   └── hello/                # 声明式 transform 示例（无 node.py）
├── workflows/                # 工作流源码正本（25 个 <id>/，每个含 workflow.json + meta.json）
├── dist/                     # 发布产物快照：<pkg>.zip + <pkg>.zip.sig + hygiene_report.json
├── docs/                     # 节点/工作流文档（索引见 docs/README.md）
├── sdk/                      # 校验 / 打包 / 签名 / 卫生工具
├── scripts/                  # 一次性运维脚本（模型拉取等）
└── .tmp/                     # 本地草稿（已 gitignore）
```

### 2.1 `packages/<pkg>/` 目录约定（`core_` / `user_` / 基础节点同一条规则）

一个包 = 一个目录，**顶层直接放 `manifest.json`**（平台 `declarative.scan()` 按 `<dir>/manifest.json` 扫描）：

| 文件 | 必需 | 说明 |
|---|---|---|
| `manifest.json` | ✅ | 唯一契约（`schema_version: 2`）。`inputs` / `outputs` / `params` 三字段必须在（可为空数组） |
| `node.py` | `kind=python` 必需 | 必须导出 manifest 里 `class_name` 指定的类，继承 `NodeBase`，提供 `spec()` 与 `run()` |
| `data/` `models/` `ui/` `web/` | 可选 | 资源子目录；`ui.entry` 必须在 `ui/` 子树，`web.entry` 必须在 `web/` 子树 |
| `__pycache__/` `*.pyc` | 🚫 | 卫生红线：`package_plugin.py` / `hygiene_dist.py` 会拒绝/清理 |

命名规则（`core_*` / `user_*` / 基础节点都一样）：目录名 `snake_case`；`manifest.type_id` 用 `前缀/名字`（如 `core/draft_export`、`user/jsm_translate_batch`），前缀与目录前缀一致；版本 `semver`（`MAJOR.MINOR.PATCH`），任何影响行为的改动必须升版本。

### 2.2 `workflows/<id>/` 工作流正本

| 文件 | 内容 |
|---|---|
| `workflow.json` | 干净的模板式工作流定义（节点 + 边 + 参数，无运行时状态、无绝对路径、无资产 id） |
| `meta.json` | 版本受控元数据：`id` / `name` / `version` / `kind` / `source` / `updated_at` / `aligned_to` / `workflow_sha256` |

分享/上架的工作流一律先落这里，再导出 zip；平台侧模板只是本仓内容的构建输入，不在平台仓单独维护副本。

**`meta.json` 两条硬不变量**（2026-10-26 起由 `sdk/check_workflow_meta.py` 强制，M1–M5）：

- `workflow_sha256` = 同目录 `workflow.json` **原样字节**的 sha256（不是「解析后重新序列化」的哈希）；
- `updated_at` = 该 `workflow.json` **最后一次提交的 committer 时间**（Unix 秒）—— 用 `git log -1 --format=%ct -- workflows/<id>/workflow.json` 取值，不是手写时间。

改完 `workflow.json` 就要同步这两项（`python sdk/check_workflow_meta.py --print-desired` 只打印应有值、不写盘；本仓不提供自动改写入口，避免"闸门自己改绿"）。`meta.id/name/version/kind` 必须与 `workflow.json` 顶层一致（模板型看 `tpl_id`，实例型看 `id`；workflow 未声明 `version`/`kind` 时跳过对应项）。

> 语义说明：`updated_at` 采用 git 提交时刻，是可被机器复核的确定性来源；旧值（2026-10-17 的生成器时间）无法从本仓历史验证，已在 `docs/元数据治理报告_workflows_meta_20261026.md` 全文留档。若要保留"创作时间"语义，应另加 `authored_at` 字段，而不是让 `updated_at` 同时承担两种含义。


### 2.3 `dist/` 是发布产物，不是源码

- 现覆盖 75 个包（75 个 `<pkg>.zip` + 75 个 `<pkg>.zip.sig` + `hygiene_report.json`）。**本轮未重打包**：新入库的 24 包中 4 个已有历史 zip，其余 20 个包与 `hello` 尚无 zip，等发布链统一出。
- zip 顶层目录 = 包名；`.sig` 是对 zip 本体的 archive 签名（`over=archive`）。dist 里的文件**不得手工编辑**，一切重新生成。

## 3. 构建与发布入口（`sdk/*.py`，全部纯标准库，签名工具除外）

| 命令 | 用途 | 退出码 |
|---|---|---|
| `python sdk/check_manifest.py <pkg_dir>` | 单包 G1-G10 校验（含 v1 兼容通道告警） | 0 = 过 |
| `python sdk/check_all.py [packages_dir]` | 全仓逐包 G1-G10 + schema/签名状态汇总（默认只查包，兼容旧调用；`signed=True` **不代表验签通过**） | 0 = 全过 |
| `python sdk/check_all.py --with-workflows --with-signature` | 包 + `workflows/` meta（M1–M5）+ 官方锚验签，**提交前必跑这条；改包必带 `--with-signature`** | 0 = 全过 |
| `python sdk/check_signature.py [--pkg NAME] [--json] [--no-allowlist] [--anchor PATH]` | 官方锚 ed25519 逐包验签（纯标准库、只读公钥）。允许清单命中与未登记失败**分开报**；丢签名（NO_SIG）不豁免 | 0 = 全 OK 或失败全在允许清单内 / 1 = 有未登记失败 / 2 = 环境缺失（缺官方锚等） |
| `python sdk/check_workflow_meta.py [--workflows DIR] [--no-git] [--json] [--print-desired]` | 只读核对 `workflows/<id>/meta.json` ↔ `workflow.json`：文件/字段齐备、`workflow_sha256`、`updated_at` vs git 提交时间、基本字段 | 0 = 全一致 / 1 = 有不一致 / 2 = 环境缺失 |
| `python sdk/check_port_consistency.py [packages_dir]` | manifest 端口声明 ↔ `node.py spec()` 逐端口对拍（需平台 venv） | 0 = 全一致 |
| `python sdk/package_plugin.py <pkg_dir> [-o dist]` | 校验通过后打 `<pkg>.zip`（拒绝路径穿越 / 超大 / `__pycache__`） | 0 = 成功 |
| `python sdk/backfill_core_manifests.py [--write]` | 从 `node.py spec()` 回填 manifest v2 端口声明（默认 dry-run） | 0 = 无差异 |
| `sdk/sign_core_manifests.py [--packages DIR] [pkg...]` | **仅授权时**：用平台发布私钥签 `core_*` manifest（锚匹配 + G1-G10 预检 + 签后自验） | 非 0 = 拒绝/失败 |
| `sdk/hygiene_dist.py` | 清 `__pycache__` → 全量重打 zip → 重签 `.sig` → 目录/归档逐文件 sha256 对拍 + 出报告 | 0 = 全 OK |

**签名纪律**：私钥只在平台仓 `backend/data/sign-keys/` 下、只在平台 venv 里使用；验签只用公钥锚。日常开发**不要**为了让门禁变绿去重签 —— 先确认 manifest 内容是否本来就该改。

**已知例外清单** `sdk/signature_allowlist.json`（当前 4 条 `user_*`，2026-10-18 构建即失效、待用户裁定 A 重签 / B 下架）：

- **只减不增**：新增失败**不许**往清单里加（加条目属改契约，需用户/队长裁定）；删条目同样需用户裁定。
- 条目逐个钉 `manifest_sha256`：登记的包一旦被改动（哪怕一行），豁免立即失效、门变红，逼人先看内容。
- **只豁免 `SIG_FAIL`**：`NO_SIG`（manifest 丢了 `signature`）永不豁免 —— 丢签名不等于豁免。
- 包恢复有效签名后，门会把它报成「允许清单过期」，届时经用户裁定删除条目。

典型提交前自检：

```bash
cd frameweave-plugins
python sdk/check_all.py --with-workflows --with-signature   # 三门全绿：G1-G10（96 包）+ meta（M1-M5）+ 官方锚验签
python sdk/check_port_consistency.py                        # 端口声明与 spec() 一致（需平台 venv）
```

> 三个开关都是**显式**的（`--with-workflows` / `--with-signature`）：旧调用方按「只校验包」理解本脚本，
> 而验签门在只有节点仓、没有平台仓公钥锚的机器上会 exit 2（环境缺失 ≠ 通过）。
> **改/加节点包必须带 `--with-signature`**；动了 `workflows/` 必须带 `--with-workflows`。
> CI / 本地钩子建议直接钉死：`python sdk/check_all.py --with-workflows --with-signature`。

## 4. 与平台仓的拉取约定

平台**只从本仓拉取**，不在平台仓库内改节点源码：

1. **出厂基础节点种子**（随安装包内置）：
   ```bash
   cd FrameWeave/backend
   .venv/Scripts/python.exe packaging/build_seed.py \
     --source "F:/1223/解说工坊/frameweave-plugins/packages" \
     --out packaging/build/builtin_basics
   ```
   - 源目录就是本仓 `packages/`；包的取舍由平台 `packaging/build_seed.py:BASIC_NODE_DIRS`（44 项）决定，**不按目录全量播种**。
   - `build_seed` 会对每个包跑 `manifest_v2.validate_manifest` + 官方锚验签，任一失败即中止（exit 4 缺包 / exit 5 验签失败）。
   - 与 `packaging/seed_manifest.lock.json` 逐包逐文件比对，不一致 exit 6；改基线需人工确认后 `--write-lock`。
   - **2026-10-26 实测**：切源本仓 44/44 包验签通过；锁不一致 7 处（3 个漂移包的文件/版本），需平台侧重定基线。见 `docs/迁移报告_平台独占节点入库_20261026.md` §7。
2. **运行时安装**：平台把 zip 解到 `backend/user_nodes/<pkg>/`（或用户数据目录），该目录**只是落地位置**。装完不要在该目录里改代码 —— 改了就等于在版本控制之外开发（本轮就抓到 2 处这种"只存在于被忽略目录"的修复，已回灌本仓）。
3. **商城/分发**：`dist/<pkg>.zip` + `<pkg>.zip.sig` 由本仓产出，平台侧只校验 `over=archive` 签名后安装。

## 5. 现状基线（2026-10-26）

- `packages/` **96 包**：67 个 `core_*` + 28 个 `user_*` + `hello`；G1-G10 **96/96 OK**；端口一致性 **96/96 对齐**；官方锚验签 **92 SIG_OK / 4 失败（全部命中已知例外清单）**，`python sdk/check_all.py --with-signature` → exit 0（未登记失败 0）。4 个 `user_*` 为历史失效签名，按私钥纪律未重签，见 `docs/验签门报告_20261026.md` 与 `docs/迁移报告_平台独占节点入库_20261026.md` §4.2。
- `workflows/` **25 个工作流**：`meta.json` 的 `workflow_sha256` 与 `updated_at` **25/25 与实测一致**（2026-10-26 治理，治理前 16 条哈希过期、25 条时间戳与 git 不符）；`python sdk/check_all.py --with-workflows` 实跑 exit 0。见 `docs/元数据治理报告_workflows_meta_20261026.md`。
- `workflows/` 25 个 `<id>/`；`docs/` 索引见 [docs/README.md](docs/README.md)。
- 平台运行目录 `FrameWeave/backend/user_nodes/` 的 58 个包，本仓全部有正本（平台独占 = 0）；其中 53 个字节一致，5 个有判定（正本一律在本仓）。
- 历史：本仓首次入库为 2026-10-24 的全量快照，`packages/` 当时取自 2026-10-18 08:35 的 `dist/*.zip` 构建产物 —— 这也是本轮 7 个"漂移包"的成因（zip 冻结后，平台运行目录继续被热修、插件仓继续被提交）。逐包判定与证据见 `docs/迁移报告_平台独占节点入库_20261026.md`。

## 6. 参考

- 节点开发：`docs/plugin-development-spec.md`（规范 v4）+ `docs/插件开发指南_第三方_20261026.md`（实操 5 分钟出包）
- 工作流开发：`docs/工作流开发指南_第三方_20261026.md`
- 平台内部契约（校验门编号 / 槽位推导 / 缓存键算法）：平台仓 `FrameWeave/docs/specs/`
