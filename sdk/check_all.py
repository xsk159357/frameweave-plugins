#!/usr/bin/env python3
"""批次校验本仓两道门禁：插件包 G1-G10 + 工作流 meta 元数据一致性。

用法：
    python sdk/check_all.py [packages_dir]      # 只校验包（旧行为，兼容）
    python sdk/check_all.py --with-workflows    # 包 + workflows meta（推荐：提交前/CI 用这个）
    python sdk/check_all.py --only-workflows    # 只校验 workflows meta

- 包门禁：逐包 `check_manifest.py`（G1-G10），汇总 schema_version / 签名状态。
- meta 门禁：`check_workflow_meta.py`（M1 文件齐备 / M2 字段齐备 / M3 sha256 /
  M4 updated_at vs git 提交时间 / M5 meta↔workflow 基本字段）。
- `--with-workflows` 是显式开关而不是默认：既有调用方（含文档示例）按「只校验包」
  理解本脚本，默认改语义会把别人的绿门变成红门。提交前自检清单已改为带该开关。

退出码 0 = 门禁全过（G10 未签名警告允许，报告里标注签名状态）；
      1 = 存在校验失败的包或 workflows meta 不一致；2 = 环境缺失（目录不存在/无包）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PACKAGES = os.path.normpath(os.path.join(HERE, "..", "packages"))
DEFAULT_WORKFLOWS = os.path.normpath(os.path.join(HERE, "..", "workflows"))


def check_packages(packages_dir: str) -> int:
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


def check_workflow_meta(workflows_dir: str) -> int:
    if not os.path.isdir(workflows_dir):
        # 显式要求校验 meta 却找不到目录 ⇒ 算环境缺失（不能当「通过」）
        print(f"[check_all] 错误：workflows 目录不存在: {workflows_dir}")
        return 2
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "check_workflow_meta.py"), "--workflows", workflows_dir],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode == 0:
        for line in tail.splitlines():
            print(line)
        return 0
    for line in tail.splitlines():
        print(line)
    return 1 if proc.returncode == 1 else 2


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    unknown = flags - {"--with-workflows", "--only-workflows"}
    if unknown:
        print(f"[check_all] 未知参数: {', '.join(sorted(unknown))}")
        return 2
    do_packages = "--only-workflows" not in flags
    do_workflows = ("--with-workflows" in flags) or ("--only-workflows" in flags)
    packages_dir = args[0] if args else DEFAULT_PACKAGES
    workflows_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(packages_dir)), "workflows"))

    codes = []
    if do_packages:
        codes.append(check_packages(packages_dir))
    if do_workflows:
        codes.append(check_workflow_meta(workflows_dir))
    else:
        print("[check_all] 提示：加 --with-workflows 同时校验 workflows meta（M1-M5 一致性，提交前建议带上）")
    if len(codes) > 1:
        verdict = "PASS" if all(c == 0 for c in codes) else "FAIL"
        print(f"[check_all] 门禁汇总：包={'OK' if codes[0] == 0 else 'FAIL'} / "
              f"workflows-meta={'OK' if codes[1] == 0 else 'FAIL'} → {verdict}")
    if any(c == 2 for c in codes):
        return 2
    return 0 if all(c == 0 for c in codes) else 1


if __name__ == "__main__":
    sys.exit(main())
