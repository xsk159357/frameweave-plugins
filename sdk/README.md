# FrameWeave 插件开发 SDK

独立插件项目 `frameweave-plugins` 的第三方插件开发脚手架。
完整契约与指南见本仓 `docs/plugin-development-spec.md`（《节点插件开发规范 **v4**》，约 300 行，**唯一正本**）。
平台仓 `FrameWeave/docs/specs/plugin-development-spec.md` 自 2026-10-26 起只是一份 **6 行跨仓指针**（**不是镜像**，正文只在本仓维护）。
验签门：`python sdk/check_signature.py`（官方锚 ed25519，只读公钥；**改包必跑**；商城分发产物用 `--only-dist`）。

## 快速开始（一条龙）

```bash
# 1. 初始化你的插件包
mkdir my_plugin
cp sdk/manifest.example.json my_plugin/manifest.json
cp sdk/node.py.template      my_plugin/node.py

# 2. 编辑：改 type_id / title / 端口声明（inputs|outputs|params 必填）与 run() 逻辑

# 3. 校验（必须零错误）
python sdk/check_manifest.py my_plugin

# 4. 打包（输出 zip + sha256）
python sdk/package_plugin.py my_plugin -o dist
#    → dist/my_plugin.zip + sha256 摘要

# 5. 发布：在 FrameWeave「商城」发布表单填 type_id / zip / sha256 / 描述…
#    （付费/免费、download_url 或内联 zip）
```

transform 声明式插件（不写 Python）：参考 `sdk/manifest.transform.example.json`，
包内只需 manifest.json，无需 node.py。

## 文件清单

| 文件 | 说明 |
|---|---|
| `node.py.template` | python 节点骨架（NodeBase：spec/setup/run + Asset + ctx + 子进程托管示例） |
| `manifest.example.json` | python 插件完整示例 manifest（**含端口声明**，v2） |
| `manifest.transform.example.json` | transform 声明式示例（op 白名单 + 非媒体端口） |
| `check_manifest.py` | 契约校验器 v3（G1-G10 全部门 + v1 兼容通道 + 大插件上限常量，纯标准库） |
| `package_plugin.py` | 打包 zip + sha256（先校验后打包；单文件/总量/成员/zip 本体上限预检） |
| `check_dist_parity.py` | **dist 归档 ↔ packages 正本一致性门禁**（第六道门；逐 zip 逐文件 sha256 判等；**只读**） |

## 发布前门禁（六道）

改包 / 重打包 / 发版前，按下面顺序跑。每一道拦一类问题，缺一道就有一类盲区。

| # | 脚本 | 断言什么 | 性质 |
|---:|---|---|---|
| 1 | `check_manifest.py <pkg>` | manifest v2 契约（G1–G10） | 只读 |
| 2 | `check_all.py --with-workflows` | 逐包契约 + 工作流 meta（M1–M5） | 只读 |
| 3 | `check_signature.py` / `check_all --with-signature` | manifest 签名 vs 官方锚 | 只读 |
| 4 | `check_signature.py --only-dist` | `dist/*.zip.sig` vs 官方锚 | 只读 |
| 5 | `check_port_consistency.py` | manifest 端口 vs `node.py` 的 `spec()` | 只读 |
| 6 | **`check_dist_parity.py`** / `check_all --with-dist` | **zip 内容 == packages 正本内容** | **只读** |

### 第六道门：`check_dist_parity.py`（dist 与正本一致性）

**为什么需要它**：`.sig` 签的是 zip 的**字节**，所以「验签全绿」只证明这个 zip 没被篡改，
**不证明它是最新版**。前五道门没有一条断言 `dist/<pkg>.zip` 的内容等于 `packages/<pkg>/` 的内容
——「改了包、忘了重打包」在它们眼里全部 exit 0。

**判据（逐 zip）**：

1. 成员必须全部落在 `<pkg>/` 前缀下（越界即 drift）；
2. 相对路径集合必须相等；`__pycache__/`、`*.pyc`、`*.pyo`、`.DS_Store`、`Thumbs.db`
   **出现在 zip 里本身就是缺陷**（单独记为 banned 并判 drift，不是忽略）；
3. 同路径文件的 sha256 必须逐字节相等；
4. （默认开，`--no-pairing` 可关）`<pkg>.zip` 必须配有 `<pkg>.zip.sig`。

有包无 zip **不算 drift**（出厂基础节点只走 `build_seed`，不打商城包）。

**命令**：

```bash
python sdk/check_dist_parity.py                  # 默认仓内 dist/ ↔ packages/
python sdk/check_dist_parity.py --pkg core_tts   # 只判某个包（可重复 --pkg）
python sdk/check_dist_parity.py --json           # 机器可读
python sdk/check_dist_parity.py --no-pairing     # 跳过 zip↔sig 配对检查
python sdk/check_all.py --with-dist              # 等价：随主干门一起跑
```

**退出码（三态，与其它门禁一致）**：

| 码 | 含义 |
|---:|---|
| `0` | 全部一致（0 drift） |
| `1` | 存在 drift（逐条列出包名 + 差异文件） |
| `2` | 环境缺失（`dist` / `packages` 目录不存在、`dist` 下没有 zip）—— **`2` 不等于通过** |

**它是门，不是体检**：只读、不动任何文件、跑多少次都不会改变仓库状态。

⚠️ 不要拿 `hygiene_dist.py` 代替它：那是**写操作**（清理 `__pycache__` + 全量重打重签 75 个 zip +
覆写 `docs/s10-dist-hygiene.md`），只在明确发版时跑。日常判读一律用 `check_dist_parity.py`。


## 校验红线（摘自规范 §0/§2）

1. **端口声明必填**：`inputs` / `outputs` / `params` 字段必须存在（可空数组）。
2. `schema_version: 2` + `engine_api: "1.0"`。
3. `type_id` 前缀：`user/` 或已登记前缀；**不要用 `core/`**。
4. `kind` ∈ transform | python；python 包必须给 `entrypoint`/`class_name`。
5. transform 端口禁止媒体类型（VIDEO/IMAGE/AUDIO）；op 走白名单。
6. 密钥类参数经 `@secret:` 保险箱；子进程走 `ctx["run_tracked"]` 托管。
