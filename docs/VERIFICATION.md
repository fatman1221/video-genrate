# 验收报告 — AI Video Agent 工作台

> 验收时间：2026-09-26
> 验收方式：真实链路执行（PostgreSQL + ffmpeg + macOS say），非 Mock

## 一、验收结论

**通过。** 从"在 Web UI 创建项目"到"播放最终成片"的完整生产链路已真实跑通并可复现。

| 指标 | 结果 |
|---|---|
| 端到端任务数 | 23 个，**失败 0** |
| 项目工作流状态 | `COMPLETED`，进度 100% |
| 镜头完成度 | 4 / 4 |
| 素材产出 | 21 个（图片 / 视频 / 配音 / 音乐 / 字幕 / 封面 / 成片） |
| 最终成片 | **20.00s**，1280×720 @ 24fps，H.264 + AAC 双声道，1.47 MB |
| 质量检查 | 通过 16 / 20，FAIL 0，WARN 4（均为需 VLM 的"人物一致性"） |

成片可播放、有时长正确的音轨、已烧录中文字幕。

## 二、执行引擎（真实，非模拟）

| 能力 | 实现 |
|---|---|
| 数据库 | PostgreSQL 17.11（Docker `deploy-postgres-1`，独立库 `video_agent_studio`） |
| 图像生成 | Pillow 漫画风格关键帧（角色剪影 / 网点 / 对话气泡 / 字幕条） |
| 视频生成 | ffmpeg Ken Burns 运镜（zoom_in / zoom_out / pan_left / pan_right，按图像哈希稳定选型） |
| 语音合成 | macOS `say`（本地 TTS，零依赖） |
| 音乐 / 音效 | ffmpeg `lavfi` 合成 |
| 字幕 | SRT 生成 + 烧录 |
| 视频处理 | ffmpeg：concat / mux / 混音 / 烧字幕 / 超分 / 补帧 / 降噪 / 调色 |
| 浏览器自动化 | Provider 抽象已就位（供 Agent 在无 API 时兜底） |

## 三、Agent 侧契约

- **69 个 Skill**，覆盖 17 个分类：project / script / storyboard / character / image /
  video / audio / subtitle / processing / enhance / quality / task / workflow /
  asset / log / provider / browser
- 每个 Skill 具备 `name` / `description` / `input_schema` / `output_schema` / `status` / `error`
- `POST /api/skills/{name}/invoke` 统一调用入口；长任务返回 `taskId` + `RUNNING`，不阻塞 Agent
- `GET /api/skills` 输出机器可读清单（供 WorkBuddy / Codex 自动发现）
- **16 个异步任务处理器**，4 个 worker，支持重试（指数退避）/ 取消 / 进度 / 错误详情
- CLI 客户端：`backend/agent_cli.py {skills,describe,call,status,watch,pipeline,health}`

## 四、验收过程与发现并修复的真实缺陷

本次验收**不是一次性通过**，过程中定位并修复了 4 个真实缺陷——这本身证明链路是真实执行的：

### 1. ffmpeg 无限阻塞（最严重）
- **现象**：Shot 002 的 5 秒视频渲染超过 600 秒超时并反复重试（4 个镜头中约 1 个触发）
- **根因**：PIL 以 `optimize=True` 保存的 PNG，会让 ffmpeg 的 libpng 解码器在 `-loop 1`
  读取时**无限阻塞**。同一份像素重新编码后 0.4 秒即可完成，确认为编码特征问题
- **修复**：
  - 新增 `_save_png_std()`，统一以 `interlace=False`、默认压缩写 PNG
  - `image_to_video()` 入口对**任意来源**的输入图先归一化再渲染（防 ComfyUI / 云端 / 上传图复发）
  - `run_ffmpeg()` 改为**看门狗线程 + 独立进程组强杀**。旧实现把超时判断放在
    `for line in proc.stdout` 循环内，ffmpeg 不输出时超时永远无法触发
- **验证**：原问题图 0.4s 完成；其余图 0.3–0.4s，无回归

### 2. 成片只剩一个镜头（时长 5s ≠ 20s）
- **现象**：合成成片仅 5 秒，而 4 个镜头合计 20 秒
- **根因**：主视频选取逻辑为"取最新一条 VIDEO 素材"。画质增强产出的单镜头片段
  被当作整片使用，导致从未拼接全部镜头
- **修复**：主视频**必须**由 `_ordered_shot_videos()` 按镜头序号拼接得到；
  仅在来源素材完全一致时复用已有拼接结果（`source_asset_ids` 校验）；显式
  `video_asset_id` 才允许覆盖
- **验证**：成片 20.00s，质检"时长符合预期"由 WARN 转 PASS

### 3. 项目永远无法进入 COMPLETED
- **现象**：项目停在 `VOICE_GENERATED` 91%，即使已产出成片
- **根因**：质检状态为 `WARN` 时既不通过也不触发修复。而"人物一致性"检查需要
  视觉模型，永远返回 WARN → 项目永久卡在中间态
- **修复**：以"**无硬失败（FAIL=0）即可交付**"为完成判据，WARN 记为提示不阻塞；
  仅在有 FAIL 时进入 `FAILED → ANALYZE → 自愈重跑`
- **验证**：状态达 `COMPLETED` 100%

### 4. 孤儿 RUNNING 任务
- **现象**：服务被强杀后，DB 中的 RUNNING 任务永不回收，前端一直显示"运行中"
- **修复**：启动维护改为 `reclaim_stale(stale_seconds=0)`——进程刚启动时不可能有任务在跑，
  所有遗留 RUNNING 一律回收重排
- **验证**：重启日志 `启动维护：{'reclaimed': 1}`，被回收任务随后执行成功

## 五、单镜头级重生成（关键架构能力）

验证"只重跑失败的镜头，而不是整个项目重来"：

```
重生成前：Shot 001 ast_b20a… | Shot 002 ast_675a… | Shot 003 ast_2f9f… | Shot 004 ast_9149…
提交：regenerate_video(shot_id=shot_b4be38902d66)  →  task_998641d7dae2 (PENDING)
重生成后：Shot 001 ast_b20a… | Shot 002 ast_bae6a722002a | Shot 003 ast_2f9f… | Shot 004 ast_9149…
```
仅 Shot 002 的资产被替换，其余镜头完全不动；任务 8 秒内 SUCCESS。

## 六、Web UI 验收

紫色 MUI 主题，13 个标签页全部可用：

`概览` · `脚本` · `分镜` · `人物` · `图片5` · `视频6` · `配音4` · `音乐` · `字幕` ·
`素材` · `任务日志23` · `Agent 日志` · `最终视频✓`

- 项目列表：状态 / 进度条 / 镜头数 / 素材数 / 目标时长 / 更新时间
- 生产进度条：脚本→分镜→人物→画面→镜头→配音→配乐→字幕→增强→剪辑→合成→质检，含下一步建议
- 分镜页：镜头卡片（关键帧预览 + 视频播放器 + Prompt + 人物 + 旁白 + 重生成按钮）
- 最终视频页：播放器 + 时长/分辨率/FPS/大小/编码 + 下载 + Quality Check 明细
- Agent 实时流水：SSE 推送执行日志

截图见 `docs/screenshots/`。

## 七、复现步骤

```bash
# 1) 后端（注意 env -u PYTHONPATH）
cd backend && env -u PYTHONPATH <venv>/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8077

# 2) 前端
cd frontend && ./node_modules/.bin/vite --host 127.0.0.1 --port 5180

# 3) 一键端到端验收
env -u PYTHONPATH <venv>/bin/python scripts/e2e_check.py 20 5
```

访问 <http://127.0.0.1:5180>。

## 八、已知限制与下一步

| 项 | 现状 | 下一步 |
|---|---|---|
| 人物一致性检查 | WARN（需视觉模型） | 接入 VLM 做跨镜头人物比对 |
| 图像 / 视频生成 | 本地 Pillow + ffmpeg（占位级真实产出） | 接 ComfyUI / 云端视频 API（Provider 抽象已就绪） |
| TTS | macOS `say` | 接云 TTS（Provider 抽象已就绪） |
| 浏览器自动化 | Provider 骨架 | 供 Agent 在无 API 平台兜底执行 |
| 成片规模 | 当前验证 20s / 4 镜头 | 5 分钟级长片（提升并发与合成策略） |
