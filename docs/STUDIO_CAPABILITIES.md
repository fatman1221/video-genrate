# 视觉设定 · 连续性 · Prompt 编译 · 生成计划 —— Agent 调用指南

> 本文件是 `docs/DRAMA_SKILLS_MIGRATION.md` 的**使用侧**配套：那份讲「为什么这么设计」，
> 这份讲「Agent 该怎么调」。
>
> 能力全部以 Skill 暴露：`GET /api/skills?detail=true` 拿 JSON Schema，
> `POST /api/skills/{name}/invoke` 调用。信封恒为 `{ok, status, data, error, task_id}`。

---

## 0. 一句话理解新增的这层

原来只有「项目 → 分镜 → 镜头 → 产物」的**扁平**结构：
镜头自带一堆提示词字符串，谁写的、依据什么、改没改过，都说不清。

新增的这层把「**设定**」和「**这一次生成**」拆开了：

```
VisualBible（视觉设定：时代锚点/全局规则/文字政策）
     └── VisualStyle（形态卡：live_action / guoman_2d / dynamic_comic / ...）
     └── Character ── CharacterLook（造型变体）
         Location  ── LocationView（观看变体）
         Prop      ── PropState   （状态变体）
     └── ContinuityLock（什么永远不变）  ContinuityDelta（什么变了、从什么变成什么）

Shot ──(绑定)──> 上述实体
  └── Prompt（逻辑单元：一个镜头一类产物一条）
        └── PromptVersion（**只增不改**，每次编译产生新版本，带编译输入快照）

GenerationPlan（Preview → Confirm → Produce 三步闸门）
  └── GenerationPlanItem ──> Task ──> Asset（回填 prompt_version_id，血缘闭环）
```

**四条必须记住的纪律**

| # | 纪律 | 为什么 |
|---|---|---|
| 1 | **新层是唯一事实来源**；旧字段（`Shot.image_prompt` 等）= 单向兼容投影 | 只写不读，不做双向同步，否则「改了一边忘了另一边」必然出现 |
| 2 | **Prompt 只增不改**；改设定只产生新版本 | 历史可复现；STALE 由 service 动态判定，不写死字段 |
| 3 | **锁面是名词短语**，必须逐字出现在提示词正向正文里 | 状态/动作/位置词进锁面 = 锁的是一瞬间，不是身份 |
| 4 | **预览与确认不花钱**，只有物化才创建任务 | 「看到预览之后明确同意这一项当前任务」要成为可核验动作 |

---

## 1. 典型链路（照这个顺序调）

```
① create_visual_bible              视觉设定基线（时代锚点、全局规则、文字政策）
② create_style                     形态卡 + 渲染/光线/配色/镜头语言 → set_current_style
③ （角色）create_character → set_character_identity → create_look
   （地点）create_location → create_location_view
   （道具）create_prop     → create_prop_state
④ create_continuity_lock           把"永远不变的东西"锁成最小名词短语
⑤ set_shot_bindings / set_shot_location   把镜头绑到实体上（不绑就只能靠文字描述）
⑥ compile_image_prompt / compile_video_prompt   编译 → 落一个 PromptVersion
⑦ check_prompt_staleness           改过设定之后，问一句"哪些镜头要重出"
⑧ create_generation_plan → preview_generation_plan → confirm_generation_plan
   → materialize_generation_plan    前三步不花钱；最后一步才建任务
⑨ get_asset_provenance             产物出来后，随时能反查"它到底是怎么来的"
```

> ⚠️ ⑥ 之前必须完成 ①②③④⑤。编译器只读已落库的设定 ——
> **设定没落库，编译出来的正文里就没有它**。

---

## 2. 视觉设定

### `create_visual_bible` / `get_visual_bible`

```jsonc
POST /api/skills/create_visual_bible/invoke
{
  "project_id": "proj_xxx",
  "era_anchors": ["近未来地月货运时代", "设备为哑光深灰工业风"],
  "global_rules": {
    "stage_policy": "环境内展示，保持舷窗可见",
    "avoid": ["现代都市街景", "亮色塑料制品"]
  },
  "text_policy": { "readable_text_allowed": false }   // 全局文字政策
}
```

- bible 每次改动 `version + 1`；`get_visual_bible` 一次拿回 bible + 全部子实体概要。
- `text_policy.readable_text_allowed = false` 与任何道具的
  `exact_readable`（要求画面出现精确可读文字）**不能共存** —— 编译直接报错。

### `create_style` / `set_current_style`

`form_card` 必须是六个之一：`live_action` / `guoman_2d` / `dynamic_comic` /
`chibi` / `stylized_3d` / `ink_wash`。

> 判据：「删掉它，提示词一字不变」→ 那它只是标签，不是形态卡。
> 每个形态卡都必须真的改变渲染描述、光线口径与镜头语言。

同项目可以建多个 style 做对比，但同一时刻只有一个 `is_current`。

### 身份 / 变体

| Skill | 建什么 | 关键入参 |
|---|---|---|
| `set_character_identity` | 给已有角色补身份锚点 | `identity_anchors`（跨造型不变的特征）、`not_identity`、`persistent_performance_facts`、`voice_direction` |
| `create_look` | 角色造型变体 | `base_look_id`、`differences`（服装层次/妆发/损伤）、`validity` |
| `create_location` | 地点身份 | `spatial_identity`（形制 + 固定锚点） |
| `create_location_view` | 观看变体 | `orientation`（朝向哪面）、`state_differences`（时段/天气/灯光） |
| `create_prop` | 道具身份 | `identity_anchors`、`text_policy` |
| `create_prop_state` | 道具状态变体 | `condition`（开合/完好度）、`custody`（在谁手里）、`contents` |

**判据**：换一个**身份**就不再是同一个人/地/物 → 这是新实体；只是服装/伤势/时段/
天气/开合/持有状态变了 → 这是**变体**。镜头的瞬态（姿势/视线/站位/角度）**既不是身份也不是变体**，
它属于 Shot。

---

## 3. 连续性：锁 vs 增量（两套机制，别混）

### `create_continuity_lock` —— 什么永远不变

```jsonc
{
  "project_id": "proj_xxx",
  "name": "深灰夹克",
  "surface": "dark grey zip-up stand collar jacket",   // 最小名词短语
  "subject_type": "character",
  "subject_id": "chr_xxx",
  "shot_scope": ["all"]          // 或 ["shot_01", "shot_02"]
}
```

**锁面必须满足**（不满足直接报错，不静默放过）：

- 只有**颜色 + 材质** / **形制 + 物体**，是名词短语
- ❌ 不能含状态/动作/位置词（`half-finished ... in her hands`）
- ❌ 不能含标点、镜头信息（`镜头 03 的 ...`）、数量词
- ❌ 不能超过词数上限

**编译时的两条硬约束**：

1. **逐字出现在正向正文里**才算数。粘在别的词上不算
   （`unchipped white enamel mug` **不**满足 `chipped white enamel mug`）。
2. **负面提示词里的命中不算在场证据**。
   所以「把锁面写进 negative prompt」是不生效的。

> 同项目锁数超过提示线（12）会记 WARN 日志。这是 `craft_default` 级建议，**不阻断** ——
> 一集通常个位数，但长片需要更多也不该被硬拦。

### `create_continuity_delta` —— 什么变了

```jsonc
{
  "project_id": "proj_xxx",
  "subject_type": "prop", "subject_id": "prop_xxx",
  "before": "完好", "after": "右肩拉链断裂",
  "cause": "第 12 镜被撞倒",
  "shot_scope": ["shot_12", "shot_13"],
  "reconciliation_status": "must_match_or_revise"
}
```

**纪律：未知 ≠ 恢复默认。** 没写出来的差异按「未变」处理，而不是「回到基线」——
否则一次爆炸之后，下一镜的杯子会莫名其妙变回完好。

### `verify_continuity_locks` —— 当闸门用

```jsonc
{ "project_id": "proj_xxx", "prompt_type": "image" }
```

返回里有两个**分开的**计数，别混：

| 字段 | 含义 | 是否阻断 |
|---|---|---|
| `missing_total` | **有提示词但锁面不在正向正文** | ✅ 阻断（`compliant=false`） |
| `not_compiled_total` | 镜头还没编译过提示词 | ❌ 不阻断（覆盖度问题） |

每镜头给 `state ∈ {present, missing, not_compiled}`。
把「没编译」当成「违规」会让生产期永远非合规、闸门失效。

---

## 4. Prompt 编译

### `compile_image_prompt` / `compile_video_prompt`

```jsonc
POST /api/skills/compile_image_prompt/invoke
{
  "shot_id": "shot_xxx",
  // 下面这些可选字段**会被打包进 options**
  "provider": "comfyui",
  "resolution": "2K",          // 720p / 1080p / 2K / 4K
  "aspect_ratio": "16:9",
  "model": "qwen_image_2.1_int8_convrot",
  "mirror": true,              // 是否回写 Shot 老字段（单向兼容投影），默认 true
  "raw_prompt": "…"            // 可选：手工基线，用于「版本差异」段
}
```

返回：

```jsonc
{
  "prompt_id": "prm_xxx", "prompt_version_id": "pv_xxx",
  "code": "IMG-Shot 001", "version": 2,
  "compiled_prompt": "…八段式正文…",
  "negative_prompt": "…",
  "reference_assets": [ { "slot": "…", "kind": "REF", "role": "身份",
                          "admission_status": "ready", "file_path": "…",
                          "may_control": ["身份"], "must_not_control": ["构图","姿势","临时造型","文字"] } ],
  "continuity_lock_ids": ["lock_xxx"],
  "compiled_from": { "bible": {...}, "style": {...}, "subjects": [...],
                     "locks": [...], "shot": {...} }
}
```

**八段式结构**（`recipe = video-genrate-8seg@1.0.0`）：
用途与主体 → 稳定锚点 → 版本差异 → 构图与尺度 → 材质色彩光线 →
**连续性锁（强制注入，单独成段）** → 背景舞台 → 文字与功能 → 排除与保留（→ 负向）

**编译期硬失败（抛 `CompileError`，不产生半成品）**：

- 注锁失败（锁面不在正向正文）
- **正文卫生**不达标：含 64 位哈希 / 引擎语法（`--ar` `--v` `::2`）/ JSON 键名与字段路径
  （`compiled_from`、`prompt_version_id`…）/ 内部代号（`LOCK-3`、`CHAR-...`）/
  文件路径与盘符 / 流程说明（「请模型务必」「第 3 次尝试」）
- **文字政策冲突**：`exact_readable` 没给精确文字；`readable` 与「全局无文字」并存

### `check_prompt_staleness` / `get_prompt` / `list_prompts`

```
check_prompt_staleness {prompt_id}
  → { stale: true, reasons: ["角色「林野」的设定已更新（v2 → v3）", "连续性锁「深灰夹克」锁面已变更"] }

GET /api/projects/{id}/prompts?prompt_type=image
  → { count, stale_count, prompts: [ {id, code, type, latest_version, stale, stale_reasons}, … ] }
```

**STALE 是动态判定的**（比对 `compiled_from` 快照里的版本号），不是入库时写死的字段。
改设定后**不需要**回头批量刷新全库，问一句就行。

> ⚠️ 快照里**不含** `shot.updated_at` —— 因为回写兼容投影会 bump 它，
> 放进去会导致「刚编译完就判定 stale」的死循环。

---

## 5. 生成计划：Preview → Confirm → Produce

### 四步硬闸门

```jsonc
// ① 建计划（不花钱）
POST /api/skills/create_generation_plan/invoke
{
  "project_id": "proj_xxx", "name": "第 1 集关键帧",
  "items": [
    { "shot_id": "shot_01", "modality": "image",
      "prompt_version_id": "pv_xxx", "provider": "comfyui", "resolution": "2K" },
    { "shot_id": "shot_02", "modality": "video", "provider": "comfyui" }
  ]
}
// → { plan: { id: "plan_xxx", status: "DRAFT", … } }

// ② 预览（不花钱）：校验 + 算指纹 + 出汇总
POST /api/skills/preview_generation_plan/invoke  { "plan_id": "plan_xxx" }
// → { plan: { status: "PREVIEWED", fingerprint: "9f2c…", summary: {item_count, by_modality, …} },
//     failures: [], warnings: [] }

// ③ 确认（不花钱）：必须原样回传指纹
POST /api/skills/confirm_generation_plan/invoke
{ "plan_id": "plan_xxx", "fingerprint": "9f2c…" }
// → { action: "CONFIRM plan_xxx 9f2c1a3b4d5e", status: "CONFIRMED", expires_at: "…" }

// ④ 物化（这一步才花钱）：一次性消费确认
POST /api/skills/materialize_generation_plan/invoke  { "plan_id": "plan_xxx" }
// → { plan_id, task_ids: ["task_…"], count: 12, status: "RUNNING" }
```

### 闸门语义（每一条都有测试守）

| 语义 | 表现 |
|---|---|
| **预览绝不消耗** | Task 表增量为 0、Provider 不被调用 |
| **确认一次性** | 物化成功后 plan → `RUNNING`；再次物化**被拒** |
| **指纹不符即拒** | 改了 items / parameters 任一字段 → 旧指纹失效，确认被拒且**状态不前进** |
| **未确认不可物化** | `DRAFT` / `PREVIEWED` 直接物化 → 被拒 |
| **过期不可物化** | 确认带 `expires_at`（默认 30 分钟），过期需重新确认 |
| **预估不承诺** | `summary.est_*` 只描述口径与数量，**不报金额** |
| **PLAN 态不可投产** | `items[].reference_assets[].kind == "PLAN"` → 预览直接失败 |

### 参考图三态（记号必须分清）

| 记号 | 含义 | 可作生产输入？ |
|---|---|---|
| `REF-` | 项目内**真实存在**、准入为 `ready` | ✅ |
| `IMG-` | 提示词条目 ID | ✅（可作 job 来源） |
| `PLAN-` | 创作者自备、**项目内并不存在** | ❌ 预览直接失败 |

参考图槽位必须声明 `may_control`（能决定什么）与 `must_not_control`（绝不能决定什么）——
防止「一张定妆图越权决定了构图和服装」。

---

## 6. 血缘反查

```
GET /api/assets/{asset_id}/provenance?depth=1     # depth 控制参考图递归层数
GET /api/prompts/{prompt_id}/provenance
```

返回 `{ chain, missing, complete, summary }`。`chain` 的必备节点：

```
project ─ prompt ─ prompt_version ─ shot ─ scene ─ storyboard ─ task
                         └─ compiled_from（bible/style/subjects/locks/shot 的 id+版本）
```

`prompt_version` 节点**带正文本身**（`compiled_prompt` / `negative_prompt`）、
编译输入快照、`continuity_lock_ids`、`reference_assets` 槽位。

> 设计立场：反查的意义是回答「当时到底依据什么、发了什么指令」。
> 只回一个 id 还得再查一次，等于没闭环。

---

## 7. 分辨率档位（Phase 9）

`generate_image` / `regenerate_image` / `generate_all_images` 新增两个入参：

```jsonc
{ "shot_id": "shot_xxx", "resolution": "2K", "aspect_ratio": "16:9", "provider": "comfyui" }
```

- 档位：`720p` / `1080p` / `2K` / `4K`（别名 `2160p`、`UHD`、`FHD`、`QHD` 会自动归一化）
- 画幅：`16:9` / `9:16` / `1:1` / `4:3` / `3:2`
- **不传 = 维持项目默认尺寸**（向后兼容）
- 显式 `width` / `height` 优先于 `resolution`

返回值里附：

```jsonc
{
  "task_id": "task_xxx",
  "size": { "resolution": "2K", "aspect_ratio": "16:9", "width": 2752, "height": 1536,
            "megapixels": 4.23, "above_native": false },
  "provider": "comfyui",
  "provider_note": "",          // 超原生档位时这里给出说明
  "warnings": []
}
```

**闸门规则**（由 Provider 的能力声明决定，模型名不写死在校验代码里）：

| 情况 | 行为 |
|---|---|
| Provider 声明支持该档位 | 放行 |
| 请求档位 **高于** `native_resolution` | ✅ 放行 + **告警**（「超采样不等于更高画质，建议原生出图后走超分链」） |
| Provider 声明了档位但它**不在列表里** | ❌ 失败，并列出它支持哪些 |
| Provider **没声明** `resolution:` | 跳过校验（**不把「没声明」当成「不支持」**） |

> **本机实测**：2K（2752×1536）真实出图 130 秒；4K 通路可用但 16GB 显存下过慢，
> **暂不作为默认档位**。成片三环（关键帧 → 图生视频模板 → 成片）都在 90–106 万像素，
> 2K 已是原生上限，4K 只适合单独出静态素材。

---

## 8. 常见坑（都是踩过的）

| 坑 | 表现 | 正解 |
|---|---|---|
| `character_ids` 存的是**角色 id 不是名字** | 回写成名字 → `db.get(Character, cid)` 查不到 → **人物一致性静默失效**（任务仍 SUCCESS） | 让 `set_shot_bindings` 回写 id |
| 参考图工作流用于无角色镜头 | `qwen_edit_scene` 的 `LoadImage` 拿不到 `{{reference_image}}` → 必失败 | 混有「有角色/无角色」镜头时**逐镜分派**工作流 |
| `GET /api/tasks?project_id=` 返 0 条 | 误判成「没有任务」 | 用不带参数的 `/api/tasks`、`GET /api/tasks/{id}`，或 ComfyUI `/queue` |
| 查 `STALE` 后顺手重编译 | 对历史版本做「就地修复」 | **只增不改**：重新编译产生新版本，历史版本永远不动 |
| 把「镜头没编译」算作违规 | `compliant` 永远 false，闸门失效 | 看 `missing_total`（真违规）与 `not_compiled_total`（覆盖度）两个账 |

---

## 9. 命令速查

```bash
export STUDIO=http://127.0.0.1:8077

# 能力清单（含 JSON Schema）
curl "$STUDIO/api/skills?detail=true" | python -m json.tool | head -50

# 只读端点
curl "$STUDIO/api/projects/proj_xxx/visual-bible"
curl "$STUDIO/api/projects/proj_xxx/continuity-locks"
curl "$STUDIO/api/projects/proj_xxx/prompts?prompt_type=image"
curl "$STUDIO/api/projects/proj_xxx/generation-plans"
curl "$STUDIO/api/prompts/prm_xxx"
curl "$STUDIO/api/assets/ast_xxx/provenance?depth=1"

# 跑测试（不需要 GPU）
cd backend && unset PYTHONPATH && ../.venv/Scripts/python.exe -m pytest
# 真实出图（占 GPU，ComfyUI 不可达则 skip）
../.venv/Scripts/python.exe -m pytest -m gpu -s
```
