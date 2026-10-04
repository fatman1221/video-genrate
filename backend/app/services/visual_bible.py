"""视觉设定体系（Visual Bible）—— 「身份 / 变体分离」的落地。

**判据**（来自 drama-skills，先想清楚再落库）

- **身份**：换一个就不再是同一个人 / 地 / 物 → ``Character`` / ``Location`` / ``Prop``
- **变体**：身份不变，但服装、伤势、时段、天气、开合或持有状态改变
  → ``CharacterLook`` / ``LocationView`` / ``PropState``
- **镜头瞬态**：姿势、视线、左右手、站位、相机角度 → 归 ``Shot``
- **故事语义**：知识、目标、关系 → 归写作，只引用可见后果

反面例子（drama-skills 原文）：把「橙色雨衣女人，湿头发，脸上有伤」写成身份锚点 ——
雨衣脱下、头发变干、伤口愈合时，人物就失去所有身份特征。

**兼容纪律**（用户裁定：新字段为唯一事实来源，旧字段只做单向回写）

本模块是旧字段回写的**两个合法入口之一**（另一个是 ``prompt_compiler._mirror_to_shot``）：

- ``shot.location``(String) ← 由 :func:`set_shot_location` 回写
- ``shot.character_ids``（**角色 id 数组**）← 由 :func:`set_shot_bindings` 回写

除此之外**任何地方都不许写旧字段**，也**任何地方都不许把旧字段当输入读**。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Character, CharacterLook, Location, LocationView, Project, Prop, PropState,
    Shot, VisualBible, VisualStyle, new_id,
)
from . import agent_log

#: 代号长度上限（与列宽保持一致）
CODE_MAX = 60

_BIBLE_TEXT_FIELDS = ("title", "visual_logline", "status")
_BIBLE_JSON_FIELDS = ("era_anchors", "global_rules", "text_policy")

#: 形态卡（drama-skills 的六种视觉形态）—— 作为**种子枚举**，不是硬编码逻辑
FORM_CARDS = (
    "live_action",      # 实拍
    "guoman_2d",        # 国漫二次元
    "dynamic_comic",    # 二维动态漫
    "chibi",            # Q 版表达
    "stylized_3d",      # 风格化三维
    "ink_wash",         # 水墨笔触
)

#: 参考图用途（封闭词表，源自 drama-skills）
REFERENCE_ROLES = (
    "身份", "造型状态", "地理", "构图", "尺度", "效果", "起始帧", "结束帧", "风格",
)

#: 参考图记号（三态，含义完全不同，不得混用）
#: - ``REF``  项目内真实存在的参考图 → 可作生产输入
#: - ``IMG``  提示词条目 ID → 可作 job 来源
#: - ``PLAN`` 创作者项目外自备、生成时才挂载 → **不可作生产输入**，prepare 直接失败
REFERENCE_KINDS = ("REF", "IMG", "PLAN")


# --------------------------------------------------------------------------- #
# 代号与查重
# --------------------------------------------------------------------------- #
def slug(text: str, *, seed: str = "") -> str:
    """把任意文本规范成代号片段：保留 ASCII 字母数字，其余折成连字符并大写。

    纯中文名（如「林野」）折完为空 → 退化为确定性摘要（**不用随机数**，
    否则同一名字两次调用会得到不同代号）。
    """
    cleaned = re.sub(r"[^0-9A-Za-z]+", "-", text or "").strip("-").upper()
    if cleaned:
        return cleaned[:CODE_MAX]
    digest = hashlib.md5((seed or text or "").encode("utf-8")).hexdigest()[:6].upper()
    return f"X{digest}"


def make_code(prefix: str, name: str, *, fallback_seed: str = "") -> str:
    """生成带前缀的稳定代号，如 ``LOC-FERRY-OFFICE``。"""
    return f"{prefix}-{slug(name, seed=fallback_seed)}"[:CODE_MAX]


def _unique_code(
    db: Session, model, scope_col, scope_value: Any, code: str, *, exclude_id: str | None = None
) -> str:
    """在同一作用域内保证 ``code`` 唯一（冲突则追加 ``-2`` / ``-3``）。"""
    query = select(model.code).where(scope_col == scope_value)
    if exclude_id:
        query = query.where(model.id != exclude_id)
    taken = {c for (c,) in db.execute(query).all() if c}
    if code not in taken:
        return code
    n = 2
    while f"{code}-{n}" in taken:
        n += 1
    return f"{code}-{n}"


# --------------------------------------------------------------------------- #
# VisualBible
# --------------------------------------------------------------------------- #
def get_bible(db: Session, project_id: str) -> VisualBible | None:
    return db.execute(
        select(VisualBible).where(VisualBible.project_id == project_id)
    ).scalar_one_or_none()


def get_or_create_bible(db: Session, project_id: str) -> VisualBible:
    """取项目的视觉设定；没有就建一条空的（迁移回填已保证多数项目有）。"""
    bible = get_bible(db, project_id)
    if bible is None:
        bible = VisualBible(
            id=new_id("vb"), project_id=project_id, title="", status="DRAFT", version=1
        )
        db.add(bible)
        db.flush()
    return bible


def update_bible(db: Session, project: Project, *, actor: str = "agent", **fields: Any) -> VisualBible:
    """局部更新视觉设定。**只允许白名单字段**，改动即版本 +1。"""
    bible = get_or_create_bible(db, project.id)
    changed: list[str] = []
    for key in _BIBLE_TEXT_FIELDS:
        if key in fields and fields[key] is not None:
            setattr(bible, key, fields[key])
            changed.append(key)
    for key in _BIBLE_JSON_FIELDS:
        if key in fields and fields[key] is not None:
            setattr(bible, key, fields[key])
            changed.append(key)
    if "series_id" in fields and fields["series_id"] is not None:
        bible.series_id = fields["series_id"]
        changed.append("series_id")
    if changed:
        bible.version = (bible.version or 1) + 1
        agent_log.log_event(
            db, project_id=project.id, event="visual_bible.updated", actor=actor,
            message=f"更新视觉设定：{', '.join(changed)}",
            detail={"bible_id": bible.id, "version": bible.version, "fields": changed},
        )
    return bible


# --------------------------------------------------------------------------- #
# VisualStyle
# --------------------------------------------------------------------------- #
def create_style(
    db: Session, project: Project, *, name: str, form_card: str = "",
    bible_id: str | None = None, set_current: bool = False, actor: str = "agent", **fields: Any,
) -> VisualStyle:
    """新建视觉风格。``form_card`` 必须落在 :data:`FORM_CARDS` 里（防止自由发挥出标签）。"""
    if form_card and form_card not in FORM_CARDS:
        raise ValueError(f"未知形态卡「{form_card}」，可选：{', '.join(FORM_CARDS)}")
    bible = get_or_create_bible(db, project.id)
    style = VisualStyle(
        id=new_id("sty"),
        project_id=project.id,
        bible_id=bible_id or bible.id,
        name=name,
        form_card=form_card,
        version=1,
        status="DRAFT",
    )
    for key in (
        "narrative_duty", "identity_carrier", "continuity_carriers", "layer_split",
        "rendering", "lighting", "palette", "camera_language", "composition_rules",
        "motion_budget", "negative_rules", "status",
    ):
        if key in fields and fields[key] is not None:
            setattr(style, key, fields[key])
    db.add(style)
    db.flush()
    if set_current:
        set_current_style(db, project, style.id, actor=actor)
    return style


def set_current_style(db: Session, project: Project, style_id: str, *, actor: str = "agent") -> VisualStyle:
    """把某个风格设为当前生效（同时只允许一个）。"""
    style = db.get(VisualStyle, style_id)
    if style is None or style.project_id != project.id:
        raise ValueError(f"风格不存在或不属于本项目：{style_id}")
    for other in db.execute(
        select(VisualStyle).where(VisualStyle.project_id == project.id, VisualStyle.is_current.is_(True))
    ).scalars().all():
        other.is_current = False
    style.is_current = True
    if style.status == "DRAFT":
        style.status = "ACCEPTED"
    bible = get_or_create_bible(db, project.id)
    bible.current_style_id = style.id
    db.flush()
    agent_log.log_event(
        db, project_id=project.id, event="visual_style.set_current", actor=actor,
        message=f"切换视觉风格为「{style.name}」", detail={"style_id": style.id},
    )
    return style


def current_style(db: Session, project_id: str) -> VisualStyle | None:
    return db.execute(
        select(VisualStyle).where(
            VisualStyle.project_id == project_id, VisualStyle.is_current.is_(True)
        )
    ).scalars().first()


# --------------------------------------------------------------------------- #
# Character / Look
# --------------------------------------------------------------------------- #
def ensure_character_code(db: Session, character: Character) -> str:
    """给角色补稳定代号（幂等）。"""
    if not character.code:
        character.code = _unique_code(
            db, Character, Character.project_id, character.project_id,
            make_code("CHAR", character.name, fallback_seed=character.id),
        )
        db.flush()
    return character.code


def create_look(
    db: Session, character: Character, *, name: str, code: str = "",
    differences: dict[str, Any] | None = None, base_look_id: str | None = None,
    cause_shot_id: str | None = None, valid_from: str = "", valid_until: str = "",
    reference_asset_id: str | None = None, is_current: bool = False,
    status: str = "DRAFT", actor: str = "agent",
) -> CharacterLook:
    """新建造型变体。``differences`` 只写**相对基准的变化**。

    结构约定：``{wardrobe_layers[], hair_styling[], makeup[], injury[], weathering[]}``
    """
    if not name:
        raise ValueError("造型必须给名称")
    look = CharacterLook(
        id=new_id("look"),
        project_id=character.project_id,
        character_id=character.id,
        name=name,
        code="",
        differences=differences or {},
        base_look_id=base_look_id,
        cause_shot_id=cause_shot_id,
        valid_from=valid_from,
        valid_until=valid_until,
        reference_asset_id=reference_asset_id,
        status=status,
    )
    look.code = code or _unique_code(
        db, CharacterLook, CharacterLook.character_id, character.id,
        make_code("LOOK", name or character.name, fallback_seed=character.id),
    )
    db.add(look)
    db.flush()
    if is_current:
        set_current_look(db, character, look.id, actor=actor)
    return look


def set_current_look(db: Session, character: Character, look_id: str, *, actor: str = "agent") -> CharacterLook:
    look = db.get(CharacterLook, look_id)
    if look is None or look.character_id != character.id:
        raise ValueError(f"造型不存在或不属于该角色：{look_id}")
    for other in db.execute(
        select(CharacterLook).where(
            CharacterLook.character_id == character.id, CharacterLook.is_current.is_(True)
        )
    ).scalars().all():
        other.is_current = False
    look.is_current = True
    db.flush()
    if character.project_id:
        agent_log.log_event(
            db, project_id=character.project_id, event="character.look.set_current", actor=actor,
            message=f"角色「{character.name}」当前造型切换为「{look.name}」",
            detail={"character_id": character.id, "look_id": look.id},
        )
    return look


def current_look(db: Session, character_id: str) -> CharacterLook | None:
    return db.execute(
        select(CharacterLook).where(
            CharacterLook.character_id == character_id, CharacterLook.is_current.is_(True)
        )
    ).scalars().first()


# --------------------------------------------------------------------------- #
# Location / LocationView
# --------------------------------------------------------------------------- #
def create_location(
    db: Session, project: Project, *, name: str, code: str = "", description: str = "",
    spatial_identity: dict[str, Any] | None = None, era_form: dict[str, Any] | None = None,
    not_identity: list[Any] | None = None, reference_asset_id: str | None = None,
    bible_id: str | None = None, status: str = "DRAFT", actor: str = "agent",
) -> Location:
    """新建地点身份。``spatial_identity`` 结构：
    ``{shape, zones[], entrances[{id, connects_to}], fixed_anchors[], materials[]}``
    """
    if not name:
        raise ValueError("地点必须给名称")
    loc = Location(
        id=new_id("loc"),
        project_id=project.id,
        bible_id=bible_id or get_or_create_bible(db, project.id).id,
        name=name,
        display_name=name,
        description=description,
        spatial_identity=spatial_identity or {},
        era_form=era_form or {},
        not_identity=not_identity or [],
        reference_asset_id=reference_asset_id,
        status=status,
    )
    loc.code = code or _unique_code(
        db, Location, Location.project_id, project.id,
        make_code("LOC", name, fallback_seed=project.id + name),
    )
    db.add(loc)
    db.flush()
    agent_log.log_event(
        db, project_id=project.id, event="location.created", actor=actor,
        message=f"新建地点「{name}」（{loc.code}）", detail={"location_id": loc.id},
    )
    return loc


def create_location_view(
    db: Session, location: Location, *, name: str, code: str = "",
    orientation: dict[str, Any] | None = None,
    state_differences: dict[str, Any] | None = None,
    base_view_id: str | None = None, cause_shot_id: str | None = None,
    valid_from: str = "", valid_until: str = "",
    reference_asset_id: str | None = None, is_current: bool = False,
    actor: str = "agent",
) -> LocationView:
    """新建观看变体。``orientation``：``{from_zone, toward, visible_fixed_anchors[]}``。"""
    if not name:
        raise ValueError("机位视图必须给名称")
    view = LocationView(
        id=new_id("view"),
        project_id=location.project_id,
        location_id=location.id,
        name=name,
        code="",
        orientation=orientation or {},
        state_differences=state_differences or {},
        base_view_id=base_view_id,
        cause_shot_id=cause_shot_id,
        valid_from=valid_from,
        valid_until=valid_until,
        reference_asset_id=reference_asset_id,
    )
    view.code = code or _unique_code(
        db, LocationView, LocationView.location_id, location.id,
        make_code("VIEW", name, fallback_seed=location.id + name),
    )
    db.add(view)
    db.flush()
    if is_current:
        for other in db.execute(
            select(LocationView).where(
                LocationView.location_id == location.id, LocationView.is_current.is_(True)
            )
        ).scalars().all():
            other.is_current = False
        view.is_current = True
        db.flush()
    return view


# --------------------------------------------------------------------------- #
# Prop / PropState
# --------------------------------------------------------------------------- #
def create_prop(
    db: Session, project: Project, *, name: str, code: str = "",
    identity_anchors: dict[str, Any] | None = None,
    text_policy: dict[str, Any] | None = None, not_identity: list[Any] | None = None,
    reference_asset_id: str | None = None, bible_id: str | None = None,
    status: str = "DRAFT", actor: str = "agent",
) -> Prop:
    """新建道具身份。

    ``identity_anchors``：``{scale_and_form, materials[], function, permanent_marks[]}``
    ``text_policy``：``{mode: exact_readable|graphic_only|no_readable_text|pending_creator_text,
    text, placement}``
    """
    if not name:
        raise ValueError("道具必须给名称")
    prop = Prop(
        id=new_id("prop"),
        project_id=project.id,
        bible_id=bible_id or get_or_create_bible(db, project.id).id,
        name=name,
        display_name=name,
        identity_anchors=identity_anchors or {},
        text_policy=text_policy or {},
        not_identity=not_identity or [],
        reference_asset_id=reference_asset_id,
        status=status,
    )
    prop.code = code or _unique_code(
        db, Prop, Prop.project_id, project.id,
        make_code("PROP", name, fallback_seed=project.id + name),
    )
    db.add(prop)
    db.flush()
    agent_log.log_event(
        db, project_id=project.id, event="prop.created", actor=actor,
        message=f"新建道具「{name}」（{prop.code}）", detail={"prop_id": prop.id},
    )
    return prop


def create_prop_state(
    db: Session, prop: Prop, *, name: str, code: str = "",
    condition: dict[str, Any] | None = None, custody: dict[str, Any] | None = None,
    contents: list[Any] | None = None, text_visibility: str = "",
    base_state_id: str | None = None, cause_shot_id: str | None = None,
    valid_from: str = "", valid_until: str = "", is_current: bool = False,
    actor: str = "agent",
) -> PropState:
    """新建道具状态变体。``condition``：``{open, damage, powered}`` 或 ``{summary}``。"""
    if not name:
        raise ValueError("道具状态必须给名称")
    state = PropState(
        id=new_id("pst"),
        project_id=prop.project_id,
        prop_id=prop.id,
        name=name,
        code="",
        condition=condition or {},
        custody=custody or {},
        contents=contents or [],
        text_visibility=text_visibility,
        base_state_id=base_state_id,
        cause_shot_id=cause_shot_id,
        valid_from=valid_from,
        valid_until=valid_until,
    )
    state.code = code or _unique_code(
        db, PropState, PropState.prop_id, prop.id,
        make_code("PSTATE", name, fallback_seed=prop.id + name),
    )
    db.add(state)
    db.flush()
    if is_current:
        for other in db.execute(
            select(PropState).where(PropState.prop_id == prop.id, PropState.is_current.is_(True))
        ).scalars().all():
            other.is_current = False
        state.is_current = True
        db.flush()
    return state


# --------------------------------------------------------------------------- #
# Shot 绑定（新字段 = 真相；旧字段 = 单向回写）
# --------------------------------------------------------------------------- #
def set_shot_location(db: Session, shot: Shot, location: Location | None, *, actor: str = "agent") -> Shot:
    """设置镜头地点实体，并**单向回写**字符串 ``shot.location`` 供老消费者读取。"""
    shot.location_id = location.id if location else None
    # --- 单向兼容投影：只写不读 ---
    shot.location = (location.display_name or location.name) if location else ""
    db.flush()
    if shot.project_id:
        agent_log.log_event(
            db, project_id=shot.project_id, shot_id=shot.id,
            event="shot.location_set", actor=actor,
            message=f"镜头 {shot.code or shot.sequence} 地点设为「{shot.location or '（清空）'}」",
            detail={"location_id": shot.location_id},
        )
    return shot


def set_shot_bindings(db: Session, shot: Shot, bindings: list[dict[str, Any]], *, actor: str = "agent") -> Shot:
    """设置镜头绑定（``[{kind, id, variant_id, role}]``）并**单向回写** ``character_ids``。

    ``asset_bindings`` 是真相；``character_ids`` 只是给老消费者看的**角色 id 数组**。

    ⚠️ 回写的是 **角色 id 而非名字** —— ``handlers.py`` 里
    ``_ensure_shot_image`` 是用 ``db.get(Character, cid)`` 消费这个字段的，
    写名字会让它查不到角色，**人物一致性会静默失效**。
    """
    normalized: list[dict[str, Any]] = []
    for item in bindings or []:
        kind = (item.get("kind") or "").strip()
        subject_id = (item.get("id") or "").strip()
        if not kind or not subject_id:
            raise ValueError("绑定项必须同时给 kind 与 id")
        normalized.append({
            "kind": kind,
            "id": subject_id,
            "variant_id": (item.get("variant_id") or "") or None,
            "role": (item.get("role") or "").strip(),
        })
    shot.asset_bindings = normalized
    # --- 单向兼容投影：只写不读（值必须是角色 id，与 handlers 的口径一致）---
    shot.character_ids = [item["id"] for item in normalized if item["kind"] == "character"]
    db.flush()
    if shot.project_id:
        agent_log.log_event(
            db, project_id=shot.project_id, shot_id=shot.id,
            event="shot.bindings_set", actor=actor,
            message=f"镜头 {shot.code or shot.sequence} 绑定 {len(normalized)} 个实体",
            detail={"bindings": normalized},
        )
    return shot


def resolve_bindings(db: Session, shot: Shot) -> list[dict[str, Any]]:
    """取得镜头的有效绑定。

    真相是 ``asset_bindings``；为空时**回退**到旧字段 ``character_ids``
    —— 这是唯一的「读旧字段」例外，且只发生在迁移过渡期（新数据一定走 asset_bindings）。

    旧字段里的值**可能是角色 id 也可能是中文名**（``resolve_character_ids`` 两种都收），
    因此按 id 优先、名字兜底的顺序解析。
    """
    if shot.asset_bindings:
        return list(shot.asset_bindings)
    fallback: list[dict[str, Any]] = []
    for value in shot.character_ids or []:
        token = str(value or "").strip()
        if not token:
            continue
        character = db.get(Character, token)
        if character is None:
            character = db.execute(
                select(Character).where(
                    Character.project_id == shot.project_id, Character.name == token
                )
            ).scalars().first()
        if character is not None:
            fallback.append({"kind": "character", "id": character.id, "variant_id": None, "role": ""})
    return fallback


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def bible_brief(bible: VisualBible, *, styles: list[VisualStyle] | None = None) -> dict[str, Any]:
    """视觉设定概要。传入 ``styles`` 时一并带上风格列表（避免在序列化里查库）。"""
    data = {
        "id": bible.id,
        "project_id": bible.project_id,
        "series_id": bible.series_id,
        "title": bible.title,
        "visual_logline": bible.visual_logline,
        "current_style_id": bible.current_style_id,
        "era_anchors": bible.era_anchors or {},
        "global_rules": bible.global_rules or {},
        "text_policy": bible.text_policy or {},
        "status": bible.status,
        "version": bible.version,
        "created_at": bible.created_at.isoformat() if bible.created_at else None,
        "updated_at": bible.updated_at.isoformat() if bible.updated_at else None,
    }
    if styles is not None:
        data["styles"] = [style_brief(s) for s in styles]
    return data


def style_brief(style: VisualStyle) -> dict[str, Any]:
    return {
        "id": style.id,
        "project_id": style.project_id,
        "bible_id": style.bible_id,
        "name": style.name,
        "form_card": style.form_card,
        "narrative_duty": style.narrative_duty,
        "identity_carrier": style.identity_carrier or {},
        "continuity_carriers": style.continuity_carriers or {},
        "layer_split": style.layer_split or {},
        "rendering": style.rendering or {},
        "lighting": style.lighting or {},
        "palette": style.palette or {},
        "camera_language": style.camera_language or {},
        "composition_rules": style.composition_rules or {},
        "motion_budget": style.motion_budget or {},
        "negative_rules": style.negative_rules or {},
        "is_current": style.is_current,
        "status": style.status,
        "version": style.version,
    }


def look_brief(look: CharacterLook) -> dict[str, Any]:
    return {
        "id": look.id,
        "character_id": look.character_id,
        "code": look.code,
        "name": look.name,
        "base_look_id": look.base_look_id,
        "differences": look.differences or {},
        "cause_shot_id": look.cause_shot_id,
        "valid_from": look.valid_from,
        "valid_until": look.valid_until,
        "reference_asset_id": look.reference_asset_id,
        "is_current": look.is_current,
        "status": look.status,
    }


def location_brief(location: Location) -> dict[str, Any]:
    return {
        "id": location.id,
        "project_id": location.project_id,
        "code": location.code,
        "name": location.name,
        "display_name": location.display_name,
        "description": location.description,
        "spatial_identity": location.spatial_identity or {},
        "era_form": location.era_form or {},
        "not_identity": location.not_identity or [],
        "reference_asset_id": location.reference_asset_id,
        "status": location.status,
        "version": location.version,
    }


def location_view_brief(view: LocationView) -> dict[str, Any]:
    return {
        "id": view.id,
        "location_id": view.location_id,
        "code": view.code,
        "name": view.name,
        "base_view_id": view.base_view_id,
        "orientation": view.orientation or {},
        "state_differences": view.state_differences or {},
        "cause_shot_id": view.cause_shot_id,
        "valid_from": view.valid_from,
        "valid_until": view.valid_until,
        "reference_asset_id": view.reference_asset_id,
        "is_current": view.is_current,
    }


def prop_brief(prop: Prop) -> dict[str, Any]:
    return {
        "id": prop.id,
        "project_id": prop.project_id,
        "code": prop.code,
        "name": prop.name,
        "display_name": prop.display_name,
        "identity_anchors": prop.identity_anchors or {},
        "text_policy": prop.text_policy or {},
        "not_identity": prop.not_identity or [],
        "reference_asset_id": prop.reference_asset_id,
        "status": prop.status,
        "version": prop.version,
    }


def prop_state_brief(state: PropState) -> dict[str, Any]:
    return {
        "id": state.id,
        "prop_id": state.prop_id,
        "code": state.code,
        "name": state.name,
        "base_state_id": state.base_state_id,
        "condition": state.condition or {},
        "custody": state.custody or {},
        "contents": state.contents or [],
        "text_visibility": state.text_visibility,
        "cause_shot_id": state.cause_shot_id,
        "valid_from": state.valid_from,
        "valid_until": state.valid_until,
        "is_current": state.is_current,
    }
