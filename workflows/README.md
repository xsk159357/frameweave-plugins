# frameweave-plugins/workflows — 工作流版本受控产物目录（S6）

> 建立：2026-10-17（t5 工作流更新任务）｜用途：上架/回滚/复用的受控 workflow 构建产物（t1 报告 S6 建议）。

每个 `<id>/` 目录含：
- `workflow.json`：干净的模板式工作流定义（无运行时状态）
- `meta.json`：版本受控元数据（version/source/updated_at/aligned_to/sha256）

覆盖：内置模板（oneclick/minimal）、backend/data/workflows 保留实例（cleaned）、服务器 9 个工作流条目本地源。
对齐基准：t3 优化节点口径（frameweave-plugins/packages 70 包 + backend/user_nodes 33 包，2026-10-17）。
