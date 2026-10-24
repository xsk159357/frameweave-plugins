# B3 精选复刻匹配 · 特征管线模型就绪清单（S2-T0）

> 依据：`docs/reports/项目方向审计/07_实施任务拆解_阶段二.md` S2-T0 契约 +
> `docs/reports/jieshuomao解说工坊/r2-match-analysis.md`（curated_remake_matcher 侦察）。
> 本文件是**模型下载指引与降级路径**的权威说明；节点实现（S2-T1 user/jsm_remake_match）按此消费。

## 1. 三模型就绪状态（2026-10 实测）

| 模型 | 用途（B3 管线） | 本机状态 | 就绪路径 |
|---|---|---|---|
| **Chinese-CLIP ViT-B/16** | 语义精排**回退**（DINOv2 不可用时） | ✅ **已就绪** | HF 缓存 `~/.cache/huggingface/hub/models--OFA-Sys--chinese-clip-vit-base-patch16`（`pytorch_model.bin` 753MB）；`deps.resolve_model("chinese-clip")` 或 `FRAMEWEAVE_CLIP_DIR` / `DATA_DIR/models/chinese-clip-vit-base-patch16` |
| **DINOv2-Small** | 语义精排**主模型**（自监督，无语言域偏差） | ❌ 未就绪 | 登记于 `models_registry.json`（`dinov2-small`，facebook/dinov2-small，pytorch_model.bin ≈89MB）；放置 `DATA_DIR/models/dinov2-small` 或 `FRAMEWEAVE_DINO_DIR` |
| **DINOv2-Base** | 语义精排（精准档推荐） | ❌ 未就绪 | 登记于 `models_registry.json`（`dinov2-base`，facebook/dinov2-base ≈344MB）；放置 `DATA_DIR/models/dinov2-base` |
| **ffmpeg log-mel** | 音频特征（全局 DP 音频裁决） | ✅ **已就绪** | ffmpeg 由 `deps.resolve_binary("ffmpeg")` 统一解析（imageio_ffmpeg → jieshuomao 构建 → 默认位）；libavfilter/afade → log-mel 由 ffmpeg 计算（r2 §5 `_audio_index` 语义） |

## 2. 下载指引（脚本：`scripts/fetch_b3_models.py`，可重复执行）

```text
# DINOv2-S（主）：
hf download facebook/dinov2-small --local-dir "%USERPROFILE%\.cache\huggingface\hub\..." 或
python scripts/fetch_b3_models.py --model dinov2-small --target <DATA_DIR>/models/dinov2-small
# DINOv2-Base（精准档）：
python scripts/fetch_b3_models.py --model dinov2-base --target <DATA_DIR>/models/dinov2-base
```

## 3. 降级路径（节点实现必须遵循）

```
semantic_precision 档位:
  fast   → 灰度投票粗筛 + 全局 DP（无语义模型，零模型依赖）
  balanced → DINOv2-Small（缺 → Chinese-CLIP；缺 → 灰度回退），report.degraded 记录
  precise → DINOv2-Base（缺 → DINOv2-Small → Chinese-CLIP → 灰度），report.degraded 记录
音频裁决（log-mel + 全局 DP）三层都保留 —— 平台红线：全局 DP + 音频裁决是灵魂，不回退。
```

- 特征磁盘缓存（灰度索引 / CLIP 索引 / log-mel）按路径哈希缓存，二次运行秒级（r2 §5）。
- 模型就绪不阻断节点运行：**至少 fast 档（零模型）可跑通**；balanced/precise 在模型缺失时
  显式降级并在 `report.degraded` / 日志给出 `model_missing` 错误码与放置指引（deps.error_message）。

## 4. fw env-check 联动

`python packaging/fw_cli.py env-check` 现在包含 `dinov2-small` / `dinov2-base` 两项模型检查
与 `model_help:dinov2` 下载指引条目（修复命令为 hf download 三步：下载/放置/接线）。