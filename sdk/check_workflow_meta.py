#!/usr/bin/env python3
"""工作流 meta 元数据一致性校验器（纯标准库，只读）。

校验 `workflows/<id>/meta.json` 与同目录 `workflow.json` 是否自洽：

  M1 文件齐备：workflow.json 与 meta.json 都必须存在
  M2 字段齐备：meta 必须有 id / name / version / kind / source / updated_at / workflow_sha256
  M3 内容哈希：meta.workflow_sha256 == sha256(workflow.json 原样字节)
  M4 时间戳：  meta.updated_at == workflow.json 最后一次提交的 committer 时间（Unix 秒）
  M5 基本字段：meta.id/name/version/kind 与 workflow.json 顶层一致
                 （tpl_id 优先、其次 id；workflow 未声明 version/kind 时跳过对应项）

设计要点：
- **只读**：本脚本不写任何文件；要刷新请用 `--print-desired` 打印应写值，人工/脚本改。
- 为什么要有 M4：本仓的 workflows 是正本，meta 会随目录一起被复制/导出；哈希与时间戳
  是「这份 meta 描述的就是这份字节」的唯一凭据。无闸门时它会烂掉（2026-10-26 实测
  16/25 哈希过期、25/25 时间戳与 git 不符）。
- 非 git 环境（导出的裸目录）用 `--no-git` 跳过 M4。

用法：
    python sdk/check_workflow_meta.py                    # 校验，exit 0=全过 / 1=有不一致
    python sdk/check_workflow_meta.py --json             # 机器可读
    python sdk/check_workflow_meta.py --no-git           # 跳过 M4（非 git 环境）
    python sdk/check_workflow_meta.py --print-desired     # 打印每个 meta 的应有值（不改盘）
    python sdk/check_workflow_meta.py --workflows DIR     # 指定 workflows 目录
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
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_WORKFLOWS = os.path.join(REPO, "workflows")

REQUIRED_META_KEYS = ("id", "name", "version", "kind", "source", "updated_at", "workflow_sha256")


def git_commit_epoch(repo: str, rel_path: str):
    """workflow.json 最后一次提交的 committer 时间（Unix 秒）；未跟踪/无历史 → None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%ct", "--", rel_path],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def git_available(repo: str) -> bool:
    try:
        proc = subprocess.run(["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "true"


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def check_one(workflows_dir: str, wid: str, repo: str, use_git: bool) -> dict:
    """返回 {id, problems[], desired{}, actual{}}；problems 为空 = 通过。"""
    d = os.path.join(workflows_dir, wid)
    wf_p = os.path.join(d, "workflow.json")
    meta_p = os.path.join(d, "meta.json")
    res = {"id": wid, "problems": [], "desired": {}, "actual": {}}

    if not os.path.isfile(wf_p):
        res["problems"].append("M1 缺 workflow.json")
    if not os.path.isfile(meta_p):
        res["problems"].append("M1 缺 meta.json")
    if res["problems"]:
        return res

    raw = open(wf_p, "rb").read()
    actual_sha = hashlib.sha256(raw).hexdigest()
    try:
        wf = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        res["problems"].append(f"M3 workflow.json 解析失败: {ex}")
        return res
    try:
        meta = json.load(open(meta_p, encoding="utf-8"))
    except (OSError, ValueError) as ex:
        res["problems"].append(f"M2 meta.json 解析失败: {ex}")
        return res

    missing = [k for k in REQUIRED_META_KEYS if k not in meta]
    if missing:
        res["problems"].append(f"M2 meta 缺字段: {', '.join(missing)}")

    desired = {"workflow_sha256": actual_sha}
    res["actual"] = {"workflow_sha256": meta.get("workflow_sha256"),
                     "updated_at": meta.get("updated_at")}

    # M3 内容哈希
    if meta.get("workflow_sha256") != actual_sha:
        res["problems"].append(
            f"M3 workflow_sha256 过期：meta={str(meta.get('workflow_sha256'))[:16]}… / 实际={actual_sha[:16]}…")

    # M5 基本字段（以 workflow.json 顶层为准；未声明则跳过）
    wf_id = wf.get("tpl_id") or wf.get("id")
    pairs = [("id", wf_id), ("name", wf.get("name")), ("version", wf.get("version")),
             ("kind", wf.get("kind"))]
    for key, expect in pairs:
        if expect is None:
            continue
        if meta.get(key) != expect:
            res["problems"].append(f"M5 meta.{key} 与 workflow 顶层不一致：meta={meta.get(key)!r} / workflow={expect!r}")

    # M4 时间戳
    if use_git:
        epoch = git_commit_epoch(repo, os.path.relpath(wf_p, repo).replace("\\", "/"))
        if epoch is None:
            res["problems"].append("M4 无法取到 workflow.json 的最后提交时间（文件未跟踪或不在 git 仓库内）")
        else:
            desired["updated_at"] = epoch
            upd = _num(meta.get("updated_at"))
            if upd is None or int(upd) != epoch:
                res["problems"].append(
                    f"M4 updated_at 与最后提交时间不符：meta={meta.get('updated_at')!r} / git={epoch}")

    res["desired"] = desired
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="workflows meta 元数据一致性校验（只读）")
    ap.add_argument("--workflows", default=DEFAULT_WORKFLOWS, help="workflows 目录（默认仓内 workflows/）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--no-git", action="store_true", help="跳过 M4（updated_at vs git 提交时间）")
    ap.add_argument("--print-desired", action="store_true", help="打印每个 meta 的应有值（不写盘）")
    args = ap.parse_args()

    workflows_dir = os.path.abspath(args.workflows)
    if not os.path.isdir(workflows_dir):
        print(f"[wf-meta] 目录不存在: {workflows_dir}")
        return 2
    repo = os.path.dirname(workflows_dir)
    use_git = not args.no_git
    if use_git and not git_available(repo):
        print(f"[wf-meta] 警告：{repo} 不是 git 工作区，自动跳过 M4（等价 --no-git）")
        use_git = False

    ids = sorted(d for d in os.listdir(workflows_dir)
                 if os.path.isdir(os.path.join(workflows_dir, d)))
    if not ids:
        print(f"[wf-meta] {workflows_dir} 下没有工作流目录")
        return 2

    results = [check_one(workflows_dir, wid, repo, use_git) for wid in ids]
    failed = [r for r in results if r["problems"]]

    if args.print_desired:
        for r in results:
            if r["desired"]:
                print(f"  {r['id']:<28} " + "  ".join(f"{k}={v}" for k, v in r["desired"].items()))
        print()

    if args.json:
        print(json.dumps({
            "workflows_dir": workflows_dir, "use_git": use_git,
            "checked": len(results), "failed": len(failed),
            "entries": [{"id": r["id"], "problems": r["problems"],
                         "desired": r["desired"], "actual": r["actual"]} for r in results],
            "exit_code": 1 if failed else 0,
        }, ensure_ascii=False, indent=2))
    else:
        for r in results:
            if r["problems"]:
                print(f"  FAIL {r['id']}")
                for p in r["problems"]:
                    print(f"       - {p}")
        print(f"[wf-meta] 校验 {len(results)} 个工作流：{len(results) - len(failed)} OK / {len(failed)} FAILED"
              + ("" if use_git else "（已跳过 M4 时间戳）"))
        if failed:
            print("[wf-meta] 修法：按 --print-desired 打印的值更新对应 meta.json 的 "
                  "workflow_sha256 / updated_at（本脚本不写盘）")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
