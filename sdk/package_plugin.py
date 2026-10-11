"""插件打包工具：校验 + 打包分发 zip（纯标准库）。

用法：
    python package_plugin.py <pkg_dir> [-o <out_dir>]

流程：读 <pkg_dir>/manifest.json → check_manifest v3 校验（G1-G10，必过）→
打包 <pkg_dir>/（zip 顶层目录 = pkg 名，拒绝路径穿越成员）→ 输出明文 sha256 与字节数。
大插件上限（与后端 worker/limits.py 对齐）：单文件 ≤4GB、总量 ≤8GB、成员 ≤20000、
zip 本体 ≤2GB；超限拒绝打包。
"""
from __future__ import annotations

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import argparse
import hashlib
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_manifest import (  # noqa: E402
    ZIP_MEMBER_COUNT,
    ZIP_SINGLE_FILE,
    ZIP_TOTAL_UNCOMPRESSED,
    UPLOAD_MAX_BYTES,
    check_manifest,
    check_manifest_verbose,
)


def _walk_safe(base: str):
    """逐文件枚举；成员一律相对 base。"""
    base_abs = os.path.abspath(base)
    for root, _dirs, files in os.walk(base_abs):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base_abs).replace("\\", "/")
            if rel.startswith("..") or os.path.isabs(rel):
                raise ValueError(f"非法相对路径: {rel}")
            yield rel, full


def package(pkg_dir: str, out_dir: str) -> dict:
    base_abs = os.path.abspath(pkg_dir)
    mp = os.path.join(base_abs, "manifest.json")
    if not os.path.isfile(mp):
        raise ValueError(f"{pkg_dir} 缺少 manifest.json")
    with open(mp, "r", encoding="utf-8") as f:
        m = json.load(f)
    errs, warnings = check_manifest_verbose(m)
    if errs:
        raise ValueError("manifest 校验失败（G1-G10），拒绝打包:\n  " + "\n  ".join(errs))
    # 大插件上限预检（与后端 worker/limits.py 对齐）
    total = 0
    count = 0
    for _rel, full in _walk_safe(base_abs):
        size = os.path.getsize(full)
        count += 1
        if size > ZIP_SINGLE_FILE:
            raise ValueError(f"单文件超限（>{ZIP_SINGLE_FILE} bytes）: {full}")
        total += size
    if total > ZIP_TOTAL_UNCOMPRESSED:
        raise ValueError(f"解压总量超限（>{ZIP_TOTAL_UNCOMPRESSED} bytes）: {total}")
    if count > ZIP_MEMBER_COUNT:
        raise ValueError(f"成员数超限（>{ZIP_MEMBER_COUNT}）: {count}")
    pkg_name = os.path.basename(base_abs.rstrip("/\\")) or "plugin"
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, pkg_name + ".zip")
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, full in _walk_safe(base_abs):
            zf.write(full, pkg_name + "/" + rel)
    if os.path.getsize(out) > UPLOAD_MAX_BYTES:
        raise ValueError(f"zip 本体超限（>{UPLOAD_MAX_BYTES} bytes，平台上传上限）: {out}")
    # sha256 / 字节数以 zip 文件本身为准（与商城下载校验、official 打包脚本一致）
    sha = hashlib.sha256()
    size = 0
    with open(out, "rb") as fobj:
        for chunk in iter(lambda: fobj.read(1 << 20), b""):
            sha.update(chunk)
            size += len(chunk)
    return {"zip": out, "bytes": size, "sha256": sha.hexdigest(), "pkg": pkg_name,
            "warnings": warnings}


def main() -> int:
    ap = argparse.ArgumentParser(description="插件打包（校验 + zip + sha256）")
    ap.add_argument("pkg_dir", help="插件包目录（含 manifest.json）")
    ap.add_argument("-o", "--out", default="dist", help="输出目录（默认 dist/）")
    args = ap.parse_args()
    try:
        r = package(args.pkg_dir, args.out)
    except ValueError as ex:
        print(f"[package] 拒绝: {ex}")
        return 1
    for w in r.get("warnings") or []:
        print(f"  [warn] {w}")
    print(f"[package] OK: {r['pkg']}")
    print(f"  zip    : {r['zip']}")
    print(f"  size   : {r['bytes']} bytes")
    print(f"  sha256 : {r['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
