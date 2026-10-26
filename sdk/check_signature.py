#!/usr/bin/env python3
"""官方锚验签门（纯标准库、只读公钥、绝不加载私钥）。

为什么需要它：`check_all.py` 的 `signed=True` 只判断「signature 是个对象」，
不验签 —— 4 个 `user_*` 包从 2026-10-18 起签名就失效，而门一直是绿的。
本脚本把「官方锚 + ed25519 逐包验签」做成节点仓自己的常驻门禁。

验签对象与口径（与平台仓 `backend/app/signatures.py` 完全一致，逐字节可对拍）：
  - manifest（`over=manifest`）：签名体 = manifest 剥掉 `signature` 键后按 JCS（RFC 8785 实用子集）规范化的 UTF-8 字节；
  - 归档 zip（`over=archive`，`--dist`）：签名体 = **zip 整体字节的 sha256 摘要**（`signatures.py:330 sign_bytes(sha256_bytes(zip))`）——
    注意不是对裸 zip 字节签名；按裸字节验会 75/75 全红（实测，见 `docs/仓库收尾报告_20261026.md` §2）；
  - 两者共用 signature = {alg:"ed25519", key_id, value(base64 64B), over:...}；
  - key_id 必须等于锚的 key_id（缺省 `a8998646b062c170`，由锚公钥 sha256 前 16 hex 派生）。

ed25519 用 RFC 8032 §5.1.7 的**纯 Python** 实现（只依赖 hashlib）：
节点仓的 SDK 必须在没有平台 venv / 没有 cryptography 的机器上也能跑，
所以不 import 平台模块、不 import 第三方库。正确性由「与平台 cryptography 逐包对拍」保证。

公钥锚只读平台仓 `FrameWeave/backend/app/fw_official_pubkey.json`（双仓契约：私钥与锚
都归平台仓，节点仓只读）。找不到锚 → exit 2（环境缺失 ≠ 通过）。

用法：
    python sdk/check_signature.py                    # 校验全部包的 manifest
    python sdk/check_signature.py --pkg core_asr     # 单包
    python sdk/check_signature.py --dist             # 包 manifest + dist/*.zip 归档签名
    python sdk/check_signature.py --only-dist        # 只验 dist/*.zip 归档签名（发版前/商城产物）
    python sdk/check_signature.py --json             # 机器可读
    python sdk/check_signature.py --anchor PATH      # 指定锚文件
    python sdk/check_signature.py --no-allowlist     # 严格模式：忽略允许清单
    python sdk/check_signature.py --packages DIR     # 指定包目录（CI 复用）
    python sdk/check_signature.py --dist-dir DIR     # 指定 dist 目录（默认仓内 dist/）

退出码：0 = 全部 SIG_OK / ARCH_SIG_OK，或失败全部落在允许清单内；
        1 = 存在未登记失败（含 NO_SIG）；2 = 环境缺失。
"""
from __future__ import annotations

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import argparse
import base64
import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                      # frameweave-plugins/
WORKSPACE = os.path.dirname(REPO)                 # 工作区根
DEFAULT_PACKAGES = os.path.join(REPO, "packages")
DEFAULT_DIST = os.path.join(REPO, "dist")
DEFAULT_ANCHOR = os.path.join(WORKSPACE, "FrameWeave", "backend", "app", "fw_official_pubkey.json")
DEFAULT_ALLOWLIST = os.path.join(HERE, "signature_allowlist.json")
EXPECTED_KEY_ID = "a8998646b062c170"
ANCHOR_ENV = "FW_OFFICIAL_PUBKEY"

# ------------------------------------------------------------------ ed25519（RFC 8032 §5.1.7）
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int):
    """由 y 与 x 的奇偶位恢复 x；无解返回 None。"""
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - xx) % _P != 0:
        return None
    if x % 2 != sign:
        x = _P - x
    return x


def _point_add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * 2 * _D * t2 % _P
    dd = z1 * 2 * z2 % _P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(s: int, P):
    Q = (0, 1, 1, 0)  # 单位元
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _point_equal(P, Q) -> bool:
    x1, y1, z1, _ = P
    x2, y2, z2, _ = Q
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _decompress(raw: bytes):
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _recover_x(_BY, 0)
_BASE = (_BX, _BY, 1, _BX * _BY % _P)
del _BX, _BY


def ed25519_verify(public_key: bytes, msg: bytes, sig: bytes) -> bool:
    """RFC 8032 §5.1.7 验签（cofactorless）：[S]B == R + [k]A。任何格式问题返回 False。"""
    if len(public_key) != 32 or len(sig) != 64:
        return False
    A = _decompress(public_key)
    if A is None:
        return False
    R_bytes = sig[:32]
    R = _decompress(R_bytes)
    if R is None:
        return False
    S = int.from_bytes(sig[32:], "little")
    if S >= _L:
        return False
    k = int.from_bytes(hashlib.sha512(R_bytes + public_key + msg).digest(), "little") % _L
    return _point_equal(_point_mul(S, _BASE), _point_add(R, _point_mul(k, A)))


# ------------------------------------------------------------------ JCS 规范化（镜像平台实现）
def _esc(s: str) -> str:
    """RFC 8785 键转义：\\u0000-\\u001F（引号转义由调用方负责，manifest 键均为普通标识符）。"""
    return "".join("\\u%04x" % ord(ch) if 0x00 <= ord(ch) <= 0x1F else ch for ch in s)


def _stable_json(v):
    """数字稳定序列化：int 直接；浮点按平台同一规则（与签名侧共用口径）。"""
    if isinstance(v, int):
        return str(v)
    if v != v or v in (float("inf"), float("-inf")):
        return "null"
    if v == int(v) and abs(v) < 1e16:
        return str(int(v))
    return repr(v)


def jcs_canonicalize(obj) -> str:
    """JCS 风格规范化（RFC 8785 实用子集）：对象键升序、控制字符转义、字符串不转义非 ASCII。"""
    if isinstance(obj, dict):
        return "{" + ",".join(
            '"%s":%s' % (_esc(str(k)), jcs_canonicalize(v))
            for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(jcs_canonicalize(x) for x in obj) + "]"
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, (int, float)):
        return _stable_json(obj)
    raise TypeError(f"无法规范化的 JSON 值: {type(obj)!r}")


def manifest_canonical_bytes(manifest: dict) -> bytes:
    """剥掉 signature 后的规范化字节（签名体）。"""
    m = dict(manifest)
    m.pop("signature", None)
    return jcs_canonicalize(m).encode("utf-8")


# ------------------------------------------------------------------ 锚 / 允许清单
def load_anchor(path: str):
    """返回 (pubkey_bytes, key_id, anchor_dict)；文件缺失/格式错 → 抛 RuntimeError（调用方转 exit 2）。"""
    if not os.path.isfile(path):
        raise RuntimeError(f"官方公钥锚不存在: {path}（节点仓只读平台仓的锚；缺锚 = 环境缺失，不是通过）")
    try:
        anchor = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as ex:
        raise RuntimeError(f"官方公钥锚无法解析: {path}（{ex}）")
    b64 = str(anchor.get("public_key") or "")
    if anchor.get("alg") != "ed25519":
        raise RuntimeError(f"锚算法不是 ed25519: {anchor.get('alg')!r}")
    try:
        raw = base64.b64decode(b64.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as ex:
        raise RuntimeError(f"锚公钥 base64 非法: {b64!r}（{ex}）")
    if len(raw) != 32:
        raise RuntimeError(f"Ed25519 公钥必须 32 字节，锚里是 {len(raw)}")
    derived = hashlib.sha256(raw).hexdigest()[:16]
    declared = str(anchor.get("key_id") or "")
    if declared and declared != derived:
        raise RuntimeError(f"锚自相矛盾：key_id={declared} 但公钥派生 key_id={derived}")
    return raw, derived, anchor


def load_allowlist(path: str):
    """返回 {pkg: entry}。文件缺失 → 空清单（不豁免任何包）。"""
    if not os.path.isfile(path):
        return {}, f"允许清单不存在（按空清单处理，所有失败都必须修）: {path}"
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as ex:
        return {}, f"允许清单无法解析（按空清单处理）: {path}（{ex}）"
    entries = {}
    for e in (data.get("entries") or []):
        pkg = str(e.get("pkg") or "")
        if not pkg:
            continue
        entries[pkg] = e
    return entries, ""


def resolve_anchor_path(cli_value: str) -> str:
    if cli_value:
        return os.path.abspath(cli_value)
    env = (os.environ.get(ANCHOR_ENV) or "").strip()
    if env:
        return os.path.abspath(env)
    return DEFAULT_ANCHOR


# ------------------------------------------------------------------ 单包校验
def check_pkg(pkg_dir: str, pub: bytes, anchor_key_id: str) -> dict:
    """返回 {pkg, status, reason, manifest_sha256, over, key_id}。status ∈ SIG_OK/SIG_FAIL/NO_SIG。"""
    name = os.path.basename(pkg_dir.rstrip("\\/"))
    mpath = os.path.join(pkg_dir, "manifest.json")
    out = {"pkg": name, "status": "SIG_FAIL", "reason": "", "manifest_sha256": None,
           "over": None, "key_id": None}
    if not os.path.isfile(mpath):
        out["status"] = "NO_SIG"
        out["reason"] = "缺 manifest.json"
        return out
    raw = open(mpath, "rb").read()
    out["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as ex:
        out["status"] = "NO_SIG"
        out["reason"] = f"manifest.json 无法解析: {ex}"
        return out
    sig = manifest.get("signature")
    if not isinstance(sig, dict):
        out["status"] = "NO_SIG"
        out["reason"] = "manifest 无 signature 字段"
        return out

    out["over"] = sig.get("over")
    out["key_id"] = sig.get("key_id")

    if sig.get("alg") != "ed25519":
        out["reason"] = f"alg={sig.get('alg')!r}（只支持 ed25519）"
        return out
    if sig.get("over") != "manifest":
        # 本仓 manifest 只应 over=manifest；over=archive 是 zip 本体的签名，另属 dist 验签范围
        out["reason"] = f"over={sig.get('over')!r}（manifest 只认 over=manifest）"
        return out
    kid = str(sig.get("key_id") or "")
    if kid != anchor_key_id:
        out["reason"] = f"key_id={kid!r} 非官方锚 {anchor_key_id}"
        return out
    value = str(sig.get("value") or "")
    if not value or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for ch in value):
        out["reason"] = "signature.value 非 base64 形态"
        return out
    try:
        sig_bytes = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        out["reason"] = "signature.value base64 解码失败"
        return out
    if len(sig_bytes) != 64:
        out["reason"] = f"signature.value 解码后 {len(sig_bytes)} 字节（ed25519 应为 64）"
        return out

    body = manifest_canonical_bytes(manifest)
    if not ed25519_verify(pub, body, sig_bytes):
        out["reason"] = "ed25519 验签不通过（canonical 字节与签名不匹配 ⇒ 签名后被改过）"
        return out
    out["status"] = "SIG_OK"
    return out


def _sha256_file_digest(path: str, block: int = 1 << 20) -> bytes:
    """zip 整体字节的 sha256 摘要（流式，不把大 zip 读进内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.digest()


def check_archive(zip_path: str, pub: bytes, anchor_key_id: str) -> dict:
    """验 `dist/<pkg>.zip` + `<pkg>.zip.sig` 的 archive 签名。

    口径与平台 `signatures.py:324-357` 一致：value = sign(sha256(zip 整体字节))，
    signature = {alg:"ed25519", key_id, value, over:"archive"}，`.sig` 为独立 JSON 文件。
    返回 {pkg, status, reason, zip_sha256, zip_bytes}；status ∈ ARCH_SIG_OK/ARCH_SIG_FAIL/NO_SIG。
    """
    name = os.path.basename(zip_path)[:-len(".zip")] if zip_path.endswith(".zip") else os.path.basename(zip_path)
    out = {"pkg": name, "zip": os.path.basename(zip_path), "status": "ARCH_SIG_FAIL",
           "reason": "", "zip_sha256": None, "zip_bytes": None}
    if not os.path.isfile(zip_path):
        out["status"] = "NO_SIG"
        out["reason"] = "zip 不存在"
        return out
    out["zip_bytes"] = os.path.getsize(zip_path)
    digest = _sha256_file_digest(zip_path)
    out["zip_sha256"] = digest.hex()

    sig_path = zip_path + ".sig"
    if not os.path.isfile(sig_path):
        out["status"] = "NO_SIG"
        out["reason"] = f"缺 {os.path.basename(sig_path)}（未签 archive）"
        return out
    try:
        sig = json.load(open(sig_path, encoding="utf-8"))
    except (OSError, ValueError) as ex:
        out["reason"] = f"{os.path.basename(sig_path)} 无法解析: {ex}"
        return out
    if not isinstance(sig, dict):
        out["reason"] = f"{os.path.basename(sig_path)} 不是 JSON 对象"
        return out
    if sig.get("alg") != "ed25519":
        out["reason"] = f"alg={sig.get('alg')!r}（只支持 ed25519）"
        return out
    if sig.get("over") != "archive":
        out["reason"] = f"over={sig.get('over')!r}（archive 签名只认 over=archive）"
        return out
    kid = str(sig.get("key_id") or "")
    if kid != anchor_key_id:
        out["reason"] = f"key_id={kid!r} 非官方锚 {anchor_key_id}"
        return out
    value = str(sig.get("value") or "")
    if not value or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for ch in value):
        out["reason"] = "signature.value 非 base64 形态"
        return out
    try:
        sig_bytes = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        out["reason"] = "signature.value base64 解码失败"
        return out
    if len(sig_bytes) != 64:
        out["reason"] = f"signature.value 解码后 {len(sig_bytes)} 字节（ed25519 应为 64）"
        return out
    if not ed25519_verify(pub, digest, sig_bytes):
        out["reason"] = "ed25519 验签不通过（zip 摘要与签名不匹配 ⇒ zip 被改过或 .sig 与 zip 不同批）"
        return out
    out["status"] = "ARCH_SIG_OK"
    return out


def _waive_hit(entry: dict, kind: str, sha_value: str) -> bool:
    """允许清单条目是否可豁免该失败。

    kind ∈ {"manifest","archive"}：
      - manifest 失败只接受 scope=manifest（缺省）且 manifest_sha256 逐字节相同的条目；
      - archive 失败只接受 scope=archive 且 zip_sha256 相同的条目；
      - NO_SIG 一律不豁免（调用方保证不把 NO_SIG 传进来）。
    """
    if not isinstance(entry, dict):
        return False
    scope = str(entry.get("scope") or "manifest")
    if kind == "manifest":
        return scope == "manifest" and entry.get("manifest_sha256") == sha_value
    return scope == "archive" and entry.get("zip_sha256") == sha_value


def main() -> int:
    ap = argparse.ArgumentParser(description="官方锚验签门（纯标准库、只读公钥）")
    ap.add_argument("--packages", default=DEFAULT_PACKAGES, help="包目录（默认仓内 packages/）")
    ap.add_argument("--pkg", action="append", default=[], help="只校验指定包（可重复）")
    ap.add_argument("--anchor", default="", help="官方公钥锚路径（默认平台仓 fw_official_pubkey.json）")
    ap.add_argument("--allowlist", default=DEFAULT_ALLOWLIST, help="已知例外清单路径")
    ap.add_argument("--no-allowlist", action="store_true", help="严格模式：忽略允许清单")
    ap.add_argument("--dist", action="store_true", help="同时验 dist/*.zip 的 archive 签名（over=archive）")
    ap.add_argument("--only-dist", action="store_true", help="只验 dist/*.zip 归档签名（跳过包 manifest）")
    ap.add_argument("--dist-dir", default=DEFAULT_DIST, help="dist 目录（默认仓内 dist/）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    do_manifest = not args.only_dist
    do_dist = args.dist or args.only_dist

    if args.only_dist and args.pkg:
        print("[sig] --pkg 只适用于包 manifest，与 --only-dist 互斥")
        return 2

    anchor_path = resolve_anchor_path(args.anchor)
    try:
        pub, anchor_key_id, anchor = load_anchor(anchor_path)
    except RuntimeError as ex:
        print(f"[sig] 环境缺失：{ex}")
        return 2

    allow = {}
    if not args.no_allowlist:
        allow, allow_note = load_allowlist(args.allowlist)
        if allow_note:
            print(f"[sig] 提示：{allow_note}")
    else:
        print("[sig] 严格模式：忽略允许清单（任何失败都算未登记）")

    # ---------------------------------------------------------------- manifest 侧
    results, ok, bad = [], [], []
    packages_dir = None
    if do_manifest:
        packages_dir = os.path.abspath(args.packages)
        if not os.path.isdir(packages_dir):
            print(f"[sig] 环境缺失：包目录不存在: {packages_dir}")
            return 2
        if args.pkg:
            targets = [os.path.join(packages_dir, p) for p in args.pkg]
            missing = [p for p in args.pkg if not os.path.isdir(os.path.join(packages_dir, p))]
            if missing:
                print(f"[sig] 环境缺失：指定的包不存在: {', '.join(missing)}")
                return 2
        else:
            targets = [os.path.join(packages_dir, d) for d in sorted(os.listdir(packages_dir))
                       if os.path.isfile(os.path.join(packages_dir, d, "manifest.json"))]
            if not targets:
                print(f"[sig] 环境缺失：{packages_dir} 下没有包（缺 manifest.json）")
                return 2
        results = [check_pkg(p, pub, anchor_key_id) for p in targets]
        ok = [r for r in results if r["status"] == "SIG_OK"]
        bad = [r for r in results if r["status"] != "SIG_OK"]

    # ---------------------------------------------------------------- dist 侧
    arch_results, arch_ok, arch_bad = [], [], []
    dist_dir = None
    if do_dist:
        dist_dir = os.path.abspath(args.dist_dir)
        if not os.path.isdir(dist_dir):
            print(f"[sig] 环境缺失：dist 目录不存在: {dist_dir}")
            return 2
        zips = sorted(f for f in os.listdir(dist_dir) if f.endswith(".zip"))
        if not zips:
            print(f"[sig] 环境缺失：{dist_dir} 下没有 .zip（未打包过不能当通过）")
            return 2
        arch_results = [check_archive(os.path.join(dist_dir, z), pub, anchor_key_id) for z in zips]
        arch_ok = [r for r in arch_results if r["status"] == "ARCH_SIG_OK"]
        arch_bad = [r for r in arch_results if r["status"] != "ARCH_SIG_OK"]

    # ---------------------------------------------------------------- 允许清单归属
    waived, unregistered, stale = [], [], []   # 元素: (kind, result, entry)
    for r in bad:
        entry = allow.get(r["pkg"])
        if r["status"] == "SIG_FAIL" and _waive_hit(entry, "manifest", r["manifest_sha256"]):
            waived.append(("manifest", r, entry))
        else:
            unregistered.append(("manifest", r, entry or {}))
    for r in arch_bad:
        entry = allow.get(r["pkg"])
        if r["status"] == "ARCH_SIG_FAIL" and _waive_hit(entry, "archive", r["zip_sha256"]):
            waived.append(("archive", r, entry))
        else:
            unregistered.append(("archive", r, entry or {}))

    # 「允许清单过期」只在全量扫描时有意义（--pkg / 单侧模式下未参与校验的条目不该被判过期）
    fail_pkgs = {r["pkg"] for r in bad} | {r["pkg"] for r in arch_bad}
    for pkg, entry in allow.items():
        scope = str(entry.get("scope") or "manifest")
        if scope == "archive" and not do_dist:
            continue
        if scope == "manifest" and not do_manifest:
            continue
        if scope == "manifest" and args.pkg:
            continue
        if pkg not in fail_pkgs:
            stale.append((pkg, entry))

    # ---------------------------------------------------------------- 输出
    unregistered_names = [f"{kind}:{r['pkg']}" for kind, r, _e in unregistered]
    if args.json:
        print(json.dumps({
            "anchor": {"path": anchor_path, "key_id": anchor_key_id,
                       "declared_key_id": anchor.get("key_id")},
            "mode": {"manifest": do_manifest, "dist": do_dist},
            "packages_dir": packages_dir,
            "dist_dir": dist_dir,
            "allowlist": None if args.no_allowlist else args.allowlist,
            "manifest": {
                "checked": len(results),
                "sig_ok": [r["pkg"] for r in ok],
                "failed": [{"pkg": r["pkg"], "status": r["status"], "reason": r["reason"],
                            "manifest_sha256": r["manifest_sha256"],
                            "waived": any(w[1] is r for w in waived)} for r in bad],
            },
            "archive": {
                "checked": len(arch_results),
                "arch_sig_ok": [r["pkg"] for r in arch_ok],
                "failed": [{"pkg": r["pkg"], "status": r["status"], "reason": r["reason"],
                            "zip_sha256": r["zip_sha256"], "zip_bytes": r["zip_bytes"],
                            "waived": any(w[1] is r for w in waived)} for r in arch_bad],
            },
            "allowlist_hits": [{"scope": kind, "pkg": r["pkg"], "since": e.get("since"),
                                "reason": e.get("reason")} for kind, r, e in waived],
            "unregistered_failures": unregistered_names,
            "allowlist_stale": [{"pkg": p, "since": e.get("since")} for p, e in stale],
            "exit_code": 0 if not unregistered else 1,
        }, ensure_ascii=False, indent=2))
        return 0 if not unregistered else 1

    # 人读输出：先逐条失败（WAIVED/FAIL 分开），再分侧汇总，最后总账
    for kind, r, e in waived:
        label = "SIG_FAIL" if kind == "manifest" else "ARCH_SIG_FAIL"
        print(f"  WAIVED {r['pkg']:<28} {label}({r['reason']})")
    for kind, r, e in unregistered:
        print(f"  FAIL   {r['pkg']:<28} {r['status']}({r['reason']})")

    print(f"[sig] 锚: {anchor_path}")
    print(f"[sig]       key_id={anchor_key_id}（= 公钥 sha256 前 16 hex，自校验通过）")
    if do_manifest:
        print(f"[sig] manifest：校验 {len(results)} 个包 → {len(ok)} SIG_OK / {len(bad)} 失败")
    if do_dist:
        print(f"[sig][dist] archive：校验 {len(arch_results)} 个 zip → "
              f"{len(arch_ok)} ARCH_SIG_OK / {len(arch_bad)} 失败")
    print(f"[sig] 允许清单命中 {len(waived)} 条（均为已知未闭合项，不阻断合并）")
    for kind, r, e in waived:
        print(f"        - [{kind}] {r['pkg']}  since={e.get('since')}  原因={e.get('reason')}")
    print(f"[sig] 未登记失败 {len(unregistered)} 条" + ("" if not unregistered else "："))
    for kind, r, entry in unregistered:
        if entry and r["status"] in ("NO_SIG",):
            why = "（清单里有该包，但丢签名不等于豁免）"
        elif entry:
            why = "（清单里有该包，但内容/归档已变 ⇒ 不豁免）"
        else:
            why = ""
        print(f"        - [{kind}] {r['pkg']}  {r['status']}({r['reason']}){why}")
    if stale:
        print(f"[sig] 允许清单过期 {len(stale)} 条（已恢复有效签名，条目应经用户裁定后删除）：")
        for p, e in stale:
            print(f"        - {p}  since={e.get('since')}")

    return 0 if not unregistered else 1


if __name__ == "__main__":
    sys.exit(main())
