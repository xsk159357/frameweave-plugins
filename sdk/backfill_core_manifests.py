"""R1 回填（最终版）：从官方 core_* 包 node.py 的 spec() 转出 manifest v2 端口声明。

用法（在 FrameWeave 后端 venv 下执行，因为依赖 app.nodespec）:
  cd F:\\1223\\解说工坊\\FrameWeave\\backend
  .venv\\Scripts\\python.exe F:\\1223\\解说工坊\\frameweave-plugins\\sdk\\backfill_core_manifests.py [--write]

说明：
- 读取 frameweave-plugins/packages/core_*/node.py 的 spec()（NodeSpec），
  转成 manifest v2 所需 inputs/outputs/params 端口声明。
- 排除 NodeBase 基类（其 spec() 抛 NotImplementedError，非可导出节点）。
- core_load_video 没有 spec()（纯 run 节点），走 MANUAL_FALLBACK 手动端口声明。
- 默认 dry-run：打印与当前 manifest（剥离 signature 后）的差异；--write 才落盘。
- signature 字段由签名工具（backend/packaging/sign_plugin.py）后续补齐，此处不写。
"""
import json, os, sys, importlib.util, argparse

PLUGINS_ROOT = r"F:\1223\解说工坊\frameweave-plugins\packages"
BACKEND_ROOT = os.environ.get("FW_BACKEND_ROOT", r"F:\1223\解说工坊\FrameWeave\backend")
if os.path.isdir(BACKEND_ROOT) and BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)  # 使 node.py 的 `from app...` 可导入

# core_load_video 无 spec()：手动端口声明（与 node.py run() 输出保持一致）
MANUAL_FALLBACK = {
    "core_load_video": {
        "type_id": "core/load_video",
        "title": "导入视频",
        "category": "输入",
        "description": "导入本地视频文件并解析元数据",
        "version": "1.0.0",
        "inputs": [],
        "outputs": [
            {"name": "video", "type": "VIDEO", "label": "视频", "required": True,
             "default": None, "widget": "text", "options": [], "description": "导入的视频资产"},
            {"name": "meta", "type": "JSON", "label": "元数据", "required": False,
             "default": None, "widget": "text", "options": [], "description": "ffprobe 解析的媒体元数据"},
        ],
        "params": [
            {"name": "path", "type": "STRING", "label": "视频路径", "required": True,
             "default": None, "widget": "file", "options": [], "description": "本地视频文件路径"},
        ],
    },
}


def load_spec(pkg_dir: str, expected_type_id: str):
    """加载 node.py 并调用节点类 spec()，返回 NodeSpec（dict 视图）。

    排除 NodeBase 基类与任何 spec() 抛 NotImplementedError 的类；
    只接受 type_id 与 expected_type_id 一致的节点。
    """
    node_py = os.path.join(pkg_dir, "node.py")
    if not os.path.isfile(node_py):
        return None
    spec = importlib.util.spec_from_file_location("fw_core_node", node_py)
    mod = importlib.util.module_from_spec(spec)
    try:
        sys.modules["fw_core_node"] = mod
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"  !! 加载 node.py 失败: {e}")
        return None
    for name in dir(mod):
        if name == "NodeBase":
            continue  # 基类：spec() 抛 NotImplementedError，非可导出节点
        obj = getattr(mod, name)
        if not (isinstance(obj, type) and hasattr(obj, "spec")):
            continue
        try:
            ns = obj.spec()
        except NotImplementedError:
            continue
        except Exception as e:
            print(f"  !! spec() 调用失败 {name}: {e}")
            continue
        if not hasattr(ns, "type_id"):
            continue
        if ns.type_id != expected_type_id:
            print(f"  !! 跳过 {name}: type_id={ns.type_id} != {expected_type_id}")
            continue
        return _node_spec_to_dict(ns)
    print("  !! 未找到匹配节点类（type_id=%s）" % expected_type_id)
    return None


def _node_spec_to_dict(ns):
    """NodeSpec 对象 → manifest 端口 dict。"""
    def ports(plist):
        out = []
        for p in (plist or []):
            out.append({
                "name": p.name,
                "type": getattr(p.type, "value", str(p.type)),
                "label": getattr(p, "label", "") or p.name,
                "required": getattr(p, "required", True),
                "default": getattr(p, "default", None),
                "widget": getattr(p, "widget", "text"),
                "options": getattr(p, "options", []) or [],
                "description": getattr(p, "description", "") or "",
            })
        return out
    return {
        "type_id": ns.type_id,
        "title": ns.title,
        "category": ns.category,
        "description": ns.description,
        "version": ns.version,
        "inputs": ports(ns.inputs),
        "outputs": ports(ns.outputs),
        "params": ports(ns.params),
    }


def build_v2(d, spec_dict, old):
    """组装 v2 manifest（保留原元信息，补端口）。"""
    return {
        "schema_version": 2,
        "engine_api": "1.0",
        "type_id": spec_dict.get("type_id") or old.get("type_id"),
        "title": spec_dict.get("title") or old.get("title", d),
        "category": spec_dict.get("category") or old.get("category", "用户节点"),
        "description": spec_dict.get("description") or old.get("description", ""),
        "version": spec_dict.get("version") or old.get("version", "1.0.0"),
        "kind": "python",
        "runtime": "python",
        "entrypoint": old.get("entrypoint", "node.py"),
        "class_name": old.get("class_name", ""),
        "inputs": spec_dict["inputs"],
        "outputs": spec_dict["outputs"],
        "params": spec_dict["params"],
        "permission": old.get("permission", {}),
    }


def main():
    ap = argparse.ArgumentParser(description="core_* 包 manifest v2 回填（dry-run 默认）")
    ap.add_argument("--write", action="store_true", help="落盘写入（默认仅 dry-run 对比）")
    args = ap.parse_args()

    changed = 0
    for d in sorted(os.listdir(PLUGINS_ROOT)):
        if not d.startswith("core_"):
            continue
        pkg = os.path.join(PLUGINS_ROOT, d)
        mpath = os.path.join(pkg, "manifest.json")
        print("== %s ==" % d)
        old = {}
        if os.path.isfile(mpath):
            with open(mpath, encoding="utf-8") as f:
                old = json.load(f)
        old_nosig = {k: v for k, v in old.items() if k != "signature"}
        expected_type_id = "core/" + d[len("core_"):]
        spec_dict = MANUAL_FALLBACK.get(d) or load_spec(pkg, expected_type_id)
        if not spec_dict:
            print("  !! 无法从 spec() 转出（无手动兜底），保留原 manifest")
            continue
        v2 = build_v2(d, spec_dict, old)
        if v2 == old_nosig:
            print("  == 一致（端口声明无需回填）")
            continue
        print("  差异：")
        for k in sorted(set(v2) | set(old_nosig)):
            if v2.get(k) != old_nosig.get(k):
                print("    - %s: %r -> %r" % (k, old_nosig.get(k), v2.get(k)))
        if args.write:
            with open(mpath, "w", encoding="utf-8") as f:
                json.dump(v2, f, ensure_ascii=False, indent=2)
            print("  已写入 manifest v2: %s (inputs=%d outputs=%d params=%d)"
                  % (d, len(v2["inputs"]), len(v2["outputs"]), len(v2["params"])))
            changed += 1
        else:
            print("  （dry-run，未写入；加 --write 落盘）")
    print("DONE changed=%d" % changed)


if __name__ == "__main__":
    main()