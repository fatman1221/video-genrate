# 节点式生产流水线 —— 回退 / 审核 / 重新生成

> 交付时间：2026-09-26
> 范围：把「生产进度」从一根进度条升级为可交互的节点图，并新增节点级回退、审核、重生成能力。

## 一、为什么改

原版 Web UI 是一个进度条 + 13 个文字标签页：信息密度高、视觉噪点多，
且「生产进度」只能看不能动 —— 用户看到某一步不满意，只能整项目重跑。

新版本的三个诉求：

1. **视觉简约大气**，减少文字堆砌，用缩略图、图标、环形进度表达信息
2. **生产进度是节点**，不是一个百分比数字
3. **节点可交互**：点进去可查看产出、回退到该步、重新生成该步、审核通过或驳回

## 二、后端新增能力

### 2.1 数据模型

`workflow_steps` 新增 6 列（启动时自动 `ALTER TABLE ADD COLUMN IF NOT EXISTS`，无需手工迁移）：

| 列 | 说明 |
|---|---|
| `review_status` | `NONE` / `APPROVED` / `REJECTED` |
| `reviewed_by` / `reviewed_at` / `review_comment` | 审核人与意见 |
| `rolled_back_at` / `rollback_count` | 回退审计 |

**关键设计**：生产状态（`state`）与审核状态（`review_status`）解耦。
一个节点可以「已生产完成但尚未审核」，也可以「被驳回后重新生产」。

### 2.2 服务层 `services/pipeline.py`

| 能力 | 语义 |
|---|---|
| `node_view` | 12 个节点的完整视图：状态、真实产出比例、产出缩略图、审核结论、可执行动作 |
| `rollback_to` | 回退到某节点：该节点 + 全部下游重置为 `PENDING`；下游产物引用清空（文件保留） |
| `regenerate_stage` | 只重跑某一个节点；默认先清空该节点产物再全量重跑 |
| `review_stage` | 审核通过 / 驳回；驳回可选自动回退 + 立即重跑 |

**下游失效矩阵** `STAGE_DOWNSTREAM`：上游变 ⇒ 下游一律失效。
例如回退到「画面生成」，则 `video / enhancement / editing / composing / quality_check`
全部重置 —— 避免出现「画面换了、视频还是旧的」这种脏状态。

### 2.3 Skill 契约（供 WorkBuddy / Codex 调用）

| Skill | 异步 | 说明 |
|---|---|---|
| `get_pipeline` | 否 | 读节点视图 |
| `rollback_stage` | 否 | 回退 |
| `regenerate_stage` | 是 | 重生成，返回 `taskId` |
| `review_stage` | 否 | 审核 |

Skill 总数 69 → **73**。

### 2.4 REST

`GET /api/projects/{id}/pipeline` —— 节点图数据源（前端 3 秒轮询）。

### 2.5 顺带修掉的真实缺陷

- **合成时内部完成了拼接，但「剪辑」节点没被标记完成** → 节点图出现「有产物却是待执行」的状态漂移。已在 `handle_compose_video` 中补 `complete_step("editing")`。
- **回退后重跑完成时无法前向推进**（`STORYBOARD_GENERATED → IMAGE_GENERATED` 属跳跃迁移被拒）→ `complete_step` 增加 force 自愈。

## 三、前端重构

### 3.1 视觉

- 主题改为「白底 + 紫色点缀」：AppBar 从紫色渐变改为半透明毛玻璃 + 发丝分割线
- 卡片统一 16px 圆角、1px 发丝边框、极轻阴影；正文收敛到 13.5px
- 去掉满屏 Chip 与表格，改用环形进度、缩略图、状态点

### 3.2 节点式生产进度 `PipelineFlow`

- 12 个节点横向排列，环形进度弧表达该节点的真实完成度
- 图标语义化：脚本=文档 / 分镜=网格 / 人物=面部 / 画面=图片 / 视频=胶片 / 配音=麦克风 /
  配乐=音符 / 字幕=字幕条 / 增强=闪光 / 剪辑=剪刀 / 合成=滤镜 / 质检=盾牌
- 状态用配色区分：已完成=紫、进行中=实心紫+脉冲、失败=红、待执行=灰虚线
- 节点右上角审核徽章：✓ 通过 / ! 驳回
- 连接线：前置节点已完成=紫色实线，否则灰色虚线

### 3.3 节点操作面板 `StagePanel`（右侧抽屉）

- **产出**：缩略图墙（视频用所属镜头关键帧当封面，点击可播放）
- **元信息**：耗时 / 尝试次数 / 更新时间
- **审核**：意见输入 + 审核通过 / 驳回（驳回二次确认，可一并回退重做）
- **重新生成**：「全量重跑 / 只补缺」开关 + 一键重跑
- **回退**：二次确认后回到该节点重来

### 3.4 信息架构

13 个文字标签页 → 8 个：概览 / 脚本 / 分镜 / 人物 / 素材 / 任务 / 日志 / 成片。
素材页内部用 ToggleButton 过滤类型，不再为每种素材单开标签。

## 四、验证记录（真实执行）

### 4.1 回退

```
POST /api/skills/rollback_stage/invoke
{"project_id":"proj_5af2...","step_key":"subtitle","reason":"人工审核：字幕断句需要调整"}
→ 重置节点: [subtitle, composing, quality_check]
→ 清空产物: {subtitle: 4}          # 4 个镜头的字幕引用被清空
→ 工作流状态: MUSIC_GENERATED      # 回退到上一个节点已完成的状态
→ 进度: 84% → 80%
```

### 4.2 重新生成

```
POST /api/skills/regenerate_stage/invoke
{"project_id":"proj_5af2...","step_key":"subtitle","reset":true}
→ 提交任务: 1 个（GENERATE_SUBTITLE）
→ 12 秒后：字幕生成节点 SUCCESS 4/4，工作流自愈到 SUBTITLE_GENERATED
```

### 4.3 审核

```
审核通过：{"step_key":"script","decision":"APPROVED","comment":"脚本结构清晰，可用"}
→ review_status=APPROVED, reviewed_by=human

驳回并回退：{"step_key":"music","decision":"REJECTED","comment":"配乐情绪与教学基调不符","auto_rollback":true}
→ review_status=REJECTED（结论在回退后仍然保留）
→ 重置节点: [music, composing, quality_check]
→ 工作流: VOICE_GENERATED，进度 100% → 80%
→ 节点图：脚本生成显示 ✓ 徽章，配乐生成显示 ! 徽章
```

### 4.4 截图

| 文件 | 内容 |
|---|---|
| `docs/screenshots/10_list_new.png` | 卡片式项目列表（封面 + 环形进度 + 节点计数） |
| `docs/screenshots/11_detail_new.png` | 项目详情：节点式生产流水线 |
| `docs/screenshots/12_stage_panel.png` | 节点面板：产出缩略图墙 |
| `docs/screenshots/13_stage_actions.png` | 节点面板：审核 / 重新生成 / 回退 |
| `docs/screenshots/14_review_badges.png` | 审核徽章 + 驳回后的下游失效 |

## 五、已知限制

- 审核结论暂无独立的角色权限体系（`actor` 由 `?actor=` 查询参数决定，Web UI 记为 `human`）
- `node_view` 每次会聚合缩略图，节点较多时建议加缓存（当前 12 节点开销可忽略）
- 「人物一致性」类质检项仍需接入 VLM 才能给出确定结论，目前固定为 WARN
