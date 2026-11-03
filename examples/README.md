# examples/ —— 不进发布面的样例包

`packages/` 是发布正本：里面每个包都必须带官方签名（`check_signature.py` 的规则是
**NO_SIG 永不豁免**，只有 SIG_FAIL 才可能在 `sdk/signature_allowlist.json` 里登记）。
样例包没有签名，所以放在这里，不放在 `packages/`。

> 2026-11-03 之前这里叫 `packages/hello/`（未跟踪），它会让
> `check_all --with-signature` 直接 `exit 1`（`signature=FAIL`），
> 也就是说**一个本地样例能把发布门禁判红**。挪出 `packages/` 后才真正做到
> 「门禁只由版本库内容决定」。

## `user_declarative_hello/`

零 Python 代码的声明式节点：`kind=transform`，用 `transform` 里的白名单运算
（`concat` / `len`）拼出两个输出端口。可以直接当自写声明式包的最小模板。

三条硬约束（在它身上都能看到）：

| 约束 | 本例取值 | 违反会怎样 |
|---|---|---|
| 目录名 = `id` 的 `/` 换成 `_` | `user_declarative_hello` ↔ `user/declarative_hello` | V2 FAIL「目录名与 id 不匹配」 |
| `params[].type` 是小写值类型（`string`/`int`/…），端口 `type` 是大写端口类型（`STRING`/`INT`/…） | 参数 `string`，端口 `STRING` | V12 FAIL「必须是 […小写] 之一」 |
| `params[]` 的 `description` 必填非空；`outputs[]` 不许有 `required` | 参数写了 description，两个输出都没 required | V12 / B3 FAIL |

## 校验与实跑

```bash
# 结构校验（真跑 SDK 的 V1–V13）
python sdk/check_manifest_v3.py examples/user_declarative_hello
# → 检查 1 个包：PASS（V6/V8/V9/V10 为有意的 SKIP，见脚本头注）

# 平台侧实跑（不是只看结构：真构造节点类、真执行 transform）
cd ../FrameWeave/backend && ./.venv/Scripts/python.exe -c "
import asyncio, os, sys; sys.path.insert(0,'.')
from app.plugin import manifest_io
from app import declarative
m = manifest_io.load_normalized(os.path.abspath('../../frameweave-plugins/examples/user_declarative_hello'))
out = asyncio.run(declarative._make_class(m)().run({}, {'name': '小明'}, {'prefix': '你好'}))
print(out)   # {'greeting': '你好，小明！', 'length': 2}
"
```

## 为什么样例不放 `packages/`

| 方案 | 结果 |
|---|---|
| 放 `packages/` 并签名 | 会进 `dist/`，等于向用户多发一个无实际用途的包；且样例的价值在于「随便改」，签名一改就失效 |
| 放 `packages/` 不签名 | `check_all --with-signature` 判红（`NO_SIG` 不许豁免）——门禁被本地文件左右 |
| 放 `examples/`（当前） | 门禁只扫 `packages/`，18 包四门全绿；样例可自由修改、可单独校验 |
