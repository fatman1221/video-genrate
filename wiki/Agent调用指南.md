# Agent 调用指南

> Agent 只需要三步：**看清单 → 看契约 → 调用**。不需要被"教流程"。

---

## 一、三条使用路径

| 路径 | 谁用 | 场景 |
|---|---|---|
| **A. 让 Agent 干** | Agent | 主要方式：Agent 自己读契约、组参数、推进流水线 |
| **B. 人来操作** | 人 | Web UI 上做 Agent 不方便做的判断：**审核 / 回退 / 微调** |
| **C. 脚本 / CLI** | 脚本 | 批量出图、CI 验收、挂到别的流水线上 |

三者**不并列** —— Web UI 和 CLI 都是 Skill 层的客户端。这正是[人机同源](核心概念#09--人和-agent-走同一套接口)的落地。

---

## 二、三步调用

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

`agent_cli.py` 的子命令：

```
skills    列出能力（可按 category 过滤）
describe  查看某个 Skill 的契约
call      调用 Skill
status    查任务状态
watch     盯任务直到完成
pipeline  查看/推进流水线
health    后端健康
```

---

## 三、HTTP 直连

```bash
# 清单
curl http://127.0.0.1:8077/api/skills

# 清单（带完整 JSON Schema，适合塞进 system prompt）
curl 'http://127.0.0.1:8077/api/skills?detail=true'

# 调用
curl -X POST http://127.0.0.1:8077/api/skills/generate_video/invoke \
     -H 'Content-Type: application/json' -d '{"shot_id":"shot_xxxxxxxx"}'

# 轮询
curl http://127.0.0.1:8077/api/tasks/task_xxxxxxxx
```

---

## 四、异步约定（重要）

> **所有生成类 Skill 立即返回** `{ ok, task_id, status: "ACCEPTED" }`，
> Agent 用 `get_task_status` 轮询（或 `agent_cli.py watch`），**绝不阻塞**。

**为什么这样设计**：让 Agent 能在一个回合里**并发派发几十个任务**，而不是串行等待。一个 5 分钟的视频有几十个镜头，串行等待会慢几十倍。

```
提交 → { ok, task_id, status: "ACCEPTED" }   ← 立即返回
         ↓ 轮询
       PENDING → RUNNING（带进度）→ SUCCESS / FAILED
```

任务状态机：`PENDING / RUNNING / SUCCESS / FAILED / RETRYING / CANCELLED`。

---

## 五、给通用 LLM Agent 接入

Skill 层是**纯 HTTP + JSON Schema**，任何能发请求的 Agent 都能用：

- **通用 LLM Agent**：把 `/api/skills?detail=true` 的结果塞进 system prompt（当工具定义），
  用 function calling 直接映射到 `POST /api/skills/{name}/invoke`
- **传统工作流引擎**：把 `invoke` 当成一个 HTTP 节点
- **自研脚本**：直接用 `agent_cli.py`

---

## 六、Skill 契约长什么样

每个 Skill 具备：`name` / `description` / `category` / `input_schema` / `output_schema` / 是否异步 / `status` / `error`。

调用统一返回：

```json
{
  "ok": true,
  "data": { "...": "同步结果" },
  "task_id": "task_xxx",
  "status": "ACCEPTED",
  "error": null,
  "error_detail": null,
  "elapsed_ms": 12
}
```

参数在**入口就被 JSON Schema 校验**，错误不会等到执行到一半才爆。

---

## 七、89 个 Skill 的分类

| 分类 | 数量 | 覆盖 |
|---|---|---|
| `series` | 11 | 连续剧 / 分集 / 系列级角色 |
| `processing` | 10 | 拼接 / 裁剪 / 混音 / 烧字幕 / 合成 / 封面 |
| `workflow` | 8 | 状态机推进 / 节点回退 / 审核 / 重生成 |
| `storyboard` | 7 | 分镜拆解 / 场景 / 镜头 |
| `audio` | 6 | 配音 / 配乐 / 音效 |
| `image` | 6 | 出图 / 重生成 / 批量 / 工作流模板 |
| `project` | 6 | 建项目 / 概览 / 推进 |
| `asset` | 5 | 素材列表 / 独立语音 / 删除 |
| `task` | 5 | 任务状态 / 取消 / 重试 |
| `character` | 4 | 角色设定 / 参考图 |
| `orchestration` | 4 | 一键跑批 |
| `video` | 4 | 镜头视频 / 重生成 |
| `provider` | 3 | 引擎切换 / 列表 |
| `script` | 3 | 脚本生成 / 改写 |
| `browser` | 2 | 浏览器任务登记 |
| `log` | 2 | Agent 日志 |
| `quality` | 2 | 质检 / 修复 |
| `subtitle` | 1 | 字幕生成 |

> 实时查询：`curl http://127.0.0.1:8077/api/skills | python3 -m json.tool`

---

## 八、一次完整生产的链路

以"为 Shot 003 生成视频"为例：

```
Agent: POST /api/skills/generate_video/invoke  { shot_id: "shot_03" }
  │
  ├─ Skill 层 建任务并立即返回 { ok:true, task_id:"task_xx", status:"ACCEPTED" }
  │
  └─ worker 线程领取（SELECT ... FOR UPDATE SKIP LOCKED）
        ├─ 读 Shot 与关键帧（缺失则先补图）
        ├─ registry.get("video", provider).generate(...)
        │     └─ LocalVideoProvider → ffmpeg(zoompan 运镜) → 真实 mp4
        ├─ 落盘 + 建 Asset（含 prompt/model/provider/workflow/parameters）
        ├─ shot.video_asset_id = asset.id；shot.status = READY
        ├─ 写 agent_logs：video.started / video.finished
        └─ 若全部 Shot 都有视频 → 推进到 VIDEO_GENERATED
```

**链路上每一环都在做记录** —— 这才是"可观察、可回退、可追溯"的实现方式，而不是靠 Agent 自己记住。

---

## 九、actor 区分

人工操作走 `?actor=human`，Agent 调用默认 `actor=agent`，便于日志区分与审计。

---

## 相关

- [核心概念](核心概念) —— 第 01 / 02 / 09 条
- [生产流水线](生产流水线) —— 节点级回退与审核
- [扩展开发](扩展开发) —— 加一个新 Skill
