#!/usr/bin/env python3
"""官方锚验签门（纯标准库、只读公钥、绝不加载私钥）。

为什么需要它：`check_all.py` 的 `signed=True` 只判断「signature 是个对象」，
不验签 —— 4 个 `user_*` 包从 2026-10-18 起签名就失效，而门一直是绿的。
本脚本把「官方锚 + ed25519 逐包验签」做成节点仓自己的常驻门禁。

验签对象与口径（与平台仓 `backend/app/signatures.py` 完全一致，逐字节可对拍）：
  - 签名体 = manifest 剥掉 `signature` 键后按 JCS（RFC 8785 实用子集）规范化的 UTF-8 字节；
  - signature = {alg:"ed25519", key_id, value(base64 64B), over:"manifest"}；
  - key_id 必须等于锚的 key_id（缺省 `a8998646b062c170`，由锚公钥 sha256 前 16 hex 派生）。

ed25519 用 RFC 8032 §5.1.7 的**纯 Python** 实现（只依赖 hashlib）：
节点仓的 SDK 必须在没有平台 venv / 没有 cryptography 的机器上也能跑，
所以不 import 平台模块、不 import 第三方库。正确性由「与平台 cryptography 逐包对拍」保证。

公钥锚只读平台仓 `FrameWeave/backend/app/fw_official_pubkey.json`（双仓契约：私钥与锚
都归平台仓，节点仓只读）。找不到锚 → exit 2（环境缺失 ≠ 通过）。

用法：
    python sdk/check_signature.py                    # 校验全部包
    python sdk/check_signature.py --pkg core_asr     # 单包
    python sdk/check_signature.py --json             # 机器可读
    python sdk/check_signature.py --anchor PATH      # 指定锚文件
    python sdk/check_signature.py --no-allowlist     # 严格模式：忽略允许清单
    python sdk/check_signature.py --packages DIR     # 指定包目录（CI 复用）

退出码：0 = 全部 SIG_OK 或失败全部落在允许清单内；1 = 存在未登记失败（含 NO_SIG）；2 = 环境缺失。
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


def main() -> int:
    ap = argparse.ArgumentParser(description="官方锚验签门（纯标准库、只读公钥）")
    ap.add_argument("--packages", default=DEFAULT_PACKAGES, help="包目录（默认仓内 packages/）")
    ap.add_argument("--pkg", action="append", default=[], help="只校验指定包（可重复）")
    ap.add_argument("--anchor", default="", help="官方公钥锚路径（默认平台仓 fw_official_pubkey.json）")
    ap.add_argument("--allowlist", default=DEFAULT_ALLOWLIST, help="已知例外清单路径")
    ap.add_argument("--no-allowlist", action="store_true", help="严格模式：忽略允许清单")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    anchor_path = resolve_anchor_path(args.anchor)
    try:
        pub, anchor_key_id, anchor = load_anchor(anchor_path)
    except RuntimeError as ex:
        print(f"[sig] 环境缺失：{ex}")
        return 2

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

    allow, allow_note = load_allowlist("" if args.no_allowlist else args.allowlist)
    if allow_note:
        print(f"[sig] 提示：{allow_note}")

    results = [check_pkg(p, pub, anchor_key_id) for p in targets]

    ok = [r for r in results if r["status"] == "SIG_OK"]
    bad = [r for r in results if r["status"] != "SIG_OK"]
    waived, unregistered, stale = [], [], []
    for r in bad:
        entry = allow.get(r["pkg"])
        # 清单只豁免「签名校验不过且内容与登记时逐字节一致」的 SIG_FAIL；
        # NO_SIG（丢签名）永不豁免 —— 丢签名不等于豁免。
        if (entry and r["status"] == "SIG_FAIL"
                and entry.get("manifest_sha256") == r["manifest_sha256"]):
            waived.append((r, entry))
        else:
            unregistered.append((r, entry))
    failure_map = {r["pkg"] for r in bad}
    # 「允许清单过期」只在全量扫描时有意义：--pkg 单包模式下未参与校验的包不该被判过期
    if not args.pkg:
        for pkg, entry in allow.items():
            if pkg not in failure_map:
                stale.append((pkg, entry))

    if args.json:
        print(json.dumps({
            "anchor": {"path": anchor_path, "key_id": anchor_key_id,
                       "declared_key_id": anchor.get("key_id")},
            "packages_dir": packages_dir,
            "allowlist": None if args.no_allowlist else args.allowlist,
            "checked": len(results),
            "sig_ok": [r["pkg"] for r in ok],
            "failed": [{"pkg": r["pkg"], "status": r["status"], "reason": r["reason"],
                        "manifest_sha256": r["manifest_sha256"],
                        "waived": any(w[0]["pkg"] == r["pkg"] for w in waived)} for r in bad],
            "allowlist_hits": [{"pkg": r["pkg"], "since": e.get("since"), "reason": e.get("reason")}
                               for r, e in waived],
            "unregistered_failures": [r["pkg"] for r, _e in unregistered],
            "allowlist_stale": [{"pkg": p, "since": e.get("since")} for p, e in stale],
            "exit_code": 0 if not unregistered else 1,
        }, ensure_ascii=False, indent=2))
    else:
        for r in bad:
            mark = "WAIVED" if any(w[0]["pkg"] == r["pkg"] for w in waived) else "FAIL  "
            print(f"  {mark} {r['pkg']:<28} {r['status']}({r['reason']})")
        print(f"[sig] 锚: {anchor_path}")
        print(f"[sig]       key_id={anchor_key_id}（= 公钥 sha256 前 16 hex，自校验通过）")
        print(f"[sig] 校验 {len(results)} 个包：{len(ok)} SIG_OK / {len(bad)} 失败")
        print(f"[sig] 允许清单命中 {len(waived)} 条（均为已知未闭合项，不阻断合并）")
        for r, e in waived:
            print(f"        - {r['pkg']}  since={e.get('since')}  原因={e.get('reason')}")
        print(f"[sig] 未登记失败 {len(unregistered)} 条" + ("" if not unregistered else "："))
        for r, entry in unregistered:
            if entry and r["status"] == "NO_SIG":
                why = "（清单里有该包，但丢签名不等于豁免）"
            elif entry:
                why = "（清单里有该包，但 manifest 已变 ⇒ 不豁免）"
            else:
                why = ""
            print(f"        - {r['pkg']}  {r['status']}({r['reason']}){why}")
        if stale:
            print(f"[sig] 允许清单过期 {len(stale)} 条（包已恢复有效签名，条目应经用户裁定后删除）：")
            for p, e in stale:
                print(f"        - {p}  since={e.get('since')}")

    return 0 if not unregistered else 1


if __name__ == "__main__":
    sys.exit(main())
