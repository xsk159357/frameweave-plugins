# FrameWeave 节点插件开发规范 v4（最终版）

> **状态**：现行权威规范（2026）　**契约**：manifest v2（`schema_version: 2`）
> **适用代码**：`backend/app/plugin/manifest_v2.py`、`backend/schemas/manifest_v2.json`、`backend/app/plugin/errors.py`、`backend/app/worker/limits.py`、`backend/app/declarative.py`、`backend/app/plugin_registry.py`、`backend/app/market.py`。
> **SDK**：`frameweave-plugins/sdk/`。旧版 平台仓 `FrameWeave/docs/specs/node-plugin-spec.md` 仅作历史参考；新插件必须写 v2。本文是**唯一正本**（平台仓同路径文件 `FrameWeave/docs/specs/plugin-development-spec.md` 自 2026-10-26 起降为跨仓指针，内容只在本仓维护）。

## 0. 五分钟读懂

插件包是一个目录（发布时为 zip），至少包含顶层目录下的 `manifest.json`。声明式插件使用 `kind=transform`，只执行白名单数据运算；Python 插件使用 `kind=python`，提供 `entrypoint` 和 `class_name`。`manifest.json` 是唯一契约来源，`inputs`、`outputs`、`params` 三字段必须存在（可为空数组）；**Python 类不要写 `spec()`**：NodeSpec 由平台从 manifest 生成（见 §4「为什么不要写 spec()」），node.py 只实现 `run()`。

扫描、安装和 SDK 打包均通过 G1-G11 强校验。失败必须返回稳定错误码，不能静默降级。插件不得使用 `core/` 前缀（官方签名包专用）；社区插件使用 `user/` 或已登记前缀。安装包的 zip 本体最多 2 GiB、解压总量最多 8 GiB、单文件最多 4 GiB、成员最多 20,000，且拒绝路径穿越、符号链接、加密和特殊成员。

## 1. 包结构、位置与加载

```text
my_plugin/
├── manifest.json       # 必需，唯一契约入口
├── node.py             # kind=python 必需；transform 不需要
├── ui/                 # 可选，entry 必须在 ui/ 子树
├── web/                # 可选（S1-T7 / R-平台-7）：web 目录，entry 必须在 web/ 子树
└── models/ data/ ...   # 可选资源
```

源码开发目录为 `FrameWeave/backend/user_nodes/`；独立插件工程建议使用 `frameweave-plugins/packages/<pkg>/`。打包版默认使用 `%APPDATA%/FrameWeave/user_nodes/`，可用 `FRAMEWEAVE_USER_NODES` 覆盖。服务启动或 `POST /api/user_nodes/reload` 时，`declarative.scan()` 遍历目录并将通过校验的节点加入注册表；坏包记录在 `scan_errors()`，不会覆盖已注册节点。

zip 必须只有一个顶层目录 `<pkg>/`，不得在根级混入文件；顶层目录名不能以 `.` 开头，也不能是 `.staging`、`.backup`、`_vendor`、`.git`。

## 2. manifest v2 契约

### 2.1 字段总表

|字段|必填|类型/取值|规则|
|---|---:|---|---|
|`schema_version`|是|整数，固定 `2`|缺失或 `1` 可走 v1 兼容；其它值拒绝|
|`engine_api`|是|`主.次` 字符串|当前平台为 `"1.0"`；主版本不兼容拒绝|
|`min_engine_api`/`max_engine_api`|否|`主.次`|平台版本必须位于范围内|
|`type_id`|是|`前缀/名称`|正则 `^([a-z0-9]{2,16})/[a-z0-9][a-z0-9_.-]*$`；前缀须在白名单|
|`title`|是|非空字符串|展示标题|
|`category`|是|枚举|输入、语义、分析、音频、剪辑、输出、控制、用户节点|
|`description`|是|字符串|说明文字|
|`version`|是|semver|例如 `1.2.0` 或 `1.2.0-beta.1`。**v2 严格模式（schema_version=2）必填，缺失即拒**（`manifest_version`）；v1 兼容通道对缺失自动注入 `1.0.0`（不写盘）。缓存键含 version，升级插件请递增。|
|`kind`|是|`transform` 或 `python`|仅开放这两种|
|`runtime`|否|`transform` 或 `python`|transform 与 kind 一致；python 必须为 python|
|`entrypoint`|python 必填|包内相对 `*.py`|禁止绝对路径和 `..`|
|`class_name`|python 必填|Python 标识符|例如 `MyPluginNode`|
|`inputs`/`outputs`/`params`|是|PortSpec 数组|字段不可省略，可为空数组|
|`transform`|transform 必填|输出名到 op 表达式|键必须属于 outputs|
|`gpu_required`/`gpu_recommended`/`streaming`|否|布尔|默认 false|
|`permission`|否|对象|五作用域，默认 deny-all|
|`dependencies`|否|数组|channel 为 pip、bundle 或 market|
|`ui`|否|对象|entry 必须是 ui/ 下的 js/mjs/css|
|`web`|否|对象|**S1-T7（R-平台-7）**：web 目录声明（EXTENSION_WEB_DIRS 等价）；entry 必须是 web/ 下的 js/mjs/css，平台静态服务 `/extensions/<pkg>/...` 并注入画布|
|`signature`|否|对象|签名占位；无签名为社区级并告警|

v2 严格模式 `additionalProperties=false`；未知顶层字段和未知 PortSpec 字段均拒绝。PortSpec 允许字段为 `name/type/label/required/default/widget/options/description/min/max/step/placeholder/hint`。

### 2.1.1 新增或改名 manifest 字段时的强制动作

`manifest.json` 在平台侧被 10 处以上读取，且分成两种用途：**校验/验签读磁盘原样，
使用/查找读归一化字典**。漏改一处的表现是**静默失效**，不是报错。

v3 改名（`type_id`→`id`、`permission`→`perm`、`engine_api`→`min_engine`）时，
平台侧有 4 处仍在读旧名并拿到空值，其中 `pkg_dir()` 恒返回 `None` ⇒ 权限门在拿到
manifest 之前就退出，`exposable` 一条也没校验过。这类缺陷的共同点是：
**装得上、跑得起来、结果错，且没有任何一处报错。**

所以改字段必须按顺序做四件事：

1. **改三份实现**：`backend/schemas/manifest_v2.json`（结构层）、
   `backend/app/plugin/manifest_v2.py`（语义门 + `V3_TOP_LEVEL_KEYS` 白名单）、
   本仓 `sdk/check_manifest_v3.py`（V 门）。缺一处就是「SDK 拒了平台收」或反过来。
2. **改归一化，不改下游**：新名 → 旧名的投影只写在 `manifest_v2.normalize_manifest()`
   一处；下游继续读旧名。**不要**在十几个调用点各加一个 `.get("新名") or .get("旧名")`——
   §6.7 的实测结论是「同一个文件里都会一处兼容一处没兼容」。
3. **数一遍读点**：`grep -rn 'manifest\.json' backend/app --include=*.py`，
   按「读原始 / 读归一化」两张清单归位。新增读点必须走
   `backend/app/plugin/manifest_io.py` 的 `read_raw` / `load_normalized` / `load_validated`
   之一；否则 `backend/tests/test_manifest_io_normalized_20261103.py` 会红。
4. **两侧对拍**：平台 G 门与本仓 V 门用同一批夹具各跑一遍，结论必须逐例一致。
   样板两份：`test_manifest_exposable_v11.py`（V11/G11，14 例）与
   `test_manifest_gate_parity_20261103.py`（V1/V2/V5/V7/V12/V13，30 例 +
   9 组常量逐字对拍 + 反证）。**先跑探针再写测试**：只看「某门是否开火」会漏掉
   「平台报 G5、节点仓储报 V12」这类门号错位，主断言必须是「拒 / 放是否一致」。

判据（**用测试，不要用 grep**）：

```
cd ../FrameWeave
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_manifest_io_normalized_20261103.py -q -k bare_manifest
```

→ `1 passed`。这条测试用 AST 排除文档字符串与 `#` 注释后扫描真实读点，
白名单只保留 `plugin/manifest_io.py`（唯一实现）、`signatures.py`（zip 成员）、
`routes/export.py`（只写）、`runtime_component.py`（另一个文件名）。

> `grep -rn 'manifest\.json' backend/app --include=*.py | grep -v '#\|"""'` 只能当粗筛：
> 多行 docstring 的后续行不以 `"""` 开头，会照样命中（2026-11-03 实测：12 行命中里
> 6 行是文档），所以那个数字不能当判据——见平台仓 §7.4「判据骗人的三种形态」。

### 2.1.2 v3 门禁的强制约束（2026-11-03 两侧对齐后）

下面这些规则**平台侧现在会拒**，而之前只在本仓 V 门拒（或两边都没查）。
写 v3 包时按这张表自查；`sdk/check_manifest_v3.py` 与平台 `manifest_v2.py` 的判定
已由 30 例对拍钉死，任一侧单独改会被 `test_manifest_gate_parity_20261103.py` 打红。

| 位置 | 约束 | 之前的状态 |
|---|---|---|
| 顶层 | `perm` **必填**（缺 = 拒，不是"默认全拒就放行"） | 平台 `_check_perm_v3` 直接 return |
| `perm.net.hosts` | 不允许 `"*"`，逐个域名列出 | 平台只查类型 |
| `perm.model` | 每项必须是 `{name, sha256}`，**sha256 必填** 64 位十六进制 | 平台 sha256 可选；字符串写法也放过 |
| `perm.exec` | 每项 `{bin, args_policy?}`；`args_policy` 只允许 `"argv_only"`，可省略 | 平台只查「是字符串」；本仓完全不查 |
| `perm.fs.read` / `perm.fs.write` | 必须字符串数组 | 本仓只查键名 |
| `perm.secret` | 必须字符串数组 | 本仓完全不查 |
| 端口 `type` | 必须是**大写**枚举，只接受 v3 的 16 种；`JSON` / `ANY` 已移除 | 平台用 v2 集合且 `.upper()`，`JSON`、`string` 都能过 |
| 端口 `label` | 必填非空 | 平台不检查 |
| `outputs[].required` | **不允许出现**（输出没有"可选"语义） | 平台不检查 |
| 端口 `name` | 同节点内唯一——**跨 inputs/outputs 也算** | 本仓把作用域缩到单侧 |
| `params[].label` / `description` | 必填非空（`""` 不算填了） | 平台不检查 |
| `params[].default` | **必须出现**（确实没有就写 `null`） | 平台不检查 |
| `params[].type` | 严格小写枚举，`"String"` 不算 `"string"` | 平台 `.lower()` 后放过 |
| `params[] type=enum` | 必须提供非空 `options` | 平台不检查 |

**为什么平台也要拒。** 「SDK 拒、平台收」的后果比两边都松更糟：作者按本文档写完，
`check_manifest` 报错，改到过为止；换个人直接打 zip 上传，同样的包平台装着跑起来了
——于是「文档说的规则」和「实际能装的包」变成两套标准，文档先失效。

**为什么 `category` 不在上表。** 平台允许 `用户节点`、本仓不允许，这是**唯一**允许的差异
（平台要在插件管理器里单独分组）。`test_categories_is_documented_superset` 把差值
写死成 `{"用户节点"}`，多一个就会红。

### 2.2 可直接校验的 Python manifest 示例

> **这份示例是 v2 的**（`schema_version: 2`，端口可以用 `JSON`）。写 v3 包请按
> `docs/reports/节点重做_20261029/manifest_v3规格.md` 与上表 §2.1.2：
> v3 端口没有 `JSON`/`ANY`，`permission` 改名 `perm` 且必填，`params[]` 多了 `default` 必填。

```json
{
  "schema_version": 2,
  "engine_api": "1.0",
  "type_id": "user/my_plugin",
  "title": "文本处理",
  "category": "用户节点",
  "description": "将输入文本按模式转换并输出统计信息",
  "version": "1.0.0",
  "kind": "python",
  "runtime": "python",
  "entrypoint": "node.py",
  "class_name": "MyPluginNode",
  "inputs": [{"name":"text","type":"STRING","label":"文本","required":true}],
  "outputs": [
    {"name":"result","type":"STRING","label":"结果"},
    {"name":"stats","type":"JSON","label":"统计"}
  ],
  "params": [
    {"name":"prefix","type":"STRING","label":"前缀","default":"FW"},
    {"name":"mode","type":"STRING","label":"模式","widget":"select","options":["upper","lower","raw"],"default":"upper"}
  ],
  "permission": {"fs":{"write":["scratch/**"]}}
}
```

### 2.3 PortSpec 语义

`name` 必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，同一数组内不可重复；同名端口不能同时出现在 inputs 与 outputs。`type` 必须是已注册 PortType。`required` 默认为 true，`widget` 默认为 text；`select` 必须有非空 options；`number` 的 default 必须是数字；`file`、`toggle`、`password` 只用于 params。

### 2.4 permission、dependencies、ui、signature

```json
"permission": {
  "fs": {"read":["models/**"],"write":["scratch/**"]},
  "network": {"allow":["api.example.com"],"deny":["169.254.169.254"]},
  "subprocess": {"enabled":true,"argv_allowlist":["ffmpeg@**"]},
  "secret": {"refs":["MY_API_KEY"]},
  "env": {"vars":["MY_PLUGIN_HOME"]}
}
```

未声明权限即 deny-all；五作用域为 `fs`、`network`、`subprocess`、`secret`、`env`，作用域内只允许校验器定义的键。依赖形如 `{ "id":"ffmpeg-python", "version":">=0.2", "channel":"pip", "optional":false }`；`pip` 表示 Python 依赖、`bundle` 表示平台 bundle、`market` 表示商城插件依赖。

UI 形如 `{ "entry":"ui/index.js", "sandbox":{}, "capabilities":["panel"] }`。entry 必须匹配 `ui/...(.js|.mjs|.css)`，不得绝对路径或含 `..`；文件不存在时安装通过但加入警告。

Web 目录（**S1-T7 / R-平台-7**，EXTENSION_WEB_DIRS 等价）形如：

```json
"web": {
  "entry": "web/index.js",
  "sandbox": {},
  "capabilities": ["canvas"]
}
```

`web.entry` 必须匹配 `web/...(.js|.mjs|.css)` 且落在包内 `web/` 子树，不得绝对路径或含 `..`；文件不存在时安装通过但加入警告。平台只静态服务该包的 `web/` 子树（`/extensions/<pkg>/<path>`），不暴露包内其它文件；`sandbox`/`capabilities` 是前端加载器的策略输入（当前约定 `capabilities: ["canvas"]` 表示挂载到画布宿主），插件不得借此绕过 manifest 权限。

signature 占位字段为 `alg/key_id/value/over`；社区包不带签名会产生警告，结构不完整则拒绝，`core/` 包必须携带 signature。

### 2.5 transform 运算语法

v2 只接受 canonical 形式；取值源为字面量、`{"$const": value}`、`{"$param":"name"}`、`{"$input":"port"}`、`{"$list":[...]}`，可递归嵌套。顶层 op 白名单：

|op|参数|
|---|---|
|`concat`|`sources` 数组；可选 `sep/prefix/suffix`|
|`json_get`|`source`、点路径 `path`|
|`math`|`a/b`，`op` 为 `+ - * / % max min`|
|`str`|`template`、`values`|
|`upper`/`lower`/`len`|`source`|
|`list`|`sources` 数组|

示例：`{"op":"concat","sources":[{"$input":"name"}],"sep":"，","suffix":"！"}`。v1 的 `$concat`、`$upper`、`get` 等仅在兼容模式归一化；v2 中直接拒绝未知 `$key` 或未知 op。transform 是纯数据运算，不读文件、不联网、不执行 Python；VIDEO/IMAGE/AUDIO 类型不得出现在 transform 的任一端口。

**能力下限（P2-11 明示）**：transform 是**安全优先的纯数据子集**，不是通用编程环境——
- 白名单恒定 8 个数据 op（`concat/json_get/math/str/upper/lower/len/list`），不支持自定义函数、循环、分支、正则、加解密或任何 I/O；
- 端口类型仅限非媒体数据（`STRING/INT/FLOAT/BOOL/JSON/SCRIPT/SUBTITLE/SEGMENTS/TIMELINE/ANY`），`VIDEO/IMAGE/AUDIO` 一律拒绝（G6）；
- 与 ComfyUI 等"任意 Python 表达式节点"相比，这是**有意的能力下限**：换取零代码执行风险与可静态审计（manifest 即全部行为），媒体/重逻辑一律走 `kind=python` 插件（经 manifest v2 校验 + 权限门 + worker 隔离）。
- 业务判断：transform 节点适合拼串/取值/数值换算/列表组装四类小任务；超出即升级为 python 插件，不要用 op 硬凑。

### 2.6 v1 兼容

manifest 缺少 `schema_version` 或为 1 时，只在内存副本注入 `schema_version=2`、`engine_api="1.0"`、默认权限和布尔字段，并探测 kind、归一化旧 op；磁盘文件不改写。v1 Python 包缺少端口时兼容通道补空数组并警告，生产插件仍应立即迁移到 v2；v1 transform 缺端口不能通过校验。新插件禁止依赖兼容通道。

## 3. G1-G11 校验门与错误码

扫描、上传安装、生命周期启用和 SDK 打包必须共享同一契约语义。错误码按首个失败门返回，问题列表同时保留。

|门|检查|主要错误码|
|---|---|---|
|G1|JSON、根对象、schema_version、engine_api|`manifest_schema_version`、`manifest_engine_api`|
|G2|type_id 前缀/格式、元字段、未知字段|`manifest_type_id`、`manifest_meta`、`manifest_unknown_field`|
|G3|semver、引擎版本边界|`manifest_version`、`manifest_engine_api`|
|G4|kind/runtime/entrypoint/class_name|`manifest_kind`|
|G5|三端口数组、PortSpec、类型/widget/重复名|`manifest_ports`|
|G6|transform 映射、op 递归白名单、媒体禁入|`manifest_op`|
|G7|五作用域 permission|`manifest_permission`|
|G8|dependencies 与 ui、web（S1-T7：web 目录声明）|`manifest_dependencies`、`manifest_ui`、`manifest_web`|
|G9|zip 资源限制与安全成员|`zip_too_large`、`total_size_exceeded`、`file_size_exceeded`、`member_count_exceeded`、`zip_bomb_ratio`、`path_traversal`、`symlink_member`、`encrypted_member`、`special_member`、`empty_zip`、`pkg_layout`|
|G10|signature 占位结构|`manifest_signature`；无签名为警告|
|G11|`exposable` 引用完整性（仅 v3；对应节点仓 V11）|`manifest_exposable`：`param` 必须存在于 `params` 且不重复、`recipe_role` 非空、`safe_for_wizard` 为布尔、`params[].advanced:true` 的项 `safe_for_wizard` 必须为 `false`；条目含三键（`param`/`recipe_role`/`safe_for_wizard`）之外的字段即拒。**`advanced:false` + `safe_for_wizard:false` 合法**（2026-11-03 取消原先的 WARN 规则：`advanced` 只管画布，不能拿来满足向导规则）|

> G11 是 2026-11-03 补的（此前平台侧只有字段白名单、没有校验，向导会渲染空控件）。
> 它与节点仓 `sdk/check_manifest_v3.py` 的 V11 由平台测试
> `backend/tests/test_manifest_exposable_v11.py` 逐例对拍——两仓各有一份实现，
> 所以「结论一致」这件事必须由测试保证，不能靠两边各写一遍规则。

其它兜底码为 `manifest_gate`、`bad_manifest`、`not_zip`、`staging_failed`、`install_failed`。后端错误定义见 `backend/app/plugin/errors.py` 与 `backend/app/worker/limits.py`。

## 4. Python 插件（kind=python）

入口类继承公开基类 `app.nodespec.NodeBase`，实现 `async run(ctx, inputs, params)`，可选 `async setup(ctx)`。**不要写 `spec()`。**

manifest 是唯一端口契约：平台在加载时用 `_spec_from_manifest(manifest)` 生成 NodeSpec，插件自己写的 `spec()` 会被**整段覆盖**（实测见下）。类型、默认值、端口名只写在 `manifest.json` 里。

```python
from typing import Any, Dict
from app.assets import Asset
from app.nodespec import NodeBase
from app.ports import PortType

class MyPluginNode(NodeBase):
    # 不要写 spec()，也不要 @register：端口/参数/类型/默认值全部只在 manifest.json 里。
    # 平台在加载时用 _spec_from_manifest(manifest) 生成 NodeSpec，写了也会被覆盖。

    async def setup(self, ctx):
        pass

    async def run(self, ctx: Dict[str, Any], inputs, params) -> Dict[str, Any]:
        text = str(inputs.get("text") or "")
        result = str(params.get("prefix", "FW")) + " - " + text
        ctx["progress"](0.5)
        stats = ctx["store"].save_asset(
            Asset(id="", kind=PortType.JSON.value), payload={"length": len(text)})
        return {"result": result, "stats": stats}
```

输入中的媒体/结构化资产通常是 `Asset`，媒体读取 `asset.path`，JSON 类资产使用 `ctx["store"].read_json(asset.id)`。返回值必须是 dict；键应为声明的输出端口名，值可以是 Asset、字符串、数字、布尔、dict 或 list。异常应直接 raise，使节点进入 FAILED，不要吞异常伪造成功。

ctx 公开字段：`workdir`、`store`、`logger`、`progress(0..1)`、`spawn_tracked`、`run_tracked`、`track_subprocess`、`untrack_subprocess`、`subprocess_registry`。async run 内禁止裸 `subprocess.run`；同步子进程必须走 `await ctx["run_tracked"]([...], timeout=...)`，以便超时和取消时杀掉进程树。

默认节点超时按信任级为 L0 600 秒、L1 300 秒、L2 120 秒；声明上限分别为 7200/1800/900 秒，官方渲染节点 L0 可到 1800 秒；setup 上限为 `min(120s, run_timeout/3)`。

### 为什么不要写 `spec()`

**实测（2026-10-30）**：把 `core_subtitle_compose/node.py` 的 `spec()` 整段删掉，重新加载，**11 个字段逐个比对完全相同**。

原因：`backend/app/declarative.py` 的 `_make_python_class` 造出的 `_P` 类里，`spec()` 返回 `_spec_from_manifest(manifest)`，**覆盖**了插件自己写的那份。

所以「`spec()` 应与 manifest 一一对应」这条要求，等于让每个包的作者维护一份**平台从来不读**的副本。这不只是无用，是**危险**——它让人以为「改了 `spec()` 就等于改了契约」，而真正的契约在 manifest。

复现脚本：`FrameWeave/_archive/tmp/verify_spec_dead.py`（删 spec 前后逐字段比对）。

> **同理不要 `@register`**：`register()` 会塞 `_REGISTRY`，而声明式路径塞 `_decl`，`list_specs()` 是两处相加（`registry.py:70`）。两个都写 ⇒ 同一节点出现两个 spec。

## 5. Asset 协议与存储

`Asset` 字段：`id`、`kind`、`path`、`meta`、`fingerprint`、`node_id`、`created_at`、`size`。媒体资产的 path 是文件路径；结构化资产调用 `store.save_asset(asset, payload=data)` 自动写 JSON。`AssetStore`（默认 `LocalAssetStore`）公开：`save_asset`、`get`、`read_json`、`delete`、`root`、`files_dir`、`meta_dir`、`prune_orphans`。第三方存储可实现 `StoreAdapter` 并通过 `register_store_adapter/create_store` 注册。

Python 节点接收上游 Asset 原样；transform 节点由引擎将 JSON 解包为载荷，媒体转换为 `{kind,path,meta}` 视图。不要把 Asset 的 dataclass repr 当成业务文本。

### 5.1 两种执行模式下的 `path` 语义（2026-11-03 实测补）

平台有两种执行模式，**同一条媒体资产在两种模式下 `path` 指向不同地方**。节点必须能同时活在这两种模式下。

| 模式 | 怎么触发 | 媒体落盘 | 源片被移动/删除 |
|---|---|---|---|
| **worker（默认，发布形态）** | 不设 `FW_PLUGIN_EXEC` | 产物与「scratch 外」的资产会**复制**进资产库（自包含）；媒体输入也会复制进 `in/`（隔离，节点只在 scratch 内读写） | 工程照常 |
| **inprocess（调试降级）** | `FW_PLUGIN_EXEC=inprocess` | **直接引用原路径**，零复制 | 工程静默断链 |

三条据此得出的硬约束：

1. **不要用 `path` 判断「是不是同一个文件」**。用 `asset.fingerprint`（内容指纹，平台已算好）。
   同一份内容导入两次不一定得到同一个 `path`：同一路径重复导入会命中执行缓存复用资产，
   但**内容相同、路径不同**的两个文件目前会各存一份（资产库没有内容去重，实测 `2.00x`）。
2. **不要「原地写回」输入文件**。inprocess 改的是原件；worker 改的是隔离副本，原件不动——
   同一个节点在两种模式下结果会不一样，而且 inprocess 那次会污染用户素材。
   要改就在 `ctx.workdir` 下写新文件，再 `store.save_asset`。
3. **同一路径供多个输出端口是允许的**（`core/media_import` 的 `video`/`audio` 就是这样）。
   宿主按 scratch 内路径去重，只搬一次，不会因为两个端口多复制一份。

## 6. PortType 与连线

13 个核心类型：`VIDEO`、`IMAGE`、`AUDIO`、`TIMELINE`（媒体资产），`SCRIPT`、`SUBTITLE`、`SEGMENTS`、`JSON`（结构化），`STRING`、`INT`、`FLOAT`、`BOOL`、`ANY`（标量/泛型）。未知类型拒绝，不再静默降级为 STRING。平台允许显式 `register_port_type(name, is_media=..., can_broadcast=...)` 注册扩展类型；扩展默认不能连线，需注册连接规则。

`ANY` 可连一切；同类型互连；JSON 可连 JSON/SCRIPT/SEGMENTS/SUBTITLE/STRING，任意源可连 JSON；媒体类型不自动窄化（例如 VIDEO 不能直接连 IMAGE）。

## 7. 大插件、流式安装与 UI

`worker/limits.py` 是唯一上限真源：

|参数|值|
|---|---:|
|zip 上传本体|2 GiB（`MAX_UPLOAD_MB=2048`）|
|解压总量|8 GiB|
|单文件|4 GiB|
|成员数|20,000|
|累计压缩比|100:1|
|流式块|1 MiB（`ZIP_BLOCK`）|

上传先 Content-Length 预检，再按 1 MiB 流式落盘并截断；安装先中央目录预检，再按块解压并复核实际字节数。商城 `_fetch_zip` 同样流式下载，累计字节数与 sha256；超过 `UPLOAD_MAX_BYTES` 立即中止，下载完成校验 `is_zipfile`，发布表单登记了 sha256 时比对摘要（防传输损坏/篡改）；超限、损坏或摘要不匹配立即删除临时文件。发布表单的 size 字段用于展示与预检。插件开发者不要在 Python 节点中一次性读入大模型；按块读写并使用资产存储。

当前单请求上限为 2 GiB。超过上限的分片协议为预留设计：客户端按固定分片上传，服务端校验 upload_id/chunk_index/chunk_total，合并后仍必须走同一解压、G1-G10、staging、原子替换事务；任一分片或 sha256 失败则清理整个 upload_id。

UI 可放在包内 `ui/`；manifest 的 `ui.entry` 必须是 `ui/*.js|mjs|css` 且文件实际存在（不存在只警告）。sandbox/capabilities 是前端加载器的策略输入，插件不得借此绕过 manifest 权限。

### 7.1 插件 web 目录（S1-T7 / R-平台-7，EXTENSION_WEB_DIRS 等价）

插件可携带 `web/` 目录（静态资源 + 前端注入脚本），平台加载并注入画布自定义 UI 组件：

- **声明**：manifest 顶层 `web` 字段，`web.entry` 必须是 `web/*.js|mjs|css`（G8 web 门 + 文件层存在性校验，缺失仅警告）。
- **静态服务**：平台为每个通过校验的 live 包提供 `GET /extensions/<pkg>/<path>`，只暴露该包 `web/` 子树（realpath 防穿越；坏包/隔离包 404）。
- **清单接口**：`GET /api/extensions` 返回 `[{pkg, type_id, version, entry, capabilities, url}]`（url 形如 `/extensions/<pkg>/web/index.js`）。
- **注入机制**：前端（`web/src/plugins.ts`）登录后拉取清单，按 `pkg@version` 去重注入 `<script src>`；插件脚本通过 `window.fwExtensions.register({pkg, type_id, version, mount(host, ctx), unmount(host)})` 自我注册；加载器把 `mount` 挂到画布 `#fw-ext-root` 内的专属 `.fw-ext-slot` 容器，从而在画布注入自定义组件。
- **ctx 能力**：`ctx.api`（本地 API 客户端）、`ctx.host`（专属容器）、`ctx.getSpecs()`（`/api/specs` 快照）。插件仅能操作自己的容器 DOM；无 `mount` 的扩展只加载资源不挂载。
- **示例**：`frameweave-plugins/packages/user_web_demo/`（manifest `web.entry=web/index.js` + `web/style.css` 多文件资源演示），安装后画布右上角出现可点击计数组件。
- **重载**：插件安装/升级/卸载（`fw-nodes-installed` 事件）后前端自动重载 web 扩展清单并按版本差量注入。

## 8. 生命周期与商城分发

插件注册表 `plugins.json` 状态为：`absent`（目录缺失）、`active`（正常加载）、`disabled`（移入 `.disabled/`）、`quarantined`（校验失败隔离）、`updating`（升级事务中）。安装/升级流程为：流式解压 → G1-G10 → `.backup` 保留最多 b1/b2/b3 → 同卷原子替换 → scan 验证 → 失败自动回滚或隔离。启动 `sync()` 会修复中断的 updating 状态。

|动作|接口|
|---|---|
|扫描|`POST /api/user_nodes/reload`|
|上传安装|`POST /api/user_nodes/install`|
|升级|`POST /api/user_nodes/{pkg}/update`|
|禁用/启用|`POST /api/user_nodes/{pkg}/disable`、`.../enable`|
|回滚/卸载|`POST /api/user_nodes/{pkg}/rollback`、`.../uninstall`|
|健康/指标|`GET /api/user_nodes/{pkg}/healthcheck`、`GET /api/user_nodes/metrics`|
|导出|`GET /api/export/node/{type_id}`|

商城发布条目为 `kind=node`，登记 title/description/author/price/tags/download_url 或内联 zip、type_id、version、size、sha256、min_client_version。客户端可先调用 `download-auth`，下载后校验 sha256，再调用 `/api/market/items/{id}/install`；商城安装最终复用相同 install_zip 校验。付费条目按平台 20%、作者 80% 分成；安装失败会回滚扣费。缺失节点可按 type_id 查询商城。

## 9. SDK 与发布

在 `frameweave-plugins` 工程执行：

```bash
mkdir my_plugin
cp sdk/manifest.example.json my_plugin/manifest.json
cp sdk/node.py.template my_plugin/node.py
# 编辑 manifest 的 id、端口、参数；node.py 只写 run()
python sdk/check_manifest.py my_plugin/manifest.json
python sdk/package_plugin.py my_plugin -o dist
# 将 dist/my_plugin.zip、sha256、size 填入商城发布表单
```

`check_manifest.py` 为纯标准库 G1-G10 检查器（已同步支持 `web` 字段的 G8 web 门）；`package_plugin.py` 先检查 manifest，再检查单文件/总量/成员/zip 本体上限并输出 sha256。transform 插件可复制 `sdk/manifest.transform.example.json`，包内无需 node.py。提交前必须确保校验输出 `OK` 且没有错误。

## 10. 最佳实践与红线

**必须**：写 `schema_version=2` 与 `engine_api="1.0"`；使用 `user/`；三端口字段齐全；端口类型准确；select 提供 options；Python 的 node.py **不写** `spec()`、不 `@register`（类型契约由 manifest 生成）；资产经 store 保存；秘密只经 params/secret 引用；子进程使用 tracked API；大文件流式处理；发布登记真实 sha256；携带前端注入能力的插件声明 `web.entry` 且只把资源放在 `web/` 子树（经 `window.fwExtensions.register` 挂载，不做任何越权访问）。

**禁止**：使用 core/ 冒充官方；依赖 v1 缺省；使用未知字段/未知类型/op；transform 使用媒体端口或读文件；在日志写入 token/password；裸 subprocess；写入包目录外任意路径；把 zip 整包载入内存；捕获异常后返回伪成功；让多个包复用同一个 type_id；`web.entry` 指向 `web/` 子树外（绝对路径/`..` 逃逸）；在 web 脚本中直接读取本地任意文件或调用未授权的后端端点。

发布前清单：`check_manifest` 零错误、manifest 端口三字段完整可解析（平台按 manifest 生成 spec，不再比对 node.py）、entrypoint/UI/Web 文件存在、zip 无穿越成员、大小在上限内、sha256 与发布信息一致、依赖可解析、权限是最小集、实测安装后 `/api/specs` 能看到完整端口、安装后 `/api/extensions` 能看到 web 目录清单。

## 11. 官方参考实现

`frameweave-plugins/packages/hello` 是 transform 示例；`sdk/manifest.transform.example.json` 是严格 v2 canonical op 示例；`frameweave-plugins/packages/user_web_demo/` 是插件 web 目录示例（S1-T7，EXTENSION_WEB_DIRS 等价：manifest `web.entry` + `web/index.js` 自注册 + `web/style.css` 多文件资源，画布注入可点击计数组件）。官方 `core_scene_detect`、`core_tts`、`core_video_render` 等 Python 节点展示 Asset、模型初始化和 ffmpeg 场景。官方包历史上部分 manifest 仍为 v1 兼容形态；新发布包必须回填完整端口，**manifest 是唯一来源**——`node.py` 不写 `spec()`，也就无从「替代」。

## 12. 权威源码索引

- manifest 结构：`backend/schemas/manifest_v2.json`
- G1-G10 语义校验：`backend/app/plugin/manifest_v2.py`（G8 web 门：`_check_web` / `WEB_ENTRY_RE`）
- manifest 错误码：`backend/app/plugin/errors.py`（`manifest_web`）
- zip 上限和成员安全：`backend/app/worker/limits.py`、`backend/app/declarative.py`
- 插件 web 目录发现：`backend/app/declarative.py`（`web_extensions()` / `web_dir()`）
- 插件 web 静态服务 + 清单接口：`backend/app/server.py`（`/api/extensions`、`/extensions/{pkg}/{path}`）
- 前端注入机制：`web/src/plugins.ts`（`window.fwExtensions` 桥 + `loadWebExtensions`）、`web/src/api.ts`（`extensions()`）、`web/src/Canvas.tsx`（`#fw-ext-root` 宿主）
- 生命周期事务：`backend/app/plugin_registry.py`
- 商城下载/安装/sha256：`backend/app/market.py`
- 节点基类和端口：`backend/app/nodespec.py`、`backend/app/ports.py`
- 资产协议：`backend/app/assets.py`
- SDK：`frameweave-plugins/sdk/check_manifest.py`、`package_plugin.py`
