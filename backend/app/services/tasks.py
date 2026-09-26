"""任务服务：创建 / 查询 / 取消 / 重试。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.constants import TaskStatus
from ..models import Task
from . import agent_log


def create_task(
    db: Session, *,
    project_id: str,
    type: str,
    name: str = "",
    payload: dict[str, Any] | None = None,
    shot_id: str | None = None,
    character_id: str | None = None,
    parent_task_id: str | None = None,
    priority: int = 5,
    max_attempts: int = 3,
    provider: str = "",
    created_by: str = "agent",
    commit: bool = True,
) -> Task:
    task = Task(
        project_id=project_id, type=type, name=name or type, payload=payload or {},
        shot_id=shot_id, character_id=character_id, parent_task_id=parent_task_id,
        priority=priority, max_attempts=max_attempts, provider=provider, created_by=created_by,
        status=TaskStatus.PENDING,
    )
    db.add(task)
    db.flush()
    agent_log.log_event(
        db, project_id=project_id, task_id=task.id, shot_id=shot_id,
        event="task.created", message=f"创建任务：{task.name}", actor=created_by,
        detail={"type": type, "payload": {k: v for k, v in (payload or {}).items() if not k.startswith("_")}},
    )
    if commit:
        db.commit()
        db.refresh(task)
    return task


def get_task(db: Session, task_id: str) -> Task | None:
    return db.get(Task, task_id)


def list_tasks(
    db: Session, *, project_id: str | None = None, status: str | None = None,
    type: str | None = None, shot_id: str | None = None, limit: int = 100, offset: int = 0,
) -> list[Task]:
    stmt = select(Task).order_by(Task.created_at.desc())
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if status:
        stmt = stmt.where(Task.status == status)
    if type:
        stmt = stmt.where(Task.type == type)
    if shot_id:
        stmt = stmt.where(Task.shot_id == shot_id)
    return list(db.execute(stmt.offset(offset).limit(limit)).scalars())


def cancel_task(db: Session, task_id: str) -> tuple[bool, str]:
    task = db.get(Task, task_id)
    if task is None:
        return False, "任务不存在"
    if task.status in TaskStatus.TERMINAL:
        return False, f"任务已处于终态 {task.status}"
    payload = dict(task.payload or {})
    payload["_cancel_requested"] = True
    task.payload = payload
    if task.status in (TaskStatus.PENDING, TaskStatus.RETRYING):
        task.status = TaskStatus.CANCELLED
        task.append_log("任务已取消", "WARN")
    db.commit()
    agent_log.log_event(db, project_id=task.project_id, task_id=task.id,
                        event="task.cancelled", level="WARN", message="任务已取消")
    db.commit()
    return True, "已请求取消"


def retry_task(db: Session, task_id: str, *, reset_attempts: bool = False) -> tuple[bool, str]:
    task = db.get(Task, task_id)
    if task is None:
        return False, "任务不存在"
    if task.status == TaskStatus.RUNNING:
        return False, "任务正在执行，请先取消"
    payload = dict(task.payload or {})
    payload.pop("_cancel_requested", None)
    payload.pop("_retry_after", None)
    task.payload = payload
    task.status = TaskStatus.PENDING
    task.progress = 0
    task.error = ""
    task.error_detail = ""
    task.finished_at = None
    if reset_attempts:
        task.attempts = 0
    task.append_log("任务已重新入队", "INFO")
    db.commit()
    agent_log.log_event(db, project_id=task.project_id, task_id=task.id,
                        event="task.requeued", message="任务已重新入队")
    db.commit()
    return True, "已重新入队"


def task_stats(db: Session, project_id: str) -> dict[str, int]:
    rows = db.execute(
        select(Task.status, func.count(Task.id)).where(Task.project_id == project_id).group_by(Task.status)
    ).all()
    return {r[0]: r[1] for r in rows}


def serialize_task(task: Task, *, include_logs: bool = True) -> dict[str, Any]:
    data = {
        "id": task.id,
        "project_id": task.project_id,
        "shot_id": task.shot_id,
        "character_id": task.character_id,
        "parent_task_id": task.parent_task_id,
        "type": task.type,
        "name": task.name,
        "status": task.status,
        "progress": task.progress,
        "priority": task.priority,
        "provider": task.provider,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "worker": task.worker,
        "error": task.error,
        "error_detail": task.error_detail,
        "payload": {k: v for k, v in (task.payload or {}).items() if not k.startswith("_")},
        "result": task.result or {},
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
    if include_logs:
        data["logs"] = task.logs or []
    return data
