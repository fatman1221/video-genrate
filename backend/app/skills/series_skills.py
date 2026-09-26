"""连续剧（Series）Skill：一部剧 → 多集 → 每集一条完整流水线。

设计取舍见 ``services/series.py`` 顶部注释：**一集 = 一个 Project**，
从而零成本复用已有的工作流 / 回退 / 审核 / 成片能力。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..core.constants import ProjectStatus
from ..models import Character, Project, Series
from ..services import agent_log, projects as projects_svc, serializers as S, series as series_svc
from .base import SkillContext, SkillError, skill


def _get_series(db, series_id: str) -> Series:
    series = db.get(Series, series_id)
    if series is None:
        raise SkillError(f"系列不存在: {series_id}", code="NOT_FOUND")
    return series


# --------------------------------------------------------------------------- #
# Series
# --------------------------------------------------------------------------- #
@skill(
    name="create_series", category="series", is_async=False,
    description=(
        "创建一部连续剧（系列）。系列是「集」的容器：之后用 create_episode 逐集生产，"
        "每一集都是一条独立的完整流水线。可选参数 episodes 可在创建时批量开好前 N 集。"
    ),
    tags=("series", "write"),
    input_schema={"type": "object", "properties": {
        "name": {"type": "string", "description": "剧名，例如『AI Agent 从入门到实战』"},
        "requirement": {"type": "string", "description": "整部剧的总需求 / 定位"},
        "description": {"type": "string"},
        "style": {"type": "string", "default": "漫画教学风格"},
        "language": {"type": "string", "default": "zh-CN"},
        "episode_duration": {"type": "number", "default": 300,
                             "description": "每一集的目标时长（秒）"},
        "planned_episodes": {"type": "integer", "default": 0, "description": "计划集数，0 = 边做边加"},
        "aspect_ratio": {"type": "string", "default": "16:9"},
        "width": {"type": "integer", "default": 1280},
        "height": {"type": "integer", "default": 720},
        "fps": {"type": "integer", "default": 24},
        "owner": {"type": "string"},
        "episodes": {"type": "integer", "default": 0,
                     "description": "创建时顺带开好的集数（只建空壳，不启动生成）"},
    }, "required": ["name"]},
    examples=({"name": "AI Agent 从入门到实战", "requirement": "12 集 AI Agent 教学连续剧，漫画教学风格",
               "episode_duration": 300, "planned_episodes": 12, "episodes": 3},),
)
def create_series(
    ctx: SkillContext, *, name: str, requirement: str = "", description: str = "",
    style: str = "漫画教学风格", language: str = "zh-CN", episode_duration: float = 300.0,
    planned_episodes: int = 0, aspect_ratio: str = "16:9", width: int = 1280,
    height: int = 720, fps: int = 24, owner: str = "workbuddy", episodes: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    series = Series(
        name=name, requirement=requirement, description=description or requirement,
        style=style, language=language, episode_duration=float(episode_duration),
        planned_episodes=int(planned_episodes), aspect_ratio=aspect_ratio,
        width=int(width), height=int(height), fps=int(fps), owner=owner,
        status=ProjectStatus.PLANNING, extra=extra or {},
    )
    ctx.db.add(series)
    ctx.db.flush()

    created: list[Project] = []
    for _ in range(max(0, int(episodes))):
        created.append(series_svc.create_episode(ctx.db, series))

    agent_log.log_event(
        ctx.db, event="series.created", actor=ctx.actor,
        message=f"创建连续剧：{series.name}（已开 {len(created)} 集）",
        detail={"series_id": series.id, "episode_duration": episode_duration,
                "planned_episodes": planned_episodes},
    )
    ctx.db.commit()
    return {
        "series": series_svc.summarize_series(ctx.db, series),
        "series_id": series.id,
        "episode_ids": [p.id for p in created],
        "message": "系列已创建，可用 create_episode 追加集，或直接对某一集调用 create_script",
    }


@skill(
    name="list_series", category="series", is_async=False,
    description="列出全部连续剧及其进度摘要（集数、完结集数、整体进度）。",
    tags=("series", "read"),
    input_schema={"type": "object", "properties": {
        "status": {"type": "string"}, "keyword": {"type": "string"},
        "limit": {"type": "integer", "default": 50}, "offset": {"type": "integer", "default": 0}}},
)
def list_series(ctx: SkillContext, *, status: str = "", keyword: str = "",
                limit: int = 50, offset: int = 0) -> dict[str, Any]:
    stmt = select(Series).order_by(Series.updated_at.desc())
    if status:
        stmt = stmt.where(Series.status == status)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(Series.name.ilike(like) | Series.requirement.ilike(like))
    rows = list(ctx.db.execute(stmt.offset(max(0, offset)).limit(max(1, limit))).scalars())
    return {"items": [series_svc.summarize_series(ctx.db, s) for s in rows],
            "total": len(rows)}


@skill(
    name="get_series", category="series", is_async=False,
    description="获取连续剧详情。include_episodes=true 时同时返回各集摘要与系列级角色库。",
    tags=("series", "read"),
    input_schema={"type": "object", "properties": {
        "series_id": {"type": "string"},
        "include_episodes": {"type": "boolean", "default": True}},
        "required": ["series_id"]},
)
def get_series(ctx: SkillContext, *, series_id: str, include_episodes: bool = True) -> dict[str, Any]:
    series = _get_series(ctx.db, series_id)
    if include_episodes:
        return {"series": series_svc.series_overview(ctx.db, series)}
    return {"series": series_svc.summarize_series(ctx.db, series)}


@skill(
    name="update_series", category="series",
    description="更新连续剧信息（剧名、总需求、风格、每集时长、计划集数等）。",
    tags=("series", "write"),
    input_schema={"type": "object", "properties": {
        "series_id": {"type": "string"}, "name": {"type": "string"},
        "description": {"type": "string"}, "requirement": {"type": "string"},
        "style": {"type": "string"}, "language": {"type": "string"},
        "status": {"type": "string"}, "episode_duration": {"type": "number"},
        "planned_episodes": {"type": "integer"},
        "aspect_ratio": {"type": "string"}, "width": {"type": "integer"},
        "height": {"type": "integer"}, "fps": {"type": "integer"},
        "owner": {"type": "string"}}, "required": ["series_id"]},
)
def update_series(ctx: SkillContext, *, series_id: str, **fields: Any) -> dict[str, Any]:
    series = _get_series(ctx.db, series_id)
    allowed = {"name", "description", "requirement", "style", "language", "status",
               "episode_duration", "planned_episodes", "aspect_ratio", "width", "height",
               "fps", "owner"}
    changed: dict[str, Any] = {}
    for key, value in fields.items():
        if key in allowed and value is not None:
            setattr(series, key, value)
            changed[key] = value
    ctx.db.commit()
    return {"series": series_svc.summarize_series(ctx.db, series), "changed": changed}


@skill(
    name="delete_series", category="series",
    description=(
        "删除连续剧。delete_episodes=false（默认）只解散系列、各集保留为独立项目；"
        "delete_episodes=true 会连同每一集的素材、镜头、任务一并彻底删除。"
    ),
    tags=("series", "destructive"),
    input_schema={"type": "object", "properties": {
        "series_id": {"type": "string"},
        "confirm": {"type": "boolean", "default": False},
        "delete_episodes": {"type": "boolean", "default": False}},
        "required": ["series_id", "confirm"]},
)
def delete_series(ctx: SkillContext, *, series_id: str, confirm: bool = False,
                  delete_episodes: bool = False) -> dict[str, Any]:
    if not confirm:
        raise SkillError("删除系列属于危险操作，请显式传 confirm=true", code="CONFIRM_REQUIRED")
    try:
        return series_svc.delete_series(ctx.db, series_id, delete_episodes=delete_episodes)
    except LookupError as exc:
        raise SkillError(str(exc), code="NOT_FOUND") from exc


# --------------------------------------------------------------------------- #
# Episode（一集 = 一个 Project）
# --------------------------------------------------------------------------- #
@skill(
    name="create_episode", category="series", is_async=False,
    description=(
        "在连续剧下新建一集。集号自动递增，自动继承系列的风格 / 画幅 / 每集时长，"
        "并初始化该集独立的工作流。返回的 project_id 即这一集，后续按普通项目流程生产。"
    ),
    tags=("series", "project", "write"),
    input_schema={"type": "object", "properties": {
        "series_id": {"type": "string"},
        "name": {"type": "string", "description": "集标题，留空则自动生成『剧名 第 N 集』"},
        "requirement": {"type": "string", "description": "本集的具体需求"},
        "description": {"type": "string"},
        "target_duration": {"type": "number", "description": "本集时长，默认取系列设置"},
    }, "required": ["series_id"]},
    examples=({"series_id": "ser_xxx", "requirement": "第 1 集：讲清楚 Agent 与传统脚本的区别"},),
)
def create_episode(ctx: SkillContext, *, series_id: str, name: str = "",
                   requirement: str = "", description: str = "",
                   target_duration: float | None = None) -> dict[str, Any]:
    series = _get_series(ctx.db, series_id)
    project = series_svc.create_episode(
        ctx.db, series, name=name, requirement=requirement, description=description,
        target_duration=target_duration,
    )
    agent_log.log_event(
        ctx.db, project_id=project.id, event="episode.created", actor=ctx.actor,
        message=f"新建第 {project.episode_no} 集：{project.name}",
        detail={"series_id": series.id, "episode_no": project.episode_no},
    )
    ctx.db.commit()
    return {"project": S.serialize_project(project, db=ctx.db), "project_id": project.id,
            "episode_no": project.episode_no,
            "message": f"第 {project.episode_no} 集已创建，建议下一步调用 create_script"}


@skill(
    name="list_episodes", category="series", is_async=False,
    description="列出某连续剧的全部集，按集号排序，含每集进度与当前节点。",
    tags=("series", "read"),
    input_schema={"type": "object", "properties": {"series_id": {"type": "string"}},
                  "required": ["series_id"]},
)
def list_episodes(ctx: SkillContext, *, series_id: str) -> dict[str, Any]:
    series = _get_series(ctx.db, series_id)
    episodes = series_svc.list_episodes(ctx.db, series.id)
    return {
        "series_id": series.id,
        "items": [
            {**projects_svc.summarize_project(ctx.db, p), "episode_no": p.episode_no}
            for p in episodes
        ],
        "total": len(episodes),
    }


@skill(
    name="attach_project_to_series", category="series",
    description="把已有项目挂到某个连续剧下并指定集号（用于把散落的独立项目整理成剧集）。",
    tags=("series", "project", "write"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "series_id": {"type": "string"},
        "episode_no": {"type": "integer", "description": "留空则自动取下一个集号"}},
        "required": ["project_id", "series_id"]},
)
def attach_project_to_series(ctx: SkillContext, *, project_id: str, series_id: str,
                             episode_no: int | None = None) -> dict[str, Any]:
    project = ctx.db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
    series = _get_series(ctx.db, series_id)
    project.series_id = series.id
    project.episode_no = int(episode_no) if episode_no else series_svc.next_episode_no(
        ctx.db, series.id
    )
    ctx.db.commit()
    return {"project": S.serialize_project(project, db=ctx.db),
            "episode_no": project.episode_no}


@skill(
    name="detach_project_from_series", category="series",
    description="把某一集从连续剧中摘出，变为独立项目（保留其全部素材与记录）。",
    tags=("series", "project", "write"),
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}},
                  "required": ["project_id"]},
)
def detach_project_from_series(ctx: SkillContext, *, project_id: str) -> dict[str, Any]:
    project = ctx.db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
    previous = {"series_id": project.series_id, "episode_no": project.episode_no}
    project.series_id = None
    project.episode_no = 0
    ctx.db.commit()
    return {"project": S.serialize_project(project, db=ctx.db), "detached_from": previous}


# --------------------------------------------------------------------------- #
# 系列级角色（跨集复用）
# --------------------------------------------------------------------------- #
@skill(
    name="list_series_characters", category="series", is_async=False,
    description="列出系列级角色库（跨集复用、保证主角形象一致的公共角色）。",
    tags=("series", "character", "read"),
    input_schema={"type": "object", "properties": {"series_id": {"type": "string"}},
                  "required": ["series_id"]},
)
def list_series_characters(ctx: SkillContext, *, series_id: str) -> dict[str, Any]:
    series = _get_series(ctx.db, series_id)
    chars = series_svc.series_characters(ctx.db, series.id)
    return {"series_id": series.id,
            "items": [S.serialize_character(c, db=ctx.db) for c in chars],
            "total": len(chars)}


@skill(
    name="promote_character_to_series", category="series",
    description=(
        "把某一集里的角色提升为系列级角色，供全系列各集引用。"
        "角色的参考图与设定保留，只改变归属。"
    ),
    tags=("series", "character", "write"),
    input_schema={"type": "object", "properties": {
        "character_id": {"type": "string"},
        "series_id": {"type": "string", "description": "留空则取其所属集的系列"}},
        "required": ["character_id"]},
)
def promote_character_to_series(ctx: SkillContext, *, character_id: str,
                                series_id: str = "") -> dict[str, Any]:
    char = ctx.db.get(Character, character_id)
    if char is None:
        raise SkillError(f"角色不存在: {character_id}", code="NOT_FOUND")

    target = series_id
    if not target and char.project_id:
        project = ctx.db.get(Project, char.project_id)
        target = project.series_id if project else None
    if not target:
        raise SkillError("无法确定目标系列：请显式传 series_id", code="MISSING_SERIES")
    series = _get_series(ctx.db, target)

    char.series_id = series.id
    char.project_id = None          # 系列级角色不属于任何单集
    ctx.db.commit()
    return {"character": S.serialize_character(char, db=ctx.db),
            "series_id": series.id,
            "message": "已提升为系列级角色，系列下各集均可引用"}
