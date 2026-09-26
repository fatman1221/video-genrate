"""项目状态与进度计算。

进度不是简单看 workfow 状态，而是结合 Shot 完成度、Asset 数量、
Task 失败情况综合计算，保证 Web UI 上的「62%」是真实的。
"""
from __future__ import annotations

from typing import Any

from pathlib import Path

from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.orm import Session

from ..core.constants import (
    AssetType, ProjectStatus, ShotStatus, TaskStatus, WORKFLOW_STEPS, WorkflowState,
    STEP_WEIGHTS,
)
from ..models import (
    AgentLog, Asset, BrowserTask, Character, Project, QualityCheck, Scene, Shot, Task,
    WorkflowStep,
)
from ..storage import TYPE_DIRS, get_storage
from . import characters as characters_svc
from . import workflow as workflow_svc


def _ratio(done: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return min(done / total, 1.0)


def compute_progress(db: Session, project: Project) -> int:
    wf = workflow_svc.get_workflow(db, project.id)
    shot_total = db.scalar(select(func.count(Shot.id)).where(Shot.project_id == project.id)) or 0
    shot_image = db.scalar(
        select(func.count(Shot.id)).where(Shot.project_id == project.id, Shot.image_asset_id.isnot(None))
    ) or 0
    shot_video = db.scalar(
        select(func.count(Shot.id)).where(Shot.project_id == project.id, Shot.video_asset_id.isnot(None))
    ) or 0
    shot_voice = db.scalar(
        select(func.count(Shot.id)).where(Shot.project_id == project.id, Shot.voice_asset_id.isnot(None))
    ) or 0
    shot_sub = db.scalar(
        select(func.count(Shot.id)).where(Shot.project_id == project.id, Shot.subtitle_asset_id.isnot(None))
    ) or 0

    ratios: dict[str, float] = {}
    if wf is not None:
        for step in wf.steps:
            if step.state == "SUCCESS":
                ratios[step.step_key] = 1.0
            elif step.state == "RUNNING":
                ratios[step.step_key] = 0.35
            elif step.state == "FAILED":
                ratios[step.step_key] = 0.2
            else:
                ratios[step.step_key] = 0.0
        # 生成类步骤用真实产出比例覆盖
        if wf and shot_total:
            ratios["image"] = max(ratios.get("image", 0.0), _ratio(shot_image, shot_total))
            ratios["video"] = max(ratios.get("video", 0.0), _ratio(shot_video, shot_total))
            ratios["voice"] = max(ratios.get("voice", 0.0), _ratio(shot_voice, shot_total))
            ratios["subtitle"] = max(ratios.get("subtitle", 0.0), _ratio(shot_sub, shot_total))

    if project.workflow_state == WorkflowState.COMPLETED:
        return 100

    total_weight = sum(STEP_WEIGHTS.values())
    earned = sum(STEP_WEIGHTS.get(key, 0) * ratios.get(key, 0.0) for key, _, _ in WORKFLOW_STEPS)
    return int(round(earned / total_weight * 100))


def refresh_progress(db: Session, project: Project, *, commit: bool = True) -> int:
    project.progress = compute_progress(db, project)
    if commit:
        db.commit()
    return project.progress


def stage_overview(db: Session, project: Project) -> list[dict[str, Any]]:
    """给 Web UI 展示的阶段列表（✓ 脚本 / ◉ 视频生成 / ○ 配音 ...）。"""
    wf = workflow_svc.get_workflow(db, project.id)
    steps = {s.step_key: s for s in (wf.steps if wf else [])}
    shot_total = db.scalar(select(func.count(Shot.id)).where(Shot.project_id == project.id)) or 0
    shot_video = db.scalar(
        select(func.count(Shot.id)).where(Shot.project_id == project.id, Shot.video_asset_id.isnot(None))
    ) or 0

    overview = []
    for key, label, _desc in WORKFLOW_STEPS:
        step = steps.get(key)
        state = step.state if step else "PENDING"
        detail = ""
        if key == "video" and shot_total:
            detail = f"{shot_video}/{shot_total} 个镜头"
        elif key == "image" and shot_total:
            c = db.scalar(
                select(func.count(Shot.id)).where(
                    Shot.project_id == project.id, Shot.image_asset_id.isnot(None)
                )
            ) or 0
            detail = f"{c}/{shot_total} 个镜头"
        elif key == "storyboard" and shot_total:
            scene_total = db.scalar(
                select(func.count(Scene.id)).where(Scene.project_id == project.id)
            ) or 0
            detail = f"{scene_total} 场景 / {shot_total} 镜头"
        elif key == "character":
            c = len(characters_svc.character_pool(db, project))
            detail = f"{c} 个角色"
        elif step and step.output:
            detail = ", ".join(f"{k}={v}" for k, v in list(step.output.items())[:2])
        overview.append({
            "step_key": key, "label": label, "state": state,
            "attempts": step.attempts if step else 0,
            "error": step.error if step else "",
            "detail": detail,
        })
    return overview


def project_status(db: Session, project: Project) -> dict[str, Any]:
    wf = workflow_svc.get_workflow(db, project.id)
    shot_rows = db.execute(
        select(Shot.status, func.count(Shot.id)).where(Shot.project_id == project.id).group_by(Shot.status)
    ).all()
    task_rows = db.execute(
        select(Task.status, func.count(Task.id)).where(Task.project_id == project.id).group_by(Task.status)
    ).all()
    asset_rows = db.execute(
        select(Asset.type, func.count(Asset.id)).where(Asset.project_id == project.id).group_by(Asset.type)
    ).all()

    shots_by_status = {r[0]: r[1] for r in shot_rows}
    shot_total = sum(shots_by_status.values())
    done_shots = shots_by_status.get(ShotStatus.READY, 0) + shots_by_status.get(ShotStatus.VIDEO_READY, 0)
    failed_shots = shots_by_status.get(ShotStatus.FAILED, 0)

    tasks_by_status = {r[0]: r[1] for r in task_rows}
    assets_by_type = {r[0]: r[1] for r in asset_rows}
    character_count = len(characters_svc.character_pool(db, project))
    scene_count = db.scalar(select(func.count(Scene.id)).where(Scene.project_id == project.id)) or 0

    progress = compute_progress(db, project)
    next_actions: list[str] = []
    for step in stage_overview(db, project):
        if step["state"] in ("PENDING", "FAILED", "RUNNING"):
            next_actions.append(f"{step['label']}（{step['step_key']}）")
    if failed_shots:
        next_actions.insert(0, f"重跑 {failed_shots} 个失败的镜头")

    return {
        "project_id": project.id,
        "name": project.name,
        "status": project.status,
        "progress": progress,
        "workflow_state": wf.state if wf else project.workflow_state,
        "workflow_paused": bool(wf.paused) if wf else False,
        "stages": stage_overview(db, project),
        "counts": {
            "scenes": scene_count,
            "shots": shot_total,
            "shots_done": done_shots,
            "shots_failed": failed_shots,
            "characters": character_count,
            "assets": sum(assets_by_type.values()),
            "tasks": sum(tasks_by_status.values()),
            "tasks_failed": tasks_by_status.get(TaskStatus.FAILED, 0),
            "tasks_running": tasks_by_status.get(TaskStatus.RUNNING, 0)
                               + tasks_by_status.get(TaskStatus.PENDING, 0),
        },
        "shots_by_status": shots_by_status,
        "tasks_by_status": tasks_by_status,
        "assets_by_type": assets_by_type,
        "next_actions": next_actions,
        "can_complete": bool(shot_total) and failed_shots == 0 and (
            project.workflow_state in (WorkflowState.QUALITY_CHECK, WorkflowState.COMPOSING, WorkflowState.COMPLETED)
        ),
    }


def pending_shots(db: Session, project_id: str, *, kind: str = "video") -> list[Shot]:
    """返回需要生成指定产物的镜头。"""
    column = {
        "image": Shot.image_asset_id,
        "video": Shot.video_asset_id,
        "voice": Shot.voice_asset_id,
        "subtitle": Shot.subtitle_asset_id,
    }.get(kind)
    if column is None:
        return []
    stmt = (
        select(Shot)
        .where(Shot.project_id == project_id, column.is_(None))
        .order_by(Shot.sequence.asc())
    )
    return list(db.execute(stmt).scalars())


def summarize_project(db: Session, project: Project) -> dict[str, Any]:
    """项目列表页用的轻量摘要（含封面与节点进度，供卡片式 UI 直接渲染）。"""
    shot_total = db.scalar(select(func.count(Shot.id)).where(Shot.project_id == project.id)) or 0
    asset_total = db.scalar(select(func.count(Asset.id)).where(Asset.project_id == project.id)) or 0

    cover = None
    for asset_type in (AssetType.PROJECT_OUTPUT, AssetType.IMAGE, AssetType.CHARACTER):
        row = db.execute(
            select(Asset).where(Asset.project_id == project.id, Asset.type == asset_type)
            .order_by(Asset.created_at.desc())
        ).scalars().first()
        if row is not None and row.url:
            cover = row.url
            break

    wf = workflow_svc.get_workflow(db, project.id)
    steps = list(wf.steps) if wf else []
    done_steps = sum(1 for s in steps if s.state == "SUCCESS")
    current = next((s for s in sorted(steps, key=lambda x: x.order_index) if s.state != "SUCCESS"), None)
    pending_review = sum(
        1 for s in steps if s.state == "SUCCESS" and s.review_status in ("NONE",)
        and s.step_key in ("script", "storyboard", "character", "image", "video",
                           "voice", "music", "subtitle", "composing")
    )
    failed_tasks = db.scalar(
        select(func.count(Task.id)).where(Task.project_id == project.id, Task.status == TaskStatus.FAILED)
    ) or 0

    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "requirement": project.requirement,
        "style": project.style,
        "status": project.status,
        "workflow_state": project.workflow_state,
        # 分集信息（列表/卡片直接展示「第 N 集」）
        "series_id": project.series_id,
        "episode_no": project.episode_no,
        "episode_label": f"第 {project.episode_no} 集" if project.episode_no else "",
        "progress": compute_progress(db, project),
        "shot_count": shot_total,
        "asset_count": asset_total,
        "target_duration": project.target_duration,
        "cover_url": cover,
        "nodes_done": done_steps,
        "nodes_total": len(steps),
        "current_node": current.name if current is not None else "全部完成",
        "pending_review": pending_review,
        "failed_tasks": failed_tasks,
        "updated_at": project.updated_at,
        "created_at": project.created_at,
    }


def purge_project(db: Session, project_id: str) -> dict[str, Any]:
    """彻底删除项目：关联数据（级联）+ 磁盘素材，均不可恢复。

    顺序很重要——先定位并清理磁盘文件，再删数据库记录，避免出现
    「记录没了、文件还占着磁盘」的孤儿素材。素材落盘路径统一为
    ``{类型子目录}/{project_id}``（见 ``storage.type_dir``），因此按
    ``TYPE_DIRS`` 枚举子目录逐个整目录清理即可覆盖全部产物。
    """
    import shutil

    project = db.get(Project, project_id)
    if project is None:
        raise LookupError(f"项目不存在: {project_id}")

    name = project.name
    storage = get_storage()
    removed_files = 0

    # 1) 精确删除登记在册的素材文件（兼容未来对象存储后端）
    assets = list(db.execute(select(Asset).where(Asset.project_id == project_id)).scalars())
    for asset in assets:
        if not asset.file_path:
            continue
        try:
            target = storage.abs_path(asset.file_path) if storage.name == "local" else Path(asset.file_path)
            if target.exists():
                target.unlink()
                removed_files += 1
        except (OSError, ValueError):
            pass

    # 2) 整目录兜底清理（含渲染临时产物等未登记文件）
    removed_dirs = 0
    if storage.name == "local":
        for sub in sorted(set(TYPE_DIRS.values())):
            directory = storage.abs_path(f"{sub}/{project_id}")
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
                removed_dirs += 1

    # 3) 清理尚未挂外键级联的从属表。
    #    这四张表在早期版本里只声明了裸 project_id 列（见 models.py 已修正），
    #    老数据库不会随项目删除自动清理，这里显式兜底，避免产生孤儿记录。
    for model in (WorkflowStep, QualityCheck, AgentLog, BrowserTask):
        db.execute(sa_delete(model).where(model.project_id == project_id))

    # 4) 数据库级联删除（shots / scenes / assets / tasks / workflow 等）
    db.delete(project)
    db.commit()

    return {
        "deleted": True,
        "project_id": project_id,
        "name": name,
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
    }


def final_output_asset(db: Session, project_id: str) -> Asset | None:
    return db.execute(
        select(Asset)
        .where(Asset.project_id == project_id, Asset.type == AssetType.PROJECT_OUTPUT)
        .order_by(Asset.created_at.desc())
    ).scalars().first()
