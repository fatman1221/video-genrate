# 项目待完善清单 · 全量勘察

> 勘察时间：2026-10-06 13:30　范围：`video-genrate` 全工程
> 方法：git 状态 / 测试实跑 / 代码扫描 / 文档比对 / 端口探活。**结论均有一手证据，非推测。**

## 0. 一句话结论

工程主干是健康的（144 用例全绿、124 Skill、29 表、已有成片先例），
**真正的"待完善"集中在三处**：① 大量成果还没进版本库；② 内容生产侧有一批"未达标"待验收；
③ EP001 有 3 个必须拍板的决策卡在门口。工程债都是 README 自己诚实列过的，不紧急。

---

## 一 🔴 立刻要处理（会丢东西 / 会卡住下一步）

| # | 事项 | 证据 | 动作 |
|---|---|---|---|
| 1 | **15 个改动未提交** | `git status` 15 条：5 个新脚本 + 超分工具大改 927 行 + EP001 报告 | 分批 commit。新脚本不进库 = 下次会话/换机即丢 |
| 2 | **妲己 9 图是"野生文件"** | `backend/storage/temp/stills/daji_assets9/deliver/` 有图，DB 里 `characters` 无妲己（8 个角色全属别的项目） | 走 `POST /api/projects/{id}/references` + `set_character_reference` 登记，**不要手写 SQL** |
| 3 | **后端 8077 / 前端 5180 未启动** | 探活：8077 无响应、5180 无响应；ComfyUI 8188 在线（0.37.1） | 常态（会话切换即回收），开工前先探活 |

未提交清单：
```
新增: scripts/gen_daji_assets.py  gen_daji_assets9.py  upscale_stills.py
      scripts/check_asset_colors.py  scripts/_color_baseline_daji.json
      docs/EP001_ARCHITECTURE_CHECK.md
      tools/video-super-resolution/web/{video.html, video_jobs.py, selftest/}
修改: .gitignore  tools/video-super-resolution/{SKILL.md, scripts/upscale_video.py,
      web/index.html, web/server.py, web/start.cmd}
```

## 二 🟡 EP001《赤焰狐妃》—— 3 个决策卡住全部 44 镜

详见 `docs/EP001_ARCHITECTURE_CHECK.md`。这三项**事后改 = 换脸全废**，必须现在定死：

1. **画幅**：16:9（零改动）vs 9:16 竖屏（须改 `minimax_h3_i2v` 模板 node 11 的硬编码 1376×768）
2. **妲己基准图**：`01_front_full`（建议）vs `09_hero_master`
3. **走直接 Prompt** vs Visual Bible + 连续性锁编译层

另有 2 处能力缺口：本地文件入库桥、纵向画幅支持。

## 三 🟡 内容生产侧未达标项（妲己资产，待验收）

来自 10-05 / 10-06 两日实测，**已如实记录但未闭环**：

| 项 | 现状 |
|---|---|
| `05_45°背面` | 狐尾仍越到腿前方，属轻中度遮挡 |
| 九尾质感 | 三版都偏**羽片状**（写死兽尾物理形态 + 负向补 5 条后有改善，**未根治**） |
| 服装东方宫廷感 | 香槟金腰饰 / 云纹刺绣偏弱 —— 根因是 `COSTUME` 定义了但从未拼进 prompt，13:03 已修，**待复验** |
| 眼瞳色 | 出图偏琥珀金，spec 要「琥珀赤」（红调） |
| `06 半身` | 返工 2 次才达标（根因 `prep_upper_crop` 补灰底，已修） |

→ 需要一轮**人眼验收**并给"接受 / 返工"裁定。注意：狐尾"完全展开"是 spec 要求的，
扇形排布本身没问题，问题在**质感像羽毛**。

## 四 🟢 工程债（README §八 已诚实声明 + 本次新增发现）

| 项 | 现状 | 方向 |
|---|---|---|
| `ADD_*` 任务类型 | 枚举声明 20 个，`ADD_VOICE/ADD_MUSIC/ADD_SFX/ADD_SUBTITLE` **有枚举无 handler**（实际 16 类生效） | 实现或从枚举移除 |
| 人物一致性/伪影检测 | 固定 `WARN` + `requires: vision_model` | 接 VLM 做跨镜头比对 |
| Face Enhancement | 算子已定义，未接模型时 `skipped` | 接 GFPGAN / CodeFormer |
| 转场/精细剪辑 | 只有拼接与基础处理 | `EDIT_VIDEO` 加 xfade + 时间线 DSL |
| 认证与多租户 | 单机单用户 | API Key 中间件 + owner 过滤 |
| 成本与配额 | 未统计 | 在 `tasks.result` 记耗时/算力，加预算护栏 |
| `seed` 非独立列 | 存在 `Asset.parameters` JSON 内 | 要可查询就做小增量迁移 |
| **前端只覆盖少数 Skill** | 后端 124 Skill / 8 只读端点，UI **不动态渲染** Skill 列表 → 新能力"有 API 没界面" | 做一个通用 Skill 调用台 |
| **无 CI** | 无 `.github/workflows`，144 用例全靠手跑 | 加最小 CI（pytest 即可，不需 GPU） |
| **README 数字过期** | README 写"140 用例"，实际 **144** 全绿 | 顺手更正 |
| 超分操作台是独立服务 | `tools/video-super-resolution/web/server.py --port 8090`，未并进主前端 | 视需要整合或维持独立 |

## 五 🔴 高危静默失败（已知，但值得再标一次）

这两条会造成"任务 SUCCESS 但结果是假的"，属最贵的一类 bug：

1. **出图 provider 键名必须是 `provider`**（写 `image_provider` → 静默回落 local 占位渲染器）
2. **视频 provider 三级优先级不含 `workflow_name`**：`.env` 里 `DEFAULT_PROVIDER_VIDEO=local`，
   必须显式设 `project.extra.video_provider=comfyui` + `video_workflow_name=minimax_h3_i2v`，
   否则静默走 local Ken Burns 伪视频。

→ 建议：给这两处加**启动期断言/告警**，而不是靠记忆规避。

## 六 本次勘察的量化底数

| 指标 | 数值 |
|---|---|
| 后端测试 | **169 用例，全绿**（不需 GPU；本轮新增 25 条参数回归） |
| Skill 数 | 124 |
| DB 表 | 29 |
| 真实 TaskType handler | 16 |
| services / routers | 21 / 11 |
| ComfyUI 模板 | 5（含 `minimax_h3_i2v`） |
| 待办标记 TODO/FIXME | **0**（代码里没有遗留标记） |
| 工程结构 | `backend/app/{core,skills,services,executors,providers,routers,workflows}` 分层清晰 |

---

## 七 修复进展（持续更新）

### 2026-10-06 14:00 —— 出图参数控制权（本次勘察过程中新发现，已修）

勘察后追加排查"Studio 出图与直连 ComfyUI 不一样"，挖出**两条引擎缺陷**并已修复：

| # | 缺陷 | 修法 |
|---|---|---|
| 1 | **`steps` / `cfg` 不可配**（写死在模板 JSON） | 模板支持 `{{steps}}`/`{{cfg}}` 占位符 + `__meta__.defaults` 兜底；provider 透传；三级优先级：**单次调用 > 项目 `image_steps`/`image_cfg` > 模板默认** |
| 2 | **ComfyUI 真实 seed 不落库**（提交用随机值，`parameters` 里没有 seed） | `actual_seed` 算一次全程复用，同时写入 `GenerationResult.seed` 与 `parameters`。图像 + 视频通道都修了 |
| 3 | 顺带修：`seed=0` 被 `or` 当假值丢掉换成随机种子 | 改用显式 `is not None` 判断 |
| 4 | 顺带修：提示词被**静默注入**前缀，无从查证 | 新增 `inject_scene_prefix: false` 开关；并把 `final_prompt` / `prefix_injected` 落进 `Asset.parameters` |

**验证**：新增 25 条回归用例（`tests/test_sampler_params.py`），全套 **169 用例全绿**；
端到端实拍确认——`steps=28 / cfg=4.0` 确实进到提交给 ComfyUI 的工作流、seed 落库为真实值、
**同参同 seed 两次出图逐字节相同（可复现）**。

> 结论修正：Studio 与直连的差距**不是画质，是控制权**。实测 cfg 4.0 在 20 步下对比度反而
> 最低（σ35.2 < 默认的 40.6）、脸更暗、高光过曝，「cfg 高 = 画质好」不成立。详见
> `docs/COMFYUI_QWEN.md` 的采样参数章节。

### 仍待处理

- 本文件一、二、三、四节列出的其余各项：妲己资产入库桥、EP001 三个决策、各项工程债，
  以及第五节那两条高危静默失败（建议改成启动期断言，别靠人的记性规避）。
