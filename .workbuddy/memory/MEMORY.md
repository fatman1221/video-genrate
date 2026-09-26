# AI Video Agent Studio —— 项目长期约定

## 定位
- Agent（WorkBuddy / Codex）= 大脑；本项目 = 工具箱 / Skill Server / 执行平台
- Web UI = 人类控制台；Backend = 状态 / 数据 / 执行能力
- 所有可执行能力必须抽象为 Skill，不允许把业务逻辑写死在 Prompt 里

## 运行方式
| 项 | 值 |
|---|---|
| 后端 | FastAPI，`127.0.0.1:8077`，必须 `env -u PYTHONPATH` 启动 |
| 前端 | Vite + React + MUI，`127.0.0.1:5180`，代理 `/api` 与 `/media` → 8077 |
| 数据库 | PostgreSQL 17.11（Docker `deploy-postgres-1`，库 `video_agent_studio`，用户 `trade_app`） |
| Python | `/Users/zhangdongke/.workbuddy/binaries/python/envs/default/bin/python` |
| 一键启停 | `./scripts/dev.sh` / `./scripts/dev.sh stop` |

## 核心领域概念
- **节点式流水线**：12 个生产节点（script → quality_check），每个节点有
  `state`（生产状态）+ `review_status`（审核状态），二者解耦
- **上游失效传播**：`core/constants.STAGE_DOWNSTREAM` 定义下游矩阵。
  回退/重生成某节点 ⇒ 下游一律重置。**不允许出现上游已改、下游仍是旧产物的状态**
- **回退语义**：`rollback_to` 只清「产物引用」，文件保留在素材中心（可对比、可复用），
  工作流状态回退到「上一个节点已完成」的状态
- **重生成粒度**：镜头级（`regenerate_video/image/voice`）与节点级（`regenerate_stage`）两层
- **异步契约**：长任务一律返回 `taskId`，Agent 用 `get_task_status` 轮询，绝不阻塞

## 代码约定
- 数据模型统一在 `app/models.py`；跨方言 JSON 用 `database.JSONType`
- 新增表列时同步写进 `database._ADDED_COLUMNS`（启动自动 ALTER，避免手改库）
- 服务层放 `app/services/`，Skill 只是薄封装（`app/skills/`），不放业务逻辑
- 前端组件：`PipelineFlow`（节点图）/ `StagePanel`（节点操作抽屉）是流水线交互的唯一入口
- 人工操作走 `?actor=human`，Agent 调用默认 `actor=agent`，便于日志区分

## 已知坑（详见用户级 MEMORY.md）
- PYTHONPATH shim 劫持 `os.mkdir` → 启动 Python 服务必须 `env -u PYTHONPATH`
- Node safe-delete shim 拦截批量删除 → Vite dev server 会被杀，启动前清 `node_modules/.vite`
- ffmpeg 渲染卡死 = PIL `optimize=True` 写的 PNG；已修，诊断脚本 `scripts/probe_motion.py`
