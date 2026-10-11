"""插件 manifest 契约校验器 v3（纯标准库，零依赖）。

用法：
    python check_manifest.py <manifest.json>
    python check_manifest.py <pkg_dir>            # 自动找 <pkg_dir>/manifest.json

实现《插件开发规范 v3》§2 的 G1-G10 全部门（与后端 app/plugin/manifest_v2.py 对齐）：
  G1 schema_version/engine_api、G2 type_id 前缀白名单/元字段、G3 version semver、
  G4 kind/runtime/entrypoint、G5 端口声明必填+类型/widget/重名/媒体禁入、
  G6 transform op 白名单（递归）、G7 permission 五作用域、G8 dependencies/ui、
  G9 大插件上限（常量导出供 package_plugin.py 使用）、G10 signature 占位。
v1 兼容通道：schema_version 缺失/非 2 → 注入默认值并告警（仅内存，不写盘），与后端一致。

返回非零退出码 = 校验失败。
"""
from __future__ import annotations

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import json
import re
import sys
from typing import Any, Dict, List, Set, Tuple

# ---------------------------------------------------------------- 上限（大插件支持，与 worker/limits.py 对齐）
MAX_UPLOAD_MB = 2048
UPLOAD_MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ZIP_TOTAL_UNCOMPRESSED = 8 * 1024 * 1024 * 1024
ZIP_SINGLE_FILE = 4 * 1024 * 1024 * 1024
ZIP_MEMBER_COUNT = 20000
ZIP_RATIO = 100

TYPE_ID_RE = re.compile(r"^([a-z0-9]{2,16})/[a-z0-9][a-z0-9_.-]*$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
ABI_RE = re.compile(r"^[0-9]+\.[0-9]+$")
PORT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENTRYPOINT_RE = re.compile(r"^[A-Za-z0-9_./-]+\.py$")
CLASS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
UI_ENTRY_RE = re.compile(r"^ui/[A-Za-z0-9_./-]+\.(js|mjs|css)$")

CATEGORIES = {"输入", "语义", "分析", "音频", "剪辑", "输出", "控制", "用户节点"}
KINDS = {"transform", "python"}
RUNTIMES = {"transform", "python"}
WIDGETS = {"text", "number", "select", "file", "toggle", "password"}
CORE_PORT_TYPES = {
    "VIDEO", "IMAGE", "AUDIO", "SCRIPT", "SUBTITLE", "SEGMENTS",
    "TIMELINE", "JSON", "STRING", "INT", "FLOAT", "BOOL", "ANY",
}
MEDIA_TYPES = {"VIDEO", "IMAGE", "AUDIO"}
OPS = {"concat", "json_get", "math", "str", "upper", "lower", "len", "list"}
SRC_KEYS = {"$const", "$param", "$input", "$list"}
PERMISSION_SCOPES = {"fs", "network", "subprocess", "secret", "env"}
PERMISSION_KEYS = {
    "fs": {"read", "write"},
    "network": {"allow", "deny"},
    "subprocess": {"enabled", "argv_allowlist"},
    "secret": {"refs"},
    "env": {"vars"},
}
DEP_CHANNELS = {"pip", "bundle", "market"}
CORE_PREFIXES = {"core", "user"}
TOP_LEVEL_KEYS = {
    "schema_version", "engine_api", "min_engine_api", "max_engine_api", "type_id",
    "title", "category", "description", "version", "kind", "runtime",
    "entrypoint", "class_name", "inputs", "outputs", "params", "transform",
    "gpu_required", "gpu_recommended", "streaming", "permission",
    "dependencies", "ui", "signature",
}
PORT_KEYS = {
    "name", "type", "label", "required", "default", "widget", "options",
    "description", "min", "max", "step", "placeholder", "hint",
}


def _abi_tuple(v: Any):
    if not isinstance(v, str) or not ABI_RE.match(v):
        return None
    try:
        return tuple(int(x) for x in v.split("."))
    except (TypeError, ValueError):
        return None


def _normalize_v1_op(value: Any) -> Any:
    if isinstance(value, dict):
        if len(value) == 1:
            key, sub = next(iter(value.items()))
            if key == "$concat" and isinstance(sub, list):
                return {"op": "concat", "sources": [_normalize_v1_op(x) for x in sub]}
            if key in ("$upper", "$lower", "$len", "$get"):
                op = "json_get" if key == "$get" else key[1:]
                return {"op": op, "source": _normalize_v1_op(sub)}
        out = {}
        for k, v in value.items():
            out[k] = "json_get" if (k == "op" and v == "get") else _normalize_v1_op(v)
        return out
    if isinstance(value, list):
        return [_normalize_v1_op(x) for x in value]
    return value


def _compat_normalize(m: Dict[str, Any], warnings: List[str]) -> Tuple[Dict[str, Any], bool]:
    out = dict(m)
    sv = out.get("schema_version")
    compat = not (isinstance(sv, int) and sv == 2)
    if compat:
        warnings.append("v1 兼容通道：schema_version 缺失或非 2，已注入默认值（不写盘，新插件请写 v2）")
        out["schema_version"] = 2
        if not out.get("engine_api"):
            out["engine_api"] = "1.0"
            warnings.append("v1 兼容通道：已注入 engine_api=1.0")
        if not isinstance(out.get("permission"), dict):
            out["permission"] = {}
        if out.get("kind") not in KINDS:
            if str(out.get("runtime", "")).lower() == "python" or out.get("entrypoint") or out.get("class_name"):
                out["kind"] = "python"
            elif out.get("runtime", "transform") == "transform" or isinstance(out.get("transform"), dict):
                out["kind"] = "transform"
            warnings.append(f"v1 兼容通道：已按内容探测 kind={out.get('kind')!r}")
        if not out.get("title"):
            out["title"] = out.get("type_id", "未命名节点")
        if not out.get("category"):
            out["category"] = "用户节点"
        if "description" not in out or out.get("description") is None:
            out["description"] = ""
        if not out.get("version"):
            out["version"] = "1.0.0"
            warnings.append("v1 兼容通道：已注入 version=1.0.0（不写盘；新插件请显式声明）")
        if out.get("kind") == "python":
            for f in ("inputs", "outputs", "params"):
                if not isinstance(out.get(f), list):
                    out[f] = []
                    warnings.append(f"v1 兼容通道：python 包 {f} 缺失，已注入 []")
        if isinstance(out.get("transform"), dict):
            out["transform"] = _normalize_v1_op(out["transform"])
    return out, compat


def _errs(m: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    e: List[str] = []
    warnings: List[str] = []
    sv = m.get("schema_version")
    if sv is not None and not isinstance(sv, int):
        e.append(f"G1: schema_version 必须是整数: {sv!r}")
    elif isinstance(sv, int) and sv not in (1, 2):
        e.append(f"G1: 不支持的 schema_version: {sv!r}（当前仅支持 2；缺失= v1 兼容通道）")
    norm, compat = _compat_normalize(m, warnings)
    # 未知顶层字段：v2 严格模式拒绝；compat 警告
    for k in norm:
        if k not in TOP_LEVEL_KEYS:
            if compat:
                warnings.append(f"忽略未知字段 {k!r}（v1 兼容）")
            else:
                e.append(f"G2: 未知顶层字段 {k!r}（v2 严格模式 additionalProperties=false）")
    # G1 / engine_api
    ea = norm.get("engine_api")
    if ea is None or not ABI_RE.match(str(ea)):
        e.append('G1: engine_api 必填且须为 主.次 版本（如 "1.0"）')
    else:
        # G3：engine_api 主版本与平台兼容（对齐后端 manifest_v2._check_engine_api）
        plat = _abi_tuple("1.0")
        plug = _abi_tuple(ea)
        if plug and plat and plug[0] != plat[0]:
            e.append(f"G3: engine_api {ea!r} 与平台 '1.0' 主版本不兼容")
    # G2 / type_id 前缀白名单
    tid = norm.get("type_id", "")
    mt = TYPE_ID_RE.match(str(tid)) if isinstance(tid, str) else None
    if mt is None:
        e.append(f"G2: type_id 不合法: {tid!r}（须 ^([a-z0-9]{{2,16}})/[a-z0-9][a-z0-9_.-]*$）")
    else:
        prefix = mt.group(1)
        if prefix not in CORE_PREFIXES:
            e.append(f"G2: type_id 前缀 {prefix!r} 未登记（白名单: {sorted(CORE_PREFIXES)}；"
                     "第三方前缀需向平台登记，个人插件请用 user/）")
        if prefix == "core" and not isinstance(norm.get("signature"), dict):
            if compat:
                warnings.append("G2: core/ 官方包未签名——v1 兼容放行并警告（新包必须签名）")
            else:
                e.append("G2: core/ 前缀仅官方签名包可用（须提供 signature 占位）")
    # G2 元字段
    for f in ("title", "description"):
        v = norm.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            e.append(f"G2: 必填字段缺失或为空: {f}")
    if norm.get("category") not in CATEGORIES:
        e.append(f"G2: category 不在枚举内: {norm.get('category')!r}")
    # G3 version semver / engine_api 比较
    if "version" in norm and not SEMVER_RE.match(str(norm["version"])):
        e.append(f"G3: version 不是合法 semver: {norm['version']!r}")
    elif "version" not in norm and not compat:
        # D2 收紧：v2 严格模式（schema_version=2）version 必填；v1 兼容已由 _compat_normalize 注入 1.0.0
        e.append("G3: v2 manifest 必须显式声明 version（semver），缺失即拒")
    plat = _abi_tuple("1.0")
    for k in ("min_engine_api", "max_engine_api"):
        v = norm.get(k)
        if v is not None and not ABI_RE.match(str(v)):
            e.append(f"G3: {k} 不合法: {v!r}")
        elif v is not None:
            t = _abi_tuple(v)
            if k == "min_engine_api" and t and plat and t > plat:
                e.append(f"G3: 平台 '1.0' 低于 min_engine_api {v!r}")
            if k == "max_engine_api" and t and plat and t < plat:
                e.append(f"G3: 平台 '1.0' 高于 max_engine_api {v!r}")
    # G4 kind
    kind = norm.get("kind")
    if kind not in KINDS:
        e.append(f"G4: kind 必须是 transform|python，当前: {kind!r}")
    else:
        runtime = norm.get("runtime")
        if runtime is not None and runtime not in RUNTIMES:
            e.append(f"G4: runtime 必须是 transform|python，当前: {runtime!r}")
        if kind == "python" and runtime not in (None, "python"):
            e.append("G4: kind=python 时 runtime 必须为 python")
        if kind == "transform" and runtime == "python":
            e.append("G4: kind=transform 时 runtime 恒为 transform")
        if kind == "python":
            for f in ("entrypoint", "class_name"):
                if not norm.get(f):
                    e.append(f"G4: kind=python 必填字段缺失: {f}")
            ep = str(norm.get("entrypoint", ""))
            if ep and not ENTRYPOINT_RE.match(ep):
                e.append(f"G4: entrypoint 必须是包内相对 *.py: {ep!r}")
            if ep and (ep.startswith("/") or ep.startswith("\\") or ".." in ep.split("/")):
                e.append(f"G4: entrypoint 不得为绝对路径或含 .. : {ep!r}")
            cn = str(norm.get("class_name", ""))
            if cn and not CLASS_NAME_RE.match(cn):
                e.append(f"G4: class_name 不是合法标识符: {cn!r}")
    # G5 端口
    for f in ("inputs", "outputs", "params"):
        v = norm.get(f)
        if not isinstance(v, list):
            e.append(f"G5: 端口声明缺失——'{f}' 字段必须存在（可为空数组）（R1 端口声明必填）")
            continue
        names: Set[str] = set()
        for i, p in enumerate(v):
            if not isinstance(p, dict):
                e.append(f"G5: {f}[{i}] 必须是对象")
                continue
            for k in p:
                if k not in PORT_KEYS:
                    e.append(f"G5: {f}[{i}] 未知端口字段 {k!r}（v2 严格模式）")
            nm = p.get("name", "")
            if not PORT_NAME_RE.match(str(nm)):
                e.append(f"G5: {f}[{i}] name 不合法: {nm!r}")
            if nm in names:
                e.append(f"G5: {f} 内端口名重复: {nm!r}")
            names.add(nm)
            pt = str(p.get("type", "")).upper()
            if pt not in CORE_PORT_TYPES:
                e.append(f"G5: {f}[{i}] type 未注册（13 核心类型之一）: {p.get('type')!r}")
            w = p.get("widget", "text")
            if w not in WIDGETS:
                e.append(f"G5: {f}[{i}] widget 不合法: {w!r}")
            if w == "select" and not p.get("options"):
                e.append(f"G5: {f}[{i}] widget=select 必须提供非空 options")
            if w == "number":
                d = p.get("default")
                if d is not None and not isinstance(d, (int, float)):
                    e.append(f"G5: {f}[{i}] widget=number 时 default 必须是数字: {d!r}")
            if w in ("file", "toggle", "password") and f != "params":
                e.append(f"G5: {f}[{i}] widget={w} 仅 params 允许")
    if isinstance(norm.get("inputs"), list) and isinstance(norm.get("outputs"), list):
        inames = {p.get("name") for p in norm["inputs"] if isinstance(p, dict)}
        onames = {p.get("name") for p in norm["outputs"] if isinstance(p, dict)}
        dup = inames & onames
        if dup:
            e.append(f"G5: 端口名同时出现在 inputs 与 outputs: {sorted(dup)}（须改名或合规化）")
    # G6 transform
    if kind == "transform":
        t = norm.get("transform")
        if not isinstance(t, dict) or not t:
            e.append("G6: kind=transform 必须提供非空 transform 映射")
        onames = {p.get("name") for p in norm.get("outputs") or [] if isinstance(p, dict)}
        for k, v in (t or {}).items():
            if k not in onames:
                e.append(f"G6: transform 键 {k!r} 不在 outputs 端口名中")
            _check_op("transform." + k, v, e)
        for f in ("inputs", "outputs", "params"):
            for p in (norm.get(f) or []):
                if isinstance(p, dict) and str(p.get("type", "")).upper() in MEDIA_TYPES:
                    e.append(f"G6: transform 端口 {p.get('name')!r} 类型 {p['type']} 是媒体类型，禁止")
    # G7 permission
    perm = norm.get("permission")
    if perm is not None:
        if not isinstance(perm, dict):
            e.append("G7: permission 必须是对象")
        else:
            for scope in perm:
                if scope not in PERMISSION_SCOPES:
                    e.append(f"G7: 未知 permission 作用域: {scope!r}（五作用域: {sorted(PERMISSION_SCOPES)}）")
                    continue
                val = perm[scope]
                if not isinstance(val, dict):
                    e.append(f"G7: permission.{scope} 必须是对象")
                    continue
                for k in val:
                    if k not in PERMISSION_KEYS[scope]:
                        e.append(f"G7: permission.{scope} 未知键: {k!r}")
                for k, arr in val.items():
                    if k == "enabled":
                        if not isinstance(arr, bool):
                            e.append(f"G7: permission.subprocess.enabled 必须是布尔: {arr!r}")
                    elif not isinstance(arr, list) or not all(isinstance(x, str) for x in arr):
                        e.append(f"G7: permission.{scope}.{k} 必须是字符串数组: {arr!r}")
    # G8 dependencies / ui
    deps = norm.get("dependencies")
    if deps is not None:
        if not isinstance(deps, list):
            e.append("G8: dependencies 必须是数组")
        else:
            for i, d in enumerate(deps):
                if not isinstance(d, dict):
                    e.append(f"G8: dependencies[{i}] 必须是对象")
                    continue
                for k in d:
                    if k not in ("id", "version", "channel", "optional"):
                        e.append(f"G8: dependencies[{i}] 未知字段 {k!r}")
                if not isinstance(d.get("id", ""), str) or not d.get("id", "").strip():
                    e.append(f"G8: dependencies[{i}] id 必填且非空")
                if not isinstance(d.get("version"), str) or not d.get("version", "").strip():
                    e.append(f"G8: dependencies[{i}] version 必填")
                if d.get("channel") not in DEP_CHANNELS:
                    e.append(f"G8: dependencies[{i}] channel 必须是 pip|bundle|market: {d.get('channel')!r}")
                opt = d.get("optional")
                if opt is not None and not isinstance(opt, bool):
                    e.append(f"G8: dependencies[{i}] optional 必须是布尔")
    ui = norm.get("ui")
    if ui is not None:
        if not isinstance(ui, dict):
            e.append("G8: ui 必须是对象")
        else:
            for k in ui:
                if k not in ("entry", "sandbox", "capabilities"):
                    e.append(f"G8: ui 未知字段 {k!r}（entry/sandbox/capabilities）")
            entry = ui.get("entry")
            if not isinstance(entry, str) or not UI_ENTRY_RE.match(entry):
                e.append(f"G8: ui.entry 不合法: {entry!r}（须 ui/*.js|mjs|css，包内相对路径）")
            else:
                parts = entry.replace("\\", "/").split("/")
                if any(p in ("..", "") for p in parts[:-1]) or entry.startswith("/"):
                    e.append(f"G8: ui.entry 不得逃逸 ui/ 目录: {entry!r}")
            sandbox = ui.get("sandbox")
            if sandbox is not None and not isinstance(sandbox, dict):
                e.append("G8: ui.sandbox 必须是对象")
            caps = ui.get("capabilities")
            if caps is not None and (not isinstance(caps, list) or not all(isinstance(x, str) for x in caps)):
                e.append("G8: ui.capabilities 必须是字符串数组")
    # G10 signature 占位
    sig = norm.get("signature")
    if sig is not None:
        if not isinstance(sig, dict):
            e.append("G10: signature 必须是对象")
        else:
            for k in sig:
                if k not in ("alg", "key_id", "value", "over"):
                    e.append(f"G10: signature 未知字段 {k!r}")
            alg = sig.get("alg")
            if alg is not None and (not isinstance(alg, str) or not re.match(r"^[A-Za-z0-9_-]+$", alg)):
                e.append(f"G10: signature.alg 不合法: {alg!r}")
            for k in ("key_id", "value", "over"):
                v = sig.get(k)
                if not isinstance(v, str) or not v.strip():
                    e.append(f"G10: signature.{k} 必填且非空")
    else:
        warnings.append("G10: 未签名——社区级安装（L2），官方商城上架需签名")
    return e, warnings


def _check_op(path: str, v: Any, e: List[str]) -> None:
    if isinstance(v, dict):
        if "op" in v:
            op = v.get("op")
            if op not in OPS:
                e.append(f"G6: {path} 未知 op: {op!r}（白名单: {sorted(OPS)}）")
            for sub in (v.get("sources") or []):
                _check_op(path + ".sources[]", sub, e)
            if "source" in v:
                _check_op(path + ".source", v["source"], e)
            for sv in (v.get("values") or {}).values():
                _check_op(path + ".values", sv, e)
            for key in ("a", "b"):
                if key in v:
                    _check_op(path + "." + key, v[key], e)
        else:
            keys = [k for k in v.keys() if k.startswith("$")]
            if keys and not set(keys) <= SRC_KEYS:
                e.append(f"G6: {path} 取值源含未知 $key: {keys}")
            for k in v:
                _check_op(path + "." + k, v[k], e)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _check_op(f"{path}[{i}]", x, e)


def check_manifest(m: Dict[str, Any]) -> List[str]:
    """返回错误清单（空 = 通过；警告见 check_manifest_verbose）。"""
    errs, _warnings = _errs(m)
    return errs


def check_manifest_verbose(m: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    return _errs(m)


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("用法: python check_manifest.py <manifest.json | pkg_dir>")
        return 2
    target = argv[1]
    p = target if target.endswith(".json") else target.rstrip("/\\") + "/manifest.json"
    try:
        with open(p, "r", encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, json.JSONDecodeError) as ex:
        print(f"[check_manifest] 无法读取 {p}: {ex}")
        return 1
    if not isinstance(m, dict):
        print("[check_manifest] manifest 根节点必须是对象")
        return 1
    errs, warnings = check_manifest_verbose(m)
    for w in warnings:
        print(f"  [warn] {w}")
    if not errs:
        print(f"[check_manifest] OK: {m.get('type_id', '?')} v{m.get('version', '?')} "
              f"(kind={m.get('kind')}, schema_version={m.get('schema_version')})"
              + (" [v1 兼容]" if m.get("schema_version") != 2 else ""))
        return 0
    print(f"[check_manifest] FAILED: {len(errs)} 个问题")
    for i, e in enumerate(errs, 1):
        print(f"  {i}. {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
