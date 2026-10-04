"""视觉设定 / 连续性 / Prompt 编译器 / 生成计划的 Skill 契约。

给 Agent 调用。设计原则：**不为凑数建 CRUD**，每个 Skill 都对应一段真实工作流。
写操作全部走 Skill 层（与现有约定一致），读接口另有 HTTP 端点。

命名与 drama-skills 的对应关系：

===============================  ==========================================
本工程 Skill                     drama-skills 里的对应动作
===============================  ==========================================
``create_visual_bible``          《视觉设定》建立
``create_look`` / ``create_location_view`` / ``create_prop_state``
                                 身份 → 变体的拆分
``create_continuity_lock``       ``LOCK-`` 建立（含锁面卫生校验）
``create_continuity_delta``      ``DELTA-`` 建立
``compile_image_prompt``         图片提示词编译
``verify_continuity_locks``      校验器（锁面是否逐字进了正文）
``create_generation_plan`` 等     生产四步闸门
===============================  ==========================================
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..models import (
    Asset, Character, ContinuityLock, GenerationPlan, Prompt, PromptVersion, Project, Shot,
)
from ..services import continuity as continuity_svc
from ..services import generation_plans as plans_svc
from ..services import prompt_compiler as compiler_svc
from ..services import provenance as provenance_svc
from ..services import visual_bible as vb_svc
from .base import SkillContext, SkillError, skill

VISUAL = "visual"
PLAN = "plan"

_STR = {"type": "string"}
_JSON = {"type": "object"}
_ARR = {"type": "array"}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _require_project(ctx: SkillContext, project_id: str) -> Project:
    project = ctx.db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在：{project_id}", code="NOT_FOUND")
    return project


def _require_shot(ctx: SkillContext, shot_id: str) -> Shot:
    shot = ctx.db.get(Shot, shot_id)
    if shot is None:
        raise SkillError(f"镜头不存在：{shot_id}", code="NOT_FOUND")
    return shot


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    """剔除取值为 None 的键。

    Skill 层有 JSON Schema 校验，显式传 ``null`` 会被判类型错误
    （``None is not of type 'string'``），所以调用方与服务之间统一约定：
    **不传 = 不改**，绝不传 null。
    """
    return {k: v for k, v in payload.items() if v is not None}


def _assert_not_none(args: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    bad = [k for k in keys if k in args and args[k] is None]
    if bad:
        raise SkillError(
            f"以下字段不能显式传 null：{', '.join(bad)}。不传即表示不修改。", code="BAD_INPUT"
        )
    return args


# =========================================================================== #
# 视觉设定（Visual Bible）
# =========================================================================== #
@skill(
    name="create_visual_bible",
    description="为项目建立/更新视觉设定总纲（视觉一句话、时代锚点、全局视觉规则、全局文字政策）。",
    category=VISUAL,
    tags=("visual_bible", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": _STR,
            "title": _STR,
            "visual_logline": _STR,
            "era_anchors": _JSON,
            "global_rules": _JSON,
            "text_policy": _JSON,
            "status": {"type": "string", "enum": ["DRAFT", "ACTIVE", "LOCKED"]},
        },
        "required": ["project_id"],
    },
    examples=({"project_id": "proj_xxx", "visual_logline": "冷调静谧的深空舱内",
               "global_rules": {"stage_policy": "环境内展示"}},),
)
def create_visual_bible(ctx: SkillContext, project_id: str, **fields: Any) -> dict[str, Any]:
    project = _require_project(ctx, project_id)
    _assert_not_none(fields, ("title", "visual_logline", "era_anchors", "global_rules",
                              "text_policy", "status"))
    bible = vb_svc.update_bible(ctx.db, project, actor=ctx.actor, **_drop_none(fields))
    return {"bible": vb_svc.bible_brief(bible)}


@skill(
    name="get_visual_bible",
    description="读取项目的视觉设定总纲与全部风格；项目尚无设定时会自动建一条空的。",
    category=VISUAL,
    tags=("visual_bible", "read"),
    input_schema={"type": "object", "properties": {"project_id": _STR}, "required": ["project_id"]},
)
def get_visual_bible(ctx: SkillContext, project_id: str) -> dict[str, Any]:
    project = _require_project(ctx, project_id)
    bible = vb_svc.get_or_create_bible(ctx.db, project.id)
    styles = list(ctx.db.execute(
        select(vb_svc.VisualStyle).where(vb_svc.VisualStyle.project_id == project.id)
    ).scalars().all())
    current = vb_svc.current_style(ctx.db, project.id)
    return {
        "bible": vb_svc.bible_brief(bible, styles=styles),
        "current_style": vb_svc.style_brief(current) if current else None,
        "reference_roles": list(vb_svc.REFERENCE_ROLES),
        "form_cards": list(vb_svc.FORM_CARDS),
    }


@skill(
    name="create_style",
    description="新建视觉风格（形态卡 + 材质/光色/运镜/构图/负向规则）。form_card 必须是六种形态之一。",
    category=VISUAL,
    tags=("visual_bible", "style", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": _STR, "name": _STR,
            "form_card": {"type": "string", "enum": list(vb_svc.FORM_CARDS)},
            "narrative_duty": _STR, "identity_carrier": _JSON, "continuity_carriers": _JSON,
            "layer_split": _JSON, "rendering": _JSON, "lighting": _JSON, "palette": _JSON,
            "camera_language": _JSON, "composition_rules": _JSON, "motion_budget": _JSON,
            "negative_rules": _JSON,
            "set_current": {"type": "boolean"},
        },
        "required": ["project_id", "name"],
    },
)
def create_style(ctx: SkillContext, project_id: str, name: str, form_card: str = "",
                 set_current: bool = False, **fields: Any) -> dict[str, Any]:
    project = _require_project(ctx, project_id)
    style = vb_svc.create_style(
        ctx.db, project, name=name, form_card=form_card or "", set_current=set_current,
        actor=ctx.actor, **_drop_none(fields),
    )
    return {"style": vb_svc.style_brief(style), "is_current": style.is_current}


@skill(
    name="set_current_style",
    description="把某个风格设为项目当前生效风格（同时只允许一个）。",
    category=VISUAL,
    tags=("visual_bible", "style", "write"),
    input_schema={"type": "object",
                  "properties": {"project_id": _STR, "style_id": _STR},
                  "required": ["project_id", "style_id"]},
)
def set_current_style(ctx: SkillContext, project_id: str, style_id: str) -> dict[str, Any]:
    project = _require_project(ctx, project_id)
    style = vb_svc.set_current_style(ctx.db, project, style_id, actor=ctx.actor)
    return {"style": vb_svc.style_brief(style)}


# =========================================================================== #
# 资产：身份 / 变体
# =========================================================================== #
@skill(
    name="set_character_identity",
    description="设置角色的身份锚点（换一个就不再是同一个人的可见特征）、非身份项、持续表演事实与声音方向。",
    category="character",
    tags=("visual_bible", "character", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "character_id": _STR,
            "identity_anchors": _ARR,
            "not_identity": _ARR,
            "persistent_performance_facts": _JSON,
            "voice_direction": _JSON,
        },
        "required": ["character_id"],
    },
    examples=({"character_id": "chr_xxx",
               "identity_anchors": ["方额窄下颌", "后颈发际收成尖角"],
               "not_identity": ["单场雨水", "瞬时表情"]},),
)
def set_character_identity(ctx: SkillContext, character_id: str, **fields: Any) -> dict[str, Any]:
    character = ctx.db.get(Character, character_id)
    if character is None:
        raise SkillError(f"角色不存在：{character_id}", code="NOT_FOUND")
    _assert_not_none(fields, ("identity_anchors", "not_identity",
                              "persistent_performance_facts", "voice_direction"))
    for key, value in _drop_none(fields).items():
        setattr(character, key, value)
    vb_svc.ensure_character_code(ctx.db, character)
    ctx.db.flush()
    return {"character_id": character.id, "code": character.code,
            "identity_anchors": character.identity_anchors or []}


@skill(
    name="create_look",
    description="新建角色造型变体（换装 / 伤势 / 磨损）。differences 只写相对基准的变化，身份不变。",
    category="character",
    tags=("visual_bible", "look", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "character_id": _STR, "name": _STR, "code": _STR,
            "differences": _JSON, "base_look_id": _STR, "cause_shot_id": _STR,
            "valid_from": _STR, "valid_until": _STR, "reference_asset_id": _STR,
            "is_current": {"type": "boolean"},
        },
        "required": ["character_id", "name"],
    },
)
def create_look(ctx: SkillContext, character_id: str, name: str, **fields: Any) -> dict[str, Any]:
    character = ctx.db.get(Character, character_id)
    if character is None:
        raise SkillError(f"角色不存在：{character_id}", code="NOT_FOUND")
    _assert_not_none(fields, ("code", "differences", "base_look_id", "cause_shot_id",
                              "valid_from", "valid_until", "reference_asset_id"))
    look = vb_svc.create_look(
        ctx.db, character, name=name, actor=ctx.actor, **_drop_none(fields)
    )
    return {"look": vb_svc.look_brief(look)}


@skill(
    name="create_location",
    description="新建地点身份（换一个就不再是同一个地方）。spatial_identity 含形制/分区/出入口/固定锚点/材质。",
    category=VISUAL,
    tags=("visual_bible", "location", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": _STR, "name": _STR, "code": _STR, "description": _STR,
            "spatial_identity": _JSON, "era_form": _JSON, "not_identity": _ARR,
            "reference_asset_id": _STR,
        },
        "required": ["project_id", "name"],
    },
)
def create_location(ctx: SkillContext, project_id: str, name: str, **fields: Any) -> dict[str, Any]:
    project = _require_project(ctx, project_id)
    _assert_not_none(fields, ("code", "description", "spatial_identity", "era_form",
                              "not_identity", "reference_asset_id"))
    location = vb_svc.create_location(
        ctx.db, project, name=name, actor=ctx.actor, **_drop_none(fields)
    )
    return {"location": vb_svc.location_brief(location)}


@skill(
    name="create_location_view",
    description="新建地点观看变体（机位朝向 + 时段/天气/陈设/光线差异）。",
    category=VISUAL,
    tags=("visual_bible", "location", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "location_id": _STR, "name": _STR, "code": _STR,
            "orientation": _JSON, "state_differences": _JSON, "base_view_id": _STR,
            "cause_shot_id": _STR, "valid_from": _STR, "valid_until": _STR,
            "reference_asset_id": _STR, "is_current": {"type": "boolean"},
        },
        "required": ["location_id", "name"],
    },
)
def create_location_view(ctx: SkillContext, location_id: str, name: str, **fields: Any) -> dict[str, Any]:
    location = ctx.db.get(vb_svc.Location, location_id)
    if location is None:
        raise SkillError(f"地点不存在：{location_id}", code="NOT_FOUND")
    _assert_not_none(fields, ("code", "orientation", "state_differences", "base_view_id",
                              "cause_shot_id", "valid_from", "valid_until", "reference_asset_id"))
    view = vb_svc.create_location_view(
        ctx.db, location, name=name, actor=ctx.actor, **_drop_none(fields)
    )
    return {"location_view": vb_svc.location_view_brief(view)}


@skill(
    name="create_prop",
    description="新建道具身份（形制/材质/功能/永久印记）与文字政策（可读精确文字 / 仅图形 / 不可读 / 待定）。",
    category=VISUAL,
    tags=("visual_bible", "prop", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": _STR, "name": _STR, "code": _STR,
            "identity_anchors": _JSON, "text_policy": _JSON, "not_identity": _ARR,
            "reference_asset_id": _STR,
        },
        "required": ["project_id", "name"],
    },
)
def create_prop(ctx: SkillContext, project_id: str, name: str, **fields: Any) -> dict[str, Any]:
    project = _require_project(ctx, project_id)
    _assert_not_none(fields, ("code", "identity_anchors", "text_policy", "not_identity",
                              "reference_asset_id"))
    prop = vb_svc.create_prop(ctx.db, project, name=name, actor=ctx.actor, **_drop_none(fields))
    return {"prop": vb_svc.prop_brief(prop)}


@skill(
    name="create_prop_state",
    description="新建道具状态变体（开合 / 损伤 / 通电、持有者、内容物）。",
    category=VISUAL,
    tags=("visual_bible", "prop", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "prop_id": _STR, "name": _STR, "code": _STR,
            "condition": _JSON, "custody": _JSON, "contents": _ARR,
            "text_visibility": _STR, "base_state_id": _STR, "cause_shot_id": _STR,
            "valid_from": _STR, "valid_until": _STR, "is_current": {"type": "boolean"},
        },
        "required": ["prop_id", "name"],
    },
)
def create_prop_state(ctx: SkillContext, prop_id: str, name: str, **fields: Any) -> dict[str, Any]:
    prop = ctx.db.get(vb_svc.Prop, prop_id)
    if prop is None:
        raise SkillError(f"道具不存在：{prop_id}", code="NOT_FOUND")
    _assert_not_none(fields, ("code", "condition", "custody", "contents", "text_visibility",
                              "base_state_id", "cause_shot_id", "valid_from", "valid_until"))
    state = vb_svc.create_prop_state(ctx.db, prop, name=name, actor=ctx.actor, **_drop_none(fields))
    return {"prop_state": vb_svc.prop_state_brief(state)}


@skill(
    name="set_shot_bindings",
    description="设置镜头绑定（角色/地点/道具 + 可选变体）。这是绑定的唯一真相；旧字段会单向回写以兼容既有链路。",
    category="storyboard",
    tags=("visual_bible", "shot", "write", "bindings"),
    input_schema={
        "type": "object",
        "properties": {
            "shot_id": _STR,
            "bindings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"kind": _STR, "id": _STR, "variant_id": _STR, "role": _STR},
                    "required": ["kind", "id"],
                },
            },
        },
        "required": ["shot_id", "bindings"],
    },
    examples=({"shot_id": "shot_xxx",
               "bindings": [{"kind": "character", "id": "chr_xxx", "role": "主角"}]},),
)
def set_shot_bindings(ctx: SkillContext, shot_id: str, bindings: list[dict[str, Any]]) -> dict[str, Any]:
    shot = _require_shot(ctx, shot_id)
    vb_svc.set_shot_bindings(ctx.db, shot, bindings, actor=ctx.actor)
    return {
        "shot_id": shot.id,
        "asset_bindings": shot.asset_bindings,
        "mirrored_character_ids": shot.character_ids,
    }


@skill(
    name="set_shot_location",
    description="设置镜头地点实体（并单向回写字符串位置，供既有出图链路读取）。",
    category="storyboard",
    tags=("visual_bible", "shot", "write"),
    input_schema={"type": "object",
                  "properties": {"shot_id": _STR, "location_id": {"type": ["string", "null"]}},
                  "required": ["shot_id"]},
)
def set_shot_location(ctx: SkillContext, shot_id: str, location_id: str | None = None) -> dict[str, Any]:
    shot = _require_shot(ctx, shot_id)
    location = ctx.db.get(vb_svc.Location, location_id) if location_id else None
    if location_id and location is None:
        raise SkillError(f"地点不存在：{location_id}", code="NOT_FOUND")
    vb_svc.set_shot_location(ctx.db, shot, location, actor=ctx.actor)
    return {"shot_id": shot.id, "location_id": shot.location_id, "mirrored_location": shot.location}


# =========================================================================== #
# 连续性
# =========================================================================== #
@skill(
    name="create_continuity_lock",
    description=(
        "建立连续性锁：把「跨镜永不变化的可见事实」固化成最小名词短语（颜色+材质/形制+物体），"
        "编译时强制逐字出现在所有 in-scope 提示词正文里。锁面含标点/动作/状态/数量/镜头信息会被拒绝。"
    ),
    category=VISUAL,
    tags=("continuity", "lock", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": _STR, "name": _STR, "surface": _STR, "code": _STR,
            "subject_type": {"type": "string", "enum": list(continuity_svc.SUBJECT_TYPES)},
            "subject_id": _STR, "variant_id": _STR,
            "shot_scope": _ARR, "prompt_scope": _ARR,
            "prompt_language": {"type": "string", "enum": list(continuity_svc.PROMPT_LANGUAGES)},
        },
        "required": ["project_id", "surface"],
    },
    examples=({"project_id": "proj_xxx", "name": "深灰夹克",
               "surface": "dark grey zip-up stand collar jacket",
               "subject_type": "character", "subject_id": "chr_xxx"},),
)
def create_continuity_lock(ctx: SkillContext, project_id: str, surface: str, **fields: Any) -> dict[str, Any]:
    project = _require_project(ctx, project_id)
    _assert_not_none(fields, ("name", "code", "subject_type", "subject_id", "variant_id",
                              "shot_scope", "prompt_scope"))
    lock = continuity_svc.create_lock(
        ctx.db, project, surface=surface, actor=ctx.actor, **_drop_none(fields)
    )
    return {"lock": continuity_svc.lock_brief(lock)}


@skill(
    name="create_continuity_delta",
    description="记录连续性增量（什么变了、从什么变成什么、为什么）。「未知」不等于「恢复默认」，不确定就建 unresolved。",
    category=VISUAL,
    tags=("continuity", "delta", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": _STR, "code": _STR,
            "subject_type": _STR, "subject_id": _STR, "state_field": _STR,
            "before": _JSON, "after": _JSON,
            "scene_id": _STR, "shot_id": _STR, "cause_shot_id": _STR,
            "effective_from": _STR, "effective_until": _STR,
            "next_linked_shot_id": _STR, "affected_refs": _ARR,
            "reconciliation_status": _STR,
        },
        "required": ["project_id", "subject_type", "subject_id", "state_field"],
    },
)
def create_continuity_delta(ctx: SkillContext, project_id: str, subject_type: str,
                            subject_id: str, state_field: str, **fields: Any) -> dict[str, Any]:
    project = _require_project(ctx, project_id)
    _assert_not_none(fields, ("code", "before", "after", "scene_id", "shot_id", "cause_shot_id",
                              "effective_from", "effective_until", "next_linked_shot_id",
                              "affected_refs", "reconciliation_status"))
    delta = continuity_svc.create_delta(
        ctx.db, project, subject_type=subject_type, subject_id=subject_id,
        state_field=state_field, actor=ctx.actor, **_drop_none(fields),
    )
    return {"delta": continuity_svc.delta_brief(delta)}


@skill(
    name="verify_continuity_locks",
    description=(
        "校验器：检查每条连续性锁的锁面是否逐字出现在所有 in-scope 镜头的最新提示词正文里。"
        "只认正向正文（写在负面提示词里的不算在场）。"
    ),
    category=VISUAL,
    tags=("continuity", "verify", "read"),
    input_schema={
        "type": "object",
        "properties": {"project_id": _STR, "prompt_type": _STR, "lock_code": _STR},
        "required": ["project_id"],
    },
)
def verify_continuity_locks(ctx: SkillContext, project_id: str, prompt_type: str = "image",
                            lock_code: str = "") -> dict[str, Any]:
    _require_project(ctx, project_id)
    locks = continuity_svc.list_project_locks(ctx.db, project_id)
    if lock_code:
        locks = [lock for lock in locks if lock.code == lock_code]
        if not locks:
            raise SkillError(f"未找到锁：{lock_code}", code="NOT_FOUND")

    shots = list(ctx.db.execute(
        select(Shot).where(Shot.project_id == project_id).order_by(Shot.sequence.asc())
    ).scalars().all())

    report: list[dict[str, Any]] = []
    total_violations = 0      # 有提示词但锁面不在正文里 —— 真违规，阻断合规
    total_not_compiled = 0    # 该镜头还没编译过提示词 —— 只是"未覆盖"，不算违规
    for lock in locks:
        scoped = [s for s in shots if continuity_svc.lock_applies_to_shot(lock, s)]
        entries: list[dict[str, Any]] = []
        for shot in scoped:
            prompt = ctx.db.execute(
                select(Prompt).where(Prompt.shot_id == shot.id, Prompt.type == prompt_type)
            ).scalars().first()
            version = (
                ctx.db.get(PromptVersion, prompt.current_version_id)
                if (prompt is not None and prompt.current_version_id) else None
            )
            hit = bool(version and continuity_svc.surface_hits(version.compiled_prompt, lock.surface))
            # 只有"已有提示词却缺锁面"才算违规；"尚未编译"是覆盖度问题，不计入合规判定
            if version is None:
                state = "not_compiled"
                total_not_compiled += 1
            elif hit:
                state = "present"
            else:
                state = "missing"
                total_violations += 1
            entries.append({
                "shot_id": shot.id,
                "shot_code": shot.code,
                "prompt_id": prompt.id if prompt else None,
                "prompt_version": version.version if version else None,
                "has_prompt": version is not None,
                "surface_present": hit,
                "state": state,
            })
        report.append({
            "lock_id": lock.id,
            "lock_code": lock.code,
            "surface": lock.surface,
            "shot_count": len(scoped),
            "missing_count": sum(1 for e in entries if e["state"] == "missing"),
            "not_compiled_count": sum(1 for e in entries if e["state"] == "not_compiled"),
            "shots": entries,
        })
    return {
        "prompt_type": prompt_type,
        "lock_count": len(locks),
        "missing_total": total_violations,
        "not_compiled_total": total_not_compiled,
        "compliant": total_violations == 0,
        "locks": report,
        "note": ("missing_total = 已有提示词但锁面不在正向正文（真违规，阻断 compliant）；"
                 "not_compiled_total = 该镜头尚无提示词版本（覆盖度问题，不影响 compliant）。"
                 "迁移前的历史提示词没有经过编译器，会被判为 missing —— 重新执行 "
                 "compile_image_prompt 即可纳入锁管理"),
    }


# =========================================================================== #
# Prompt 编译
# =========================================================================== #
_COMPILE_PROPS = {
    "shot_id": _STR,
    "provider": _STR, "model": _STR,
    "width": {"type": "integer"}, "height": {"type": "integer"},
    "aspect_ratio": _STR,
    "resolution": _STR,
    "parameters": _JSON,
    "extra_negative": _STR,
    "raw_prompt": _STR,
    "mirror": {"type": "boolean"},
}


def _compile(ctx: SkillContext, shot_id: str, prompt_type: str, **fields: Any) -> dict[str, Any]:
    shot = _require_shot(ctx, shot_id)
    _assert_not_none(fields, tuple(k for k in _COMPILE_PROPS if k != "shot_id"))
    try:
        version = compiler_svc.compile_prompt(
            ctx.db, shot, prompt_type=prompt_type, actor=ctx.actor, **fields
        )
    except compiler_svc.CompileError as exc:
        raise SkillError(str(exc), code="COMPILE_FAILED") from exc
    prompt = ctx.db.get(Prompt, version.prompt_id)
    return {
        "prompt_id": version.prompt_id,
        "prompt_version_id": version.id,
        "code": prompt.code if prompt else "",
        "version": version.version,
        "compiled_prompt": version.compiled_prompt,
        "negative_prompt": version.negative_prompt,
        "reference_assets": version.reference_assets,
        "continuity_lock_ids": version.continuity_lock_ids,
        "compiled_from": version.compiled_from,
        "status": version.status,
    }


@skill(
    name="compile_image_prompt",
    description=(
        "把「视觉设定 + 身份/变体 + 镜头语法 + 连续性锁 + 参考图」编译成图片提示词，"
        "并落一个**新版本**（永不覆盖旧版本）。锁面缺失 / 正文不合卫生 / 文字政策冲突会直接失败。"
    ),
    category="image",
    tags=("prompt", "compiler", "write"),
    input_schema={"type": "object", "properties": _COMPILE_PROPS, "required": ["shot_id"]},
    examples=({"shot_id": "shot_xxx", "provider": "comfyui", "width": 2752, "height": 1536,
               "resolution": "2K"},),
)
def compile_image_prompt(ctx: SkillContext, shot_id: str, **fields: Any) -> dict[str, Any]:
    return _compile(ctx, shot_id, "image", **fields)


@skill(
    name="compile_video_prompt",
    description="编译镜头运动提示词（含起始帧参考槽位），同样版本化、同样强制注锁。",
    category="video",
    tags=("prompt", "compiler", "write"),
    input_schema={"type": "object", "properties": _COMPILE_PROPS, "required": ["shot_id"]},
)
def compile_video_prompt(ctx: SkillContext, shot_id: str, **fields: Any) -> dict[str, Any]:
    return _compile(ctx, shot_id, "video", **fields)


@skill(
    name="get_prompt",
    description="读取一条 Prompt 及其全部版本与 stale 状态（版本只增不改，可逐版对照）。",
    category="image",
    tags=("prompt", "read"),
    input_schema={"type": "object",
                  "properties": {"prompt_id": _STR, "include_prompt": {"type": "boolean"}},
                  "required": ["prompt_id"]},
)
def get_prompt(ctx: SkillContext, prompt_id: str, include_prompt: bool = True) -> dict[str, Any]:
    prompt = ctx.db.get(Prompt, prompt_id)
    if prompt is None:
        raise SkillError(f"Prompt 不存在：{prompt_id}", code="NOT_FOUND")
    versions = list(ctx.db.execute(
        select(PromptVersion).where(PromptVersion.prompt_id == prompt_id)
        .order_by(PromptVersion.version.asc())
    ).scalars().all())
    return {
        "prompt": compiler_svc.prompt_brief(prompt, versions=versions, include_prompt=include_prompt),
        "staleness": compiler_svc.check_prompt_staleness(ctx.db, prompt),
    }


@skill(
    name="list_prompts",
    description="列出项目的 Prompt（含版本数、类型、stale 标记），用于决定「哪些镜头需要重出」。",
    category="image",
    tags=("prompt", "read"),
    input_schema={"type": "object",
                  "properties": {"project_id": _STR, "shot_id": _STR},
                  "required": ["project_id"]},
)
def list_prompts(ctx: SkillContext, project_id: str, shot_id: str = "") -> dict[str, Any]:
    _require_project(ctx, project_id)
    query = select(Prompt).where(Prompt.project_id == project_id)
    if shot_id:
        query = query.where(Prompt.shot_id == shot_id)
    prompts = list(ctx.db.execute(query.order_by(Prompt.created_at.asc())).scalars().all())
    items: list[dict[str, Any]] = []
    stale_count = 0
    for prompt in prompts:
        staleness = compiler_svc.check_prompt_staleness(ctx.db, prompt)
        if staleness["stale"]:
            stale_count += 1
        entry = compiler_svc.prompt_brief(prompt)
        entry["type"] = prompt.type
        entry["stale"] = staleness["stale"]
        entry["stale_reasons"] = staleness["reasons"]
        items.append(entry)
    return {"count": len(items), "stale_count": stale_count, "prompts": items}


@skill(
    name="check_prompt_staleness",
    description="批量检查 stale：角色/地点/道具/锁/风格改动后，哪些镜头的提示词已过期、需要重新编译。",
    category="image",
    tags=("prompt", "read", "dependency"),
    input_schema={"type": "object",
                  "properties": {"project_id": _STR, "prompt_id": _STR}},
)
def check_prompt_staleness(ctx: SkillContext, project_id: str = "", prompt_id: str = "") -> dict[str, Any]:
    if prompt_id:
        prompt = ctx.db.get(Prompt, prompt_id)
        if prompt is None:
            raise SkillError(f"Prompt 不存在：{prompt_id}", code="NOT_FOUND")
        return {"prompts": [{"prompt_id": prompt.id,
                             **compiler_svc.check_prompt_staleness(ctx.db, prompt)}]}
    if not project_id:
        raise SkillError("必须提供 project_id 或 prompt_id", code="BAD_INPUT")
    _require_project(ctx, project_id)
    prompts = list(ctx.db.execute(
        select(Prompt).where(Prompt.project_id == project_id)
    ).scalars().all())
    results = []
    for prompt in prompts:
        staleness = compiler_svc.check_prompt_staleness(ctx.db, prompt)
        if staleness["stale"]:
            results.append({"prompt_id": prompt.id, "code": prompt.code, **staleness})
    return {"stale_count": len(results), "checked_count": len(prompts), "stale": results}


# =========================================================================== #
# 生成计划：Preview → Confirm → Produce
# =========================================================================== #
@skill(
    name="create_generation_plan",
    description="建立生成计划（**不消耗任何资源**）。每项至少给 shot_id 与 modality。",
    category=PLAN,
    tags=("plan", "preview", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": _STR, "name": _STR,
            "plan_type": {"type": "string",
                          "enum": ["batch_image", "batch_video", "batch_voice", "mixed"]},
            "parameters": _JSON, "source_scope": _JSON,
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "shot_id": _STR, "modality": _STR, "prompt_version_id": _STR,
                        "provider": _STR, "model": _STR,
                        "width": {"type": "integer"}, "height": {"type": "integer"},
                        "aspect_ratio": _STR, "resolution": _STR,
                        "parameters": _JSON, "reference_assets": _ARR,
                    },
                },
            },
        },
        "required": ["project_id", "items"],
    },
)
def create_generation_plan(ctx: SkillContext, project_id: str, items: list[dict[str, Any]],
                           name: str = "", plan_type: str = "batch_image",
                           parameters: dict[str, Any] | None = None,
                           source_scope: dict[str, Any] | None = None) -> dict[str, Any]:
    project = _require_project(ctx, project_id)
    try:
        plan = plans_svc.create_plan(
            ctx.db, project, name=name, plan_type=plan_type, items=items,
            parameters=parameters, source_scope=source_scope, actor=ctx.actor,
        )
    except plans_svc.PlanError as exc:
        raise SkillError(str(exc), code="PLAN_INVALID") from exc
    return {"plan": plans_svc.plan_brief(plan, items=plans_svc.list_plan_items(ctx.db, plan.id))}


def _require_plan(ctx: SkillContext, plan_id: str) -> GenerationPlan:
    plan = plans_svc.get_plan(ctx.db, plan_id)
    if plan is None:
        raise SkillError(f"生成计划不存在：{plan_id}", code="NOT_FOUND")
    return plan


@skill(
    name="preview_generation_plan",
    description="预览计划：校验 + 算指纹 + 出汇总（数量/模态分布/Provider/分辨率/预估）。**不消耗任何生成资源**。",
    category=PLAN,
    tags=("plan", "preview", "read"),
    input_schema={"type": "object", "properties": {"plan_id": _STR}, "required": ["plan_id"]},
)
def preview_generation_plan(ctx: SkillContext, plan_id: str) -> dict[str, Any]:
    plan = _require_plan(ctx, plan_id)
    try:
        plans_svc.preview_plan(ctx.db, plan, actor=ctx.actor)
    except plans_svc.PlanError as exc:
        raise SkillError(str(exc), code="PREVIEW_FAILED") from exc
    return {"plan": plans_svc.plan_brief(plan, items=plans_svc.list_plan_items(ctx.db, plan.id))}


@skill(
    name="confirm_generation_plan",
    description=(
        "确认计划（**不消耗资源**）。必须带上预览时给出的指纹，"
        "计划内容变化后旧确认立即失效。确认只能消费一次。"
    ),
    category=PLAN,
    tags=("plan", "confirm", "write"),
    input_schema={"type": "object",
                  "properties": {"plan_id": _STR, "fingerprint": _STR},
                  "required": ["plan_id", "fingerprint"]},
)
def confirm_generation_plan(ctx: SkillContext, plan_id: str, fingerprint: str) -> dict[str, Any]:
    plan = _require_plan(ctx, plan_id)
    try:
        plans_svc.confirm_plan(ctx.db, plan, fingerprint, actor=ctx.actor)
    except plans_svc.PlanError as exc:
        raise SkillError(str(exc), code="CONFIRM_FAILED") from exc
    return {"plan": plans_svc.plan_brief(plan, items=plans_svc.list_plan_items(ctx.db, plan.id))}


@skill(
    name="materialize_generation_plan",
    description="把已确认的计划物化成真实任务（**这一步才开始消耗算力/费用**）。确认只能消费一次。",
    category=PLAN,
    tags=("plan", "produce", "async", "write"),
    input_schema={"type": "object", "properties": {"plan_id": _STR}, "required": ["plan_id"]},
)
def materialize_generation_plan(ctx: SkillContext, plan_id: str) -> dict[str, Any]:
    plan = _require_plan(ctx, plan_id)
    try:
        task_ids = plans_svc.materialize_plan(ctx.db, plan, actor=ctx.actor)
    except plans_svc.PlanError as exc:
        raise SkillError(str(exc), code="MATERIALIZE_FAILED") from exc
    return {
        "plan_id": plan.id,
        "status": plan.status,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "note": "任务已入队，请通过 get_task_status 轮询进度",
    }


@skill(
    name="cancel_generation_plan",
    description="取消生成计划（不删除已创建的任务）。",
    category=PLAN,
    tags=("plan", "write"),
    input_schema={"type": "object",
                  "properties": {"plan_id": _STR, "reason": _STR},
                  "required": ["plan_id"]},
)
def cancel_generation_plan(ctx: SkillContext, plan_id: str, reason: str = "") -> dict[str, Any]:
    plan = _require_plan(ctx, plan_id)
    plans_svc.cancel_plan(ctx.db, plan, actor=ctx.actor, reason=reason)
    return {"plan": plans_svc.plan_brief(plan)}


@skill(
    name="get_generation_plan",
    description="读取生成计划详情与全部条目。",
    category=PLAN,
    tags=("plan", "read"),
    input_schema={"type": "object", "properties": {"plan_id": _STR}, "required": ["plan_id"]},
)
def get_generation_plan(ctx: SkillContext, plan_id: str) -> dict[str, Any]:
    plan = _require_plan(ctx, plan_id)
    return {"plan": plans_svc.plan_brief(plan, items=plans_svc.list_plan_items(ctx.db, plan.id))}


@skill(
    name="list_generation_plans",
    description="列出项目的全部生成计划。",
    category=PLAN,
    tags=("plan", "read"),
    input_schema={"type": "object", "properties": {"project_id": _STR}, "required": ["project_id"]},
)
def list_generation_plans(ctx: SkillContext, project_id: str) -> dict[str, Any]:
    _require_project(ctx, project_id)
    plans = plans_svc.list_plans(ctx.db, project_id)
    return {"count": len(plans), "plans": [plans_svc.plan_brief(p) for p in plans]}


# =========================================================================== #
# 血缘反查
# =========================================================================== #
@skill(
    name="get_asset_provenance",
    description=(
        "从素材反查完整生成链路：项目 / 剧本段 / 镜头 / 角色 / 造型 / Prompt 版本 / "
        "模型与 Provider / 参考图 / 任务 / 生成计划。"
    ),
    category="asset",
    tags=("provenance", "read"),
    input_schema={"type": "object", "properties": {"asset_id": _STR}, "required": ["asset_id"]},
)
def get_asset_provenance(ctx: SkillContext, asset_id: str) -> dict[str, Any]:
    asset = ctx.db.get(Asset, asset_id)
    if asset is None:
        raise SkillError(f"素材不存在：{asset_id}", code="NOT_FOUND")
    return provenance_svc.asset_provenance(ctx.db, asset)


@skill(
    name="get_prompt_provenance",
    description="从 Prompt 反查：全部版本、编译输入（视觉设定/角色/造型/锁）、以及它产出的素材。",
    category="image",
    tags=("provenance", "read"),
    input_schema={"type": "object", "properties": {"prompt_id": _STR}, "required": ["prompt_id"]},
)
def get_prompt_provenance(ctx: SkillContext, prompt_id: str) -> dict[str, Any]:
    prompt = ctx.db.get(Prompt, prompt_id)
    if prompt is None:
        raise SkillError(f"Prompt 不存在：{prompt_id}", code="NOT_FOUND")
    return provenance_svc.prompt_provenance(ctx.db, prompt)


@skill(
    name="list_continuity_locks",
    description="列出项目的连续性锁与增量。",
    category=VISUAL,
    tags=("continuity", "read"),
    input_schema={"type": "object", "properties": {"project_id": _STR}, "required": ["project_id"]},
)
def list_continuity_locks(ctx: SkillContext, project_id: str) -> dict[str, Any]:
    _require_project(ctx, project_id)
    locks = continuity_svc.list_project_locks(ctx.db, project_id)
    deltas = list(ctx.db.execute(
        select(continuity_svc.ContinuityDelta)
        .where(continuity_svc.ContinuityDelta.project_id == project_id)
    ).scalars().all())
    return {
        "lock_count": len(locks),
        "locks": [continuity_svc.lock_brief(lock) for lock in locks],
        "delta_count": len(deltas),
        "deltas": [continuity_svc.delta_brief(delta) for delta in deltas],
    }
