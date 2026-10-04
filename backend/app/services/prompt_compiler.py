"""Prompt Compiler —— 把「视觉设定 + 分镜 + 镜头 + 连续性 + 参考图」编译成提示词。

## 为什么需要它

现状是 ``Shot.image_prompt`` 一个就地覆盖的文本字段：改了设定，旧提示词永久丢失，
无法回答「这张图到底是哪版提示词生成的」。本模块把它升级成**版本化 + 可反查**的编译流程。

## 八段式（源自 drama-skills 的 ``common-recipe.md``）

====  ==============  ==========================================================
#     段              来源
====  ==============  ==========================================================
1     用途与主体      本产物是什么（人物设定图 / 场景空镜 / 道具参考 / 局部修改）
2     稳定锚点        身份锚点：可见、可比较
3     版本差异        只写相对基准的变化 + 当前状态
4     构图与尺度      视角、画幅、主体占比、相对位置
5     材质与光线      表面、主次色关系、光向
（—）  **连续性锁**    **单独一步强制注入**：锁面逐字出现在正文，缺失即编译失败
6     背景 / 舞台政策  干净背景 / 环境内展示 / empty_stage
7     文字与功能      道具文字政策 → 呈现方法（两层映射）
8     排除与保留      只写当前最可能的误读 → 进 ``negative_prompt``
====  ==============  ==========================================================

## 三条不可违背的纪律

1. **Prompt 只增不改**：``prompt_versions`` 永不 UPDATE 覆盖，改动只产生新版本
2. **血缘用 id + version，不用哈希**：手工哈希必然腐烂（drama-skills 清点时
   发现 331 个手填哈希全部与字节对不上）
3. **旧字段单向回写**：编译完把正文回写 ``Shot.image_prompt`` / ``video_prompt``
   / ``negative_prompt``，**只写不读**，仅供未迁移的老消费者读取。
   回写入口唯一（:func:`_mirror_to_shot`），且回写失败**不回滚** Prompt 版本
   —— 镜像只是兼容层，丢了不影响真相。

## 关于「读旧字段」的口径

以下字段**不是**兼容投影，可以正常读：``shot.description`` / ``shot.camera`` /
``shot.visual_style`` / ``character.appearance`` / ``project.style``。

真正的兼容投影（只写不读）只有五个：``shot.location`` / ``shot.image_prompt`` /
``shot.video_prompt`` / ``shot.negative_prompt`` / ``shot.character_ids``。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Asset, Character, CharacterLook, ContinuityLock, Location, LocationView, Project,
    Prompt, PromptVersion, Prop, PropState, Shot, VisualBible, VisualStyle, new_id,
)
from . import agent_log, continuity, visual_bible as vb

#: 编译器配方标识（写进 ``prompt_versions.recipe``，用于日后判断「换过编译器没有」）
RECIPE: dict[str, str] = {"renderer": "video-genrate-8seg", "version": "1.0.0"}

PROMPT_TYPES = ("image", "video", "voice", "music")

#: 文字政策 → 允许的呈现方法（**结构强制**，源自 drama-skills 校验器）
TEXT_POLICY_PRESENTATION: dict[str, tuple[str, ...]] = {
    "exact_readable": ("readable", "postproduction"),
    "graphic_only": ("symbolic",),
    "no_readable_text": ("blank", "symbolic_unreadable"),
    "pending_creator_text": ("postproduction",),
}

#: 参考图用途 → 不该由它决定的方面（防止「一张图越权决定服装 / 构图 / 文字」）
_ROLE_MUST_NOT_CONTROL: dict[str, list[str]] = {
    "身份": ["构图", "姿势", "临时造型", "文字"],
    "造型状态": ["构图", "姿势", "身份", "文字"],
    "地理": ["身份", "构图", "人物造型", "文字"],
    "构图": ["身份", "造型状态", "文字"],
    "尺度": ["身份", "构图", "造型状态", "文字"],
    "效果": ["身份", "构图", "造型状态", "文字"],
    "起始帧": ["文字"],
    "结束帧": ["文字"],
    "风格": ["身份", "构图", "具体人物", "文字"],
}


class CompileError(ValueError):
    """编译失败（``structural_invariant`` 级）—— 缺输入、注锁失败、正文不合卫生等。"""


# --------------------------------------------------------------------------- #
# 正文卫生（照搬 image_prompt_check.py 的强制项）
# --------------------------------------------------------------------------- #
_HYGIENE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[0-9a-fA-F]{64}\b"), "64 位十六进制哈希"),
    (re.compile(r"<\s*sha256\s*>", re.IGNORECASE), "哈希占位符"),
    (re.compile(r"--(ar|v|q|niji|style|no|seed|cref|sref|chaos|stylize)\b"), "引擎专用参数语法"),
    (re.compile(r"::\s*-?\d+(\.\d+)?"), "引擎权重语法 ::"),
    (re.compile(r":\(\s*[01](\.\d+)?\s*\)"), "引擎权重语法 :(n)"),
    (re.compile(
        r"\b(compiled_from|prompt_version_id|reference_assets|visual_bible|compiled_prompt"
        r"|stale_reason|asset_bindings|continuity_lock_ids)\b"
    ), "JSON 键名 / 字段路径"),
    (re.compile(r"\b(CON|IMG|DELTA|PSTATE|LOOK|VIEW|PROP|CHAR|LOCK)-\d"), "规则编号 / 内部代号"),
    (re.compile(r"[A-Za-z]:[\\/]|/storage/|\\storage\\|\bstorage/"), "文件路径 / 盘符"),
    (re.compile(r"(请模型务必|务必生成|重试历史|第\s*\d+\s*次尝试)"), "流程说明 / 重试历史"),
)


def validate_hygiene(text: str) -> list[str]:
    """检查正文卫生，返回问题列表（空 = 通过）。"""
    if not text:
        return []
    problems: list[str] = []
    for pattern, label in _HYGIENE_RULES:
        found = pattern.search(text)
        if found:
            problems.append(f"正文含禁止内容（{label}）：{found.group(0)!r}")
    return problems


# --------------------------------------------------------------------------- #
# 编译输入
# --------------------------------------------------------------------------- #
@dataclass
class Subject:
    """一个参与编译的主体（身份 + 当前变体）。"""

    kind: str                                   # character / location / prop / style
    entity: Any
    variant: Any = None                         # CharacterLook / LocationView / PropState
    role: str = ""

    @property
    def display_name(self) -> str:
        return getattr(self.entity, "display_name", "") or getattr(self.entity, "name", "") or ""

    def snapshot(self) -> dict[str, Any]:
        """血缘快照：**id + version**，不用哈希。"""
        data: dict[str, Any] = {
            "kind": self.kind,
            "id": self.entity.id,
            "updated_at": _iso(getattr(self.entity, "updated_at", None)),
        }
        version = getattr(self.entity, "version", None)
        if version is not None:
            data["version"] = version
        if self.variant is not None:
            data["variant"] = {
                "id": self.variant.id,
                "code": getattr(self.variant, "code", ""),
                "updated_at": _iso(getattr(self.variant, "updated_at", None)),
            }
        return data


@dataclass
class CompiledInputs:
    project: Project
    shot: Shot
    prompt_type: str
    bible: VisualBible | None
    style: VisualStyle | None
    subjects: list[Subject] = field(default_factory=list)
    locks: list[ContinuityLock] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    raw_prompt: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def entity(self, kind: str) -> Any | None:
        for subject in self.subjects:
            if subject.kind == kind:
                return subject.entity
        return None


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _asset_slot(
    db: Session, asset_id: str | None, *, slot: str, order: int, role: str,
    label: str = "", kind: str = "REF",
) -> dict[str, Any] | None:
    """构造一个参考图槽位。资产不存在 → 返回 None（由调用方决定是警告还是失败）。"""
    if not asset_id:
        return None
    asset = db.get(Asset, asset_id)
    if asset is None:
        return {
            "slot": slot, "order": order, "asset_id": asset_id, "kind": kind,
            "role": role, "label": label or slot,
            "may_control": [role], "must_not_control": _ROLE_MUST_NOT_CONTROL.get(role, []),
            "admission_status": "missing_asset",
        }
    if asset.status != "READY":
        admission = "not_ready"
    else:
        admission = "ready"
    return {
        "slot": slot,
        "order": order,
        "asset_id": asset.id,
        "kind": kind,
        "role": role,
        "label": label or asset.name or slot,
        "file_path": asset.file_path,
        "may_control": [role],
        "must_not_control": _ROLE_MUST_NOT_CONTROL.get(role, []),
        "admission_status": admission,
    }


def assemble_inputs(
    db: Session, shot: Shot, prompt_type: str, *, options: dict[str, Any] | None = None,
    raw_prompt: str | None = None,
) -> CompiledInputs:
    """装配 §6.1 的全部输入。**不写库**（只读）。"""
    if prompt_type not in PROMPT_TYPES:
        raise CompileError(f"未知 Prompt 类型「{prompt_type}」，可选：{', '.join(PROMPT_TYPES)}")
    project = db.get(Project, shot.project_id)
    if project is None:
        raise CompileError(f"镜头所属项目不存在：{shot.project_id}")

    bible = vb.get_or_create_bible(db, project.id)
    style = vb.current_style(db, project.id)

    subjects: list[Subject] = []
    for binding in vb.resolve_bindings(db, shot):
        kind = binding.get("kind")
        entity = db.get({"character": Character, "location": Location, "prop": Prop}.get(kind, Character), binding["id"]) \
            if kind in ("character", "location", "prop") else None
        if entity is None:
            continue
        variant = _resolve_variant(db, kind, entity, binding.get("variant_id"))
        subjects.append(Subject(kind=kind, entity=entity, variant=variant, role=binding.get("role", "")))

    # ``location_id`` 是地点绑定的权威来源；asset_bindings 里没写地点时用它兜底
    if not any(s.kind == "location" for s in subjects) and shot.location_id:
        location = db.get(Location, shot.location_id)
        if location is not None:
            view = db.get(LocationView, shot.location_view_id) if shot.location_view_id else None
            subjects.append(Subject(kind="location", entity=location, variant=view))

    if style is not None:
        subjects.append(Subject(kind="style", entity=style))

    references = _build_references(db, shot, subjects, prompt_type)
    locks = continuity.locks_for_shot(db, shot)

    resolved_raw = raw_prompt
    if resolved_raw is None:
        resolved_raw = _latest_compiled(db, shot, prompt_type) or ""

    return CompiledInputs(
        project=project, shot=shot, prompt_type=prompt_type, bible=bible, style=style,
        subjects=subjects, locks=locks, references=references, raw_prompt=resolved_raw,
        options=dict(options or {}),
    )


def _resolve_variant(db: Session, kind: str, entity: Any, variant_id: str | None) -> Any:
    if variant_id:
        return db.get(
            {"character": CharacterLook, "location": LocationView, "prop": PropState}[kind], variant_id
        )
    if kind == "character":
        return vb.current_look(db, entity.id)
    if kind == "location":
        return db.execute(
            select(LocationView).where(
                LocationView.location_id == entity.id, LocationView.is_current.is_(True)
            )
        ).scalars().first()
    if kind == "prop":
        return db.execute(
            select(PropState).where(PropState.prop_id == entity.id, PropState.is_current.is_(True))
        ).scalars().first()
    return None


def _build_references(
    db: Session, shot: Shot, subjects: list[Subject], prompt_type: str
) -> list[dict[str, Any]]:
    """按「身份 → 造型状态 → 地理 → 尺度 → 起始帧」顺序凑参考图槽位。"""
    slots: list[dict[str, Any]] = []
    order = 1
    for subject in subjects:
        label = subject.display_name
        if subject.kind == "character":
            slot = _asset_slot(
                db, getattr(subject.variant, "reference_asset_id", None) or subject.entity.reference_asset_id,
                slot=f"REF-IDENTITY-{getattr(subject.entity, 'code', '') or subject.entity.id}",
                order=order, role="身份", label=f"{label} 定妆图",
            )
            if slot:
                slots.append(slot)
                order += 1
            if subject.variant is not None:
                slot = _asset_slot(
                    db, getattr(subject.variant, "reference_asset_id", None),
                    slot=f"REF-LOOK-{subject.variant.code or subject.variant.id}",
                    order=order, role="造型状态", label=f"{label} 造型参考",
                )
                if slot:
                    slots.append(slot)
                    order += 1
        elif subject.kind == "location":
            slot = _asset_slot(
                db, getattr(subject.variant, "reference_asset_id", None) or subject.entity.reference_asset_id,
                slot=f"REF-LOCATION-{getattr(subject.entity, 'code', '') or subject.entity.id}",
                order=order, role="地理", label=f"{label} 场景参考",
            )
            if slot:
                slots.append(slot)
                order += 1
        elif subject.kind == "prop":
            slot = _asset_slot(
                db, getattr(subject.variant, "reference_asset_id", None) or subject.entity.reference_asset_id,
                slot=f"REF-PROP-{getattr(subject.entity, 'code', '') or subject.entity.id}",
                order=order, role="尺度", label=f"{label} 道具参考",
            )
            if slot:
                slots.append(slot)
                order += 1
    if prompt_type == "video" and shot.image_asset_id:
        slot = _asset_slot(
            db, shot.image_asset_id, slot="REF-FIRSTFRAME", order=order,
            role="起始帧", label=f"镜头 {shot.code or shot.sequence} 关键帧",
        )
        if slot:
            slots.append(slot)
    return slots


def _latest_compiled(db: Session, shot: Shot, prompt_type: str) -> str:
    """上一次编译的正文（作为 ``raw_prompt`` 基线）。

    注意：读的是 **PromptVersion**（新真相），不是旧的 ``Shot.image_prompt``。
    """
    prompt = db.execute(
        select(Prompt).where(
            Prompt.project_id == shot.project_id, Prompt.shot_id == shot.id,
            Prompt.type == prompt_type,
        )
    ).scalars().first()
    if prompt is None or not prompt.current_version_id:
        return ""
    version = db.get(PromptVersion, prompt.current_version_id)
    return (version.compiled_prompt if version else "") or ""


# --------------------------------------------------------------------------- #
# 八段式渲染
# --------------------------------------------------------------------------- #
_PURPOSE_LABEL = {
    "image": "关键帧画面",
    "video": "镜头运动",
    "voice": "旁白配音",
    "music": "配乐",
}


def _join(parts: list[str]) -> str:
    return "，".join(p.strip().rstrip("，。；") for p in parts if p and p.strip())


def _identity_anchors(subject: Subject) -> list[str]:
    entity = subject.entity
    if subject.kind == "character":
        anchors = list(entity.identity_anchors or [])
        if not anchors and entity.appearance:
            # ``appearance`` 是保留字段（不是兼容投影），空锚点时作退化输入
            anchors = [entity.appearance]
        return anchors
    if subject.kind == "location":
        spatial = entity.spatial_identity or {}
        anchors = []
        if spatial.get("shape"):
            anchors.append(spatial["shape"])
        anchors.extend(spatial.get("fixed_anchors") or [])
        anchors.extend(f"材质：{m}" for m in (spatial.get("materials") or []))
        if not anchors and entity.description:
            anchors = [entity.description]
        return anchors
    if subject.kind == "prop":
        anchors_map = entity.identity_anchors or {}
        anchors = []
        if anchors_map.get("scale_and_form"):
            anchors.append(anchors_map["scale_and_form"])
        anchors.extend(anchors_map.get("permanent_marks") or [])
        anchors.extend(f"材质：{m}" for m in (anchors_map.get("materials") or []))
        if anchors_map.get("function"):
            anchors.append(f"用途：{anchors_map['function']}")
        return anchors
    return []


def _variant_differences(subject: Subject) -> list[str]:
    variant = subject.variant
    if variant is None:
        return []
    if subject.kind == "character":
        diffs = variant.differences or {}
        out: list[str] = []
        for key, label in (
            ("wardrobe_layers", "着装"), ("hair_styling", "发型"),
            ("makeup", "妆容"), ("injury", "伤势"), ("weathering", "磨损"),
        ):
            values = diffs.get(key) or []
            if values:
                out.append(f"{label}：{'、'.join(str(v) for v in values)}")
        if variant.valid_from or variant.valid_until:
            out.append(f"生效区间 {variant.valid_from or '—'} ~ {variant.valid_until or '开区间'}")
        return out
    if subject.kind == "location":
        state = variant.state_differences or {}
        out = []
        if state.get("time"):
            out.append(f"时段：{state['time']}")
        if state.get("weather"):
            out.append(f"天气：{state['weather']}")
        if state.get("dressing"):
            out.append(f"陈设：{'、'.join(str(v) for v in state['dressing'])}")
        if state.get("light"):
            out.append(f"光线：{'、'.join(str(v) for v in state['light'])}")
        return out
    if subject.kind == "prop":
        condition = variant.condition or {}
        out = []
        if condition.get("summary"):
            out.append(str(condition["summary"]))
        else:
            if condition.get("open") is not None:
                out.append("开启状态" if condition["open"] else "闭合状态")
            if condition.get("damage"):
                out.append(f"损伤：{condition['damage']}")
            if condition.get("powered") is not None:
                out.append("通电中" if condition["powered"] else "未通电")
        custody = variant.custody or {}
        if custody.get("holder_id") or custody.get("hand"):
            out.append(f"持有：{custody.get('holder_id') or ''}{custody.get('hand') or ''}".strip())
        if variant.contents:
            out.append(f"内含 {len(variant.contents)} 件物品")
        return out
    return []


def render_segments(inputs: CompiledInputs) -> tuple[list[dict[str, str]], str, str]:
    """渲染八段。返回 ``(段列表, 正向正文, 负向正文)``。"""
    shot = inputs.shot
    segments: list[dict[str, str]] = []

    # --- 1. 用途与主体 ----------------------------------------------------- #
    lead = f"{_PURPOSE_LABEL.get(inputs.prompt_type, '画面')}（镜头 {shot.code or shot.sequence}）"
    names = [s.display_name for s in inputs.subjects if s.kind != "style"]
    body = "；".join(filter(None, [
        f"主体：{'、'.join(names)}" if names else "",
        shot.description or "",
    ]))
    segments.append({"key": "purpose", "label": "用途与主体", "text": _join([lead, body])})

    # --- 2. 稳定锚点 ------------------------------------------------------- #
    anchor_parts: list[str] = []
    for subject in inputs.subjects:
        anchors = _identity_anchors(subject)
        if anchors:
            anchor_parts.append(f"{subject.display_name}：{'、'.join(str(a) for a in anchors)}")
    if anchor_parts:
        segments.append({"key": "anchors", "label": "稳定锚点", "text": _join(anchor_parts)})

    # --- 3. 版本差异 ------------------------------------------------------- #
    diff_parts: list[str] = []
    for subject in inputs.subjects:
        diffs = _variant_differences(subject)
        if diffs:
            variant_name = getattr(subject.variant, "name", "")
            diff_parts.append(f"{subject.display_name}{f'（{variant_name}）' if variant_name else ''}：{'；'.join(diffs)}")
    if diff_parts:
        segments.append({"key": "variant", "label": "版本差异", "text": _join(diff_parts)})

    # --- 4. 构图与尺度 ----------------------------------------------------- #
    framing = shot.framing or {}
    composition = "；".join(filter(None, [
        f"景别：{framing.get('size')}" if framing.get("size") else "",
        f"机位角度：{framing.get('angle')}" if framing.get("angle") else "",
        f"机高：{framing.get('camera_height')}" if framing.get("camera_height") else "",
        f"画幅说明：{framing.get('aspect_notes')}" if framing.get("aspect_notes") else "",
        f"运镜：{shot.camera}" if shot.camera else "",
    ]))
    scale_notes = [f"{r['role']}参考：{r.get('label', '')}" for r in inputs.references
                   if r.get("role") in ("构图", "尺度")]
    if composition or scale_notes:
        segments.append({
            "key": "composition", "label": "构图与尺度",
            "text": _join([composition, *scale_notes]),
        })

    # --- 5. 材质、色彩与光线 ----------------------------------------------- #
    style = inputs.style
    look_parts: list[str] = []
    if style is not None:
        rendering = style.rendering or {}
        if rendering.get("surface") or rendering.get("texture"):
            look_parts.append(f"材质：{rendering.get('surface') or rendering.get('texture')}")
        if style.lighting:
            look_parts.append(f"光线：{_flatten(style.lighting)}")
        if style.palette:
            look_parts.append(f"色彩：{_flatten(style.palette)}")
    if shot.visual_style:
        look_parts.append(shot.visual_style)
    for subject in inputs.subjects:
        if subject.kind == "location" and subject.variant is not None:
            light = (subject.variant.state_differences or {}).get("light")
            if light:
                look_parts.append(f"场景光线：{'、'.join(str(x) for x in light)}")
    if look_parts:
        segments.append({"key": "look", "label": "材质、色彩与光线", "text": _join(look_parts)})

    # --- 连续性锁（强制注入，独立一步）------------------------------------- #
    if inputs.locks:
        surfaces = [lock.surface for lock in inputs.locks if lock.surface]
        segments.append({
            "key": "continuity", "label": "连续性锁",
            "text": _join(surfaces),
        })

    # --- 6. 背景 / 舞台政策 ------------------------------------------------ #
    stage_parts: list[str] = []
    global_rules = (inputs.bible.global_rules if inputs.bible else {}) or {}
    if global_rules.get("stage_policy"):
        stage_parts.append(str(global_rules["stage_policy"]))
    if style is not None and style.composition_rules:
        stage_parts.append(_flatten(style.composition_rules))
    for subject in inputs.subjects:
        if subject.kind == "location":
            dressing = ((subject.variant.state_differences or {}).get("dressing")
                        if subject.variant is not None else None)
            if dressing:
                stage_parts.append(f"环境陈设：{'、'.join(str(x) for x in dressing)}")
    if stage_parts:
        segments.append({"key": "stage", "label": "背景与舞台", "text": _join(stage_parts)})

    # --- 7. 文字与功能 ----------------------------------------------------- #
    text_parts: list[str] = []
    for subject in inputs.subjects:
        if subject.kind != "prop":
            continue
        policy = subject.entity.text_policy or {}
        mode = policy.get("mode")
        if not mode:
            continue
        methods = TEXT_POLICY_PRESENTATION.get(mode, ())
        text_parts.append(
            f"{subject.display_name}上的文字：{_PRESENTATION_ZH.get(methods[0], methods[0])}"
            + (f"（内容：{policy.get('text')}）" if policy.get("text") else "")
        )
    if text_parts:
        segments.append({"key": "text", "label": "文字与功能", "text": _join(text_parts)})

    # --- 8. 排除与保留 → 负向正文 ----------------------------------------- #
    negative_parts: list[str] = []
    if style is not None and style.negative_rules:
        negative_parts.append(_flatten(style.negative_rules))
    global_negative = global_rules.get("negative")
    if global_negative:
        negative_parts.append(_flatten(global_negative) if isinstance(global_negative, dict) else str(global_negative))
    extra_negative = inputs.options.get("extra_negative")
    if extra_negative:
        negative_parts.append(str(extra_negative))

    positive = "\n".join(seg["text"] for seg in segments if seg["text"])
    negative = _join(negative_parts)
    return segments, positive, negative


_PRESENTATION_ZH = {
    "readable": "清晰可读",
    "postproduction": "后期合成",
    "symbolic": "以图形符号示意",
    "symbolic_unreadable": "以不可辨识的抽象笔触示意",
    "blank": "留白不写文字",
}


def _flatten(value: Any) -> str:
    """把 JSON 结构压成一句人话（嵌套列表/字典都不怕）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "、".join(_flatten(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        return "；".join(f"{k}：{_flatten(v)}" for k, v in value.items() if v not in (None, "", [], {}))
    return str(value)


# --------------------------------------------------------------------------- #
# 文字政策校验（结构强制：readable 与「全局无文字」不能共存）
# --------------------------------------------------------------------------- #
def validate_text_policy(inputs: CompiledInputs) -> list[str]:
    problems: list[str] = []
    policy = (inputs.bible.text_policy if inputs.bible else {}) or {}
    global_no_text = bool(policy.get("no_readable_text")) or policy.get("readable_text_allowed") is False

    for subject in inputs.subjects:
        if subject.kind != "prop":
            continue
        prop_policy = subject.entity.text_policy or {}
        mode = prop_policy.get("mode")
        if not mode:
            continue
        if mode not in TEXT_POLICY_PRESENTATION:
            problems.append(
                f"道具「{subject.display_name}」的文字政策「{mode}」不合法，"
                f"可选：{', '.join(TEXT_POLICY_PRESENTATION)}"
            )
            continue
        if mode == "exact_readable" and global_no_text:
            problems.append(
                f"冲突：道具「{subject.display_name}」要求文字清晰可读，"
                "但视觉设定声明全局不出现可读文字 —— 二者不可共存"
            )
        if mode == "exact_readable" and not (prop_policy.get("text") or "").strip():
            problems.append(f"道具「{subject.display_name}」声明文字可读，但没有给精确文字内容")
    return problems


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def compile_prompt(
    db: Session, shot: Shot, *, prompt_type: str = "image",
    options: dict[str, Any] | None = None, raw_prompt: str | None = None,
    mirror: bool = True, actor: str = "agent",
) -> PromptVersion:
    """编译并落一个**新版本**。返回新建的 :class:`PromptVersion`。

    任何 ``structural_invariant`` 级问题（缺输入、注锁失败、正文不合卫生、
    文字政策冲突）都会抛 :class:`CompileError`，**不产生半成品记录**。
    """
    inputs = assemble_inputs(db, shot, prompt_type, options=options, raw_prompt=raw_prompt)

    segments, positive, negative = render_segments(inputs)

    # 注锁校验：锁面必须逐字出现在正向正文里（负面提示词里的命中不算）
    lock_problems = continuity.verify_surfaces(positive, negative, inputs.locks)
    if lock_problems:
        raise CompileError(
            "连续性锁注入失败：" + "；".join(p["message"] for p in lock_problems)
        )

    hygiene = validate_hygiene(positive)
    if hygiene:
        raise CompileError("正文不合卫生：" + "；".join(hygiene))

    policy_problems = validate_text_policy(inputs)
    if policy_problems:
        raise CompileError("文字政策校验失败：" + "；".join(policy_problems))

    prompt = _get_or_create_prompt(db, shot, prompt_type)
    version_no = (prompt.latest_version or 0) + 1
    opts = inputs.options

    version = PromptVersion(
        id=new_id("pv"),
        prompt_id=prompt.id,
        version=version_no,
        raw_prompt=inputs.raw_prompt or "",
        compiled_prompt=positive,
        negative_prompt=negative,
        compiled_from=_snapshot(inputs, segments),
        recipe=dict(RECIPE),
        model=str(opts.get("model") or ""),
        provider=str(opts.get("provider") or ""),
        width=int(opts.get("width") or 0),
        height=int(opts.get("height") or 0),
        aspect_ratio=str(opts.get("aspect_ratio") or ""),
        resolution=str(opts.get("resolution") or ""),
        parameters=dict(opts.get("parameters") or {}),
        reference_assets=inputs.references,
        continuity_lock_ids=[lock.id for lock in inputs.locks],
        status="READY",
        created_by=actor,
    )
    db.add(version)
    db.flush()

    prompt.latest_version = version_no
    prompt.current_version_id = version.id
    prompt.name = prompt.name or f"镜头 {shot.code or shot.sequence} · {_PURPOSE_LABEL.get(prompt_type, prompt_type)}"

    if mirror:
        _mirror_to_shot(shot, prompt_type, positive, negative)

    db.flush()
    agent_log.log_event(
        db, project_id=shot.project_id, shot_id=shot.id, event="prompt.compiled", actor=actor,
        message=f"编译 {prompt_type} 提示词 v{version_no}（镜头 {shot.code or shot.sequence}）",
        detail={
            "prompt_id": prompt.id, "version": version_no,
            "locks": [lock.code for lock in inputs.locks],
            "references": [r.get("slot") for r in inputs.references],
        },
    )
    return version


def _snapshot(inputs: CompiledInputs, segments: list[dict[str, str]]) -> dict[str, Any]:
    """``compiled_from`` —— 编译输入快照（实体 id + 版本号，不是哈希）。

    ⚠️ **不含 shot.updated_at**：回写兼容投影会 bump 它，让它进快照会导致
    「刚编译完就判定 stale」的死循环。
    """
    return {
        "bible": {"id": inputs.bible.id, "version": inputs.bible.version} if inputs.bible else None,
        "style": {"id": inputs.style.id, "version": inputs.style.version} if inputs.style else None,
        "subjects": [s.snapshot() for s in inputs.subjects],
        "locks": [
            {"id": lock.id, "code": lock.code, "version": lock.version, "surface": lock.surface}
            for lock in inputs.locks
        ],
        "shot": {"id": inputs.shot.id, "code": inputs.shot.code},
        "segments": [seg["key"] for seg in segments],
        "project": {"id": inputs.project.id},
    }


def _prompt_code(db: Session, shot: Shot, prompt_type: str) -> str:
    prefix = {"image": "IMG", "video": "MOTION", "voice": "VO", "music": "BGM"}.get(prompt_type, "PRM")
    base = f"{prefix}-{shot.code or f'{shot.sequence:03d}'}"
    taken = {
        code for (code,) in db.execute(
            select(Prompt.code).where(Prompt.project_id == shot.project_id)
        ).all()
    }
    if base not in taken:
        return base
    existing = db.execute(
        select(Prompt.id).where(Prompt.project_id == shot.project_id, Prompt.code == base)
    ).scalar()
    if existing:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _get_or_create_prompt(db: Session, shot: Shot, prompt_type: str) -> Prompt:
    code = _prompt_code(db, shot, prompt_type)
    prompt = db.execute(
        select(Prompt).where(Prompt.project_id == shot.project_id, Prompt.code == code)
    ).scalars().first()
    if prompt is not None:
        return prompt
    prompt = Prompt(
        id=new_id("prm"), project_id=shot.project_id, shot_id=shot.id,
        type=prompt_type, code=code, name="", latest_version=0,
    )
    db.add(prompt)
    db.flush()
    return prompt


def _mirror_to_shot(shot: Shot, prompt_type: str, positive: str, negative: str) -> None:
    """**单向兼容回写**（全工程唯一入口）。

    旧字段不再是真相，这里只是把最新正文复制一份给未迁移的老消费者读。
    回写失败（如 Shot 已删除）**不影响** Prompt 版本 —— 镜像丢了不损失真相。
    """
    if prompt_type == "image":
        shot.image_prompt = positive
    elif prompt_type == "video":
        shot.video_prompt = positive
    if negative:
        shot.negative_prompt = negative


# --------------------------------------------------------------------------- #
# staleness（动态判定，不改旧记录）
# --------------------------------------------------------------------------- #
def check_prompt_staleness(db: Session, prompt: Prompt) -> dict[str, Any]:
    """当前版本是否已过期（编译输入变了）。

    返回 ``{"stale": bool, "reasons": [...], "checked": [...]}``。
    **只读**，不修改任何记录；需要更新时由 Agent 决定是否重新编译。
    """
    reasons: list[str] = []
    checked: list[str] = []

    version = db.get(PromptVersion, prompt.current_version_id) if prompt.current_version_id else None
    if version is None:
        return {"stale": False, "reasons": [], "checked": [], "note": "尚无当前版本"}

    snapshot = version.compiled_from or {}
    if snapshot.get("legacy_backfill"):
        return {
            "stale": False, "reasons": [], "checked": [],
            "note": "迁移前产物的历史 Prompt，没有编译快照可比对",
        }

    bible_ref = snapshot.get("bible")
    if bible_ref:
        bible = db.get(VisualBible, bible_ref["id"])
        checked.append("visual_bible")
        if bible is None:
            reasons.append(f"视觉设定已删除（{bible_ref['id']}）")
        elif bible.version != bible_ref.get("version"):
            reasons.append(f"视觉设定版本变化：{bible_ref.get('version')} → {bible.version}")

    style_ref = snapshot.get("style")
    if style_ref:
        style = db.get(VisualStyle, style_ref["id"])
        checked.append("visual_style")
        if style is None:
            reasons.append(f"视觉风格已删除（{style_ref['id']}）")
        elif style.version != style_ref.get("version"):
            reasons.append(f"视觉风格版本变化：{style_ref.get('version')} → {style.version}")

    model_by_kind = {"character": Character, "location": Location, "prop": Prop}
    for item in snapshot.get("subjects") or []:
        model = model_by_kind.get(item.get("kind"))
        if model is None:
            continue
        checked.append(f"{item['kind']}:{item['id']}")
        entity = db.get(model, item["id"])
        if entity is None:
            reasons.append(f"{item['kind']} 已删除（{item['id']}）")
            continue
        current_version = getattr(entity, "version", None)
        if current_version is not None and item.get("version") is not None \
                and current_version != item["version"]:
            reasons.append(
                f"{item['kind']} {getattr(entity, 'name', item['id'])} 版本变化："
                f"{item['version']} → {current_version}"
            )
        elif _iso(getattr(entity, "updated_at", None)) != item.get("updated_at"):
            reasons.append(f"{item['kind']} {getattr(entity, 'name', item['id'])} 内容已修改")
        variant_ref = item.get("variant")
        if variant_ref:
            variant = db.get(
                {"character": CharacterLook, "location": LocationView, "prop": PropState}[item["kind"]],
                variant_ref["id"],
            )
            if variant is None:
                reasons.append(f"{item['kind']} 的造型/视图/状态已删除（{variant_ref['id']}）")
            elif _iso(getattr(variant, "updated_at", None)) != variant_ref.get("updated_at"):
                reasons.append(f"{item['kind']} 的变体「{variant_ref.get('code')}」已修改")

    for lock_ref in snapshot.get("locks") or []:
        checked.append(f"lock:{lock_ref['id']}")
        lock = db.get(ContinuityLock, lock_ref["id"])
        if lock is None:
            reasons.append(f"连续性锁已删除（{lock_ref.get('code')}）")
            continue
        if lock.surface != lock_ref.get("surface"):
            reasons.append(
                f"连续性锁 {lock.code} 的锁面变化："
                f"{lock_ref.get('surface')} → {lock.surface}（锁面变了等于脸变了）"
            )
        elif lock.version != lock_ref.get("version"):
            reasons.append(f"连续性锁 {lock.code} 版本变化：{lock_ref.get('version')} → {lock.version}")

    return {"stale": bool(reasons), "reasons": reasons, "checked": checked}


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def segment_text(version: PromptVersion, key: str) -> str:
    """从已编译正文里取某一段（供 UI 折叠展示）。"""
    segments = (version.compiled_from or {}).get("segments") or []
    if key not in segments:
        return ""
    return version.compiled_prompt or ""


def version_brief(version: PromptVersion, *, include_prompt: bool = False) -> dict[str, Any]:
    data = {
        "id": version.id,
        "prompt_id": version.prompt_id,
        "version": version.version,
        "negative_prompt": version.negative_prompt,
        "compiled_from": version.compiled_from or {},
        "recipe": version.recipe or {},
        "model": version.model,
        "provider": version.provider,
        "width": version.width,
        "height": version.height,
        "aspect_ratio": version.aspect_ratio,
        "resolution": version.resolution,
        "parameters": version.parameters or {},
        "reference_assets": version.reference_assets or [],
        "continuity_lock_ids": version.continuity_lock_ids or [],
        "status": version.status,
        "stale_reason": version.stale_reason,
        "created_by": version.created_by,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }
    if include_prompt:
        data["raw_prompt"] = version.raw_prompt
        data["compiled_prompt"] = version.compiled_prompt
    return data


def prompt_brief(
    prompt: Prompt, *, versions: list[PromptVersion] | None = None, include_prompt: bool = False
) -> dict[str, Any]:
    data = {
        "id": prompt.id,
        "project_id": prompt.project_id,
        "shot_id": prompt.shot_id,
        "type": prompt.type,
        "code": prompt.code,
        "name": prompt.name,
        "current_version_id": prompt.current_version_id,
        "latest_version": prompt.latest_version,
        "created_at": prompt.created_at.isoformat() if prompt.created_at else None,
        "updated_at": prompt.updated_at.isoformat() if prompt.updated_at else None,
    }
    if versions is not None:
        data["versions"] = [version_brief(v, include_prompt=include_prompt) for v in versions]
    return data
