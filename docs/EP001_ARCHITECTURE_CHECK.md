# EP001《狐火》开工前架构勘察报告

> 勘察时间：2026-10-06　勘察人：WorkBuddy（导演/制片/编排 Agent）
> 结论先行：**能直接开工，不需要改架构；但有 3 个必须先定的决策 + 2 处能力缺口。**
> 本报告只做勘察，未创建任何 Episode/Scene/Shot，未提交任何生成任务。

---

## 一、项目实际结构（已验证，非推测）

工程根：`C:/Users/Administrator/WorkBuddy/video-generate/video-genrate`

```
backend/app/
├── core/constants.py        状态枚举 + 工作流状态机 + 阶段依赖表
├── models.py                1014 行，29 张表的 ORM
├── database.py              声明式迁移（无 Alembic）
├── skills/                 7 文件 / 124 个 Skill（base.py 定义契约）
│    ├── content_skills.py     project/script/storyboard/character
│    ├── generation_skills.py  image/video/audio/subtitle/task
│    ├── studio_skills.py      Visual Bible/Look/Location/Prop/Prompt编译/生成计划
│    ├── series_skills.py      series/episode
│    ├── post_skills.py        增强/补帧/拼接/编辑/合成
│    └── pipeline_skills.py    流水线节点推进/回退/审核
├── services/              21 个服务，6789 行（视觉设定·连续性·Prompt编译·计划·质检）
├── executors/             queue.py（DB即队列+worker池+退避重试）+ handlers.py（16 真实 handler）
├── providers/             image/video/audio/enhance/processing/subtitle/browser 抽象层
└── workflows/templates/   5 个 ComfyUI API 工作流模板（含 minimax_h3_i2v）
```

- 后端端口 **8077**，前端 **5180**，DB `backend/video_agent_studio.db`（SQLite）
- **当前后端未启动**；**ComfyUI 已启动**（`0.37.1`，队列空闲）
- 已有真实成片先例：`归途信号`（33 镜头，图 33/33、视频 33/33，`workflow_state=COMPLETED`）

---

## 二、可以直接复用（不用写一行新代码）

| 你的需求 | 项目已有能力 | 载体 |
|---|---|---|
| 创建 Episode（含集号/集间隔离） | `create_series` → `create_episode` | `series_skills.py` |
| 创建 6 个 Scene | `create_storyboard`（传 `scenes[]`，每项可带 `code`） | `content_skills.py` |
| 创建 44 个 Shot | 同上 `scenes[].shots[]`，支持 `code/camera/duration/…` | `content_skills.py` |
| 角色资产 | `create_character` / `update_character` / `generate_character_reference` | `content_skills.py` |
| 角色基准图锚定 | `set_character_reference`（把候选图提升为基准，之后全镜头以它做一致性锚点） | `content_skills.py` |
| **图片生成（Qwen-Image 2.1）** | `generate_image` → ComfyUI 模板 `qwen_image_scene` / `qwen_image_character` / `qwen_edit_scene` | `generation_skills.py` + 模板已确认 `qwen_image_2.1_int8_convrot.safetensors` |
| **视频生成（MiniMax-H3 i2v）** | `generate_video` → 模板 `minimax_h3_i2v`（首帧驱动，turbo 4-step，24fps，1376×768，5~15s） | 同上 |
| 单镜头重跑 | `regenerate_image` / `regenerate_video`（不动其它镜头，旧素材留档可对比） | `generation_skills.py` |
| 批量出图/出片 | `generate_all_images` / `generate_all_videos`（只补缺失项） | `generation_skills.py` |
| TTS 配音 | `generate_voice` / `regenerate_voice` / `list_voices` / `generate_standalone_voice` | `generation_skills.py` |
| BGM / SFX | `generate_music` / `generate_sfx` | `generation_skills.py` |
| 字幕 | `generate_subtitle`（按镜头时间轴出 SRT/ASS/VTT） | `generation_skills.py` |
| 音画合成 | `compose_video`（`COMPOSE_VIDEO` handler 已实测产出 H.264+AAC） | `post_skills.py` |
| 质量检查 + 自动修复闭环 | `run_quality_check(auto_repair=true)`：定位失败项→只重生成相关镜头→重合成 | `post_skills.py` + `handlers._auto_repair` |
| 节点审核（人工/Agent 闸门） | `review_stage` / `rollback_stage` / `regenerate_stage` | `pipeline_skills.py` |
| 任务状态机/重试 | 指数退避 60→600s、不可重试错误直挂 FAILED、启动 `reclaim_stale()` 断点恢复 | `executors/queue.py` |
| 提示词编译（设定→版本，只增不改） | `compile_image_prompt` / `compile_video_prompt` + `get_prompt_provenance` | `studio_skills.py` |
| 生成前预览+显式确认 | `create_generation_plan` → `preview_generation_plan` → `confirm_generation_plan` → `materialize_generation_plan` | `studio_skills.py` |
| 连续性锁 / 增量 | `create_continuity_lock` / `verify_continuity_locks` / `create_continuity_delta` | `studio_skills.py` |

**124 个 Skill 全部走同一入口** `POST /api/skills/{name}/invoke`，返回信封 `{ok,status,data,error,task_id}`。
生成类 Skill **全异步**：拿 `task_id` → 轮询 `get_task_status`。

---

## 三、⭐ 重大发现：妲己资产已经存在（但未入库）

上一轮工作已经在磁盘上产出了**两套完整妲己标准资产**，质量达到可用级：

**A. `backend/storage/temp/stills/daji_assets9/deliver/`（推荐，9 图，1536×2304）**

| 文件 | 内容 |
|---|---|
| `01_front_full.png` | 正面全身 Master Reference |
| `02_left_profile.png` | 左侧面 90° |
| `03_back_full.png` | 背面 |
| `04_front45.png` | 45° 正面 |
| `05_back45.png` | 45° 背面（回眸） |
| `06_bust_portrait.png` | 电影级半身肖像 |
| `07_dance_pose.png` | 东方古典舞动态 |
| `08_battle_state.png` | **战斗状态** ← 打戏直接可用 |
| `09_hero_master.png` | 终版 Hero Master |
| `00_全览_sheet.png` | 全览对照版式图 |

**B. `backend/storage/temp/stills/daji_assets/`（8 图，更早一版，含 `07_pose_reference` / `08_hero_poster`）**

设定已逐项写死在 `scripts/gen_daji_assets9.py` 的 `COSTUME` / `FACE` 常量里：
朱红绛红长裙 + 露肩收腰高开衩 + 香槟金腰饰与狐纹刺绣 + 深栗红长发 + 琥珀赤瞳 + **九条**象牙白狐尾（尾尖暖金渐变）。

⚠️ **但它们是"野生文件"，数据库里没有对应的 `characters` / `assets` 行** ——
当前库里 8 个角色全是别的项目（小林/林知夏/陈默/苏晚/珂/派派/宠物店老板/林野），**没有妲己**。

→ **需要补一个"本地文件 → Character + Asset 登记"的桥**。已有端点可复用：
`POST /api/projects/{project_id}/references`（multipart 上传参考图）。
最干净的做法是走这个端点上传承为 `Character.reference_asset_id`，而不是手写 SQL 插库。

---

## 四、需要补充的能力（2 处）

| # | 缺口 | 现状 | 建议落法（不新建独立系统） |
|---|---|---|---|
| 1 | **本地文件入库桥** | 妲己 9 图在磁盘上，DB 无记录 | 复用 `POST /api/projects/{id}/references` 端点 + `set_character_reference`。若批量导入需要脚本化，建议在 `scripts/` 加一个薄脚本只调用现有 HTTP 接口，**不碰 DB**。 |
| 2 | **纵向画幅支持** | `minimax_h3_i2v` 模板把 `width/height` **硬编码为 1376×768**（node 11），且 handler 注释明确"视频尺寸不从 payload 取" | 若要 9:16 竖屏短剧，必须把模板 node 11 的 `width/height` 改成 `{{width}}` / `{{height}}` 占位符（属**工作流模板**改动，不是架构改动），并确认 H3 对竖屏尺寸的兼容性。**保持 16:9 则零改动。** |

### 其他非阻塞项（可后续再说）

| 项 | 说明 |
|---|---|
| `seed` 不是独立列 | 存在 `Asset.parameters` JSON 内。若要可查询的 seed 列，属小增量迁移。 |
| `ADD_VOICE/ADD_MUSIC/ADD_SFX/ADD_SUBTITLE` | `TaskType` 有枚举但**无 handler**（README 已诚实标注）。实际生效 16 类，够用——合成走 `compose_video`。 |
| 人物一致性自动判定 | `run_quality_check` 对"人物脸/服装是否漂移"固定返回 `WARN` + `requires: vision_model`，**设计上交给 Agent 的多模态能力补**。→ 这正好是我的职责，我会逐张肉眼核验。 |
| BGM 精编 | `generate_music` 是简版；`scripts/gen_bgm.py` / `prepare_bgm.py` 是手工编曲版（交叉淡化/旁白闪避）。若第一集 BGM 要求高，走后者。 |
| 成片时长对齐 | 合成收尾用 `-t <片长>`，**不要 `-shortest`/`apad`**（历史坑）。 |
| 打戏 | **没有专门能力**。i2v 是首帧驱动，一次调用只能给一段运动。→ 打戏 = 靠 44 个短镜头硬切 + 每镜 motion prompt，这正是你要的分镜结构。 |

---

## 五、你要的 9 个 Shot 状态 → 现有状态机映射

**结论：不需要改架构，现有字段可以一一映射。**（`shot.status` + 4 个分模态状态 + `Task` 状态 + `WorkflowStep.review_status`）

| 你要的状态 | 现有落点 |
|---|---|
| `PENDING` | `shot.status = PENDING` ✅ 原生 |
| `IMAGE_GENERATING` | `shot.image_status = GENERATING` + `Task(type=GENERATE_IMAGE, status=RUNNING)` ✅ |
| `IMAGE_REVIEW` | `Task SUCCESS` + `shot.image_status=READY` + `WorkflowStep(step_key="image").review_status = PENDING` ✅ 原生审核闸门 |
| `IMAGE_READY` | `shot.status = IMAGE_READY` ✅ 原生 |
| `VIDEO_GENERATING` | `shot.video_status = GENERATING` + `Task(type=GENERATE_VIDEO)` ✅ |
| `VIDEO_READY` | `shot.status = VIDEO_READY` ✅ 原生 |
| `FAILED` | `shot.status = FAILED` / `Task.status = FAILED` ✅ 原生 |
| `NEEDS_RETRY` | `Task.status = RETRYING` + `shot.retry_count > 0`；不可自动重试时由我显式 `retry_task` ✅ |
| `COMPLETED` | `shot.status = READY`（图+视频+配音齐备时 handler 自动置位）✅ |

> **一个诚实的偏差**：现有 `ShotStatus` 没有 `IMAGE_REVIEW` / `NEEDS_RETRY` / `COMPLETED` 三个字面值。
> 我不建议为了对齐字面值去改 `constants.py`（会牵动 1945 条 QualityCheck、301 个 Task 的既有数据）。
> 建议：**这 9 个状态作为我对外汇报的语义层**，落库仍用现有字段；映射关系记在本报告里，
> 需要时在 `shot.extra["ep_state"]` 写一份投影，不改表结构。

---

## 六、你要的落库字段 → 现有字段映射

| 你要的字段 | 现有落点 | 备注 |
|---|---|---|
| `episode_id` | `Project.id`（挂 `series_id` + `episode_no=1`） | Episode 就是一个 Project，天然复用整条流水线 |
| `scene_id` | `Scene.id` ✅ | |
| `shot_id` | `Shot.id` ✅ | |
| `character_id` | `Character.id` / `Shot.character_ids` / `Asset.character_id` ✅ | ⚠️ `character_ids` 存的是**角色 id 不是名字** |
| `asset_id` | `Asset.id` ✅ | |
| `model` | `Asset.model` ✅ | |
| `workflow` | `Asset.workflow` ✅ | 带 `prompt_id` 可反查 ComfyUI 历史 |
| `prompt` | `Asset.prompt` / `PromptVersion.raw_prompt`+`compiled_prompt` ✅ | |
| `negative_prompt` | `Asset.negative_prompt` ✅ | |
| `seed` | `Asset.parameters["seed"]` ⚠️ | 非独立列 |
| `reference_image` | `Asset.parent_asset_id` / `PromptVersion.reference_assets[]` ✅ | |
| `input_image` | `Asset.parent_asset_id`（i2v 输入帧 = `shot.image_asset_id`） ✅ | |
| `output_image` | `Shot.image_asset_id` → `Asset.url/file_path` ✅ | |
| `output_video` | `Shot.video_asset_id` → `Asset.url/file_path` ✅ | |
| `duration` | `Shot.duration` / `Asset.duration` ✅ | |
| `status` | `Shot.status` / `Asset.status` / `Task.status` ✅ | |
| `retry_count` | `Shot.retry_count` / `Task.attempts` ✅ | |
| `error_message` | `Shot.last_error` / `Task.error`+`error_detail` ✅ | |
| `created_at` / `updated_at` | `TimestampMixin`（所有 29 张表都有） ✅ | |

**结论：20 个字段里 19 个有原生落点，只有 `seed` 在 JSON 内。**

---

## 七、你要的 7 类 Prompt → 现有字段映射

| 你要的 Prompt | 现有字段 | 说明 |
|---|---|---|
| `image_prompt` | `Shot.image_prompt` ✅ 原生列 | |
| `video_prompt` | `Shot.video_prompt` ✅ 原生列 | handler 优先用编译版本，缺则回落此列 |
| `negative_prompt` | `Shot.negative_prompt` ✅ 原生列 | |
| `camera_prompt` | `Shot.camera` ✅（`String(200)`） | 也可写进 `extra.camera_prompt` 保留完整长文 |
| `style_prompt` | `Shot.visual_style` ✅（`String(200)`） | |
| `dialogue` | `Shot.voice_script` + `Shot.subtitle_text` ✅ | 画面字幕用 `subtitle_text` |
| `audio_prompt` | `Shot.voice_instruct` + `Shot.voice_speaker` ✅ | 情感/语气指令 + 音色代号 |
| **`action_prompt`** | ⚠️ **无原生列** → 走 `Shot.extra["action_prompt"]` | 唯一真缺口；不塞进 video_prompt 是为了让"运动描述"可独立编辑与重跑 |

> 补充：`voice_speaker` 是**角色代号**（如 `M` / `AI`），必须经顶层 `voice_cast` 映射到真实音色；
> 规格 JSON 是配音的唯一事实来源。另有 9 个音色可选，`list_voices` 可查。

---

## 八、⚠️ 三个必须先定的决策（会影响全部 44 个镜头，事后改 = 全废）

### 决策 1：画幅 —— 16:9 横屏 还是 9:16 竖屏短剧？

| 选项 | 影响 |
|---|---|
| **A. 16:9 / 1280×720（推荐）** | 零改动，与项目默认、`归途信号` 一致，i2v 模板直接能用。缺点是短剧平台多为竖屏。 |
| **B. 9:16 / 720×1280 竖屏** | 需先改 `minimax_h3_i2v` 模板的 `width/height` 占位符，并验证 H3 竖屏出片。多一步验证成本。 |

⚠️ 记住历史教训：**出图分辨率一旦定下，中途改 = 换脸，33 镜 + 33 视频全废（3.3h GPU）**。
所以这一项必须现在定死。

### 决策 2：妲己基准参考图用哪一张？

- 候选 A：`01_front_full.png`（正面全身，最标准的锚点，视角最中性）
- 候选 B：`09_hero_master.png`（终版 Hero Master，气场最强但姿态带戏）
- 我的建议：**用 A 做 `Character.reference_asset_id`（身份锚点），把 `09` 和 `08_battle_state` 作为额外参考槽位**（"能控制/不得控制"字段在 `PromptVersion.reference_assets[]` 里原生支持）。

### 决策 3：走"直接 Prompt"还是走"Visual Bible 编译层"？

| 选项 | 成本 | 收益 |
|---|---|---|
| **A. 直接 `Shot.image_prompt`（推荐第一集）** | 零学习成本，`归途信号` 已验证可行 | 快；但没有版本血缘与锁面校验 |
| **B. Visual Bible + 连续性锁 + `compile_image_prompt`** | 需先建 Bible / Style / Look / Location / 若干 Lock | 44 镜头的**角色/服装/场景一致性由机器强制**（锁面必须逐字出现在提示词里，缺了就编译失败）——这对"绝对不要每个镜头重新生成一个妲己"这条硬需求是最强的保障 |

> 我的倾向：**A + 局部 B** —— 先用直接 Prompt 快速验证 3 个 Hero Shot 的可行性，
> 通过后把验证结论固化成 Visual Bible + 3 条连续性锁（狐尾数/服装配色/发色），
> 再批量推进剩余 41 镜。这样既不阻塞，又拿到一致性保障。

---

## 九、风险与成本（诚实预估）

| 风险 | 说明 | 对策 |
|---|---|---|
| **ComfyUI 是共享资源** | 既有全局 `POST /interrupt`，也会把别人的 pending 任务清出队列。`gen_still.wait()` 会傻等到超时 | 必须用**丢失检测**（连续 4 次轮询既不在 queue 也不在 history → 判定被删 → 重投）；参考 `scripts/gen_daji_assets.wait_robust()` |
| **GPU 时长** | i2v 约 **6 分钟/条**（流式采样），44 条 ≈ **4.4 小时**；首次跑建议先验证 3 条的英雄镜头 | 分批推进；英雄镜头先跑，验证后再批量 |
| **进度长停 57% 是正常的** | i2v 历史现象，不要误判为卡死 | 已在记忆里，不会盲目重投 |
| **后端重启** | 改 `handlers.py` / `models.py` / 工作流模板后**必须重启后端** | 本轮不动代码，无此风险 |
| **state 丢失** | 我的后台服务在会话切换后会被回收 | 每次开工先探活 8077 / 8188 |

---

## 十、建议的下一步（严格按你给的 15 步顺序）

我可以立刻执行、且**不启动大规模生成**的前 5 步：

1. 启动后端（8077），探活 ComfyUI（8188）
2. `create_series`（《赤焰狐妃》）→ `create_episode`（EP001「狐火」）
3. `create_storyboard` 一次性落 6 Scene + 44 Shot（含 7 类 Prompt 全字段）
4. 用 `POST /api/projects/{id}/references` 把已有 9 张妲己图入库 → `create_character`（妲己）+ `set_character_reference`
5. `create_character` 补 猎妖人首领、白面具男人；`create_prop` 补 玉佩

然后**停下**，等你确认决策 1/2/3，再生成 Shot 004 → 030 → 040 三个英雄镜头并逐张核验。

---

**我没有做的事（也是刻意的）**：没有新建任何独立的视频生成脚本，没有绕过 Skill 直连 ComfyUI/MiniMax，
没有改任何 `models.py` / `handlers.py` / `constants.py`。全部走 `POST /api/skills/{name}/invoke`。
