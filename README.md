# AI Video Agent Studio

> **一句话**：这是一个能被 Agent 调用的 **AI 视频生产基座** ——
> Agent 负责思考，它负责让思考变成成片。
>
> 它不是视频编辑器。视频编辑器是给人用的；这个系统是**给 Agent 用的**，
> 人通过 Web UI 观察和干预。

```
   思考                                                  执行
┌──────────────┐        唯一入口：JSON Schema 契约      ┌──────────────────────┐
│  WorkBuddy   │ ─────────────────────────────────────► │  AI Video Agent      │
│  Codex       │                                        │  Studio              │
│  任何 Agent  │ ◄───────────────────────────────────── │                      │
└──────────────┘        结构化状态 / 任务 / 产物         │  ├ Backend  状态·数据 │
                                                        │  ├ Web UI   人类控制台│
                                                        │  └ Engines  ffmpeg… │
                                                        └──────────────────────┘
```

📖 **[Wiki](https://github.com/fatman1221/video-genrate/wiki)** —— 按主题查阅（快速开始 / 核心概念 / 流水线 / 换机迁移 / 故障速查）
📄 **本文件** —— 设计思想全文 · 🗂 **[docs/](docs/)** —— 深度设计文档

---

## 一、它解决的是什么问题

市面上的 AI 视频工具分成两类：

- **给人用的编辑器** —— 剪映、Runway、可灵。人在界面里一步步点，AI 帮你在某一步生成素材。
- **给代码用的 API** —— 一个接口生成一个片段，剩下的你自己拼。

这两类都默认了一件事：**"人"是生产者，AI 是工具**。

但这个假设正在失效。当 Agent 能自己规划分镜、自己写提示词、自己发现"第 3 个镜头的画面和人物设定不符"并决定重跑时，**生产者变成了 Agent**。

于是问题就变了。你要建的不再是"一个更好的生成工具"，而是：

> **一个 Agent 能理解、能调用、能观察、能纠错的执行基座。**

这个基座要回答的问题很具体：Agent 跑一个 5 分钟的视频要几十个任务、十几分钟时间，
**它怎么知道自己跑到哪了？某个镜头不满意怎么只重跑那一个？换个模型要不要改代码？重启了进度还在不在？**

这个项目就是对这些问题的回答。

---

## 二、核心思想

下面十条，是这个系统的设计内核。理解它们，比记住任何 API 都重要。

### 01 · 思考归 Agent，状态与执行归平台

**矛盾在哪**：LLM 会思考、会规划，但它**会遗忘、会编造、会不一致**。
而做视频是长流程（十几个环节、几十个镜头）、强状态（每步产物是下一步的输入）、
要求一致性（主角不能第三集换脸）。把这两件事塞进同一个 Prompt 里，
跑到第 8 步时，模型早就不记得第 2 步决定了什么。

**所以切开**：

| 谁 | 干什么 | 为什么是它 |
|---|---|---|
| **Agent** | 理解需求、规划、决策、观察、修正 | 这是 LLM 真正擅长的 |
| **平台** | 持有状态、执行、记录、保证一致 | 这是代码真正擅长的 |

**一个推论**：既然状态在平台手里，它就必须能被**外部观察**。
所以"生产进度"不是一个百分比数字，而是一个**可查询、可回退、可审核的结构**。

> 落地：`skills/`（契约层）· `executors/`（执行层）· `workflow_steps`（状态）

### 02 · 能力是契约，不是 Prompt

**矛盾在哪**：如果把"怎么生成视频"写进 Prompt，这段逻辑就**不可测试、不可追踪、不可复用**。
Agent 今天理解成 A、明天理解成 B，你无从知道它为什么这么干。

**所以**：每个能力都抽象成带 JSON Schema 的 Skill。Agent 只需要三步——
**看清单 → 看契约 → 调用**。

**收益**：

- Agent 不需要被"教流程"，它自己读契约
- 新能力上线，`/api/skills` 自动出现，不用改 Agent
- 参数在入口就被校验，错误不会等到执行到一半才爆

> 落地：89 个 Skill，18 个分类（`project / script / storyboard / shot / character /
> image / video / audio / subtitle / processing / quality / workflow / asset / task /
> log / provider / browser / orchestration`）

### 03 · 模型是可替换的零件

**矛盾在哪**：AI 模型半年换一代。今天 SDXL，明天 Qwen-Image，后天 Wan2.1。
如果业务逻辑和模型绑死，每次换模型都要重写业务 —— 而业务逻辑本身根本没变。

**所以**：每类能力都有独立注册表，业务层只认接口：

```
image        local(PIL 漫画分镜)   | comfyui | cloud
video        local(关键帧+ffmpeg运镜) | comfyui | cloud
tts          local(macOS say)     | cloud
music        local(lavfi 和弦垫)   | cloud
sfx          local(lavfi 合成)
subtitle     local(SRT/ASS/VTT)   | cloud_asr
enhance      ffmpeg(超分/补帧/降噪/锐化/调色)
processing   ffmpeg(拼接/裁剪/混音/烧字幕/合成/封面)
browser      agent_browser(登记任务 + playbook)
```

**一个副作用很值钱**：**本地 / 云端随时切换**。没显卡就调云端 API，有显卡就本地跑，
业务代码一行不改。

> 落地：`providers/` + `providers/runtime.py`（运行时配置层，改完**立即生效、无需重启**）

### 04 · 生产是可回退的图，不是进度条

**矛盾在哪**：视频生产是**试错过程**，不是一次成功。
用户看到第 5 步的配音不满意，如果只能整项目重跑，这工具就没法用。

**所以**：把生产建模成 **12 个节点**，每个节点都可以
**查看产出 / 回退到该步 / 重新生成该步 / 审核通过 / 驳回**。

```
脚本 → 分镜 → 人物 → 画面 → 镜头视频 → 配音 → 配乐 → 字幕
     → 画质增强 → 剪辑 → 合成 → 质检
```

**最关键的约束：上游一变，下游一律失效。**

回退到「画面生成」，则 `镜头视频 / 增强 / 剪辑 / 合成 / 质检` **全部重置**。

为什么必须这样？否则会出现"画面换了、视频还是旧的"这种**脏状态**。
而这种状态**在有 Agent 参与的自动化流程里几乎必然发生** ——
Agent 改了画面，但没意识到下游要重跑。与其靠 Prompt 提醒它，不如让系统在结构上不可能出错。

**另一个关键设计**：生产状态（`state`）与审核状态（`review_status`）**解耦**。
一个节点可以"已生产完成但尚未审核"，也可以"被驳回后重新生产"。
驳回结论在回退之后依然保留 —— 记录的是判断，不是进度。

> 落地：`services/pipeline.py::STAGE_DOWNSTREAM` · `GET /api/projects/{id}/pipeline`

### 05 · 失败的最小粒度是镜头

**矛盾在哪**：一个 20 秒的片子有 4~8 个镜头，一次完整生成有几十个任务。
任何一个失败都要整体重来，成本不可接受。

**所以**：`shot`（单镜头）是重试的**最小粒度**。质量检查发现问题后，
自动派发最小粒度的修复任务：

```
单镜头画质问题  → 只重跑该镜头的视频
多了 / 少了镜头 → 只为缺失的镜头补任务
整体合成问题    → 只重新合成
```

而不是"整项目重来"。

> 落地：`run_quality_check(auto_repair=true)` 的质检 → 修复闭环

### 06 · 每个产物都要能说清"我是怎么来的"

**矛盾在哪**：AI 生成**不可复现**（随机种子）。你今天跑出一个满意的镜头，
明天想再要一个一模一样的，做不到。

**所以**：每个产物都记录 `prompt / model / provider / workflow / parameters / seed`。
这是唯一能"回到某个好结果"的方式。

**延伸到成片**：每次合成都是一版。历史版本可**切换预览、标记当前生效版、并排对比、逐版下载**。

> 落地：`assets` 表元数据列 · `GET /api/projects/{id}/outputs`

### 07 · 能力边界要诚实声明

**矛盾在哪**："人物一致性""明显伪影"这类判断需要视觉模型（VLM）。
如果质检**假装**能判断，就会给你一个"全部通过"的假象 —— 这比直接报错更危险。

**所以**：做不到的事**显式声明**，而不是假装完成。

```json
{
  "item": "人物一致性",
  "status": "WARN",
  "note": "需要视觉模型判断，交由 Agent 用自身多模态能力补上",
  "requires": "vision_model"
}
```

这是有意为之的**边界声明**，不是遗漏。Face Enhancement 同理：
未接模型时明确返回 `skipped`，而不是悄悄跳过。

> **这条值得单独说**：一个系统的可信度，不取决于它宣称能做什么，
> 而取决于它**在自己做不到的时候会不会说谎**。

### 08 · 连续剧是一等公民

**矛盾在哪**：真实的视频需求天然是"多集"—— 短剧、教学系列、连载内容。
如果按"一次性项目"建模，会立刻撞上一个痛点：**主角每集换脸**。

**所以**：把"系列"作为一等实体 —— **一部剧 → 多集 → 每集一条完整流水线**。

- 一集 = 一个 Project，因此每集的节点回退 / 审核 / 重生成 / 成片**全部复用现成能力**
- 角色建在**系列层**，跨集复用 —— 主角不换脸

**读写分离的边界**：读的时候用「角色池」= 本集角色 + 系列级角色；
但任何**写入只影响本集**，不会污染整个系列。

> 落地：`series` 表 + `services/characters.py::character_pool`

### 09 · 人和 Agent 走同一套接口

**矛盾在哪**：如果人有一个操作界面、Agent 有另一套接口，两边行为必然漂移 ——
人在 UI 上点了"驳回"，Agent 那边不一定知道。

**所以**：Web UI 和 Agent **走同一套 Skill**。
人在界面上做的每一次审核、回退、改音色，和 Agent 的一次调用是
**同一条路径、同一个契约、同一份日志**，只是 `actor` 不同（`human` / `agent`）。

**收益**：Agent 能干的事，人一定能干；人干过的事，Agent 能查到。

> 落地：`routers/` 与 Web UI 都只经由 `invoke_skill()`，无第二条旁路

### 10 · 状态必须比模型活得久

**矛盾在哪**：模型会换、服务会重启、机器会换。状态如果只活在内存里，一次重启就归零。

**所以**：所有配置、状态、产物都落库，**重启后原样恢复**。

- 系统设置（默认引擎 / 模型名 / 凭证）存 `providers` 表，启动时恢复
- 任务断点恢复：超过 30 分钟仍为 `RUNNING` 的任务，启动时自动放回队列
- 换机器：自带 Postgres（`local-postgres/docker-compose.yml`），不依赖外部实例

> **一个反例（已修）**：早期版本 `set_default_provider` 只改内存注册表、**不落库**，
> 重启就把用户的选择丢了。这类"看起来能用、重启就没了"的设计，是最隐蔽的坑。

---

## 三、它长什么样

### 分层架构

```
┌────────────────────────────────────────────────────────────────┐
│ L1  Agent 层      WorkBuddy / Codex / 任何能发 HTTP 的 Agent   │
│                   职责：理解需求、规划、决策、观察、修正        │
└──────────────────────────┬─────────────────────────────────────┘
                           │ 唯一入口：/api/skills（JSON Schema）
┌──────────────────────────▼─────────────────────────────────────┐
│ L2  Skill 层      89 个 Skill                                  │
│                   职责：参数校验、编排、返回统一结构            │
└──────────────────────────┬─────────────────────────────────────┘
                           │ 提交 Task（异步）/ 直接返回（同步）
┌──────────────────────────▼─────────────────────────────────────┐
│ L3  执行层        queue.py（DB 即队列 + worker 池 + 重试）     │
│                   handlers.py（16 类任务的真实执行逻辑）        │
└──────────────────────────┬─────────────────────────────────────┘
                           │ 只调用抽象接口
┌──────────────────────────▼─────────────────────────────────────┐
│ L4  Provider 层   17 个实例 / 9 类能力，屏蔽底层差异            │
└──────────────────────────┬─────────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────────┐
│ L5  引擎层        ffmpeg · PIL · macOS say · ComfyUI · 云模型   │
└────────────────────────────────────────────────────────────────┘
```

**关键约束**：上层可以依赖下层，下层**绝不**反向依赖。
`providers/` 不 import `skills/`；`executors/` 不 import 具体 Provider 实现，
只通过 `registry.get(kind, name)`。这条约束是"模型可换"能成立的前提。

### 三个入口，一套内核

| 入口 | 谁用 | 说明 |
|---|---|---|
| `/api/skills` | Agent | 唯一契约入口，89 个 Skill 的 JSON Schema |
| Web UI (`:5180`) | 人 | 观察进度、审核、回退、调音、看成片 |
| `agent_cli.py` | 脚本 / 调试 | CLI 封装，等价于直接发 HTTP |

三者**不并列** —— Web UI 和 CLI 都是 Skill 层的客户端。

---

## 四、怎么用它

### 4.1 三条使用路径

**路径 A：让 Agent 干（主要方式）**

把系统当作 Agent 的工具箱。Agent 自己读契约、组参数、推进流水线：

```
用户：「帮我做一个 5 分钟的 AI Agent 教学视频，漫画风格」
  ↓
Agent：看清单 → 看契约 → bootstrap_project → 改写脚本 → 拆分镜
       → 生成人物图 → 逐镜出图 → 出视频 → 配音配乐 → 合成 → 质检
       → 发现第 3 镜人物不符 → 只重跑第 3 镜 → 重新合成 → 交付
```

**路径 B：人来操作（Web UI）**

人在 `:5180` 上做 Agent 不方便做的判断：**审核**（这一步能不能过）、
**回退**（这版不行，回到某步重来）、**微调**（改这个镜头的台词、换个音色）。

**路径 C：脚本 / CLI**

批量出图、CI 验收、把生成挂到别的流水线上：

```bash
python scripts/gen_images.py batch --project proj_xxxx --file examples/shots_example.json
python scripts/e2e_check.py 20 5          # 端到端验收
```

### 4.2 Agent 视角：三步调用

Agent 只需要 **看清单 → 看契约 → 调用**：

```bash
export STUDIO=http://127.0.0.1:8077
CLI="python backend/agent_cli.py"

# 1) 列出能力
$CLI skills --category video

# 2) 查看某个 Skill 的输入输出契约
$CLI describe generate_video

# 3) 一键搭起项目（之后可改写脚本、分镜）
$CLI call bootstrap_project '{"name":"AI Agent 教学视频",
     "requirement":"制作一个 5 分钟的 AI Agent 教学视频，漫画教学风格",
     "target_duration":300}'

# 4) 推进流水线并盯着它跑完
$CLI pipeline proj_xxxxx --watch
```

也可以直接走 HTTP：

```bash
curl http://127.0.0.1:8077/api/skills                              # 清单
curl -X POST http://127.0.0.1:8077/api/skills/generate_video/invoke \
     -H 'Content-Type: application/json' -d '{"shot_id":"shot_xxxxxxxx"}'
curl http://127.0.0.1:8077/api/tasks/task_xxxxxxxx                 # 轮询
```

> **异步约定**：所有生成类 Skill **立即返回** `{ ok, task_id, status: "ACCEPTED" }`，
> Agent 用 `get_task_status` 轮询（或 `agent_cli.py watch`），**绝不阻塞**。
> 这是为了让 Agent 能在一个回合里并发派发几十个任务，而不是串行等待。

### 4.3 配套 Agent 的接入方式

Skill 层是纯 HTTP + JSON Schema，**任何能发请求的 Agent 都能用**：

- **通用 LLM Agent**：把 `/api/skills?detail=true` 塞进 system prompt（当工具定义），
  用 function calling 映射到 `POST /api/skills/{name}/invoke`
- **工作流引擎**：把 `invoke` 当成一个 HTTP 节点
- **自研脚本**：直接用 `agent_cli.py`

### 4.4 一次完整的生产是什么样的

以"为 Shot 003 生成视频"为例：

```
Agent: POST /api/skills/generate_video/invoke  { shot_id: "shot_03" }
  │
  ├─ L2 Skill 层
  │     └─ 建任务（type=GENERATE_VIDEO, shot_id=shot_03）
  │        └─ 返回 { ok:true, task_id:"task_xx", status:"ACCEPTED" }  ← 立即返回
  │
  └─ L3 worker 线程领取（SELECT ... FOR UPDATE SKIP LOCKED）
        ├─ 读 Shot 与关键帧（缺失则先补图）
        ├─ registry.get("video", provider).generate(...)
        │     └─ L4 LocalVideoProvider → L5 ffmpeg(zoompan 运镜) → 真实 mp4
        ├─ 落盘 + 建 Asset（含 prompt/model/provider/workflow/parameters）
        ├─ shot.video_asset_id = asset.id；shot.status = READY
        ├─ 写 agent_logs：video.started / video.finished
        └─ 若全部 Shot 都有视频 → complete_step("video") → 推进到 VIDEO_GENERATED
```

**注意这条链路上每一环都在做记录** —— 这才是"可观察、可回退、可追溯"的实现方式，
而不是靠 Agent 自己记住。

---

## 五、怎么扩展

### 5.1 换一个生图 / 生视频模型

优先用**工作流模板**机制 —— 把一大坨 ComfyUI 工作流 JSON 变成有名字的模板，
不需要改代码：

```bash
# 看有哪些模板
curl http://127.0.0.1:8077/api/skills/list_image_workflows/invoke

# 出图时指定模板名即可
{ "project_id": "proj_xxxx", "workflow_name": "qwen_image_character" }
```

仓库已内置的模板：

| 模板 | 用途 |
|---|---|
| `qwen_image_scene` / `qwen_image_character` | Qwen-Image 2.1 出场景图 / 角色定妆图 |
| `qwen_edit_scene` | Qwen-Image-Edit「参考图 + 提示词」编辑，用于**角色一致性关键帧** |
| `minimax_h3_i2v` | MiniMax H3 图生视频（关键帧 → 真运动镜头） |
| `sdxl_ipadapter_scene` | SDXL + IPAdapter 备选 |

模板放 `backend/app/workflows/templates/*.json`，占位符：
`{{prompt}} / {{negative_prompt}} / {{width}} / {{height}} / {{seed}} / {{image}} / {{frames}}`。

**换成自己的工作流只需替换 json，无需改代码。**

### 5.2 加一类全新能力（例：语音克隆）

五步，**全程不改动已有代码**：

```python
# 1) providers/base.py 加接口
class VoiceCloneProvider(Provider):
    kind = "voice_clone"
    @abstractmethod
    def clone(self, *, sample_path: str, text: str, **kw) -> GenerationResult: ...

# 2) 新建 providers/voiceclone_providers.py，实现并注册

# 3) providers/__init__.py 的 _MODULES 加入该模块

# 4) skills/ 加一个 @skill（声明 JSON Schema）

# 5) executors/handlers.py 加 handler
```

完成后 `/api/skills` 自动出现新 Skill，**Web UI 也自动可用**。

### 5.3 切换 / 配置模型引擎

在 Web UI 的**「系统设置」**里，为三类能力各选一个引擎：

| 设置项 | 可选引擎 |
|---|---|
| 生图模型 | 本地渲染引擎 / ComfyUI / 云端 API |
| 图生视频模型 | 本地渲染引擎 / ComfyUI / 云端 API |
| 语音生成模型 | 本地 TTS（macOS say）/ 云端 TTS |

也可以走 Skill：

```bash
# 切到 ComfyUI 做图生视频
curl -X POST http://127.0.0.1:8077/api/skills/set_default_provider/invoke \
     -H 'Content-Type: application/json' -d '{"kind":"video","name":"comfyui"}'

# 同时设定模型名与云端凭证
curl -X POST http://127.0.0.1:8077/api/skills/set_default_provider/invoke \
     -H 'Content-Type: application/json' \
     -d '{"kind":"tts","name":"cloud","model":"cosyvoice-v2",
          "credentials":{"base_url":"https://...","api_key":"sk-..."}}'
```

**配置的生效顺序**：

| 层 | 位置 | 说明 |
|---|---|---|
| 1 | `providers` 表（`is_default` / `config.model` / `config.credentials`） | **用户设置，唯一真相来源** |
| 2 | 环境变量（`backend/.env`） | 首次运行的默认值，会被用户设置覆盖 |
| 3 | 代码默认值 | 兜底 |

- Provider **每次调用时**才向 `runtime.py` 查连接信息 → 改完**立即生效，不需重启**
- 启动同步只刷新"代码派生字段"，**不会覆盖**用户的默认引擎、模型名与凭证
- 云端 API Key 只以掩码（`••••1234`）回显，不返回明文

---

## 六、快速开始

### 0. 一键启动（推荐）

```bash
./scripts/dev.sh check     # 环境自检：Python / Node / ffmpeg / 前端依赖
./scripts/dev.sh           # 启动后端 8077 + 前端 5180
./scripts/dev.sh stop      # 停止
```

Windows 用 `scripts\dev.cmd`。

### 1. 数据库

```bash
cd local-postgres && docker compose up -d && cd ..
```

连接串 `postgresql+psycopg://trade_app:change-me@127.0.0.1:5432/video_agent_studio`
（与 `backend/app/config.py` 默认值一致）。

**零依赖方案**（SQLite，不跑 Docker）：

```bash
export DATABASE_URL="sqlite:///./video_agent_studio.db"
```

### 2. 后端依赖与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

cd backend
unset PYTHONPATH
../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```

> `unset PYTHONPATH` 是为规避 WorkBuddy 宿主 shim 对 `os.mkdir` 的劫持；
> 在你自己的普通终端里该变量不存在，这句是空操作、可安全保留。
>
> ⚠️ **优先用 `unset` 而不是 `env -u`**。`env -u` 本身没错，但当 PATH 上
> `~/.local/bin/env`（uv 安装器留下的前插 shim）排在 `/usr/bin/env` 前面时，
> `env -u XXX cmd` 会变成**空操作** —— 静默退出（exit 0）且什么都不执行，
> 表现为"启动后立刻退出、日志空白"。`unset` 是 shell 内建，无条件正确。
> 不确定时先 `type -a env` 看一眼。

### 3. 前端依赖与启动

```bash
cd frontend && npm install && cd ..

cd frontend
unset NODE_OPTIONS
./node_modules/.bin/vite --host 127.0.0.1 --port 5180 --strictPort
# http://127.0.0.1:5180
```

> `unset NODE_OPTIONS` 是为摘掉宿主注入的 node shim
> （它的批量删除保护会让 Vite 清缓存时被拦截、dev server 当场退出）。同理优先用 `unset`。

### 4. 验证

```bash
python scripts/e2e_check.py 20 5     # 20 秒成片 / 单镜头 5 秒，端到端真实产出
python scripts/gen_images.py doctor  # 自检：后端 / ComfyUI / 工作流模板 / Provider
```

### 5. 项目介绍页

用浏览器直接打开 `intro.html` —— **单文件、零外部依赖**（截图已 base64 内嵌），紫主题。

```bash
python scripts/build_intro.py    # 改了截图后重新生成
./scripts/shot_intro.sh          # 出整页长图 intro-full.png
```

---

## 七、事实清单

### 目录结构

```
video-skill/
├── backend/
│   ├── app/
│   │   ├── main.py                 FastAPI 入口（lifespan 启动 worker）
│   │   ├── config.py               全部配置（env / .env 可覆盖）
│   │   ├── database.py             SQLAlchemy（PostgreSQL / SQLite 双支持）
│   │   ├── models.py               15 张核心表
│   │   ├── storage.py              存储抽象（local / S3 / OSS / MinIO）
│   │   ├── core/constants.py       状态枚举 + Workflow 状态机 + 下游失效矩阵
│   │   ├── providers/              ★ 执行引擎抽象层（17 个 Provider）
│   │   │   ├── base.py             Provider 接口 + 注册表
│   │   │   ├── runtime.py          运行时配置层（默认引擎 / 模型名 / 凭证）
│   │   │   ├── local_engine.py     ffmpeg / PIL / say 底层封装
│   │   │   ├── image_providers.py  local / comfyui / cloud
│   │   │   ├── video_providers.py  local / comfyui / cloud
│   │   │   ├── audio_providers.py  TTS / 音乐 / 音效
│   │   │   └── ...                 subtitle / enhance / processing / browser
│   │   ├── skills/                 ★ Agent 调用契约层（89 个 Skill）
│   │   │   ├── base.py             Skill / Registry / JSON Schema 校验
│   │   │   ├── content_skills.py   Project / Script / Storyboard / Shot / Character
│   │   │   ├── series_skills.py    连续剧：Series / Episode / 系列级角色
│   │   │   ├── generation_skills.py Image / Video / Voice / Music / SFX / Subtitle
│   │   │   └── post_skills.py      处理 / 合成 / 质检 / 工作流 / 资产 / Provider
│   │   ├── workflows/templates/    ★ ComfyUI 工作流模板 JSON
│   │   ├── executors/              ★ 异步任务执行
│   │   │   ├── queue.py            DB 即队列 + worker 池 + 重试 + 断点恢复
│   │   │   └── handlers.py         16 类任务的真实执行逻辑
│   │   ├── services/               业务服务（assets / workflow / projects / series /
│   │   │                           characters / quality / planner / tasks / settings）
│   │   └── routers/                REST 端点
│   ├── storage/                    文件存储根目录（图片 / 视频 / 音频 / 字幕）
│   └── agent_cli.py                ★ 给 Agent / 脚本用的 CLI
├── frontend/                       React 18 + Vite + MUI（紫主题，明暗双色）
│   └── src/{pages,components}      工作台 / 项目详情 / 连续剧 / 资产库 / 系统设置
├── scripts/                        dev.sh · e2e_check.py · gen_images.py · build_intro.py
├── examples/shots_example.json     批量出图清单示例
├── wiki/                           ★ GitHub Wiki 源文件（用 scripts/publish_wiki.sh 发布）
├── intro.html                      项目介绍页（单文件、零外部依赖）
├── local-postgres/                 自带 PostgreSQL
└── docs/                           ARCHITECTURE / COMFYUI_QWEN / PIPELINE_NODES /
                                    SERIES / VERIFICATION / MIGRATION
```

### 数据模型（15 张表）

`series` `projects` `scripts` `storyboards` `scenes` `shots` `characters` `assets`
`tasks` `workflows` `workflow_steps` `agent_logs` `quality_checks` `providers` `browser_tasks`

- 二进制文件**不入库**，数据库只存 `file_path` + `url`
- `shots` 是核心实体：image / video / voice / subtitle 各自的 `*_asset_id` 与状态独立
- `assets.project_id` **可为空**：表示资产库里独立生成/保存的素材（例如直接合成的语音），
  落在 `storage/{类型}/_library/`，不属于任何项目
- `tasks` 即任务队列：状态、进度、尝试次数、错误详情、逐条日志
- `series` 是连续剧层：`projects.series_id` + `episode_no` 表示"第几集"，
  `characters.series_id` 表示系列级角色

### 状态机

```
PROJECT_CREATED → SCRIPT_GENERATED → STORYBOARD_GENERATED → CHARACTER_GENERATED
→ IMAGE_GENERATED → VIDEO_GENERATED → VOICE_GENERATED → MUSIC_GENERATED
→ SUBTITLE_GENERATED → ENHANCEMENT → EDITING → COMPOSING → QUALITY_CHECK → COMPLETED
```

Agent 可动态跳转（`set_workflow_state`，跨段需 `force=true`）。自愈闭环：

```
VIDEO_GENERATED → QUALITY_CHECK → FAILED → ANALYZE → REGENERATE → QUALITY_CHECK
```

### 失败重试与恢复

| 机制 | 实现 |
|---|---|
| 指数退避重试 | 退避 60s → 120s → …上限 600s |
| 不可重试错误 | Provider 抛 `retryable=False` 时直接 FAILED，避免无效重试 |
| 取消 | 写 `payload._cancel_requested`，handler 在 `progress()` 里检查 |
| 真实进度 | 解析 ffmpeg `-progress pipe:1`，逐段回写 DB，UI 实时可见 |
| 断点恢复 | 启动时 `reclaim_stale()` 把超 30 分钟仍 RUNNING 的任务放回队列 |

### 当前实现状态

| 组件 | 状态 |
|---|---|
| Backend / DB / Skill 层 / 任务队列 | ✅ 运行中 |
| Web UI | ✅ 可访问 |
| 本地执行引擎（ffmpeg / PIL / say） | ✅ 真实产出 mp4 / png / mp3 / srt |
| ComfyUI / 云端 Provider | ⚙️ 适配器就绪，需配置地址与密钥 |
| Face Enhancement（GFPGAN 等） | ⚙️ 算子已预留，未接模型时明确返回 `skipped` |
| 浏览器自动化 | ✅ 任务登记 + playbook；实际执行由 Agent 完成 |

> **验收状态：已通过**（2026-09-26）。端到端 23 个任务 0 失败，
> 产出真实成片（H.264 + AAC），工作流到达 `COMPLETED`。
> 详见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

---

## 八、边界与下一步

诚实列一下当前**没做**的事：

| 项 | 现状 | 方向 |
|---|---|---|
| 人物一致性 / 伪影检测 | 需 VLM，当前固定 `WARN` 并声明 `requires: vision_model` | 接入视觉质检 provider |
| Face Enhancement | 算子已定义，未接模型时 `skipped` | 接入 GFPGAN / CodeFormer |
| 转场 / 精细剪辑 | 仅拼接与基础处理 | `EDIT_VIDEO` 增加 xfade 与时间线 DSL |
| `ADD_*` 任务类型 | `TaskType` 声明了 20 个，但 `ADD_VOICE / ADD_MUSIC / ADD_SFX / ADD_SUBTITLE` **只有枚举、无 handler**（实际生效 16 个） | 实现或从枚举中移除 |
| 认证与多租户 | 单机单用户 | API Key 中间件 + `owner` 过滤 |
| 成本与配额 | 未统计 | 在 `tasks.result` 记录耗时与算力消耗，做预算护栏 |

**换台电脑继续用**：见 [docs/MIGRATION.md](docs/MIGRATION.md) —— 传输方式、
新机部署步骤、验收清单与故障速查。

---

## 附：十条思想速查

| # | 思想 | 一句话 |
|---|---|---|
| 01 | 思考与执行分离 | LLM 会遗忘，状态必须外置 |
| 02 | 能力是契约 | 业务逻辑不写死在 Prompt 里 |
| 03 | 模型是可替换零件 | 换模型不改业务代码 |
| 04 | 生产是可回退的图 | 上游一变，下游一律失效 |
| 05 | 失败粒度是镜头 | 只重跑坏掉的那一个 |
| 06 | 产物可追溯 | 生成不可复现，记录是唯一退路 |
| 07 | 边界诚实声明 | 做不到就说做不到，不假装通过 |
| 08 | 连续剧是一等公民 | 主角跨集不换脸 |
| 09 | 人机同源 | 人和 Agent 走同一套 Skill |
| 10 | 状态比模型活得久 | 重启、换机都不丢 |
