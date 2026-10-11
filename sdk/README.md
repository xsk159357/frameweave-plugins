# FrameWeave 插件开发 SDK

独立插件项目 `frameweave-plugins` 的第三方插件开发脚手架。
完整契约与指南见 `docs/plugin-development-spec.md`（《插件开发规范 v3》，`FrameWeave/docs` 有镜像）。

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

## 校验红线（摘自规范 §0/§2）

1. **端口声明必填**：`inputs` / `outputs` / `params` 字段必须存在（可空数组）。
2. `schema_version: 2` + `engine_api: "1.0"`。
3. `type_id` 前缀：`user/` 或已登记前缀；**不要用 `core/`**。
4. `kind` ∈ transform | python；python 包必须给 `entrypoint`/`class_name`。
5. transform 端口禁止媒体类型（VIDEO/IMAGE/AUDIO）；op 走白名单。
6. 密钥类参数经 `@secret:` 保险箱；子进程走 `ctx["run_tracked"]` 托管。
