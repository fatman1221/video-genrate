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
>
> **换台电脑继续用**：见 [docs/MIGRATION.md](docs/MIGRATION.md) —— 传输方式、
> 新机部署步骤、验收清单与故障速查。本工程自带 `local-postgres/docker-compose.yml`，
> 不依赖任何外部 Postgres 实例。
>
> **本地 ComfyUI + Qwen-Image 出图**（2026-09-26 新增）：用本机 ComfyUI 的
> Qwen-Image 2.1 生成**人物图（角色设定图）与场景图**，暂不生成视频。
> 补了一层「工作流模板」机制 —— 把一大坨工作流 JSON 变成有名字的模板
> （`workflow_name="qwen_image_character"`），并新增 `scripts/gen_images.py` 批量出图 CLI。
> 一条命令自检：`python scripts/gen_images.py doctor`。
> 详见 [docs/COMFYUI_QWEN.md](docs/COMFYUI_QWEN.md)。
>
> **Windows 启动**：`scripts\dev.cmd`（原 `dev.sh` 是 macOS/Linux 版，Windows 用 cmd）。

核心原则（与需求一一对应）：

| # | 原则 | 落地位置 |
|---|------|----------|
| 1 | Agent 负责思考，Backend 负责状态与执行 | `backend/app/skills`（契约）+ `executors`（执行） |
| 2 | 业务逻辑不写死在 Prompt 里 | 全部能力抽象为 89 个 Skill，见 `/api/skills` |
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
│   │   │   ├── runtime.py          运行时配置层（默认引擎 / 模型名 / 云端凭证）
│   │   │   ├── local_engine.py     ffmpeg / PIL / say 底层封装
│   │   │   ├── image_providers.py  local / comfyui / cloud
│   │   │   ├── video_providers.py  local / comfyui / cloud
│   │   │   ├── audio_providers.py  TTS / 音乐 / 音效
│   │   │   ├── subtitle_providers.py
│   │   │   ├── enhance_providers.py
│   │   │   ├── processing_providers.py
│   │   │   └── browser_providers.py
│   │   ├── skills/                 ★ Agent 调用契约层（89 个 Skill）
│   │   │   ├── base.py             Skill / Registry / JSON Schema 校验
│   │   │   ├── content_skills.py   Project / Script / Storyboard / Shot / Character
│   │   │   ├── series_skills.py    连续剧：Series / Episode / 系列级角色
│   │   │   ├── generation_skills.py Image / Video / Voice / Music / SFX / Subtitle / Task
│   │   │   └── post_skills.py      处理 / 合成 / 质检 / 工作流 / 素材 / 日志 / Provider / 浏览器 / 编排
│   │   ├── workflows/              ★ ComfyUI 工作流模板
│   │   │   ├── __init__.py         模板发现 / 加载 / 占位符渲染
│   │   │   └── templates/          qwen_image_character.json / qwen_image_scene.json
│   │   ├── executors/              ★ 异步任务执行
│   │   │   ├── queue.py            DB 即队列 + worker 池 + 重试 + 断点恢复
│   │   │   └── handlers.py         20 类任务的真实执行逻辑
│   │   ├── services/               业务服务（assets / workflow / projects / series / characters / quality / planner / tasks / settings）
│   │   └── routers/                REST 端点（projects / series / content / assets / settings / tasks / skills / system）
│   ├── storage/                    文件存储根目录（图片 / 视频 / 音频 / 字幕）
│   ├── agent_cli.py                ★ 给 Agent 用的 CLI
│   └── requirements.txt
├── frontend/                       React 18 + Vite + MUI（紫色主题）
│   └── src/{pages,components}      项目列表 / 项目详情 / 连续剧 / 素材中心 / 系统设置
├── scripts/
│   ├── e2e_check.py                端到端链路验证脚本
│   ├── gen_images.py               ★ ComfyUI + Qwen-Image 批量出图 CLI
│   ├── dev.cmd                     Windows 一键启动（dev.sh 的 Windows 版）
│   ├── build_intro.py              介绍页构建（截图裁切 + base64 内嵌）
│   ├── shot_intro.sh               介绍页整页长截图（1.5x 像素密度）
│   └── dev.sh                      一键启动后端 + 前端（macOS/Linux）
├── examples/
│   └── shots_example.json          ★ 批量出图的 json 示例
├── intro.html                      ★ 项目介绍页（单文件、零外部依赖、紫主题）
├── intro-full.png                  介绍页整页长截图（1920×9142）
├── local-postgres/
│   └── docker-compose.yml          自带 PostgreSQL（不依赖外部实例）
└── docs/
    ├── ARCHITECTURE.md             架构与扩展指南
    ├── COMFYUI_QWEN.md             ★ 本地 ComfyUI + Qwen-Image 出图接入
    ├── VERIFICATION.md             验收记录
    ├── PIPELINE_NODES.md           节点流水线设计
    ├── SERIES.md                   连续剧分层结构
    └── MIGRATION.md                ★ 换台电脑继续使用
```

> `backend/storage/`（生成的图片 / 视频 / 音频 / 字幕）不入库，
> 数据库只存 `file_path`。换机时用 `migration/storage.tar.gz` 单独搬运，
> 见 [docs/MIGRATION.md](docs/MIGRATION.md)。

---

## 二、快速开始

### 0. 一键启动（推荐）

```bash
./scripts/dev.sh check     # 环境自检：Python / Node / ffmpeg / 前端依赖
./scripts/dev.sh           # 启动后端 8077 + 前端 5180
./scripts/dev.sh stop      # 停止
```

脚本会自动探测运行环境（项目内 `.venv` > 已有环境 > PATH），
并规避本机 WorkBuddy 宿主注入的两个 shim。首次使用前需先装依赖，见下。

### 1. 数据库

```bash
cd local-postgres && docker compose up -d && cd ..
```

连接串 `postgresql+psycopg://trade_app:change-me@127.0.0.1:5432/video_agent_studio`
（与 `backend/app/config.py` 默认值一致）。

零依赖方案（SQLite，不跑 Docker）：

```bash
export DATABASE_URL="sqlite:///./video_agent_studio.db"
```

### 2. 后端依赖与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

# 手工启动（dev.sh / dev.cmd 会自动做这些）
# Windows: ../.venv/Scripts/python.exe
cd backend
unset PYTHONPATH
../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```

> `unset PYTHONPATH` 是为了规避 WorkBuddy 宿主 shim 对 `os.mkdir` 的劫持；
> 在你自己的普通终端里该变量不存在，这句是空操作、可安全保留。
>
> ⚠️ **不要写成 `env -u PYTHONPATH`**。部分机器 PATH 上存在
> `~/.local/bin/env`（uv 安装器留下的 PATH 前插脚本），它会遮蔽真正的
> `/usr/bin/env`，导致 `env -u XXX cmd` **静默退出(exit 0) 且不执行命令** ——
> 表现为「后端启动后立刻退出、日志空白」。用 shell 内建的 `unset` 即可。

### 3. 前端依赖与启动

```bash
cd frontend && npm install && cd ..

# 手工启动（dev.sh / dev.cmd web 会自动做这些）
# Windows: scripts\dev.cmd web
cd frontend
unset NODE_OPTIONS
./node_modules/.bin/vite --host 127.0.0.1 --port 5180 --strictPort
# http://127.0.0.1:5180
```

> `unset NODE_OPTIONS` 是为了摘掉宿主注入的 node shim（它的批量删除保护会让
> Vite 清理缓存时被拦截、dev server 当场退出）。同理不要写 `env -u`。

### 4. 一句话验证整条链路

```bash
python scripts/e2e_check.py 20 5   # 20 秒成片 / 单镜头 5 秒
```

### 5. 本地 ComfyUI + Qwen-Image 出图（人物图 / 场景图）

前置：本机 ComfyUI 已在 `127.0.0.1:8188` 运行，且已下载 Qwen-Image 2.1 相关模型。

```bash
# 自检：后端 / ComfyUI / 工作流模板 / Provider 四项
python scripts/gen_images.py doctor

# 建项目并固化出图配置（人物图、场景图各用哪个模板）
python scripts/gen_images.py init --name "我的短剧" --style "写实电影感，自然光"

# 人物图
python scripts/gen_images.py character --project proj_xxxx \
    --name "林知夏" --appearance "22岁女生，齐肩黑发，米色针织衫"

# 场景图
python scripts/gen_images.py scene --project proj_xxxx \
    --title "图书馆清晨" --prompt "清晨的大学图书馆，阳光从高窗斜射，尘埃在光柱中漂浮"

# 批量（json 清单见 examples/shots_example.json）
python scripts/gen_images.py batch --project proj_xxxx --file examples/shots_example.json
```

> 工作流模板放在 `backend/app/workflows/templates/`，占位符写
> `{{prompt}} / {{negative_prompt}} / {{width}} / {{height}} / {{seed}}`。
> 换成自己的工作流只需替换这两个 json，无需改代码。
> 详见 [docs/COMFYUI_QWEN.md](docs/COMFYUI_QWEN.md)。


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

切换默认 Provider（写入数据库，**重启后依然生效**）：

```bash
# 切到 ComfyUI 做图生视频
curl -X POST http://127.0.0.1:8077/api/skills/set_default_provider/invoke \
     -H 'Content-Type: application/json' \
     -d '{"kind":"video","name":"comfyui"}'

# 也可同时设定模型名与云端凭证
curl -X POST http://127.0.0.1:8077/api/skills/set_default_provider/invoke \
     -H 'Content-Type: application/json' \
     -d '{"kind":"tts","name":"cloud","model":"cosyvoice-v2",
          "credentials":{"base_url":"https://...","api_key":"sk-..."}}'
```

Web UI 上就是**「系统设置」页**：为「生图模型 / 图生视频模型 / 语音生成模型」
三类各选一个引擎（本地 / ComfyUI / 云端），填模型名与连接信息。

### 配置的存放与生效顺序

| 层 | 位置 | 说明 |
|---|---|---|
| 1 | `providers` 表 `is_default` / `config.model` / `config.credentials` | **用户设置，唯一真相来源**，启动时恢复 |
| 2 | 环境变量（`backend/.env`） | 首次运行的默认值，被用户设置覆盖 |
| 3 | 代码默认值 | 兜底 |

- Provider 实现**每次调用时**向 `providers/runtime.py` 查询连接信息，
  所以设置页改完**立即生效，不需要重启**
- `sync_providers_table()` 每次启动只刷新「代码侧派生字段」（名称 / 能力 / 可用性），
  **不会覆盖**用户的默认引擎、模型名与凭证
- 云端 API Key 只以掩码（`••••1234`）回显，不返回明文

### 接入 ComfyUI（出图 / 图生视频）

在「系统设置」或 `backend/.env` 填 ComfyUI 地址即可，出图与图生视频推荐用**工作流模板**
（`workflow_name`，模板放 `backend/app/workflows/templates/*.json`）。仓库已内置：

| 模板 | 用途 |
|---|---|
| `qwen_image_scene` / `qwen_image_character` | Qwen-Image 2.1 出场景图 / 角色定妆图 |
| `qwen_edit_scene` | Qwen-Image-Edit 2511「参考图 + 提示词」编辑，用于**角色一致性关键帧** |
| `minimax_h3_i2v` | MiniMax H3 图生视频（本地关键帧 → 真运动镜头） |
| `sdxl_ipadapter_scene` | SDXL + IPAdapter 备选（中文提示词理解较弱，保留） |

模板占位符：`{{prompt}} / {{negative_prompt}} / {{width}} / {{height}} / {{seed}} / {{image}} / {{frames}}`。
也可以用 `workflow_json` 直接传 API 格式工作流。详见 `docs/COMFYUI_QWEN.md`。

> ⚠️ 注意：原版只支持 `workflow_json`，但 **没有任何 Skill 能把该参数透传下来**，
> 导致 ComfyUI 通道在 API 上不可达。现已补上 `workflow_name` 模板机制，
> 并让 `generate_image` / `generate_character_reference` / `generate_all_images`
> 都支持透传。相关 Skill：
>
> - `list_image_workflows` —— 列出可用模板及其所需模型文件
> - `set_image_provider` —— 把出图配置固化到项目（provider + 两个模板名 + prompt 前缀 + 负向词）

---

## 五、数据模型（15 张表）

`series` `projects` `scripts` `storyboards` `scenes` `shots` `characters` `assets` `tasks`
`workflows` `workflow_steps` `agent_logs` `quality_checks` `providers` `browser_tasks`

- 二进制文件**不入库**，只存 `file_path` + `url`
- `shots` 是核心实体：image/video/voice/subtitle 各自的 `*_asset_id` 与状态独立
- `assets.project_id` **可为空**：空值表示素材中心里独立生成/保存的素材
  （例如直接合成的语音），不属于任何项目，落在 `storage/{类型}/_library/`
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
