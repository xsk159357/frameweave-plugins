#!/usr/bin/env python3
"""批次校验本仓三道门禁：插件包 G1-G10 + 工作流 meta 一致性 + 官方锚验签。

用法：
    python sdk/check_all.py                          # 只校验包（旧行为，兼容）
    python sdk/check_all.py --with-workflows         # 包 + workflows meta
    python sdk/check_all.py --with-signature         # 包 + 官方锚验签（**改包必带**）
    python sdk/check_all.py --with-workflows --with-signature   # 提交前推荐：三门全开
    python sdk/check_all.py --only-workflows         # 只校验 workflows meta
    python sdk/check_all.py --only-signature         # 只验签

门禁内容：
- 包门禁 `check_manifest.py`：逐包 G1-G10，汇总 schema_version / 签名状态。
  注意：它的 `signed=True` 只表示「signature 是个对象」，**不验签** —— 要验签必须开
  `--with-signature`（那才是真验签，见 `check_signature.py`）。
- meta 门禁 `check_workflow_meta.py`：M1 文件齐备 / M2 字段齐备 / M3 sha256 /
  M4 updated_at vs git 提交时间 / M5 meta↔workflow 基本字段。
- 验签门 `check_signature.py`：官方锚（平台仓 `fw_official_pubkey.json`）+ ed25519 逐包验签，
  已知例外走 `sdk/signature_allowlist.json`（只减不增，删条目需用户裁定）。

三个开关都是**显式**的（不改默认语义）：既有调用方（含文档示例）按「只校验包」理解本脚本，
把默认行为悄悄扩大 = 把他人的绿门变红门（例如在只有节点仓、没有平台仓锚的机器上，
验签门会 exit 2 环境缺失）。README「提交前自检」已把推荐命令钉成三门全开。

退出码 0 = 门禁全过；1 = 有门禁失败；2 = 环境缺失（目录不存在 / 缺官方锚 / 非 git 等）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PACKAGES = os.path.normpath(os.path.join(HERE, "..", "packages"))
DEFAULT_WORKFLOWS = os.path.normpath(os.path.join(HERE, "..", "workflows"))
KNOWN_FLAGS = {"--with-workflows", "--only-workflows", "--with-signature", "--only-signature"}


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
    print(f"[check_all] 校验 {len(entries)} 个包：{len(ok)} OK / {len(failed)} FAILED"
          f"（signed=True 仅表示有 signature 对象，验签请开 --with-signature）")
    for name, sv, signed, tail in ok:
        print(f"  OK   {name:<28} schema_version={sv} signed={signed} | {tail}")
    for name, detail in failed:
        print(f"  FAIL {name}\n       {detail}")
    return 1 if failed else 0


def _run_step(script: str, argv: list, missing_msg: str) -> int:
    proc = subprocess.run([sys.executable, os.path.join(HERE, script)] + argv,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        print(out.rstrip())
    if proc.returncode == 2:
        print(missing_msg)
        return 2
    return 1 if proc.returncode == 1 else 0


def check_workflow_meta(workflows_dir: str) -> int:
    if not os.path.isdir(workflows_dir):
        print(f"[check_all] 错误：workflows 目录不存在: {workflows_dir}")
        return 2
    return _run_step("check_workflow_meta.py", ["--workflows", workflows_dir],
                     "[check_all] workflows meta 门禁环境缺失")


def check_signature(packages_dir: str) -> int:
    return _run_step("check_signature.py", ["--packages", packages_dir],
                     "[check_all] 验签门环境缺失（多半是找不到平台仓的官方公钥锚 "
                     "FrameWeave/backend/app/fw_official_pubkey.json；可用 FW_OFFICIAL_PUBKEY 指定）")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    unknown = flags - KNOWN_FLAGS
    if unknown:
        print(f"[check_all] 未知参数: {', '.join(sorted(unknown))}")
        return 2
    only_workflows = "--only-workflows" in flags
    only_signature = "--only-signature" in flags
    if only_workflows and only_signature:
        print("[check_all] --only-workflows 与 --only-signature 不能同时使用")
        return 2

    packages_dir = os.path.abspath(args[0]) if args else DEFAULT_PACKAGES
    workflows_dir = os.path.normpath(os.path.join(os.path.dirname(packages_dir), "workflows"))

    do_packages = not (only_workflows or only_signature)
    do_workflows = only_workflows or ("--with-workflows" in flags)
    do_signature = only_signature or ("--with-signature" in flags)

    steps = []          # [(名称, 退出码)]
    if do_packages:
        steps.append(("包", check_packages(packages_dir)))
    if do_workflows:
        steps.append(("workflows-meta", check_workflow_meta(workflows_dir)))
    if do_signature:
        steps.append(("signature", check_signature(packages_dir)))
    if not do_workflows and not only_signature:
        print("[check_all] 提示：加 --with-workflows 同时校验 workflows meta（M1-M5），")
    if not do_signature and not only_workflows:
        print("[check_all]       加 --with-signature 跑官方锚验签（改包必带；signed=True 不等于验签通过）")

    if len(steps) > 1:
        detail = " / ".join(f"{n}={'OK' if c == 0 else ('ENV' if c == 2 else 'FAIL')}" for n, c in steps)
        print(f"[check_all] 门禁汇总：{detail} → "
              f"{'PASS' if all(c == 0 for _n, c in steps) else 'FAIL'}")
    codes = [c for _n, c in steps]
    if any(c == 2 for c in codes):
        return 2
    return 0 if all(c == 0 for c in codes) else 1


if __name__ == "__main__":
    sys.exit(main())
