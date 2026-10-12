"""core_* 官方包 manifest 签名工具（t19 交付配套）。

- 用平台发布私钥（backend/data/sign-keys/signing.key，与 L0 锚 fw_official_pubkey.json 配对）
  对 manifest（剥 signature 后 JCS 规范化字节）签名并落盘。
- 签名后立即用锚公钥验签 + check_manifest G1-G10，失败即非零退出。

用法（在 FrameWeave 后端 venv 下执行）:
    cd F:\\1223\\解说工坊\\FrameWeave\\backend
    .venv\\Scripts\\python.exe F:\\1223\\解说工坊\\frameweave-plugins\\sdk\\sign_core_manifests.py [--packages DIR] [pkg...]

不带 pkg 参数 = 处理 DIR 下全部 core_* 包。
验证刚性：私钥不可用 / 验签失败 / 校验失败 → 非零退出。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))                     # .../frameweave-plugins/sdk
WORKSPACE = os.path.abspath(os.path.join(HERE, "..", ".."))           # 工作区根（FrameWeave 的上级）
BACKEND = os.path.join(WORKSPACE, "FrameWeave", "backend")
sys.path.insert(0, BACKEND)
PLUGINS = os.path.abspath(os.path.join(HERE, "..", "packages"))
sys.path.insert(0, HERE)


def main() -> int:
    ap = argparse.ArgumentParser(description="官方 core 包 manifest 签名与验签")
    ap.add_argument("--packages", default=PLUGINS)
    ap.add_argument("pkgs", nargs="*", help="指定包目录名（默认全部 core_*）")
    args = ap.parse_args()

    from app import signatures
    from check_manifest import check_manifest_verbose  # noqa: E402  (sys.path 已加 sdk)

    priv = signatures.load_private_key()
    if priv is None:
        print("[sign] 错误：发布私钥不存在（backend/data/sign-keys/signing.key）")
        return 2
    anchor = signatures.official_pubkey_b64()
    if not anchor:
        print("[sign] 错误：L0 官方公钥锚缺失（backend/app/fw_official_pubkey.json）")
        return 2
    pub = priv.public_key()
    if signatures.pubkey_b64(pub) != anchor:
        print("[sign] 错误：本地发布私钥与官方锚不匹配，拒绝签名")
        return 2

    targets = args.pkgs or sorted(
        d for d in os.listdir(args.packages)
        if d.startswith("core_") and os.path.isfile(os.path.join(args.packages, d, "manifest.json")))
    failed = 0
    for name in targets:
        mpath = os.path.join(args.packages, name, "manifest.json")
        with open(mpath, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        # 预检：携带占位签名跑 G1-G10（避免 G2/G10 对未签名包误报，其他项目照常检查）
        pre = dict(manifest)
        pre["signature"] = {"alg": "ed25519", "key_id": "x" * 16,
                            "value": "x", "over": "manifest"}
        errs, _warnings = check_manifest_verbose(pre)
        if errs:
            print(f"[sign] {name}: manifest G1-G10 校验失败，跳过签名")
            for e in errs:
                print(f"    - {e}")
            failed += 1
            continue
        if manifest.get("schema_version") != 2:
            print(f"[sign] {name}: 非 schema_version=2，拒绝签名")
            failed += 1
            continue
        signed = signatures.sign_manifest(manifest, priv, pub)
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(signed, f, ensure_ascii=False, indent=2)
        ok = signatures.verify_manifest_signature_b64(signed, anchor)
        kid = signed["signature"]["key_id"]
        print(f"[sign] {name}: {'SIG_OK' if ok else 'SIG_FAIL'} key_id={kid} "
              f"scenes?inputs={len(manifest.get('inputs', []))} "
              f"outputs={len(manifest.get('outputs', []))} params={len(manifest.get('params', []))}")
        if not ok:
            failed += 1
    print(f"[sign] DONE failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())