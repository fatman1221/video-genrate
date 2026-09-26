# AI Video Agent Studio

工程化的 **AI 视频生产基础设施**：不是又一个视频编辑器，而是能被 WorkBuddy / Codex 等 Agent 调用的
**工具箱 / Skill Server / 执行平台**。

```
WorkBuddy / Codex   ──思考、规划、决策、调用 Skill──►  本平台
                                                       ├── Backend  状态 / 数据 / 执行能力
                                                       ├── Web UI   人类控制台
                                                       └── Engines  ffmpeg / PIL / say / ComfyUI / 云端 API
```

> **验收状态：已通过**（2026-09-26）。端到端 23 个任务 0 失败，产出 20.00s / 1280×720 /
> H.264+AAC 真实成片，工作流到达 `COMPLETED`。
> 详见 [docs/VERIFICATION.md](docs/VERIFICATION.md)，截图在 `docs/screenshots/`。
> 一键启动：`./scripts/dev.sh`；一键验收：`python scripts/e2e_check.py 20 5`。
> **项目介绍页**：直接用浏览器打开 `intro.html` —— 单文件、零外部依赖（截图已 base64 内嵌），
> 视觉沿用 Web UI 的紫主题。改了截图后跑 `python scripts/build_intro.py` 重新生成；
> 要出整页长图跑 `./scripts/shot_intro.sh`（默认 1.5x，产物 `intro-full.png`）。
>
> **节点式生产流水线**（2026-09-26 新增）：生产进度是一个可交互的节点图 ——
> 点开任意节点可查看产出、**回退到该步**、**重新生成该步**、**审核通过 / 驳回**。
> 上游变更会自动让下游失效，避免半新半旧的脏状态。详见
> [docs/PIPELINE_NODES.md](docs/PIPELINE_NODES.md)。
>
> **连续剧分层**（2026-09-26 新增）：**一部剧 → 多集 → 每集一条完整流水线**。
> 一集即一个 Project，因此每集的节点回退 / 审核 / 重生成 / 成片全部复用现成能力；
> 角色建在系列层可**跨集复用**，保证主角不换集换脸。详见
> [docs/SERIES.md](docs/SERIES.md)。

核心原则（与需求一一对应）：

| # | 原则 | 落地位置 |
|---|------|----------|
| 1 | Agent 负责思考，Backend 负责状态与执行 | `backend/app/skills`（契约）+ `executors`（执行） |
| 2 | 业务逻辑不写死在 Prompt 里 | 全部能力抽象为 84 个 Skill，见 `/api/skills` |
| 3 | 每个 Skill 有清晰输入 / 输出 / 状态 / 错误 | `skills/base.py` JSON Schema 校验 + 统一返回结构 |
| 4 | 所有生成任务可追踪 | `tasks` 表：payload / result / logs / attempts / error |
| 5 | 素材与 Project / Scene / Shot 关联 | `assets` 表 + `Asset Center` API |
| 6 | 每个产物保存 Prompt / Model / 参数 / Workflow | `assets.prompt / model / provider / workflow / parameters` |
| 7 | 单镜头失败只重跑该镜头 | `shot.shot_id` 是重试最小粒度（`regenerate_video` 等） |
| 8 | 失败重试 / 断点恢复 | 任务指数退避重试 + `reclaim_stale` 回收 + `resume_project` |
| 9 | API > CLI > 浏览器 | Provider 优先级设计，浏览器只做任务登记与 playbook |
| 10 | 可换模型、可换 Agent | Provider 抽象层 + Skill 契约层，两层解耦 |

---

## 一、目录结构

```
video-skill/
├── backend/
│   ├── app/
│   │   ├── main.py                 FastAPI 入口（lifespan 启动 worker）
│   │   ├── config.py               全部配置（env / .env 可覆盖）
│   │   ├── database.py             SQLAlchemy 连接（PostgreSQL / SQLite 双支持）
│   │   ├── models.py               15 张核心表
│   │   ├── storage.py              存储抽象（local / S3 / OSS / MinIO）
│   │   ├── core/constants.py       状态枚举 + Workflow 状态机
│   │   ├── providers/              ★ 执行引擎抽象层
│   │   │   ├── base.py             Provider 接口 + 注册表
│   │   │   ├── local_engine.py     ffmpeg / PIL / say 底层封装
│   │   │   ├── image_providers.py  local / comfyui / cloud
│   │   │   ├── video_providers.py  local / comfyui / cloud
│   │   │   ├── audio_providers.py  TTS / 音乐 / 音效
│   │   │   ├── subtitle_providers.py
│   │   │   ├── enhance_providers.py
│   │   │   ├── processing_providers.py
│   │   │   └── browser_providers.py
│   │   ├── skills/                 ★ Agent 调用契约层（84 个 Skill）
│   │   │   ├── base.py             Skill / Registry / JSON Schema 校验
│   │   │   ├── content_skills.py   Project / Script / Storyboard / Shot / Character
│   │   │   ├── series_skills.py    连续剧：Series / Episode / 系列级角色
│   │   │   ├── generation_skills.py Image / Video / Voice / Music / SFX / Subtitle / Task
│   │   │   └── post_skills.py      处理 / 合成 / 质检 / 工作流 / 素材 / 日志 / Provider / 浏览器 / 编排
│   │   ├── executors/              ★ 异步任务执行
│   │   │   ├── queue.py            DB 即队列 + worker 池 + 重试 + 断点恢复
│   │   │   └── handlers.py         20 类任务的真实执行逻辑
│   │   ├── services/               业务服务（assets / workflow / projects / series / characters / quality / planner / tasks）
│   │   └── routers/                REST 端点（projects / series / content / assets / tasks / skills / system）
│   ├── storage/                    文件存储根目录（图片 / 视频 / 音频 / 字幕）
│   ├── agent_cli.py                ★ 给 Agent 用的 CLI
│   └── requirements.txt
├── frontend/                       React 18 + Vite + MUI（紫色主题）
│   └── src/{pages,components}      项目列表 / 项目详情 / 连续剧列表 / 连续剧详情
├── scripts/
│   ├── e2e_check.py                端到端链路验证脚本
│   ├── build_intro.py              介绍页构建（截图裁切 + base64 内嵌）
│   ├── shot_intro.sh               介绍页整页长截图（1.5x 像素密度）
│   └── dev.sh                      一键启动后端 + 前端
├── intro.html                      ★ 项目介绍页（单文件、零外部依赖、紫主题）
├── intro-full.png                  介绍页整页长截图（1920×9142）
└── docs/                           架构 / 验证 / 节点流水线 / 连续剧分层
```

---

## 二、快速开始

### 1. 启动后端

```bash
cd backend
# 依赖（首次）
/Users/zhangdongke/.workbuddy/binaries/python/envs/default/bin/pip install -r requirements.txt

# 启动（注意 env -u PYTHONPATH，规避本机 WorkBuddy shim 对 os.mkdir 的劫持）
env -u PYTHONPATH /Users/zhangdongke/.workbuddy/binaries/python/envs/default/bin/python \
  -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```

默认连接 PostgreSQL（`video_agent_studio`）。若要零依赖运行，设环境变量：

```bash
export DATABASE_URL="sqlite:///./video_agent_studio.db"
```

### 2. 启动前端

```bash
cd frontend
/Users/zhangdongke/.workbuddy/binaries/node/versions/22.22.2-3/bin/npm run dev
# http://127.0.0.1:5180
```

### 3. 一句话验证整条链路

```bash
env -u PYTHONPATH python scripts/e2e_check.py 20 5   # 20 秒成片 / 单镜头 5 秒
```

---

## 三、Agent 如何调用

Agent 只需要三步：**看清单 → 看契约 → 调用**。

```bash
export STUDIO=http://127.0.0.1:8077
CLI="env -u PYTHONPATH python backend/agent_cli.py"

# 1) 列出能力
$CLI skills --category video

# 2) 查看某个 Skill 的输入输出契约
$CLI describe generate_video

# 3) 一键搭起项目（Agent 可随后改写脚本/分镜）
$CLI call bootstrap_project '{"name":"AI Agent 教学视频","requirement":"制作一个 5 分钟的 AI Agent 教学视频，漫画教学风格","target_duration":300}'

# 4) 推进流水线并盯着它跑完
$CLI pipeline proj_xxxxx --watch
```

也可以直接走 HTTP：

```bash
# 清单
curl http://127.0.0.1:8077/api/skills
# 调用
curl -X POST http://127.0.0.1:8077/api/skills/generate_video/invoke \
     -H 'Content-Type: application/json' -d '{"shot_id":"shot_xxxxxxxx"}'
# 轮询
curl http://127.0.0.1:8077/api/tasks/task_xxxxxxxx
```

**异步约定**：所有生成类 Skill 立即返回 `{ ok, task_id, status: "ACCEPTED" }`，
Agent 用 `get_task_status` 轮询（或 `agent_cli.py watch`），绝不阻塞。

---

## 四、Provider 抽象

每类能力都有独立注册表，可运行时切换，业务层零改动：

```
image        local(PIL 漫画分镜) | comfyui | cloud
video        local(关键帧+ffmpeg运镜) | comfyui | cloud
tts          local(macOS say) | cloud
music        local(lavfi 和弦垫) | cloud
sfx          local(lavfi 合成)
subtitle     local(SRT/ASS/VTT) | cloud_asr
enhance      ffmpeg(超分/补帧/降噪/锐化/调色/音频)
processing   ffmpeg(拼接/裁剪/混音/烧字幕/合成/封面)
browser      agent_browser(仅登记任务 + playbook，由 Agent 执行)
```

切换默认 Provider：

```bash
curl -X POST http://127.0.0.1:8077/api/skills/set_default_provider/invoke \
     -H 'Content-Type: application/json' -d '{"kind":"video","name":"comfyui"}'
```

接入 ComfyUI 只需在 `backend/.env` 填 `COMFYUI_BASE_URL`，并在调用时传 `parameters.workflow_json`
（API 格式工作流，支持 `{{prompt}} / {{width}} / {{height}} / {{frames}} / {{seed}} / {{image}}` 占位符）。

---

## 五、数据模型（15 张表）

`series` `projects` `scripts` `storyboards` `scenes` `shots` `characters` `assets` `tasks`
`workflows` `workflow_steps` `agent_logs` `quality_checks` `providers` `browser_tasks`

- 二进制文件**不入库**，只存 `file_path` + `url`
- `shots` 是核心实体：image/video/voice/subtitle 各自的 `*_asset_id` 与状态独立
- `tasks` 即任务队列：状态、进度、尝试次数、错误详情、逐条执行日志
- `series` 是连续剧层：`projects.series_id` + `episode_no` 表示「第几集」，
  `characters.series_id` 表示系列级角色（跨集复用）。详见 [docs/SERIES.md](docs/SERIES.md)

---

## 六、Workflow 状态机

正常流水线：

```
PROJECT_CREATED → SCRIPT_GENERATED → STORYBOARD_GENERATED → CHARACTER_GENERATED
→ IMAGE_GENERATED → VIDEO_GENERATED → VOICE_GENERATED → MUSIC_GENERATED
→ SUBTITLE_GENERATED → ENHANCEMENT → EDITING → COMPOSING → QUALITY_CHECK → COMPLETED
```

Agent 可动态跳转（`set_workflow_state`，跨段跳转需 `force=true`），自愈闭环：

```
VIDEO_GENERATED → QUALITY_CHECK → FAILED → ANALYZE → REGENERATE → QUALITY_CHECK
```

`run_quality_check(auto_repair=true)` 会自动定位失败项并派发**最小粒度**的修复任务
（单镜头重跑 / 重新合成），而不是整项目重来。

---

## 七、当前实现状态

| 组件 | 状态 |
|------|------|
| Backend / DB / Skill 层 / 任务队列 | ✅ 运行中 |
| Web UI（13 个 Tab） | ✅ 可访问 |
| 本地执行引擎（ffmpeg / PIL / say） | ✅ 真实产出 mp4 / png / mp3 / srt |
| ComfyUI / 云端 Provider | ⚙️ 适配器已就绪，需配置地址与密钥 |
| Face Enhancement（GFPGAN 等） | ⚙️ 已预留算子，未接模型时明确返回 `skipped` |
| 浏览器自动化 | ✅ 任务登记 + playbook；实际执行由 Agent 完成 |

详见 `docs/ARCHITECTURE.md`。
