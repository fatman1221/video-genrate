# 架构与扩展指南

> 本文面向要**接入新模型 / 新 Agent / 新执行方式**的开发者。
> 阅读顺序建议：分层模型 → 数据流 → 扩展点 → 失败自愈 → 验收。

---

## 一、分层模型

```
┌──────────────────────────────────────────────────────────────────────┐
│ L1  Agent 层        WorkBuddy / Codex / 未来其它 Agent                │
│                     职责：理解需求、规划、决策、观察、修正            │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ 唯一入口：/api/skills（JSON Schema 契约）
┌───────────────────────────▼──────────────────────────────────────────┐
│ L2  Skill 层        backend/app/skills                                │
│                     124 个 Skill，按 project/script/storyboard/       │
│                     series/character/image/video/audio/subtitle/      │
│                     processing/quality/workflow/asset/task/log/       │
│                     provider/browser/orchestration/visual/plan 分类   │
│                     职责：参数校验、权限/确认、编排、返回统一结构      │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ 视觉设定 / 连续性 / Prompt 编译 / 生成计划
┌───────────────────────────▼──────────────────────────────────────────┐
│ L2.5 创作服务层     backend/app/services                              │
│                     visual_bible · continuity · prompt_compiler ·     │
│                     provenance · generation_plans · resolution        │
│                     职责：把「设定」编译成「提示词版本」，            │
│                     并保证每次生成都有可反查的血缘与不花钱的预览闸门  │
│                     （用法见 docs/STUDIO_CAPABILITIES.md）            │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ 提交 Task（异步）或直接返回数据（同步）
┌───────────────────────────▼──────────────────────────────────────────┐
│ L3  执行层          backend/app/executors                             │
│                     queue.py   DB 即队列 + worker 池 + 重试 + 恢复    │
│                     handlers.py 16 类 TaskType 的真实执行逻辑         │
│                     职责：调度、进度回写、错误分类、幂等              │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ 只调用抽象接口
┌───────────────────────────▼──────────────────────────────────────────┐
│ L4  Provider 层     backend/app/providers                             │
│                     image / video / tts / music / sfx / subtitle /    │
│                     enhance / processing / browser                    │
│                     职责：屏蔽底层差异（ComfyUI / 云 API / ffmpeg）   │
└───────────────────────────┬──────────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────────┐
│ L5  引擎层          ffmpeg · PIL · macOS say · ComfyUI · 云端模型     │
└──────────────────────────────────────────────────────────────────────┘
```

**关键约束**：上层可以依赖下层，下层**绝不**反向依赖。`providers/` 不 import `skills/`，
`executors/` 不 import 具体 Provider 实现（只通过 `registry.get(kind, name)`）。

---

## 二、一次完整生成的数据流

以"为 Shot 003 生成视频"为例：

```
Agent: POST /api/skills/generate_video/invoke  {shot_id: "shot_03"}
  │
  ├─ L2 Skill.generate_video
  │     └─ tasks.create_task(type=GENERATE_VIDEO, shot_id=shot_03, payload={provider, duration, motion})
  │        └─ 返回 { ok:true, task_id:"task_xx", status:"ACCEPTED" }   ← 立即返回，不阻塞
  │
  └─ L3 worker 线程领取该任务（SELECT ... FOR UPDATE SKIP LOCKED）
        ├─ 读取 Shot 与关键帧（缺失则先走 _ensure_shot_image）
        ├─ registry.get("video", provider).generate(prompt, image_path, ...)
        │     └─ L4 LocalVideoProvider → L5 ffmpeg(zoompan 运镜) → 真实 mp4
        ├─ assets.ingest_result(...)  → 落盘 + 建 Asset（含 prompt/model/provider/workflow/parameters）
        ├─ shot.video_asset_id = asset.id；shot.status = READY
        ├─ agent_logs 写入 video.started / video.finished
        └─ 若全部 Shot 都有视频 → workflow.complete_step("video") → 状态推进 VIDEO_GENERATED
```

Agent 侧只需：

```bash
agent_cli.py call generate_video '{"shot_id":"shot_03"}'
agent_cli.py status task_xx          # 反复轮询，或
agent_cli.py watch proj_xx           # 一直盯到全部完成
```

---

## 三、扩展点

### 3.1 接入一个新的图像/视频模型（例如自建 ComfyUI 工作流）

1. 在 `backend/.env` 配置：
   ```
   COMFYUI_BASE_URL=http://127.0.0.1:8188
   COMFYUI_API_KEY=            # 如需要
   ```
2. 在 ComfyUI 中导出 **API Format** 工作流 JSON，把可变字段写成占位符：
   ```json
   { "6": { "inputs": { "text": "{{prompt}}", "seed": "{{seed}}" } },
     "5": { "inputs": { "width": "{{width}}", "height": "{{height}}" } } }
   ```
3. 调用时带上 `parameters.workflow_json`：
   ```bash
   agent_cli.py call generate_video '{"shot_id":"shot_03","provider":"comfyui",
        "parameters":{"workflow_json":{...},"ckpt_name":"wan2.1.safetensors"}}'
   ```
   Provider 会自动完成 `POST /prompt` → 轮询 `/history/{id}` → 下载 `/view`，并把
   `prompt_id`、工作流名、模型名一并写入 Asset 元数据（可追溯）。

### 3.2 接入一个全新的能力类别（例如"语音克隆"）

```python
# 1) providers/base.py 里加接口
class VoiceCloneProvider(Provider):
    kind = "voice_clone"
    @abstractmethod
    def clone(self, *, sample_path: str, text: str, **kw) -> GenerationResult: ...

# 2) 新建 providers/voiceclone_providers.py 实现并在 register() 中注册
def register() -> None:
    registry.register(MyVoiceCloneProvider(), default=True)

# 3) providers/__init__.py 的 _MODULES 中加入该模块

# 4) schemas：在 skills/generation_skills.py 加一个 @skill
@skill(name="clone_voice", category="audio", is_async=True, description="...",
       input_schema={...}, output_schema={...})
def clone_voice(ctx, *, project_id, sample_asset_id, text):
    return tasks_svc.create_task(ctx.db, project_id=project_id, type="VOICE_CLONE",
                                 name="语音克隆", payload={...})

# 5) executors/handlers.py 加 handler
@task_handler("VOICE_CLONE")
def handle_voice_clone(ctx): ...
```

**新增能力全程不改动已有代码**：`/api/skills` 自动出现新 Skill，`agent_cli.py` 也立刻能调。

> ⚠️ **别把"自动可用"理解成"UI 里有界面"**。Web UI 的工作台 / 项目详情 / 资产库 / 系统设置
> 都是**面向既有流程的固定页面**，不会动态渲染 Skill 列表 —— 顶栏那个图标只是把
> `/api/skills` 的原始 JSON 开在新窗口。**纯 API 的新能力在 UI 里是看不见的**，
> 需要人观察 / 干预的话，得另外补页面。

### 3.3 接入新的 Agent（不止 WorkBuddy）

Skill 层是纯 HTTP + JSON Schema，任何能发请求的 Agent 都能用：

- 通用 LLM Agent：把 `/api/skills?detail=true` 的结果塞进 system prompt（工具定义），
  用 function calling 直接映射到 `POST /api/skills/{name}/invoke`
- 传统工作流引擎：把 `invoke` 当作 HTTP 节点
- 自研脚本：直接用 `agent_cli.py`

### 3.4 切换存储后端

`storage.py` 已定义 `StorageBackend` 抽象。实现 `S3Storage`（骨架已就位）后，
把 `STORAGE_BACKEND=s3` 与对应凭据写入 `.env` 即可，业务层无需改动。

---

## 四、失败重试与自愈

### 4.1 任务级

| 机制 | 实现 |
|------|------|
| 指数退避重试 | `attempts < max_attempts` 时置 `RETRYING`，`_retry_after` 到点由 `revive_retrying` 放回队列（退避 60s→120s→…上限 600s） |
| 不可重试错误 | Provider 抛 `ProviderError(retryable=False)` 时直接 `FAILED`，避免无效重试 |
| 取消 | 写入 `payload._cancel_requested`，handler 在 `progress()` 中检查并抛 `TaskCancelled` |
| 进度 | `-progress pipe:1` 解析 ffmpeg 真实进度，逐段回写 DB，Web UI 实时可见 |
| 断点恢复 | 服务启动时 `reclaim_stale()` 把超过 30 分钟仍为 RUNNING 的任务放回队列 |

### 4.2 项目级（质检 → 修复闭环）

`run_quality_check(auto_repair=true)` 的执行路径：

```
逐 Shot 检查（可播放 / 时长偏差 / 人物一致性需 VLM 时标 WARN）
项目级检查（视频完整 / 音频完整 / 字幕完整 / 分辨率 / 帧率 / 全部 Shot 完成 / 无失败任务 / 时长）
   ↓ 有 FAIL
workflow: FAILED → ANALYZE → REGENERATE
   ↓ _auto_repair()
定位失败项 → 只重置相关产物：
   shot_video_* → 清空该 shot 的 video_asset_id 并提交单个 GENERATE_VIDEO（不动其它镜头）
   all_shots_done → 只为缺失镜头补任务
   no_failed_tasks → 批量 retry 失败任务（reset_attempts）
   video/audio/resolution/duration → 提交 COMPOSE_VIDEO 重新合成
   ↓
重新合成 → 自动再次质检（循环收敛）
```

**设计取舍（诚实说明）**："人物一致性""明显伪影"这类判断需要视觉模型。当前实现**不假装通过**，
而是返回 `WARN` 并注明 `requires: vision_model`，交由 Agent 用自身多模态能力补上这一步。
这是有意为之的边界声明，而不是遗漏。

---

## 五、验收与自测

```bash
# 1) 引擎健康
curl -s http://127.0.0.1:8077/api/health | python3 -m json.tool

# 2) 完整链路（真实产出 mp4 / mp3 / srt）
env -u PYTHONPATH python scripts/e2e_check.py 20 5     # 或：unset PYTHONPATH && python scripts/e2e_check.py 20 5

# 3) Skill 契约可被 Agent 直接消费
curl -s 'http://127.0.0.1:8077/api/skills?detail=true' | python3 -m json.tool | head -60

# 4) Web UI
open http://127.0.0.1:5180
```

---

## 六、已知边界与下一步

| 项 | 现状 | 建议下一步 |
|----|------|-----------|
| ComfyUI / 云端 Provider | 适配器完整，未配置 endpoints | 填 `.env` 即可启用真实模型 |
| Face Enhancement | 算子已定义，未接模型时明确 `skipped` | 接 GFPGAN / CodeFormer 作为独立 enhance provider |
| 浏览器自动化 | Backend 只登记任务 + playbook | 由 Agent 执行后调用 `complete_browser_task` 回写产物 |
| 认证与多租户 | 单机单用户 | 加 API Key 中间件 + `owner` 过滤 |
| 转场 / 精细剪辑 | 仅拼接与基础处理 | 增加 `EDIT_VIDEO` 的转场算子（xfade）与时间线 DSL |
| 成本与配额 | 未统计 | 在 `tasks.result` 记录耗时与 token/算力消耗，做预算护栏 |
