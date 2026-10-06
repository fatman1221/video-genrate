# 接入本地 ComfyUI + Qwen-Image 出图

本机用 **ComfyUI + Qwen-Image 2.1** 生成**人物图（角色设定图）**与**场景图**。
本阶段**不生成视频**，视频相关能力保持原样（`local` provider）。

---

## 一、为什么需要改代码

原始工程的 `ComfyUIImageProvider` 要求调用方传 `parameters.workflow_json`，
但 **Skill 层没有任何入参能把它透传下来** —— `generate_image` 只接受
`provider / seed / width / height`。结果是：即使把 `COMFYUI_BASE_URL` 配对、
把默认 provider 切成 `comfyui`，调用仍然 100% 失败。

所以这次补了一层**工作流模板机制**，让「一大坨工作流 JSON」变成「一个有名字的模板」。

---

## 二、改动清单

| 文件 | 改动 |
|------|------|
| `backend/app/workflows/__init__.py` | **新增** 模板管理器：发现 / 加载 / 渲染占位符 |
| `backend/app/workflows/templates/*.json` | **新增** 两个模板：人物图、场景图 |
| `backend/app/config.py` | 新增 `comfyui_workflow_dir` / `comfyui_workflow_character` / `comfyui_workflow_scene` / `comfyui_timeout` |
| `backend/app/providers/image_providers.py` | 支持 `workflow_name`（模板名）；报错信息可读化（解析 `node_errors`） |
| `backend/app/providers/video_providers.py` | 同上，视频 Provider 也支持模板名 |
| `backend/app/skills/generation_skills.py` | `generate_image` / `regenerate_image` / `generate_all_images` 增加 `workflow_name`、`workflow_json`；**新增** `set_image_provider`、`list_image_workflows` |
| `backend/app/skills/content_skills.py` | `generate_character_reference` 增加 `workflow_name` / `workflow_json` / `prompt` / `seed` |
| `backend/app/executors/handlers.py` | 出图 handler 透传工作流；新增 `_resolve_workflow_name()` 三级优先级 |
| `backend/app/routers/system.py` | 修 SQLite 健康检查（`version()` → `sqlite_version()`） |
| `scripts/gen_images.py` | **新增** 批量出图 CLI |
| `examples/shots_example.json` | **新增** 批量出图的 json 示例 |

---

## 三、工作流模板

### 占位符

| 占位符 | 说明 |
|--------|------|
| `{{prompt}}` | 正向提示词 |
| `{{negative_prompt}}` | 负向提示词 |
| `{{width}}` / `{{height}}` | 分辨率（**保持 int 类型**，ComfyUI 不接受字符串） |
| `{{seed}}` | 随机种子（同样保持 int） |
| `{{steps}}` | 采样步数（覆盖模板默认值） |
| `{{cfg}}` | CFG 强度（覆盖模板默认值） |

> 关键实现细节：当某个值**整个字符串**就是 `{{key}}` 时，替换后保留原始类型；
> 若 `{{key}}` 内嵌在长字符串里，则做文本替换、结果为字符串。
> 这解决了 ComfyUI 对 `width`/`seed`/`steps`/`cfg` 必须为数字的硬要求。
> 从 `.env` / 项目 extra / HTTP 入参拿到的字符串数字（`"28"`、`"4.0"`）会被自动转成
> 数字；`None` 视为"未指定"，不会写进工作流。

### 采样参数怎么调（steps / cfg）

模板用 `__meta__.defaults` **自述默认值**，调用方按需覆盖，三级优先级：

```
单次调用（Skill 入参）  >  项目固化（set_image_provider 的 image_steps / image_cfg）
                        >  模板 __meta__.defaults
```

```jsonc
// 模板里这样写
"__meta__": { "defaults": { "steps": 20, "cfg": 2.5 } },
"7": { "class_type": "KSampler",
       "inputs": { "steps": "{{steps}}", "cfg": "{{cfg}}", ... } }
```

```jsonc
// 每次调用临时覆盖
POST /api/skills/generate_image/invoke
{ "shot_id": "shot_xxx", "steps": 28, "cfg": 4.0, "seed": 20261006 }

// 或整项目固化
POST /api/skills/set_image_provider/invoke
{ "project_id": "proj_xxx", "image_provider": "comfyui",
  "scene_workflow_name": "qwen_image_scene", "image_steps": 28, "image_cfg": 4.0 }
```

⚠️ **cfg 不是「画质旋钮」，是「版式旋钮」**。2026-10-06 实测（`_qwen_ab2`，固定
画风前缀/尺寸/seed/负向词，1280×720，只动 cfg 与 steps）：

| 组 | 平均亮度 | 对比度 σ | 饱和度 |
|---|---|---|---|
| cfg 2.5 / 20 步（默认） | 30.5 | 40.6 | 0.539 |
| cfg 4.0 / 20 步 | 24.5 | **35.2** | 0.626 |
| cfg 2.5 / 28 步 | **39.5** | **47.8** | 0.497 |
| cfg 4.0 / 28 步 | 34.2 | 44.1 | 0.537 |

cfg 4.0 在 20 步下**对比度反而最低、脸更暗、高光过曝**——「cfg 高 = 画质好」不成立。
而且**同一 seed 下改参数会改变整条采样轨迹**，出来的是**另一张图**（构图都会变），
所以不能拿两张图直接比全局色调，更不能 n=1 判优劣。要选参数请跑**多 seed 多样本**。

### 产物落库留痕（复盘用）

出图完成后，`Asset.parameters` 里会记录**实际提交给 ComfyUI 的值**（不是入参回声）：

| 字段 | 含义 |
|---|---|
| `seed` | **真实使用的种子**。不传 seed 时由服务端随机，但一定落库——出了好图可以照抄复现 |
| `steps` / `cfg` | 实际生效值（含模板默认值兜底的情况） |
| `sampler_name` / `scheduler` / `denoise` | 从提交的工作流里抄回 |
| `final_prompt` | **实际送进模型的那段文字**（含项目前缀） |
| `prefix_injected` | 本次有没有自动前置 `scene_prompt_prefix` |

> 不想让 Studio 自动前置画风前缀（想完全复刻你自己写的提示词）：
> `set_image_provider` 传 `"inject_scene_prefix": false`。
> 想确认到底送了什么，查 `Asset.parameters.final_prompt` 即可，不用再猜。

### 本机实际模型（已核对 ComfyUI `/object_info`）

```
diffusion_models/qwen_image_2.1_int8_convrot.safetensors
text_encoders/qwen3vl_8b_int8_convrot.safetensors
vae/qwen_image_2.1_vae_bf16.safetensors
```

### 模板节点结构

```
UNETLoader(1) ─┐
               ├─► KSampler(7) ─► VAEDecode(8) ─► SaveImage(9)
CLIPLoader(2) ─┴─► CLIPTextEncode(4 正向 / 5 负向)
VAELoader(3) ─────────────────────► VAEDecode(8)
EmptyLatentImage(6) ─────────────► KSampler(7)
```

采样参数：`steps=20, cfg=2.5, sampler=euler, scheduler=simple, denoise=1`

### 新增自己的模板

把 API 格式工作流丢进 `backend/app/workflows/templates/`，在需要变化的地方写占位符即可。
也可以在 `backend/.env` 里设 `COMFYUI_WORKFLOW_DIR=<目录>` 指向外部目录
（同名模板会覆盖内置模板，适合放本机专属、不便入库的工作流）。

导出方法：ComfyUI 里 **Workflow → Export (API)**，然后手工把 prompt / seed / 尺寸
替换成 `{{prompt}}` / `{{seed}}` / `{{width}}` / `{{height}}`。

---

## 四、工作流模板的选择优先级

出图时按三级决定用哪个模板（见 `handlers._resolve_workflow_name`）：

1. **单次调用**指定 —— `workflow_name` 参数
2. **项目级**固化 —— `set_image_provider` 写入项目 `extra`
3. **全局默认** —— `backend/.env` 的 `COMFYUI_WORKFLOW_CHARACTER` / `COMFYUI_WORKFLOW_SCENE`

人物图取 `character` 模板，场景图取 `scene` 模板。

---

## 五、使用方式

### 0. 启动

```bash
# 1) ComfyUI（本机 8188）
# 2) Studio 后端
cd backend
unset PYTHONPATH        # Windows cmd: set PYTHONPATH=
../.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```

# 3) 前端（另开一个终端）
```bash
cd frontend && npm install        # 首次需要
npm run dev                       # http://127.0.0.1:5180
```

> ⚠️ **不要写 `env -u PYTHONPATH`**。本机 PATH 上的 `~/.local/bin/env`
> 是一个只做 PATH 前插的 shim 脚本（uv 安装器留下的），它遮蔽了真正的
> `/usr/bin/env`：既不转发参数也不执行命令。后果是命令「静默成功退出(exit 0)、
> 实际什么都没跑」，表现为**后端启动后 0.3 秒就退出、日志一片空白**。
> 请一律改用 shell 内建的 `unset`。

### 1. 自检

```bash
python scripts/gen_images.py doctor
```

检查后端 / ComfyUI / 模板 / Provider 四项是否就绪。

### 2. 建项目并固化配置

```bash
python scripts/gen_images.py init --name "我的短剧" --style "写实电影感，自然光"
```

### 3. 出人物图

```bash
python scripts/gen_images.py character --project proj_xxxx \
    --name "林知夏" --appearance "22岁女生，齐肩黑发，米色针织衫" --seed 8888
```

### 4. 出场景图

```bash
python scripts/gen_images.py scene --project proj_xxxx \
    --title "图书馆清晨" --prompt "清晨的大学图书馆，阳光从高窗斜射，尘埃在光柱中漂浮"
```

### 5. 批量出图

```bash
python scripts/gen_images.py batch --project proj_xxxx --file examples/shots_example.json
```

---

## 六、直接用 Skill API

不写脚本也可以，走 HTTP：

```bash
# 列出可用模板
curl -X POST http://127.0.0.1:8077/api/skills/list_image_workflows/invoke \
     -H 'Content-Type: application/json' -d '{}'

# 单张场景图
curl -X POST http://127.0.0.1:8077/api/skills/generate_image/invoke \
     -H 'Content-Type: application/json' \
     -d '{"shot_id":"shot_xxx","provider":"comfyui","workflow_name":"qwen_image_scene"}'

# 轮询
curl http://127.0.0.1:8077/api/tasks/task_xxx
```

**异步约定**：所有生成类 Skill 立即返回 `{ok, task_id, status:"ACCEPTED"}`，
必须轮询 `get_task_status`，不要阻塞等待。

---

## 七、排错

### `ComfyUI 提交失败: 400`

错误信息已被解析成可读格式，例如：

```
节点 1(UNETLoader): value not in list: 'xxx.safetensors' not in [...]
```

**含义**：模板里的模型文件名在本机不存在。核对：

```bash
curl -s http://127.0.0.1:8188/object_info | python -c "
import json,sys; d=json.load(sys.stdin)
print(d['UNETLoader']['input']['required']['unet_name'][0])"
```

### `工作流模板不存在：xxx`

模板名拼错，或文件不在 `backend/app/workflows/templates/`。
用 `list_image_workflows` 或 `python scripts/gen_images.py doctor` 查看可用模板。

改动模板 json 后需**重启后端**（模板是进程内缓存的，`refresh()` 可清缓存）。

### `ComfyUI 生成超时或无输出`

本地首个任务含加载模型（int8 量化模型 + 31B 文本编码器），可能较慢。
默认超时 `COMFYUI_TIMEOUT=1800` 秒，可调大。

### health 显示 `degraded`

已修复：原代码对 SQLite 执行 `select version()`，SQLite 无此函数。
现在按方言分支（`settings.is_postgres`）。

---

## 八、实测记录

2026-09-26，Qwen-Image 2.1 int8 在 1280×720 下的实测：

| 项 | 结果 |
|----|------|
| 单张耗时 | 约 16 秒（模型已加载后） |
| 输出格式 | PNG，约 0.6–1.6 MB |
| 人物图 | 精准还原「短发+针织衫+温和神情」，可直接当角色设定图 |
| 场景图 | 精准还原「高窗斜射光+尘埃光柱+木质长桌摊开的书」 |

人物图与场景图两条链路均已端到端跑通
（`create_project` → `set_image_provider` → `create_character` / `create_storyboard`
→ `generate_character_reference` / `generate_image` → 产物落 `backend/storage/`）。
