"""任务端点：列表 / 详情 / 重试 / 取消 / 日志。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Task
from ..services import serializers as S, tasks as tasks_svc
from ..skills import invoke_skill

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
def list_tasks(
    project_id: str | None = None, status: str | None = None, type: str | None = None,
    shot_id: str | None = None, limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0), db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = tasks_svc.list_tasks(db, project_id=project_id, status=status, type=type,
                               shot_id=shot_id, limit=limit, offset=offset)
    stats = tasks_svc.task_stats(db, project_id) if project_id else {}
    return {"items": [S.serialize_task(t, include_logs=False) for t in rows],
            "total": len(rows), "stats": stats}


@router.get("/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": S.serialize_task(task)}


@router.get("/{task_id}/logs")
def task_logs(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task_id": task_id, "logs": task.logs or [], "status": task.status,
            "progress": task.progress, "error": task.error, "error_detail": task.error_detail}


@router.post("/{task_id}/retry")
def retry_task(task_id: str, reset_attempts: bool = Query(True),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    result = invoke_skill(db, "retry_task", {"task_id": task_id, "reset_attempts": reset_attempts})
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = invoke_skill(db, "cancel_task", {"task_id": task_id})
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
