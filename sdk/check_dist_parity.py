#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dist 归档 ↔ packages 正本 一致性门禁（纯标准库）。

为什么需要它（2026-10-29 T8 落地）：
    此前四道门（G1-G10 / workflows meta / manifest 验签 / archive 验签）**没有任何一条**
    断言 `dist/<pkg>.zip` 的内容等于 `packages/<pkg>/` 的内容。`.sig` 签的是 zip 的字节，
    所以「签名全绿」只证明 zip 没被篡改，**不证明它是最新版**。
    实测代价：2026-10-18 的旧快照里 17/75 个 zip 与正本不符，其中 11 个版本落后 1~3 个
    patch、4 个源文件变了却没升版本、2 个把 `__pycache__/*.pyc` 打进了分发物 —— 而四道门
    当时全部 exit 0。本脚本就是补这个盲区。

判据（逐 zip）：
    1. 成员必须全部落在 `<pkg>/` 前缀下（越界即 drift）；
    2. 相对路径集合必须相等（忽略 `__pycache__/`、`*.pyc`、`*.pyo`、`.DS_Store`、`Thumbs.db`）；
       ——「忽略」只作用于**内容判等**：zip 里**出现**这些成员本身就是缺陷（卫生红线，
       见 README §2.1），因此单独记为 banned 并判 drift，而不是睁一眼闭一眼；
    3. 同路径文件的 sha256 必须逐字节相等；
    4. （默认开，可用 --no-pairing 关）`<pkg>.zip` 必须配有 `<pkg>.zip.sig`。
    只对「有 zip 的包」判等；有包无 zip **不算 drift**（出厂基础节点只走 build_seed，不打商城包）。

用法：
    python sdk/check_dist_parity.py                    # 默认仓内 dist/ ↔ packages/
    python sdk/check_dist_parity.py --dist DIR --packages DIR
    python sdk/check_dist_parity.py --pkg core_tts --pkg user_jsm_match_visual
    python sdk/check_dist_parity.py --json             # 机器可读
    python sdk/check_dist_parity.py --no-pairing       # 不检查 zip↔sig 配对

退出码（三态，与其它门禁一致）：
    0 = 全部一致（0 drift）
    1 = 存在 drift（逐条列出包名 + 差异文件）
    2 = 环境缺失（dist/packages 目录不存在、dist 下没有 zip）—— **2 不等于通过**
"""
from __future__ import annotations

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import argparse
import hashlib
import json
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIST = os.path.normpath(os.path.join(HERE, "..", "dist"))
DEFAULT_PACKAGES = os.path.normpath(os.path.join(HERE, "..", "packages"))

EXIT_OK, EXIT_DRIFT, EXIT_ENV = 0, 1, 2

# 参与「忽略」判定的路径段与后缀：运行时编译产物与操作系统垃圾文件不属于分发内容
IGNORED_DIR_SEGMENTS = frozenset({"__pycache__"})
IGNORED_SUFFIXES = (".pyc", ".pyo")
IGNORED_BASENAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def is_ignored(rel_path: str) -> bool:
    """相对路径是否属于「不参与比对」的噪声（__pycache__ / *.pyc / OS 垃圾）。"""
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in IGNORED_DIR_SEGMENTS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in IGNORED_BASENAMES:
        return True
    return name.lower().endswith(IGNORED_SUFFIXES)


def sha256_file(path: str, block: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_dir(base: str) -> dict:
    """返回 {相对路径: sha256}，忽略噪声与符号链接逃逸。"""
    base_abs = os.path.abspath(base)
    out = {}
    for root, dirs, files in os.walk(base_abs):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIR_SEGMENTS)
        for fn in sorted(files):
            full = os.path.join(root, fn)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, base_abs).replace(os.sep, "/")
            if is_ignored(rel):
                continue
            if rel.startswith("..") or os.path.isabs(rel):
                raise ValueError(f"非法相对路径: {rel}")
            out[rel] = sha256_file(full)
    return out


def read_zip_members(zip_path: str, pkg_name: str) -> tuple:
    """返回 ({相对路径: sha256}, [越界成员], [卫生红线成员])。

    噪声成员（`__pycache__/`、`*.pyc`、`.DS_Store`…）不参与**内容判等**——它们不是源码，
    但"zip 里出现它们"本身是缺陷（README §2.1 卫生红线），所以单独收进 banned 列表，
    由调用方判为 drift：忽略是"不影响判等"，不是"允许存在"。
    """
    prefix = pkg_name + "/"
    files, escaped, banned = {}, [], []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if is_ignored(name):
                banned.append(name)
                continue
            if not name.startswith(prefix):
                escaped.append(name)
                continue
            rel = name[len(prefix):]
            if not rel:
                continue
            files[rel] = hashlib.sha256(zf.read(info)).hexdigest()
    return files, escaped, banned


def compare_package(pkg: str, zip_path: str, pkg_dir: str, check_pairing: bool) -> dict:
    """单包比对。返回 {pkg, status, ...}；status ∈ OK / DRIFT。"""
    res = {"pkg": pkg, "zip": os.path.basename(zip_path), "status": "OK",
           "only_in_zip": [], "only_in_src": [], "changed": [], "escaped": [],
           "banned_members": [], "zip_files": 0, "src_files": 0, "sig_missing": False}
    zip_files, escaped, banned = read_zip_members(zip_path, pkg)
    src_files = walk_dir(pkg_dir)
    res["zip_files"], res["src_files"] = len(zip_files), len(src_files)
    res["escaped"] = escaped
    res["banned_members"] = banned
    res["only_in_zip"] = sorted(set(zip_files) - set(src_files))
    res["only_in_src"] = sorted(set(src_files) - set(zip_files))
    res["changed"] = sorted(k for k in set(zip_files) & set(src_files)
                            if zip_files[k] != src_files[k])
    if check_pairing and not os.path.isfile(zip_path + ".sig"):
        res["sig_missing"] = True
    if (escaped or banned or res["only_in_zip"] or res["only_in_src"] or res["changed"]
            or res["sig_missing"]):
        res["status"] = "DRIFT"
    return res


def run(dist_dir: str, packages_dir: str, only_pkgs: list, check_pairing: bool) -> dict:
    report = {"dist_dir": dist_dir, "packages_dir": packages_dir,
              "checked": 0, "ok": 0, "drift": 0, "no_zip": [], "results": [],
              "pairing_checked": check_pairing}
    if not os.path.isdir(dist_dir):
        report["env_error"] = f"dist 目录不存在: {dist_dir}"
        return report
    if not os.path.isdir(packages_dir):
        report["env_error"] = f"packages 目录不存在: {packages_dir}"
        return report
    zips = sorted(f for f in os.listdir(dist_dir)
                  if f.lower().endswith(".zip") and os.path.isfile(os.path.join(dist_dir, f)))
    if not zips:
        report["env_error"] = f"dist 目录下没有 .zip: {dist_dir}"
        return report
    wanted = set(only_pkgs or [])
    all_pkgs = sorted(d for d in os.listdir(packages_dir)
                      if os.path.isfile(os.path.join(packages_dir, d, "manifest.json")))
    if wanted:
        unknown = sorted(wanted - set(all_pkgs))
        if unknown:
            report["env_error"] = f"--pkg 指定的包不存在: {unknown}"
            return report
    zipped = {z[:-4] for z in zips}
    report["no_zip"] = sorted(p for p in all_pkgs if p not in zipped)
    orphan = sorted(p for p in zipped if p not in set(all_pkgs))
    for pkg in orphan:
        report["results"].append({
            "pkg": pkg, "zip": pkg + ".zip", "status": "DRIFT",
            "only_in_zip": [], "only_in_src": [], "changed": [], "escaped": [],
            "banned_members": [], "zip_files": 0, "src_files": 0, "sig_missing": False,
            "note": "dist 有 zip 但 packages 无同名包目录（孤儿归档）"})
    for z in zips:
        pkg = z[:-4]
        if wanted and pkg not in wanted:
            continue
        if pkg in orphan:
            continue
        r = compare_package(pkg, os.path.join(dist_dir, z),
                            os.path.join(packages_dir, pkg), check_pairing)
        report["results"].append(r)
    report["checked"] = len(report["results"])
    report["drift"] = sum(1 for r in report["results"] if r["status"] != "OK")
    report["ok"] = report["checked"] - report["drift"]
    return report


def print_human(report: dict) -> int:
    if report.get("env_error"):
        print(f"[dist-parity] 环境缺失：{report['env_error']}")
        return EXIT_ENV
    print(f"[dist-parity] dist: {report['dist_dir']}")
    print(f"[dist-parity] 正本: {report['packages_dir']}")
    print(f"[dist-parity] 比对 {report['checked']} 个 zip → {report['ok']} 一致 / "
          f"{report['drift']} drift"
          f"（{len(report['no_zip'])} 个包无 zip，不算 drift；配对检查="
          f"{'开' if report['pairing_checked'] else '关'}）")
    for r in report["results"]:
        if r["status"] == "OK":
            continue
        print(f"  DRIFT {r['pkg']:<30} "
              f"changed={len(r['changed'])} only_zip={len(r['only_in_zip'])} "
              f"only_src={len(r['only_in_src'])} escaped={len(r['escaped'])} "
              f"banned={len(r.get('banned_members') or [])}"
              f"{' SIG_MISSING' if r.get('sig_missing') else ''}")
        if r.get("note"):
            print(f"        - {r['note']}")
        for k, label in (("escaped", "越界成员"), ("banned_members", "卫生红线成员"),
                         ("only_in_zip", "仅 zip 有"),
                         ("only_in_src", "仅正本有"), ("changed", "内容不同")):
            for item in r[k][:5]:
                print(f"        - {label}: {item}")
            if len(r[k]) > 5:
                print(f"        - {label}: …另有 {len(r[k]) - 5} 项（用 --json 看全量）")
    if report["drift"] == 0:
        print("[dist-parity] PASS：所有归档与正本逐文件 sha256 一致")
        return EXIT_OK
    print(f"[dist-parity] FAIL：{report['drift']} 个归档与正本不一致"
          f"（重打用 `python sdk/package_plugin.py packages/<pkg>`，"
          f"再 `python sdk/sign_core_manifests.py <pkg>` 重签，archives 用 sdk/hygiene_dist.py:sign_archive）")
    return EXIT_DRIFT


def main() -> int:
    ap = argparse.ArgumentParser(description="dist 归档 ↔ packages 正本 一致性门禁")
    ap.add_argument("--dist", default=DEFAULT_DIST, help="dist 目录（默认仓内 dist/）")
    ap.add_argument("--packages", default=DEFAULT_PACKAGES, help="包源码目录（默认仓内 packages/）")
    ap.add_argument("--pkg", action="append", default=[], help="只比对指定包（可重复）")
    ap.add_argument("--no-pairing", action="store_true", help="不检查 zip↔.sig 配对")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()
    report = run(os.path.abspath(args.dist), os.path.abspath(args.packages),
                 args.pkg, not args.no_pairing)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        if report.get("env_error"):
            return EXIT_ENV
        return EXIT_OK if report["drift"] == 0 else EXIT_DRIFT
    return print_human(report)


if __name__ == "__main__":
    sys.exit(main())
