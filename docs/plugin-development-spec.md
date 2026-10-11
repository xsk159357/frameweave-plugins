# FrameWeave 插件开发规范 v3

> **文档版本**：v3.0（2026）　**适用契约**：插件 manifest v2（`schema_version: 2`）＋ 大插件支持
> **权威依据**：《万物插件化_改造_插件契约v2与校验体系.md》（manifest 字段语义 / 校验门 G1-G10 / transform op 文法）＋ t2 大插件支持设计
> **平台对接源码**：`FrameWeave/backend/app/` 下 `declarative.py`（扫描/安装）、`app/plugin/manifest_v2.py`（**G1-G10 强校验器，安装/扫描唯一接入**）、`schemas/manifest_v2.json`（结构层契约）、`app/plugin/errors.py`（manifest_* 错误码）、`app/worker/limits.py`（**上限参数唯一真源**）、`app/nodespec.py`、`app/ports.py`、`app/assets.py`、`app/engine.py`
> **参考实现**：`frameweave-plugins/packages/core_*`（10 个官方插件）+ `packages/hello`（transform 示例）
> **旧规范兼容**：`FrameWeave/docs/node-plugin-spec.md` 为 v1 时代文档（仅 transform）；本规范 v3 为现行契约。v1 manifest 走**兼容通道**（读入注入默认值，不写盘，见 §2.5），但**新插件一律写 v2**。

---

## 0. 核心结论（五分钟读懂）

1. **插件的唯一识别入口是 `manifest.json`**。第三方开发一个插件 = 写 `manifest.json`（契约声明）+ 可选的 `node.py`（python 运行时）+ 可选的 `ui/` 资源 + 打成 zip 发布到商城。
2. **端口声明必填**（R1 核心修复）：`inputs` / `outputs` / `params` 三个数组**字段必须存在**（可为空数组，但不能缺失）。python 插件**不是**靠 `node.py` 里的 `spec()` 免声明——v2 契约下 manifest 是唯一真源，`spec()` 只用于运行时注册，两者必须一致。
3. **两类插件**：`kind: "transform"`（白名单纯数据变换，不写 Python、不执行任意代码）；`kind: "python"`（提供 `node.py`，继承 `NodeBase`，实现 `spec()` / `run(ctx, inputs, params)`）。
4. **大插件支持（v3 新增）**：上传上限 2GB、解压总量 8GB、单文件 4GB、成员 20000（上限参数唯一引用 `worker/limits.py`）；大 zip **流式下载 + 流式解压**，无需整包载入内存；zip 内可携带 `ui/` 资源目录（§6 大插件规范）。
5. **强校验（G1-G10）**：`app/plugin/manifest_v2.py` 替换旧基础门，scan/install/打包三入口同门；坏 manifest → **显式错误码**（`manifest_type_id` / `manifest_ports` / `manifest_op` …），不再静默降级。
6. **SDK**：`frameweave-plugins/sdk/` 提供脚手架——`check_manifest.py`（G1-G10 + v1 兼容，纯标准库）、`package_plugin.py`（先校验后打包 + 大包上限预检）、`node.py.template`、`manifest.example.json` / `manifest.transform.example.json`。开箱即用：`python sdk/check_manifest.py my_plugin/manifest.json` → `python sdk/package_plugin.py my_plugin`。

---

## 1. 插件包结构与生命周期

### 1.1 目录结构

```
<USER_NODES_DIR>/<pkg>/            `# 每个子目录 = 一个插件包
├── manifest.json                  `# 必需：契约声明（唯一识别入口，顶层必须有）
├── node.py                        `# kind=python 必需：入口脚本（entrypoint 指向）
├── ui/                            `# 可选：UI 资源目录（js/mjs/css，ui.entry 指向）
└── （可选资源：数据/模型/脚本/静态文件）
```

安装位置：
- 开发环境：`FrameWeave/backend/user_nodes/`（平台仓库现为空壳，`.gitkeep` 占位）
- 独立插件项目：`frameweave-plugins/packages/<pkg>/`（官方/社区统一在此开发，经发布管道产出 zip）
- 打包版运行时：`%APPDATA%\FrameWeave\user_nodes/`（server 启动时 `declarative.set_user_nodes_dir` 指向，可写、升级保留）
- 环境变量覆盖：`FRAMEWEAVE_USER_NODES`

### 1.2 加载与生命周期

| 阶段 | 触发 | 行为 |
|---|---|---|
| 扫描 | server 启动 / `POST /api/user_nodes/reload` | `declarative.scan()` 遍历子目录读 `manifest.json` → **G1-G10 强校验** → 注入默认值（v1 兼容）→ 注册 NodeSpec；坏包入 `scan_errors()`（显式 manifest_* 错误码） |
| 安装 | `POST /api/user_nodes/install`（zip 上传）/ 商城 `POST /api/market/items/{id}/install` | 解压校验（zip-slip 拦截、五上限）→ staging → **G1-G10 强校验** → 原子 rename → 重扫 |
| 导出 | `GET /api/export/node/{type_id}` | 打包 `<pkg>.zip` 下载（可再传商城/网盘） |
| 卸载/禁用/升级/回滚 | P1 生命周期 API（`plugin_registry` 事务管线） | 状态机 absent/active/disabled/quarantined/updating |

---

## 2. manifest 契约（v2）

### 2.1 字段总表

| 字段 | 必填 | 类型/取值 | 规则 |
|---|---|---|---|
| `schema_version` | ✓ | int，`const: 2` | manifest 格式版本；缺失或 `1` = v1 兼容通道触发条件（见 §2.5）；**其它整数（如 3）→ 拒绝**（`manifest_schema_version`） |
| `engine_api` | ✓ | string `主.次` | 插件声明的引擎 ABI 版本（当前平台 `1.0`）；缺失走 v1 兼容注入 `1.0`；**主版本与平台不一致 → 拒绝**（`manifest_engine_api`） |
| `min_engine_api` / `max_engine_api` | | string `主.次` | 可运行的最低/最高引擎 ABI；平台 ABI 越界 → 拒绝 |
| `type_id` | ✓ | string | **前缀白名单 + 命名**：`^(core|user|[a-z0-9]{2,16})/[a-z0-9][a-z0-9_.-]*$`。`core/` **仅官方签名包**（须 `signature` 占位）；`user/` 任意个人插件；第三方自定义前缀须 `manifest_v2.register_prefix()` 登记（未登记 → `manifest_type_id`） |
| `title` / `category` / `description` | ✓ | string / 枚举 / string | category ∈ 输入/语义/分析/音频/剪辑/输出/控制/用户节点 |
| `version` | ✓ | semver（`^\d+\.\d+\.\d+(-…)?$`） | **v1 可选 → v2 必填**；缓存键含 version |
| `kind` | ✓ | enum: `transform` `\|` `python` | 白名单落地；ui/hybrid 待能力评审后开放 |
| `runtime` | | enum: `transform` `\|` `python` | `kind=transform` → 恒 `transform`（可缺省）；`kind=python` → 必填 `python` |
| `entrypoint` / `class_name` | kind=python 时 ✓ | string | 入口脚本/类名（包内相对 `*.py`、无 `..` 逃逸、类名合法标识符；文件层校验：安装时须真实存在于包目录） |
| `inputs` / `outputs` / `params` | **✓ v2 必填**（数组可空但字段必须存在） | PortSpec[] | **R1 核心**：python 插件也必须显式声明；字段见 §2.2 |
| `transform` | kind=transform 时 ✓ | object | 输出端口名 → op 表达式；键必须 ⊆ outputs 名（白名单见 §2.3） |
| `gpu_required` / `gpu_recommended` / `streaming` | | boolean | 默认 false |
| `permission` | | object | 五作用域声明，默认 **deny-all**（§2.4） |
| `dependencies` | | array | 依赖声明（channel: pip `\|` bundle `\|` market，§2.4） |
| `ui` | | object | **UI 资源目录声明（v3 开放）**：`{entry: "ui/*.js\|mjs\|css", sandbox, capabilities}`；entry 须在包内 `ui/` 子树、不逃逸（§2.4 / §6） |
| `signature` | | object | 占位：`{alg, key_id, value, over}`；无签名为社区级安装+警告，结构非法 → 拒绝（§2.4 / G10） |
| （未知字段） | | | v2 严格模式 `additionalProperties: false` → 未知字段拒绝（`manifest_unknown_field`）；v1 兼容通道 → 忽略+警告 |

### 2.2 端口声明 PortSpec（端口声明必填）

```jsonc
{
  "name": "engine",          `// ✓ 必填，^[A-Za-z_][A-Za-z0-9_]*$，数组内唯一
  "type": "STRING",          `// ✓ 必填，∈ PortType 13 类型（§5）；未注册类型 → 拒绝（不再静默 STRING）
  "label": "引擎",           `// 展示名
  "required": true,          `// 默认 true
  "default": "edge",         `// 默认值（widget=number 时须为数字）
  "widget": "select",        `// text|number|select|file|toggle|password，默认 text
  "options": ["edge","manual","indextts","cloud"],  `// select 必填非空
  "description": "文字转语音引擎"
}
```

语义校验规则：
1. `name` 在 inputs / outputs / params **各自数组内唯一**；同一端口名不得同时出现在 inputs 与 outputs。
2. widget 组合：`select` → options 非空；`number` → default 可转数字；`file`/ `toggle`/ `password` 仅 params 允许（端口 widget 忽略）。
3. `type` 值 ∈ PortType 注册表（§5 的 13 个核心类型 + 注册制扩展）；**未知 → 拒绝**。
4. transform 插件的端口 type 不得为媒体类型（VIDEO/IMAGE/AUDIO——transform 无文件读写能力，详见 §5.3）。
5. 端口对象**未知字段 → 拒绝**（v2 严格模式；允许 name/type/label/required/default/widget/options/description/min/max/step/placeholder/hint）。

### 2.3 transform op 白名单（kind=transform）

**取值源表达式**（递归）：字面量 / `{"$const": 值}` / `{"$param": "名"}` / `{"$input": "端口"}` / `{"$list": [表达式…]}`。

**顶层 op（v2 唯一合法形态）**：

| op | 参数 | 示例 |
|---|---|---|
| `concat` | `sources`(表达式数组,必填)、`sep`/`prefix`/`suffix`(可选) | `{"op":"concat","sources":[{"$input":"name"}],"sep":"，","suffix":"！"}` |
| `json_get` | `source`(表达式)、`path`(点路径 a.b.0) | `{"op":"json_get","source":{"$input":"meta"},"path":"duration"}` |
| `math` | `a`/`b`(表达式)、`op` ∈ + - * / % max min | `{"op":"math","a":{"$input":"n"},"b":{"$const":2},"op":"*"}` |
| `str` | `template`(含 {} 占位)、`values`:{占位名:表达式} | `{"op":"str","template":"{d}分钟","values":{"d":{"$param":"dur"}}}` |
| `upper` / `lower` / `len` | `source`(表达式) | `{"op":"len","source":{"$input":"text"}}` |
| `list` | `sources`(表达式数组) | `{"op":"list","sources":[{"$const":1},{"$input":"x"}]}` |

- **未知 op → 扫描/安装/打包期拒绝**（`manifest_op`），运行期不再静默产出 None。
- v1 shorthand（`{"$concat": [...]}`、`{"$upper": x}`、`"get"` 别名）仅 v1 兼容模式接受并归一化（内存副本）；v2 严格模式直接拒绝。
- 安全模型：**纯数据变换，不执行用户代码**；transform 无文件/网络/子进程能力。

### 2.4 扩展声明

- **permission**（默认 deny-all，G7，未知作用域键 → 拒绝）：五作用域 `fs`（`read`/`write`: glob 数组）、`network`（`allow`/`deny`: 主机数组）、`subprocess`（`enabled`: bool、`argv_allowlist`: 数组）、`secret`（`refs`: 密钥名数组）、`env`（`vars`: 环境变量名数组）。
- **dependencies**（G8）：`{id, version, channel: pip|bundle|market, optional}`；P1 做声明校验 + 可解析性预检（optional=false 且 channel 不可达 → 安装拒绝）。通道语义：`pip` = PyPI 依赖、`bundle` = 平台内置运行时（ffmpeg 等）、`market` = 商城插件依赖。
- **ui**（G8，**v3 开放声明**）：`{entry: "ui/*.js|mjs|css", sandbox, capabilities}`；`entry` 必须指向包内 `ui/` 子树（`^ui/[A-Za-z0-9_./-]+\.(js|mjs|css)$`，禁止 `..`/绝对路径逃逸），安装时校验 entry 文件真实存在（缺失 → 警告）。UI 资源由前端加载器按沙箱加载（P3 前端加载器落地）。
- **signature**（G10 占位）：`{alg:"ed25519", key_id, value, over}`；无签名为 L2 社区级安装并显式警告；结构非法（缺 value/over/key_id 或未知键）→ 拒绝（`manifest_signature`）；`core/` 前缀在 v2 严格模式强制要求签名。真实验签属 P3。

### 2.5 v1 兼容模式（仅读入升级，不写盘）

- 触发：`schema_version` 缺失或为 `1`。
- 处理（只发生在内存副本 `ManifestResult.manifest`，**磁盘 manifest.json 原样**）：
  - 注入 `schema_version=2`、`engine_api="1.0"`、`permission={}`、`gpu_required/gpu_recommended/streaming=false`；
  - `kind` 探测：`runtime=python` 或存在 `entrypoint/class_name` → python；否则有 `transform` → transform；
  - `title`/`category`/`description` 缺省注入；python 包 `inputs/outputs/params` 缺失注入 `[]`（transform 包端口缺失 → 拒绝）；
  - v1 shorthand op 归一化为 v2 op 表达式；未知顶层字段忽略+警告。
- 显式非 2 整数 `schema_version`（如 3）→ **拒绝**（未来版本不可按旧规则解读）。
- **新插件一律写 v2**：完整 `schema_version` + 端口声明，不要依赖兼容通道。

### 2.6 校验门摘要（G1-G10）

| 门 | 校验 | 不通过 → 错误码 |
|---|---|---|
| G1 | schema_version（缺失=兼容）/ engine_api 必填+兼容 | 拒绝 `manifest_schema_version` / `manifest_engine_api` |
| G2 | type_id 格式 + 前缀白名单（core 须签名）/ title·category·description | 拒绝 `manifest_type_id` / `manifest_meta` |
| G3 | version semver / min·max_engine_api 边界 | 拒绝 `manifest_version` / `manifest_engine_api` |
| G4 | kind ∈ {transform`\|`python} / runtime / entrypoint / class_name | 拒绝 `manifest_kind` |
| G5 | 端口声明存在 + PortSpec 合法（类型/widget/重名/媒体禁入） | **拒绝 `manifest_ports`** |
| G6 | transform op 白名单（递归）+ 键 ⊆ outputs + 媒体端口禁入 | 拒绝 `manifest_op` |
| G7 | permission 五作用域合法 | 拒绝 `manifest_permission` |
| G8 | dependencies（channel 白名单）/ ui（entry 不逃逸） | 拒绝 `manifest_dependencies` / `manifest_ui` |
| G9 | 安装前大小/成员/压缩比上限（§6 上限表，`worker/limits.py`） | 拒绝（413/422，`zip_too_large` 等） |
| G10 | signature 占位结构 | 无签名→警告；结构非法→拒绝 `manifest_signature` |

---

## 3. NodeBase 开发指南（kind=python）

平台基类 `app.nodespec.NodeBase`（`FrameWeave/backend/app/nodespec.py`）：

```python
class NodeBase:
    @classmethod
    def spec(cls) -> NodeSpec:                     `# 声明类型与端口（与 manifest 一致）
        raise NotImplementedError
    async def run(self, ctx, inputs, params) -> Dict[str, Any]:   `# 执行
        raise NotImplementedError
    async def setup(self, ctx) -> None:            `# 一次性初始化（可选；默认无操作）
        ...
```

### 3.1 spec()

返回 `NodeSpec(type_id=…, title=…, category=…, inputs=[…], outputs=[…], params=[…], version=…)`。
- inputs/outputs 用 `PortSpec(name, type=PortType.X, label=…, required=…)`；
- params 另加 `widget`/`options`/`default`（前端 ParamPanel 渲染）。
- **与 manifest 的一致性要求**：v2 契约下 manifest 的 `inputs/outputs/params` 是唯一真源；`spec()` 用于运行时注册，二者必须一一对应。

### 3.2 run(ctx, inputs, params)

- **签名**：`async def run(self, ctx, inputs, params) -> Dict[str, Any]`。
- **inputs**：`{端口名: 值}`。上游是 python 节点 → 值为 `Asset`（读文件走 `asset.path` / `ctx["store"].read_json(asset.id)`）；上游是 transform 或未连 → `None`/标量/dict。`@` 直接引用素材库 → `Asset`。
- **params**：`{参数名: 值}`，来自节点面板（含 `@secret:名称` 保险箱引用形态）。
- **返回值**：`{输出端口名: Asset 或值}`。**必须是 dict**（非 dict → 运行时错误 FAILED）。
- **异常语义**：`raise` → 节点 FAILED 并记录 error（平台兜底）；不要吞异常伪造成功。
- **超时**：平台按节点分级 `asyncio.timeout` 包裹 setup/run；setup 超时上限 `min(120s, run超时/3)`。

### 3.3 setup(ctx)

一次性初始化（如加载模型），失败 → 节点 failed。默认无操作。

### 3.4 ctx 字段表（平台构造）

| 键 | 类型 | 说明 |
|---|---|---|
| `ctx["workdir"]` | str | 资产库根目录（`store.root`） |
| `ctx["store"]` | AssetStore | 资产读写（§4.2） |
| `ctx["logger"]` | callable | 事件回调（`self._emit`）；插件可发 `node_update` 等事件 |
| `ctx["progress"]` | callable(p) | 进度上报（0~1，发 node_update 事件） |
| `ctx["spawn_tracked"]` / `ctx["run_tracked"]` | callable | **子进程托管入口**：把同步 subprocess 阻塞移出事件循环；取消/超时统一 kill 进程树 |
| `ctx["track_subprocess"]` / `ctx["untrack_subprocess"]` | callable | 子进程登记/注销 |
| `ctx["subprocess_registry"]` | object | 子进程注册表（Engine 自身） |

**同步阻塞红线**：插件内裸 `subprocess.run`（阻塞式、无 timeout 捕获）禁止直接出现在 async run 内——必须走 `ctx["run_tracked"]` / `ctx["spawn_tracked"]` 托管。

### 3.5 编写约束清单

1. 不 import 平台私有模块（`app.engine` 内部符号）；只依赖公开面 `app.nodespec` / `app.assets` / `app.ports` / `app.registry.register`。
2. 不读写平台数据目录以外路径；写资产必须经 `ctx["store"]`。
3. 密钥类参数（key/token/password/api/secret）只经 `params` 读取，不入日志。
4. 输出文件名用 uuid 后缀避免并发覆盖。
5. 大插件注意：模型/数据文件放包内任意层级均可（单文件 ≤ 4GB、总量 ≤ 8GB、成员 ≤ 20000）；**不要**在 `run()` 里整包读入超大文件，用 `ctx["store"]` 流式处理。

---

## 4. Asset 资产协议

### 4.1 Asset 数据类（app/assets.py）

| 字段 | 说明 |
|---|---|
| `id` | 资产 id（save_asset 时生成） |
| `kind` | VIDEO/AUDIO/IMAGE/SCRIPT/SEGMENTS/TIMELINE/JSON…（字符串，与 PortType.value 一致） |
| `path` | 媒体类：本地文件路径；结构化：json 落盘路径 |
| `meta` | 元数据 dict（时长/分辨率/引擎等） |
| `fingerprint` | 内容指纹（缓存键组成；save_asset 自动计算） |
| `node_id` / `created_at` / `size` | 产出节点 / 时间 / 字节数 |

### 4.2 AssetStore 接口（ctx["store"]）

| 方法 | 说明 |
|---|---|
| `save_asset(asset, payload=None)` | 落盘：id 自动生成；payload 非空且无 path → 写 json；fingerprint/size 自动补全；返回 asset |
| `get(aid)` | 取 Asset（None = 不存在） |
| `read_json(aid)` | 读结构化资产内容（json 解析） |
| `delete(aid)` | 删资产（文件+meta） |
| `root` / `files_dir` / `meta_dir` | 目录属性 |

### 4.3 产出资产规范

- **媒体**（视频/音频/图片）：先写文件（建议 `store.files_dir/<子目录>/`），构造 `Asset(id="", kind=PortType.X.value, path=…, meta=…)` → `store.save_asset(asset)` → 返回 dict 值。
- **结构化**（JSON/SEGMENTS/SUBTITLE/SCRIPT…）：构造 `Asset(id="", kind=…, meta=…)`，调 `store.save_asset(asset, payload=<结构化数据>)` 自动落 json。
- 端口声明的输出必须是 ports 里声明过的键（v2 语义下非 ports 声明的输出键会被忽略/告警）。

### 4.4 transform 节点的 Asset 解包（引擎自动）

- python 节点：上游 Asset **原样**传入（需要读文件）。
- transform 节点：引擎自动解包——`Asset` 且 path 以 `.json` 结尾 → `store.read_json` 载荷；媒体 → `{"kind", "path", "meta"}` 视图 + 审计告警。
- 防御层：`$input` 遇 Asset 返回结构化视图 + 告警，杜绝 dataclass repr 渗入输出。

---

## 5. PortType 13 类型与连线规则

### 5.1 类型表（app/ports.py）

| 类型 | 语义 |
|---|---|
| `VIDEO` / `IMAGE` / `AUDIO` / `TIMELINE` | 媒体资产（文件路径 + 元数据） |
| `SCRIPT` | 文案文本 |
| `SUBTITLE` | 字幕列表 |
| `SEGMENTS` | 片段列表（场景/镜头切分结果） |
| `JSON` | 通用结构化数据 |
| `STRING` / `INT` / `FLOAT` / `BOOL` | 标量 |
| `ANY` | 宽泛类型 |

### 5.2 连线规则（app/ports.py can_connect）

- `ANY` 可连一切；同类型互连；
- `JSON` 可连 JSON/SCRIPT/SEGMENTS/SUBTITLE/STRING；目标为 JSON 时任意源可连；
- 媒体资产**不窄化**：VIDEO 不可直接连 IMAGE（需显式抽帧节点）。

### 5.3 transform 端口类型限制

transform 的 inputs/outputs/params type 必须 ∈ {STRING, INT, FLOAT, BOOL, JSON, SCRIPT, SUBTITLE, SEGMENTS, TIMELINE, ANY}；**VIDEO/IMAGE/AUDIO → 拒绝**。

---

## 6. 大插件支持（v3）

### 6.1 上限表（参数唯一引用 `app/worker/limits.py`，SDK 常量同源对齐）

| 参数 | v2（旧） | **v3（新）** | 说明 |
|---|---|---|---|
| 上传包体积（zip 本体） | 50MB | **2GB** | `MAX_UPLOAD_MB=2048`；Content-Length 预检 413 + 流式字节截断防绕过 |
| 解压总量 | 500MB | **8GB** | 逐文件字节级累计（声明值预检 + 实际解压复核） |
| 单文件 | 200MB | **4GB** | 中央目录声明预检 + 流式解压字节级复核；>4GB 走 ZIP64 |
| 成员数 | 4096 | **20000** | 中央目录成员计数 |
| 压缩比 | 100:1 | 100:1（不变） | 累计 uncompressed/zip_size；防 zip 炸弹 |
| 流式块 | 1MB | 1MB（不变） | `ZIP_BLOCK`，上传/下载/解压共用 |

超限错误码：`zip_too_large`（413）/ `total_size_exceeded` / `file_size_exceeded` / `member_count_exceeded` / `zip_bomb_ratio`。

### 6.2 大 zip 流式下载（商城）

`market._fetch_zip` 流式下载：按 `ZIP_BLOCK` 块读落盘（2GB 级包不整包载入内存）→ 下载过程累计字节数与 sha256 → 超过 `UPLOAD_MAX_BYTES` 立即中止并清理 → 下载完成校验 `zipfile.is_zipfile` → 发布表单登记 `sha256` 时摘要比对（不匹配拒绝+清理，防传输损坏/篡改）。超时 120s（大包慢链路）。

### 6.3 分片安装设计（P3 预留）

单次 HTTP 上传上限 2GB 对绝大多数模型包已足够；对超 2GB 场景预留分片协议设计：
- 客户端按固定分片（如 256MB）顺序上传，服务端 `POST /api/user_nodes/install/chunk`（`{chunk_index, chunk_total, upload_id}`）落 `DATA_DIR/tmp/chunks/<upload_id>/`，全部到齐后合并为 zip 再走既有 `install_zip` 事务（staging + G1-G10 + 原子 rename）；
- 分片清单与 sha256 校验在合并前完成；任一超限/损坏分片 → 整个 upload_id 清理，显式错误码返回；
- 前端加载器侧做断点续传（Range 续传 + 本地已下载分片索引）。
（本设计为协议契约，实现随 P3 前端加载器任务落地。）

### 6.4 UI 资源目录 `ui/` 声明

- 插件包可携带 `ui/` 目录（js/mjs/css），在 manifest 用 `ui` 字段声明：
  ```jsonc
  "ui": {
    "entry": "ui/index.js",          `// 必填：^ui/[A-Za-z0-9_./-]+\.(js|mjs|css)$，须在包内 ui/ 子树
    "sandbox": {"allow": [], "allowSameOrigin": true},   `// 可选：沙箱策略对象
    "capabilities": ["readonly"]     `// 可选：能力声明数组
  }
  ```
- 校验（G8）：`entry` 格式 + 不逃逸（无 `..`、非绝对路径）；安装时 entry 文件须真实存在于包内（缺失 → 警告，不阻断安装）。
- 加载：由前端加载器在沙箱 iframe/worker 中按 `capabilities` 授权加载（P3 落地）；`sandbox` 字段为加载器策略输入，未知键 → 拒绝。

### 6.5 大包开发建议

- 模型/数据文件放包内任意层级；`manifest.json` 必须在包内（顶层最常见，`_resolve_package_dir` 要求单一顶层目录）。
- SDK `package_plugin.py` 打包前预检：单文件 ≤4GB、总量 ≤8GB、成员 ≤20000、zip 本体 ≤2GB，超限拒绝打包并提示。
- 大文件用 ZIP_STORED 或高压缩级别按需选择；不要打包缓存/临时文件。

---

## 7. zip 打包与商城分发

### 7.1 打包布局

```
<my_plugin>/                `# zip 顶层目录名 = 包名（安装后成为 user_nodes/<顶层>/）
├── manifest.json           `# 必须位于包内任意层级；顶层最常见
├── node.py / 资源
└── ui/                     `# 可选：UI 资源目录
```
打包要求：zip 合法、非空、无路径穿越成员（`..` / 绝对路径 / 驱动器符 → 拒绝）；`manifest.json` 存在且通过 G1-G10；大小/成员符合 §6.1 上限。

### 7.2 发布流程（商城）

1. 本地校验：`python sdk/check_manifest.py my_plugin/manifest.json`（零错误；v1 包提示兼容警告）；
2. 打包：`python sdk/package_plugin.py my_plugin -o dist` → `dist/my_plugin.zip` + 打印 sha256 与字节数（超限拒绝）；
3. 发布表单（M19 商城）：kind=node、标题/描述/作者、价格（0=免费）、标签、download_url（GitHub Release/网盘直链）或内联 zip、type_id、sha256、size、min_client_version；
4. 平台侧：`GET /api/export/node/{type_id}` 可随时导出已装包 zip；安装走 `POST /api/market/items/{id}/install`（付费扣积分，20/80 分成进作者）；大 zip 下载走 §6.2 流式 + sha256 校验。

### 7.3 检查清单（发布前）

- [ ] manifest 含 `schema_version: 2` 与 `engine_api: "1.0"`
- [ ] `type_id` 前缀合法（`user/` 或已登记前缀；不要用 `core/` 除非官方签名）
- [ ] `version` 是 semver
- [ ] **`inputs`/`outputs`/`params` 字段齐全，端口 type ∈ 13 类型，select 有 options**
- [ ] transform 包：op 白名单、键 ⊆ outputs、无媒体端口
- [ ] ui 声明：entry 在 `ui/` 子树、资源存在
- [ ] 大小/成员符合上限表（§6.1）；zip 无穿越成员；sha256 与发布表单一致
- [ ] 依赖/permission 声明合法（五作用域；channel ∈ pip/bundle/market）

---

## 8. 参考实现逐一标注（10 个 core_*）

> 通则：**端口声明必填**——core_* 包的端口当前声明在 `node.py` 的 `spec()`，应 1:1 回填进 `manifest.json`（v2 唯一真源，P0.1 R1 修复项）；回填前这些包以 v1 兼容通道加载（缺失 schema_version/engine_api/端口 → 注入 + 警告，不写盘）。

### 8.1 core_load_video — 导入视频（反例 → 必须补端口声明）
- run()：`params.get("path")` → ffprobe → 复制入 `store.files_dir/videos/` → `Asset(kind=VIDEO, path, meta, fingerprint)` → return `{"video": asset, "meta": meta}`。
- **要点（R1 缺口示范）**：manifest 无端口、spec() 不存在 → v2 校验必拒（兼容通道也要求 transform 端口；python 包注入空端口仅容忍，官方包须回填）。应补：params `path:STRING widget=file`；outputs `video:VIDEO`；`meta` 收敛进 `video.meta` 或声明 `meta:JSON` 输出。

### 8.2 core_scene_detect — 场景检测
- inputs `video:VIDEO`；outputs `segments:SEGMENTS`、`count:INT`；params `sample_fps/threshold/min_duration:FLOAT`。
- 消费上游 VIDEO Asset（`asset.path`）；产出 `SEGMENTS` 用 `save_asset(asset, payload=…)`。

### 8.3 core_ai_narrate — AI 解说文案
- inputs `video:VIDEO`、`segments:SEGMENTS`、`script:SCRIPT`(optional)；outputs `script:SCRIPT`、`narration:JSON`；params `mode/style/language/api_key/base_url/model:STRING`、`word_count:INT`。
- `api_key` 走保险箱；`segments` 读上游结构化资产。

### 8.4 core_asr — 语音转字幕
- inputs `video:VIDEO`、`audio:AUDIO`；outputs `subtitle:SUBTITLE`、`srt:STRING`；params `model/language/device:STRING`。
- 模型加载放 `setup`；输出字幕 `save_asset(asset, payload=[{start,end,text}…])`。

### 8.5 core_subtitle_compose — 字幕装配
- inputs `subtitle:SUBTITLE`、`script:SCRIPT`；outputs `styled_subtitle:SUBTITLE`；params `preset/font/color/stroke/animation:STRING`、`size:INT`、`karaoke:BOOL`（toggle）。

### 8.6 core_translate — 翻译
- inputs `script:SCRIPT`、`subtitle:SUBTITLE`；outputs `script:SCRIPT`、`subtitle:SUBTITLE`；params `target:STRING`、`keep_style:BOOL`、`api_key/base_url/model`。
- **注意**：同名端口 inputs 与 outputs 都有 `script`/`subtitle`——**v2 语义校验「同端口名不得同时出现在 inputs 与 outputs」** 在回填 manifest 时需处理（改名或合规化）。

### 8.7 core_tts — TTS 配音（端口声明 + widget 完整范例）
- inputs `script:SCRIPT`(必填)、`audio_in:AUDIO`(可选)、`audio_ref:AUDIO`(可选)；outputs `audio:AUDIO`、`marks:JSON`；params（14 个）含 `engine:STRING`(select+options edge/manual/indextts/cloud)、`voice:STRING`(select)、`output_format:STRING`(select mp3/wav/ogg)、`auto_start_server:BOOL`(toggle)、`api_key/base_url`(text)。

### 8.8 core_video_render — MP4 渲染
- inputs `video:VIDEO`、`segments:SEGMENTS`、`audio:AUDIO`、`styled_subtitle:SUBTITLE`；outputs `video:VIDEO`；params `aspect/resolution/transition/output_name:STRING`、`fps:INT`、`burn_subtitle:BOOL`。
- ffmpeg 调用走 `ctx["run_tracked"]`。

### 8.9 core_draft_export — 剪映草稿导出
- inputs `video:VIDEO`、`segments:SEGMENTS`、`audio:AUDIO`、`styled_subtitle:SUBTITLE`；outputs `draft:JSON`、`xml:STRING`、`edl:STRING`、`fcpxml:STRING`；params `format/aspect/resolution/draft_name/transition/output_dir:STRING`、`transition_duration:FLOAT`。

### 8.10 core_batch_render — 批量渲染（Loop 参考）
- inputs `video:VIDEO`、`segments:SEGMENTS`、`items:JSON`；outputs `results:JSON`、`video:VIDEO`；params `manual_items/variables/voice/aspect/resolution/transition/output_prefix:STRING`、`fps:INT`、`max_concurrent:INT`。
- `items:JSON` 输入接收矩阵数据；`max_concurrent:INT` 并发控制。

---

## 9. 开发脚手架（SDK）快速上手

`frameweave-plugins/sdk/`：

| 文件 | 用途 |
|---|---|
| `node.py.template` | python 插件节点骨架（NodeBase 子类：spec/setup/run + Asset + ctx + 子进程托管） |
| `manifest.example.json` | **含端口声明的完整 python 插件示例 manifest**（v2，含 permission） |
| `manifest.transform.example.json` | transform 声明式插件示例（op 白名单 + 非媒体端口） |
| `check_manifest.py` | **G1-G10 全部门校验器 + v1 兼容通道 + 大插件上限常量**（纯标准库，与后端 `app/plugin/manifest_v2.py` 对齐） |
| `package_plugin.py` | 打包 `<pkg>` 目录为分发 zip（先校验；单文件/总量/成员/zip 本体上限预检；输出 sha256/字节数） |
| `README.md` | 快速开始 |

一条龙：

```bash
`# 1. 初始化：拷贝模板
cp sdk/manifest.example.json   my_plugin/manifest.json
cp sdk/node.py.template        my_plugin/node.py
`# 2. 改 type_id / 端口 / 逻辑
`# 3. 校验（必过）
python sdk/check_manifest.py my_plugin/manifest.json
`# 4. 打包
python sdk/package_plugin.py my_plugin -o dist
`# 5. 发布：商城表单填 dist/my_plugin.zip + sha256
```

---

## 10. 与既有文档/任务的关系

- 本规范替代旧的 `FrameWeave/docs/node-plugin-spec.md`（v1 时代，仅 transform）；`FrameWeave/docs/plugin-development-spec.md` 与 `frameweave-plugins/docs/plugin-development-spec.md` 互为镜像。
- manifest 字段权威：契约 v2 文档 §2；校验门：§2.6 / `app/plugin/manifest_v2.py`；transform op：§2.3；上限：§6.1 / `app/worker/limits.py`。
- P1 配套任务：manifest 端口回填 + 生命周期 API（backend-p1/t3）；引擎 SPI/Provider（backend-p2/t4）；前端加载器（P3）；商城端到端（market-e2e/t5）；集成验证（verify-integrate/t7）。
