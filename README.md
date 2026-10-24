# frameweave-plugins — FrameWeave 官方/社区插件独立项目

平台与插件边界重构（P0.1 平台空壳化）后，FrameWeave 官方插件从平台仓库
（`FrameWeave/backend/user_nodes`）整体移出，独立于此项目开发、构建与发布。
平台仓库保持零插件空壳，仅通过 `backend/user_nodes/.gitkeep` 占位。

## 目录结构

```
frameweave-plugins/
├── packages/            # 插件包源（每包 = manifest.json + node.py 骨架）
│   ├── core_ai_narrate/
│   ├── core_asr/
│   ├── core_batch_render/
│   ├── core_draft_export/
│   ├── core_load_video/
│   ├── core_scene_detect/
│   ├── core_subtitle_compose/
│   ├── core_translate/
│   ├── core_tts/
│   ├── core_video_render/
│   └── hello/
├── docs/                # （预留）插件文档
└── sdk/                 # （预留）插件开发 SDK
```

## 来源与版本基线

- 本次迁移（P0.1 平台空壳化）一次性复制自 **FrameWeave 仓库 HEAD commit `18e68ac`**
  （`18e68ac900154b467430a369f4ec9ef7a8c5d493`：docs(platform): 节点市场化空壳分发设计说明 + 项目深度分析报告）。
- 迁移方式：逐文件字节级复制，并逐一以 `git rev-parse HEAD:<path>` 语义哈希核对，
  11 个插件包（10 个 `core_*` + `hello`）共 21 个文件与迁移前 git HEAD 完全一致。
- 包内 `manifest.json` + `node.py` 骨架与迁移前平台仓库版本一致（含后续 P0 任务的
  manifest 端口回填、运行时安全加固等变更将在独立项目内继续演进，并由发布管道产出安装包）。

## 平台对接约定

- 平台 `declarative.scan()` 扫描 `<USER_NODES_DIR>/<pkg>/manifest.json`，
  插件包顶层目录必须直接含 `manifest.json`（`hello` 为声明式 transform 示例，无 node.py）。
- 安装：平台提供 `POST /api/user_nodes` 上传 zip / 商城安装；独立项目负责打包发布。

## 历史来源（git）

FrameWeave 仓库中 `backend/user_nodes/*` 的历史（含 `evil_outside.txt` 测试残留）仍保留在
FrameWeave git 历史（HEAD 18e68ac 及更早 commit），本次迁移不删除历史，仅从工作区/索引移除。
