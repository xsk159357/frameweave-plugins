# user/jsm_remake_match — B3 精选复刻匹配节点

> 依据：[07_实施任务拆解_阶段二.md](../../../docs/reports/项目方向审计/07_实施任务拆解_阶段二.md) S2-T1
> + [r2-match-analysis.md](../../../docs/reports/jieshuomao解说工坊/r2-match-analysis.md) §3（curated_remake_matcher 侦察）

## 功能契约

输入「解说片 VIDEO + 原片[] JSON」→ 输出「匹配表 JSON」：解说画面逐段取材自哪个原片、哪段时间，
含 `ref_source / ref_name / ref_path / ref_start / ref_end / similarity / speed / audio_evidence / suspect`。

干净重实现（不复刻 pyd 受限逻辑，无 LLM、全本地）：

```
解说片 ──灰度缩略图索引(48×27@fps)──→ 镜头切分
原片[] ──灰度索引──→ 每段多子帧投票+滑窗精排（gray_sim 候选 top-k）
              ↓
语义精排（DINOv2-Base/Small → Chinese-CLIP → 灰度，懒加载 + report.degraded）
              ↓
全局时序一致性 DP（灵魂①：偏移分段恒定、换段有代价：within_shot_jump_pen /
 cross_file_pen / drift_scale / back_jump_extra）
              ↓
段治理（合并相邻/吸收短段/重叠吸附）→ speed 估计
              ↓
音频最终裁决（灵魂②：log-mel band-z-norm 互相关，suspect 段/全段按档位）
              ↓
匹配表 + 报告（语义降级链 / 缓存命中 / DP 统计 / 音频证据）
```

## 39 参数档位化

`preset: fast | balanced | precise`（默认 balanced）+ `advanced` JSON 高级参数组（可覆盖全部 39 键）：

| 档位 | 语义 | 音频 | fps/细修 |
|---|---|---|---|
| fast | none（纯灰度） | 关 | 4 / 4 |
| balanced | auto（DINOv2-S→CLIP→灰度） | 仅 suspect 段 | 4 / 8 |
| precise | dino（DINOv2-Base→Small→CLIP→灰度） | 全段 | 6 / 12 |

降级路径（模型缺失不阻断，report.degraded 记录）：`dinov2_base → dinov2_small → chinese_clip → 灰度投票`。

## 模型就绪（S2-T0 已交付）

- DINOv2-Small / DINOv2-Base：`DATA_DIR/models/dinov2-{small,base}`（pytorch_model.bin + config/preprocessor）
- Chinese-CLIP：HF 缓存 `models--OFA-Sys--chinese-clip-vit-base-patch16`（753MB）
- ffmpeg / ffprobe：统一 resolver
- 详见 [data/MODEL_READINESS.md](data/MODEL_READINESS.md)

## 验证

```bash
# SDK 门禁（G1-G10）
python ../sdk/check_manifest.py .

# 单测 + 端到端（合成视频对 + 特征缓存二次运行加速）
python test_b3.py --all
#  → 纯函数 6 项 ✅ / e2e matches=1 first≈0.18s second≈0.08s（缓存命中）/ 节点集成 DINOv2-Base+音频裁决 ✅
```

## 文件

- `manifest.json` — 插件契约（v2，G1-G10 零错误）
- `node.py` — 平台节点入口（NodeBase，资产解析/参数合并/run 编排）
- `b3_matcher.py` — 纯算法层（预设/灰度/DP/音频/缓存/语义，runner 注入可单测）
- `test_b3.py` — 验证脚本（`--all` 全量 / `--e2e` / `--node`）
- `data/MODEL_READINESS.md` — 模型就绪与降级路径权威说明