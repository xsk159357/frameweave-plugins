# -*- coding: utf-8 -*-
"""spec/manifest 端口一致性扫描（S3-T17 verify：R-工程-2 关闭证据）。

对 frameweave-plugins/packages/ 下每个 kind=python 包：
  1. manifest.json 端口声明（inputs/outputs/params）
  2. 经 app.declarative._make_python_class 加载 node.py → spec() 端口声明
  3. 逐端口对比 name/type/required/widget/options（默认值容忍差异：规格默认以 manifest 为准）
输出不一致清单；退出码 0=全部一致。

用法：python check_port_consistency.py [packages_dir]
"""
from __future__ import annotations

import json
import os
import sys

BACKEND = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "..", "FrameWeave", "backend"))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

DEFAULT_PACKAGES = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "packages"))


def _port_key(p: dict) -> tuple:
    """端口对比键：name + type（大小写归一）。"""
    return (str(p.get("name", "")), str(p.get("type", "")).upper())


def _ports(manifest: dict, kind: str) -> list:
    out = []
    for p in manifest.get(kind, []) or []:
        if isinstance(p, dict):
            out.append(p)
    return out


def _spec_ports(spec, kind: str) -> list:
    """从 NodeSpec 对象取 ports。kind ∈ inputs|outputs|params。"""
    objs = getattr(spec, kind, []) or []
    out = []
    for o in objs:
        d = {}
        for f in ("name", "type", "label", "required", "default", "widget", "options", "description"):
            if hasattr(o, f):
                v = getattr(o, f)
                if f == "type" and hasattr(v, "value"):
                    v = v.value
                d[f] = v
        out.append(d)
    return out


def check_package(pkg_dir: str) -> list:
    """返回不一致条目列表（空=一致）。"""
    issues = []
    mpath = os.path.join(pkg_dir, "manifest.json")
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return [{"pkg": os.path.basename(pkg_dir), "error": "manifest 读取失败"}]
    if manifest.get("kind") != "python":
        return []
    try:
        from app.declarative import _make_python_class
        cls = _make_python_class(manifest, pkg_dir)
        spec = cls.spec()
    except Exception as e:  # noqa: BLE001
        return [{"pkg": os.path.basename(pkg_dir), "error": f"节点加载失败: {type(e).__name__}: {e}"}]

    for kind in ("inputs", "outputs", "params"):
        m_ports = _ports(manifest, kind)
        s_ports = _spec_ports(spec, kind)
        m_keys = [_port_key(p) for p in m_ports]
        s_keys = [_port_key(p) for p in s_ports]
        if m_keys != s_keys:
            issues.append({
                "pkg": os.path.basename(pkg_dir), "kind": kind,
                "manifest": [list(k) for k in m_keys],
                "spec": [list(k) for k in s_keys],
            })
        # widget/options 一致性（仅 manifest 对应端口存在时核；required 缺失视为 true 不报）
        s_by = {_port_key(p): p for p in s_ports}
        for mp in m_ports:
            sp = s_by.get(_port_key(mp))
            if sp is None:
                continue
            for f in ("widget", "options"):
                mv, sv = mp.get(f), sp.get(f)
                if f == "options" and mv is None:
                    mv = []
                if f == "widget" and mv is None:
                    continue  # manifest 未声明 widget → 取默认 text，不视为不一致
                if str(mv or "") != str(sv or ""):
                    issues.append({"pkg": os.path.basename(pkg_dir), "kind": kind,
                                  "port": mp.get("name"), "field": f,
                                  "manifest": mv, "spec": sv})
            # required：仅当 manifest 显式声明且与 spec 冲突时报
            if "required" in mp and bool(mp["required"]) != bool(sp.get("required", True)):
                issues.append({"pkg": os.path.basename(pkg_dir), "kind": kind,
                              "port": mp.get("name"), "field": "required",
                              "manifest": mp.get("required"), "spec": sp.get("required")})
    return issues


def main() -> int:
    packages_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PACKAGES
    total_issues = []
    checked = 0
    for name in sorted(os.listdir(packages_dir)):
        pkg_dir = os.path.join(packages_dir, name, "manifest.json")
        if not os.path.isfile(pkg_dir):
            continue
        checked += 1
        total_issues.extend(check_package(os.path.join(packages_dir, name)))
    if total_issues:
        print(f"[port-consistency] {len(total_issues)} 个不一致（checked {checked} 包）：")
        for it in total_issues:
            print("  ", json.dumps(it, ensure_ascii=False))
        return 1
    print(f"[port-consistency] 全部一致：{checked} 个 python 包 spec/manifest 端口对齐 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())