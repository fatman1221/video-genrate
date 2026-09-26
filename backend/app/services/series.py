"""连续剧（Series）服务层。

分层：Series（整部剧）→ Project（每一集）→ 现有流水线。

之所以把「一集」建模成一个普通 Project，而不是在 Project 内部再开一层 Episode，
是为了完整复用已经验证过的能力：每集都有自己的 Workflow、节点回退、审核、
重生成与成片；某一集出问题不会影响其它集。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.constants import ProjectStatus, WorkflowState
from ..models import Asset, Character, Project, Series
from . import projects as projects_svc
from . import serializers as S
from . import workflow as workflow_svc


def list_episodes(db: Session, series_id: str) -> list[Project]:
    """按集号返回系列下全部集（未编号的排在最后）。"""
    stmt = (
        select(Project)
        .where(Project.series_id == series_id)
        .order_by(Project.episode_no.asc(), Project.created_at.asc())
    )
    return list(db.execute(stmt).scalars())


def next_episode_no(db: Session, series_id: str) -> int:
    current = db.scalar(
        select(func.max(Project.episode_no)).where(Project.series_id == series_id)
    ) or 0
    return int(current) + 1


def series_characters(db: Session, series_id: str) -> list[Character]:
    """系列级角色库：跨集复用的角色（project_id 为空、series_id 指向本系列）。"""
    stmt = (
        select(Character)
        .where(Character.series_id == series_id)
        .order_by(Character.created_at.asc())
    )
    return list(db.execute(stmt).scalars())


def _cover_url(db: Session, series: Series, episodes: list[Project]) -> str | None:
    for char in series_characters(db, series.id):
        if char.reference_asset_id:
            asset = db.get(Asset, char.reference_asset_id)
            if asset is not None and asset.url:
                return asset.url
    for project in episodes:
        brief = projects_svc.summarize_project(db, project)
        if brief.get("cover_url"):
            return brief["cover_url"]
    return None


def summarize_series(db: Session, series: Series) -> dict[str, Any]:
    """系列摘要：用于列表页卡片（集数、完结集数、整体进度、封面）。"""
    episodes = list_episodes(db, series.id)
    progresses = [projects_svc.compute_progress(db, p) for p in episodes]
    completed = sum(1 for p in episodes if p.workflow_state == WorkflowState.COMPLETED)
    character_count = db.scalar(
        select(func.count(Character.id)).where(Character.series_id == series.id)
    ) or 0

    return {
        "series_id": series.id,
        "id": series.id,
        "name": series.name,
        "description": series.description,
        "requirement": series.requirement,
        "style": series.style,
        "language": series.language,
        "status": series.status,
        "aspect_ratio": series.aspect_ratio,
        "width": series.width,
        "height": series.height,
        "fps": series.fps,
        "episode_duration": series.episode_duration,
        "planned_episodes": series.planned_episodes,
        "owner": series.owner,
        "extra": series.extra or {},
        "episode_count": len(episodes),
        "episodes_completed": completed,
        # 整体进度 = 各集进度的平均，未开机时为 0
        "progress": int(round(sum(progresses) / len(progresses))) if progresses else 0,
        "character_count": character_count,
        "cover_url": _cover_url(db, series, episodes),
        "created_at": S.iso(series.created_at),
        "updated_at": S.iso(series.updated_at),
    }


def series_overview(db: Session, series: Series) -> dict[str, Any]:
    """系列总览：摘要 + 各集卡片 + 系列级角色库。"""
    data = summarize_series(db, series)
    data["episodes"] = [
        {
            **projects_svc.summarize_project(db, project),
            "episode_no": project.episode_no,
            "series_id": project.series_id,
        }
        for project in list_episodes(db, series.id)
    ]
    data["characters"] = [
        S.serialize_character(char, db=db) for char in series_characters(db, series.id)
    ]
    return data


def create_episode(
    db: Session, series: Series, *, name: str = "", requirement: str = "",
    description: str = "", target_duration: float | None = None,
    **extra: Any,
) -> Project:
    """在系列下新建一集（即一个 Project），并初始化它的工作流。"""
    episode_no = next_episode_no(db, series.id)
    project = Project(
        series_id=series.id,
        episode_no=episode_no,
        name=name or f"{series.name} 第 {episode_no} 集",
        requirement=requirement or series.requirement,
        description=description or requirement or series.description,
        style=series.style,
        language=series.language,
        target_duration=float(target_duration or series.episode_duration),
        aspect_ratio=series.aspect_ratio,
        width=series.width,
        height=series.height,
        fps=series.fps,
        owner=series.owner,
        status=ProjectStatus.PLANNING,
        workflow_state=WorkflowState.PROJECT_CREATED,
        extra=extra or {},
    )
    db.add(project)
    db.flush()
    workflow_svc.ensure_workflow(db, project)
    return project


def delete_series(db: Session, series_id: str, *, delete_episodes: bool = False) -> dict[str, Any]:
    """删除系列。

    ``delete_episodes=False``（默认）：只解散系列，各集降级为独立项目，数据保留。
    ``delete_episodes=True``：连同每一集的全部素材、镜头、任务一并彻底清理。
    """
    series = db.get(Series, series_id)
    if series is None:
        raise LookupError(f"系列不存在: {series_id}")

    name = series.name
    episodes = list_episodes(db, series_id)
    removed_episodes: list[str] = []

    if delete_episodes:
        for project in episodes:
            projects_svc.purge_project(db, project.id)
            removed_episodes.append(project.id)
    else:
        for project in episodes:
            project.series_id = None
            project.episode_no = 0
        db.flush()

    # 系列级角色由外键 ON DELETE CASCADE 一并清除
    db.delete(series)
    db.commit()

    return {
        "deleted": True,
        "series_id": series_id,
        "name": name,
        "episodes_detached": 0 if delete_episodes else len(episodes),
        "episodes_deleted": len(removed_episodes),
        "deleted_episode_ids": removed_episodes,
    }
