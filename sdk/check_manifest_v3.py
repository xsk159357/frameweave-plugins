"""manifest v3 校验器（最小实现）。

覆盖范围（对齐 docs/reports/节点重做_20261029/manifest_v3规格.md）：

  V1  顶层必填字段 + 未知字段拒绝 + manifest_version == 3
  V2  id 形如 `前缀/名称`
  V3  kind 枚举
  V5  端口：允许字段白名单 + type 必须在注册表 + 端口名唯一
  V12 params[].default / description 必填（且 description 非空）
  V13 params[].name 不得含 `.`
  V11 exposable[].param 必须存在于 params
  —   exposable[].recipe_role 非空 / safe_for_wizard 布尔 / advanced 不变量
  V7  perm 五键 + net 禁 `*` + model 必须有 sha256
  —   deps 结构与 optional/degrade 联动

未覆盖（本版有意不做，见输出里的 SKIP）：V4 端口类型兼容矩阵、V6 transform、V8 ui/web 子树、V9 包体上限、V10 签名。

用法:
    python check_manifest_v3.py <包目录|manifest.json 路径> [...]

退出码:
    0  全部通过
    1  有 FAIL
    2  环境缺失（路径不存在 / 不是合法 JSON）
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Tuple

PORT_TYPES = frozenset({
    "VIDEO", "AUDIO", "IMAGE", "TIMELINE",
    "TEXT", "SCRIPT", "SUBTITLE",
    "SEGMENTS", "CUES", "TRANSCRIPT", "LAYOUT",
    "STRING", "INT", "FLOAT", "BOOL",
})   # 注意：JSON 已从 v3 端口体系移除，不在此集合内

PARAM_TYPES = frozenset({"string", "int", "float", "bool", "enum", "asset", "text"})
KINDS = frozenset({"python", "transform"})
CATEGORIES = frozenset({"输入", "语义", "分析", "音频", "剪辑", "输出", "控制"})

# A1（2026-10-30 定，两次修正后的终值）：id 前缀 = **命名空间保留**。
#   前缀只承担两件事：① 归属命名空间（core 官方 / user 第三方）
#                     ② B1 的目录名映射（id 的 '/' 换 '_'）
#   **不作为权限判据。** 官方身份由签名决定（signature.key_id 命中官方锚 +
#   验签通过），与前缀无关；功能分类由 category 表达，前缀不再重复一遍。
#
#   曾一度定成七个（core/voice/visual/input/export/flow/user），那是把 category 的活
#   派给了前缀：四个功能值在重复 category 已有的能力，两个归属值在承担 category 表达
#   不了的东西——一个字段混装两种语义，两头都不好使。
ID_PREFIXES = frozenset({
    "core",     # 官方（归属）
    "user",     # 第三方 / 用户自建（归属）
})

TOP_ALLOWED = frozenset({
    "manifest_version", "id", "title", "category", "description", "version",
    "kind", "entrypoint", "class_name", "inputs", "outputs", "params",
    "transform", "perm", "deps", "min_engine", "ui", "web",
    "search_aliases", "deprecated", "runtime_hints", "signature", "exposable",
})
TOP_REQUIRED = ("manifest_version", "id", "title", "category", "description",
                "version", "kind", "inputs", "outputs", "params", "perm", "min_engine")

PORT_FIELDS = frozenset({"name", "type", "label", "required", "default", "description"})
PARAM_FIELDS = frozenset({"name", "type", "label", "default", "description",
                          "options", "min", "max", "step", "unit", "advanced"})
EXPOSABLE_FIELDS = frozenset({"param", "recipe_role", "safe_for_wizard"})
PERM_KEYS = frozenset({"fs", "net", "model", "exec", "secret"})

ID_RE = re.compile(r"^[a-z0-9_]+/[a-z0-9_]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.\-]+)?$")
ENGINE_RE = re.compile(r"^\d+\.\d+$")

Issues = List[Tuple[str, str]]        # (code, message)


def _is_nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def check(path: str) -> Tuple[List[Tuple[str, str, str]], bool]:
    """返回 (结果行, 是否有 FAIL)。结果行 = (级别, 码, 说明)。"""
    rows: List[Tuple[str, str, str]] = []
    ok = True

    def fail(code: str, msg: str) -> None:
        nonlocal ok
        ok = False
        rows.append(("FAIL", code, msg))

    def passed(code: str, msg: str = "") -> None:
        rows.append(("PASS", code, msg))

    # ---------- 读取 ----------
    mf_path = path
    if os.path.isdir(path):
        mf_path = os.path.join(path, "manifest.json")
    if not os.path.isfile(mf_path):
        return [("FAIL", "E_ENV", f"找不到 manifest: {mf_path}")], False
    try:
        with open(mf_path, "r", encoding="utf-8-sig") as fh:
            m = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return [("FAIL", "E_PARSE", f"不是合法 JSON: {exc}")], False
    if not isinstance(m, dict):
        return [("FAIL", "E_PARSE", "manifest 顶层必须是对象")], False

    # ---------- V1 顶层 ----------
    if m.get("manifest_version") != 3:
        fail("V1", f"manifest_version 必须为整数 3，实际 {m.get('manifest_version')!r}")
    else:
        passed("V1", "manifest_version=3")

    missing = [k for k in TOP_REQUIRED if k not in m]
    if missing:
        fail("V1", f"缺少必填字段: {missing}")
    unknown = sorted(set(m) - TOP_ALLOWED)
    if unknown:
        fail("V1", f"未知字段（v3 白名单外）: {unknown}")
    if not missing and not unknown:
        passed("V1", "顶层字段齐全且无未知字段")

    # ---------- V2 id ----------
    nid = m.get("id")
    if not isinstance(nid, str) or not ID_RE.match(nid):
        fail("V2", f"id 必须形如 `前缀/名称`（小写字母数字下划线），实际 {nid!r}")
    else:
        # A1：前缀必须在白名单里
        prefix = nid.split("/", 1)[0]
        if prefix not in ID_PREFIXES:
            fail("V2", f"id 前缀 {prefix!r} 不在白名单 {sorted(ID_PREFIXES)}")
        # B1：目录名必须等于 id 的 '/'→'_'（传 manifest 文件时取父目录名）
        expect_dir = nid.replace("/", "_")
        actual_dir = os.path.basename(os.path.normpath(os.path.dirname(mf_path)))
        if actual_dir and actual_dir != expect_dir:
            fail("V2", f"目录名 {actual_dir!r} 与 id 不匹配（应为 {expect_dir!r}）")
        if prefix in ID_PREFIXES and actual_dir == expect_dir:
            passed("V2", f"{nid}（前缀 {prefix} 合法，目录名一致）")

    # ---------- V3 kind / version / category / min_engine ----------
    if m.get("kind") not in KINDS:
        fail("V3", f"kind 必须是 {sorted(KINDS)} 之一，实际 {m.get('kind')!r}")
    if not isinstance(m.get("version"), str) or not SEMVER_RE.match(str(m.get("version"))):
        fail("V3", f"version 必须是 semver，实际 {m.get('version')!r}")
    if m.get("category") not in CATEGORIES:
        fail("V3", f"category 必须是 {sorted(CATEGORIES)} 之一，实际 {m.get('category')!r}")
    if not isinstance(m.get("min_engine"), str) or not ENGINE_RE.match(str(m.get("min_engine"))):
        fail("V3", f"min_engine 必须形如 `主.次`，实际 {m.get('min_engine')!r}")
    if m.get("kind") in KINDS:
        passed("V3", f"kind={m.get('kind')} version={m.get('version')} min_engine={m.get('min_engine')}")

    # ---------- V5 端口 ----------
    port_names: Dict[str, str] = {}
    for side in ("inputs", "outputs"):
        ports = m.get(side)
        if not isinstance(ports, list):
            fail("V5", f"{side} 必须是数组")
            continue
        for i, p in enumerate(ports):
            where = f"{side}[{i}]"
            if not isinstance(p, dict):
                fail("V5", f"{where} 必须是对象")
                continue
            extra = sorted(set(p) - PORT_FIELDS)
            if extra:
                fail("V5", f"{where} 含端口不允许的字段 {extra}（v3 端口只有 {sorted(PORT_FIELDS)}）")
            # B3：输出端口没有 required 这个概念（要么产生要么不产生）
            if side == "outputs" and "required" in p:
                fail("V5", f"{where}: outputs 不允许 required 字段")
            for k in ("name", "type", "label"):
                if not _is_nonempty_str(p.get(k)):
                    fail("V5", f"{where}.{k} 必填且非空")
            t = p.get("type")
            if isinstance(t, str):
                if t not in PORT_TYPES:
                    hint = "（JSON 已从 v3 端口体系移除）" if t == "JSON" else ""
                    fail("V5", f"{where}.type={t!r} 不在类型注册表{hint}")
                elif t in PARAM_TYPES:
                    fail("V5", f"{where}.type={t!r} 是参数值类型，端口类型须用大写枚举")
            nm = p.get("name")
            if isinstance(nm, str):
                # 规格 §1.2：「端口名，同节点内唯一」——**跨 inputs/outputs 也算**。
                # 曾经把 key 写成 f"{side}:{nm}"，等于把作用域缩到单侧：
                # 一个包同时有 inputs.video 和 outputs.video 能过 SDK 检查，
                # 而平台 G5 拒它。同一个名字在上游下游各指一件事，
                # 连线时会取到哪一边取决于遍历顺序——这正是要拦的那种"看起来能跑"。
                if nm in port_names:
                    fail("V5", f"端口名重复: {nm!r}"
                               f"（{port_names[nm]} 与 {where}；v3 要求同节点内唯一）")
                port_names[nm] = where
    passed("V5", f"端口 {len(m.get('inputs') or [])} 入 / {len(m.get('outputs') or [])} 出")

    # ---------- params: V12 / V13 ----------
    params = m.get("params")
    param_names: List[str] = []
    if not isinstance(params, list):
        fail("V12", "params 必须是数组")
        params = []
    for i, p in enumerate(params):
        where = f"params[{i}]"
        if not isinstance(p, dict):
            fail("V12", f"{where} 必须是对象")
            continue
        extra = sorted(set(p) - PARAM_FIELDS)
        if extra:
            fail("V12", f"{where} 含未知字段 {extra}")
        nm = p.get("name")
        if not _is_nonempty_str(nm):
            fail("V12", f"{where}.name 必填且非空")
        else:
            # V13
            if "." in nm:
                fail("V13", f"{where}.name={nm!r} 含点号——overrides 键用 `.` 作分隔符，会造成切分歧义")
            if nm in param_names:
                fail("V12", f"参数名重复: {nm}")
            param_names.append(nm)
        t = p.get("type")
        if t not in PARAM_TYPES:
            fail("V12", f"{where}.type={t!r} 必须是 {sorted(PARAM_TYPES)} 之一（小写）")
        if not _is_nonempty_str(p.get("label")):
            fail("V12", f"{where}.label 必填且非空")
        if "default" not in p:
            fail("V12", f"{where}.default 必填（v3：配方不填也要有安全默认）")
        if not _is_nonempty_str(p.get("description")):
            fail("V12", f"{where}.description 必填且非空（空字符串不算填了）")
        if t == "enum" and not isinstance(p.get("options"), list):
            fail("V12", f"{where} 是 enum 但没有 options")

    passed("V12", f"{len(params)} 个参数，default 与 description 全填")
    passed("V13", f"{len(param_names)} 个参数名均不含点号")

    # ---------- V11 exposable ----------
    exp = m.get("exposable", [])
    if not isinstance(exp, list):
        fail("V11", "exposable 必须是数组")
        exp = []
    advanced = {p.get("name") for p in params if isinstance(p, dict) and p.get("advanced") is True}
    seen_exp: set = set()
    for i, e in enumerate(exp):
        where = f"exposable[{i}]"
        if not isinstance(e, dict):
            fail("V11", f"{where} 必须是对象")
            continue
        extra = sorted(set(e) - EXPOSABLE_FIELDS)
        if extra:
            fail("V11", f"{where} 含未知字段 {extra}")
        target = e.get("param")
        if not _is_nonempty_str(target):
            fail("V11", f"{where}.param 必填且非空")
        elif target not in param_names:
            fail("V11", f"{where}.param={target!r} 不存在于 params（向导会渲染出空控件）")
        elif target in seen_exp:
            fail("V11", f"{where}.param={target!r} 重复暴露")
        else:
            seen_exp.add(target)
        if not _is_nonempty_str(e.get("recipe_role")):
            fail("V11", f"{where}.recipe_role 必填且非空（不设兜底：向导不能显示内部参数名）")
        sfw = e.get("safe_for_wizard", True)
        if not isinstance(sfw, bool):
            fail("V11", f"{where}.safe_for_wizard 必须是布尔")
        # 不变量：advanced:true 进 exposable ⇒ safe_for_wizard 必须 false
        if target in advanced and sfw is not False:
            fail("V11", f"{where}.param={target!r} 在 params 里是 advanced:true，safe_for_wizard 必须为 false")
        # C：不变量②——advanced:false + safe_for_wizard:false 属可疑组合，给 WARN 不拦
        if target in param_names and target not in advanced and sfw is False:
            rows.append(("WARN", "V11",
                         f"{where}.param={target!r}: advanced:false 却 safe_for_wizard:false"
                         f"（要么标 advanced，要么允许直出）"))

    passed("V11", f"{len(exp)} 条 exposable，全部存在于 params 且 recipe_role 非空")

    # ---------- V7 perm ----------
    perm = m.get("perm")
    if not isinstance(perm, dict):
        fail("V7", "perm 必须是对象（v3 必填，缺省不是放行而是拒）")
    else:
        extra = sorted(set(perm) - PERM_KEYS)
        if extra:
            fail("V7", f"perm 含未知键 {extra}（v3 只有 {sorted(PERM_KEYS)}）")
        lost = sorted(PERM_KEYS - set(perm))
        if lost:
            rows.append(("WARN", "V7", f"perm 未声明 {lost} —— 按「不声明即拒绝」处理，功能可能失效"))
        fs = perm.get("fs", {})
        if isinstance(fs, dict):
            for k in fs:
                if k not in ("read", "write"):
                    fail("V7", f"perm.fs 只允许 read/write，出现 {k!r}")
                # 规格 §1.4：read/write「都是数组」。写 {"read": "x"} 时执行层
                # 要么把它当单条路径、要么当空——两种都猜，猜错就是越权或功能失效。
                elif not _is_str_list(fs[k]):
                    fail("V7", f"perm.fs.{k} 必须是字符串数组，实际 {type(fs[k]).__name__}")
        net = perm.get("net", {})
        hosts = net.get("hosts", []) if isinstance(net, dict) else []
        if "*" in (hosts or []):
            fail("V7", 'perm.net.hosts 不允许 "*"')
        models = perm.get("model", [])
        if isinstance(models, list):
            for i, mm in enumerate(models):
                if not isinstance(mm, dict) or not _is_nonempty_str(mm.get("sha256")):
                    fail("V7", f"perm.model[{i}] 必须含 sha256（与 deps 同源校验）")
        # 规格 §1.4：secret 是字符串数组（密钥名），不是单个名字、不是对象
        secret = perm.get("secret")
        if secret is not None and not _is_str_list(secret):
            fail("V7", f"perm.secret 必须是字符串数组，实际 {type(secret).__name__}")
        # 规格 §1.4：exec = [{bin, args_policy}]，args_policy 只允许 "argv_only"
        ex = perm.get("exec")
        if ex is not None:
            if not isinstance(ex, list):
                fail("V7", f"perm.exec 必须是数组 [{{bin,args_policy}}]，实际 {type(ex).__name__}")
            else:
                for i, item in enumerate(ex):
                    if not isinstance(item, dict):
                        fail("V7", f"perm.exec[{i}] 必须是对象（bin 为白名单可执行）")
                        continue
                    for k in item:
                        if k not in ("bin", "args_policy"):
                            fail("V7", f"perm.exec[{i}] 未知键 {k!r}（只允许 bin/args_policy）")
                    if not _is_nonempty_str(item.get("bin")):
                        fail("V7", f"perm.exec[{i}].bin 必填且非空")
                    ap = item.get("args_policy")
                    if ap is not None and ap not in ("argv_only",):
                        fail("V7", f"perm.exec[{i}].args_policy 只允许 'argv_only'，实际 {ap!r}")
        if isinstance(perm, dict) and not extra and not lost:
            passed("V7", f"perm 五键齐全（net.hosts={hosts}）")

    # ---------- deps ----------
    deps = m.get("deps", [])
    if not isinstance(deps, list):
        fail("V4", "deps 必须是数组")
        deps = []
    for i, d in enumerate(deps):
        where = f"deps[{i}]"
        if not isinstance(d, dict):
            fail("V4", f"{where} 必须是对象")
            continue
        kind = d.get("kind")
        if kind not in ("model", "pip", "bundle"):
            fail("V4", f"{where}.kind 必须是 model/pip/bundle，实际 {kind!r}")
            continue
        if not _is_nonempty_str(d.get("name")):
            fail("V4", f"{where}.name 必填")
        if kind == "model":
            if not isinstance(d.get("size"), int) or d.get("size", 0) <= 0:
                fail("V4", f"{where}.size 必须是精确字节数（正整数字面量）")
            if not _is_nonempty_str(d.get("sha256")):
                fail("V4", f"{where}.sha256 必填")
            if "optional" not in d:
                fail("V4", f"{where}.optional 必填")
            dg = d.get("degrade")
            if dg not in ("block", "fallback", "skip"):
                fail("V4", f"{where}.degrade 必填且为 block/fallback/skip，实际 {dg!r}")
            if d.get("optional") is False and dg != "block":
                fail("V4", f"{where}: optional:false 只能配 degrade=block（缺了就跑不了，不存在降级）")
            if not _is_nonempty_str(d.get("user_message")):
                fail("V4", f"{where}.user_message 必填（用户要看到体积与动作）")
        elif kind == "bundle":
            if not _is_nonempty_str(d.get("path")):
                fail("V4", f"{where}.path 必填（bundle 是包内相对路径）")

    # ---------- 未覆盖项 ----------
    for code, why in (
        ("V4/端口兼容", "类型兼容矩阵不在 manifest 校验范围内（需整图）"),
        ("V6", "transform 节点未在本版实现校验"),
        ("V8", "ui/web 子树未校验"),
        ("V9", "包体大小上限由 limits 管"),
        ("V10", "签名由 check_signature.py 管"),
    ):
        rows.append(("SKIP", code, why))

    return rows, ok


def main(argv: List[str]) -> int:
    targets = argv[1:]
    if not targets:
        print("用法: python check_manifest_v3.py <包目录|manifest.json> [...]")
        return 2

    env_bad = False
    any_fail = False
    total = 0
    for t in targets:
        rows, ok = check(t)
        total += 1
        name = os.path.basename(os.path.normpath(t))
        print(f"\n=== {name} ===")
        for level, code, msg in rows:
            line = f"  [{level:4}] {code:16} {msg}"
            print(line.rstrip())
        if not ok:
            any_fail = True
        if any(r[1] == "E_ENV" for r in rows):
            env_bad = True

    print(f"\n检查 {total} 个包：{'FAIL' if any_fail else 'PASS'}")
    if env_bad:
        return 2
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
