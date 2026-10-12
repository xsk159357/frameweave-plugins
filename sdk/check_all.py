#!/usr/bin/env python3
"""批次校验所有插件包：check_manifest G1-G10 + schema_version/签名状态汇总。

用法：
    python sdk/check_all.py [packages_dir]

退出码 0 = 全部包校验通过（错误为零；G10 未签名警告允许，报告里标注签名状态）；
非零 = 存在校验失败的包。供 CI / SDK 校验门禁使用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PACKAGES = os.path.normpath(os.path.join(HERE, "..", "packages"))


def main() -> int:
    packages_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PACKAGES
    if not os.path.isdir(packages_dir):
        print(f"[check_all] 目录不存在: {packages_dir}")
        return 2
    entries = []
    for name in sorted(os.listdir(packages_dir)):
        mpath = os.path.join(packages_dir, name, "manifest.json")
        if os.path.isfile(mpath):
            entries.append(name)
    if not entries:
        print(f"[check_all] {packages_dir} 下没有插件包（缺 manifest.json）")
        return 2
    failed, ok = [], []
    for name in entries:
        pkg_dir = os.path.join(packages_dir, name)
        mpath = os.path.join(pkg_dir, "manifest.json")
        try:
            with open(mpath, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as ex:
            failed.append((name, f"manifest.json 解析失败: {ex}"))
            continue
        sv = manifest.get("schema_version")
        signed = isinstance(manifest.get("signature"), dict)
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "check_manifest.py"), pkg_dir],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            ok.append((name, sv, signed, proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""))
        else:
            failed.append((name, proc.stdout.strip() or proc.stderr.strip() or "check_manifest 失败"))
    print(f"[check_all] 校验 {len(entries)} 个包：{len(ok)} OK / {len(failed)} FAILED")
    for name, sv, signed, tail in ok:
        print(f"  OK   {name:<28} schema_version={sv} signed={signed} | {tail}")
    for name, detail in failed:
        print(f"  FAIL {name}\n       {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())