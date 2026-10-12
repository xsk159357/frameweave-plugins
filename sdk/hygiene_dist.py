# -*- coding: utf-8 -*-
"""dist 打包卫生工具（t32 P0）：
1. 清理 40 个包目录下的 __pycache__ / *.pyc；
2. 对全部包重打包 dist/<pkg>.zip（顶层目录=pkg 名，复用 SDK package 校验与上限）；
3. 对已签名包（manifest 带 signature）重签 <zip>.sig archive 签名（官方锚密钥）；
4. 目录 vs 归档哈希一致性校验：zip 内每个成员（pkg/rel）与包目录当前文件的
   sha256 逐一比对（排除 __pycache__），输出 PASS/FAIL 报告；
5. 生成报告 docs/s10-dist-hygiene.md + dist/hygiene_report.json。

用法（在 backend venv 下执行，需要 app.signatures 官方密钥）：
    FrameWeave\\backend\\.venv\\Scripts\\python.exe frameweave-plugins\\sdk\\hygiene_dist.py

退出码：0 = 全部包 OK；1 = 存在 FAIL（哈希不一致/打包失败/签名失败）。
"""
from __future__ import annotations

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass
import hashlib
import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))                      # .../frameweave-plugins/sdk
PLUGINS = os.path.abspath(os.path.join(HERE, ".."))                    # .../frameweave-plugins
PACKAGES = os.path.join(PLUGINS, "packages")
DIST = os.path.join(PLUGINS, "dist")
BACKEND = os.path.normpath(os.path.join(HERE, "..", "..", "FrameWeave", "backend"))
sys.path.insert(0, BACKEND)

_SKIP_DIRS = {"__pycache__"}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_rel(base: str):
    """包目录 → [(rel, abs)]；跳过 __pycache__/pyc（打包卫生红线）。"""
    out = []
    base_abs = os.path.abspath(base)
    for root, dirs, files in os.walk(base_abs):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in sorted(files):
            if fn.endswith(".pyc"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base_abs).replace("\\", "/")
            out.append((rel, full))
    return sorted(out)


def clean_pycache() -> int:
    """删除所有包目录下 __pycache__；返回删除个数。"""
    n = 0
    for name in sorted(os.listdir(PACKAGES)):
        base = os.path.join(PACKAGES, name)
        cp = os.path.join(base, "__pycache__")
        if os.path.isdir(cp):
            shutil.rmtree(cp, ignore_errors=True)
            n += 1
    return n


def package_zip(pkg_dir: str, out_dir: str) -> dict:
    """打 zip（复用 SDK package 校验/上限；顶层目录=pkg 名）。"""
    sys.path.insert(0, os.path.join(HERE))
    from package_plugin import package
    return package(pkg_dir, out_dir)


def sign_archive(pkg_name: str, zip_path: str) -> dict:
    """对 zip 整体 sha256 签 archive 签名 → <zip>.sig（官方锚密钥）。"""
    from app import signatures as sg
    priv = sg.load_private_key()
    if priv is None:
        raise RuntimeError("发布私钥缺失（backend/data/sign-keys/signing.key），无法签 archive")
    pub = priv.public_key()
    sig = sg.sign_archive_signature(zip_path, priv, pub)
    sig_path = zip_path + ".sig"
    with open(sig_path, "w", encoding="utf-8") as f:
        json.dump(sig, f, ensure_ascii=False)
    # 验签
    ok = sg.verify_archive_signature(zip_path, str(sig.get("value") or ""), pub)
    if not ok:
        raise RuntimeError(f"{pkg_name}: archive 自验失败")
    return {"sig_path": sig_path, "key_id": sig.get("key_id"), "ok": ok}


def verify_consistency(pkg_dir: str, zip_path: str, pkg_name: str) -> dict:
    """目录 vs zip：成员名集合 + 每个成员 sha256 必须一致。"""
    dir_files = {rel: sha256_file(abs) for rel, abs in _walk_rel(pkg_dir)}
    zip_files = {}
    ok = True
    problems = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            prefix = pkg_name + "/"
            if not name.startswith(prefix):
                problems.append(f"zip 成员越界（非 {pkg_name}/ 前缀）: {name}")
                ok = False
                continue
            rel = name[len(prefix):]
            if "/" in rel and rel.split("/")[0] in _SKIP_DIRS:
                continue
            data = zf.read(info)
            zip_files[rel] = hashlib.sha256(data).hexdigest()
    for rel in sorted(set(dir_files) | set(zip_files)):
        if rel not in dir_files:
            problems.append(f"zip 含目录没有的文件: {rel}")
            ok = False
        elif rel not in zip_files:
            problems.append(f"目录含 zip 没有的文件: {rel}")
            ok = False
        elif dir_files[rel] != zip_files[rel]:
            problems.append(f"成员哈希不一致: {rel}")
            ok = False
    return {"consistent": ok, "dir_files": len(dir_files), "zip_files": len(zip_files),
            "problems": problems[:8]}


def main() -> int:
    n_pkg = 0
    ok_list, fail_list = [], []
    cleaned = clean_pycache()
    os.makedirs(DIST, exist_ok=True)

    for name in sorted(os.listdir(PACKAGES)):
        pkg_dir = os.path.join(PACKAGES, name)
        if not os.path.isdir(pkg_dir) or not os.path.isfile(os.path.join(pkg_dir, "manifest.json")):
            continue
        n_pkg += 1
        rec = {"pkg": name, "zip": "", "zip_sha256": "", "bytes": 0,
               "sig": "", "sig_key": "", "signed_manifest": False, "consistent": False}
        try:
            # 1) 打包
            pr = package_zip(pkg_dir, DIST)
            rec.update(zip=os.path.basename(pr["zip"]), zip_sha256=pr["sha256"],
                       bytes=pr["bytes"])
            # 2) manifest 是否有签名（决定是否签 .sig）
            with open(os.path.join(pkg_dir, "manifest.json"), "r", encoding="utf-8") as f:
                mf = json.load(f)
            has_sig = isinstance(mf.get("signature"), dict) and bool(
                (mf.get("signature") or {}).get("value"))
            rec["signed_manifest"] = has_sig
            if has_sig:
                s = sign_archive(name, pr["zip"])
                rec["sig"] = os.path.basename(s["sig_path"])
                rec["sig_key"] = s["key_id"]
            # 3) 一致性
            v = verify_consistency(pkg_dir, pr["zip"], name)
            rec["consistent"] = v["consistent"]
            rec["dir_files"] = v["dir_files"]
            rec["zip_files"] = v["zip_files"]
            if v["consistent"]:
                ok_list.append(rec)
                print(f"[hygiene] OK   {name:<32} zip={rec['bytes']}B {rec['zip_sha256'][:16]}… "
                      f"sig={'Y' if rec['sig'] else '-'} 一致={v['dir_files']}成员")
            else:
                rec["problems"] = v["problems"]
                fail_list.append(rec)
                print(f"[hygiene] FAIL {name:<32} " + "; ".join(v["problems"]))
        except Exception as e:  # noqa: BLE001
            rec["problems"] = [f"{type(e).__name__}: {e}"]
            fail_list.append(rec)
            print(f"[hygiene] FAIL {name:<32} {e}")

    report = {
        "tool": "sdk/hygiene_dist.py (t32)",
        "packages_checked": n_pkg,
        "pycache_cleaned": cleaned,
        "ok": [{"pkg": r["pkg"], "zip": r["zip"], "bytes": r["bytes"],
                "zip_sha256": r["zip_sha256"], "sig": r.get("sig", ""),
                "signed_manifest": r.get("signed_manifest", False),
                "consistent": r.get("consistent", False)} for r in ok_list],
        "failed": [{"pkg": r["pkg"], "problems": r.get("problems", [])} for r in fail_list],
        "summary": {"total": n_pkg, "ok": len(ok_list), "failed": len(fail_list)},
    }
    with open(os.path.join(DIST, "hygiene_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # RST 报告
    lines = [
        "# dist 打包卫生报告（t32）",
        "",
        f"- 检查包数：{n_pkg}（清理 `__pycache__` {cleaned} 个）",
        f"- 结果：OK={len(ok_list)}，FAIL={len(fail_list)}",
        "",
        "## 逐包结果",
        "",
        "| 包 | zip sha256 | size | .sig | manifest签名 | 目录↔归档一致 |",
        "|---|---|---|---|---|---|",
    ]
    for r in ok_list:
        lines.append(f"| {r['pkg']} | `{r['zip_sha256']}` | {r['bytes']} | "
                     f"{'Y' if r.get('sig') else '-'} | {'Y' if r.get('signed_manifest') else '-'} | Y |")
    for r in fail_list:
        lines.append(f"| {r['pkg']} | - | - | - | - | FAIL: {'; '.join(r.get('problems', []))} |")
    lines += [
        "",
        "## 一致性校验口径",
        "对每个包：遍历包目录（排除 `__pycache__`/`*.pyc`）计算每个文件 sha256，",
        "与 zip 内 `pkg/<rel>` 成员逐一比对（成员名集合须完全一致 + 哈希逐字一致）。",
        "`.sig` 为 zip 整体字节的 Ed25519 archive 签名（官方锚密钥），验签通过才写。",
        "",
    ]
    doc_path = os.path.join(os.path.dirname(HERE), "..", "docs", "s10-dist-hygiene.md")
    doc_path = os.path.normpath(doc_path)
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("=" * 80)
    print(f"[hygiene] 清理 __pycache__ x{cleaned}；重打包 {n_pkg} 包；"
          f"OK={len(ok_list)} FAIL={len(fail_list)}")
    print(f"[hygiene] 报告: {doc_path}")
    print(f"[hygiene] JSON: {os.path.join(DIST, 'hygiene_report.json')}")
    return 1 if fail_list else 0


if __name__ == "__main__":
    sys.exit(main())