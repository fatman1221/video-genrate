"""系统端点：健康检查、环境信息、实时事件流（SSE）。"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import settings
from ..core.constants import WORKFLOW_STEPS
from ..database import get_db, session_scope
from ..executors import registered_handlers, runner
from ..models import AgentLog, Project, Task
from ..providers import local_engine as engine, provider_catalog, register_all
from ..services import serializers as S

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    db_ok = True
    db_version = ""
    try:
        db_version = db.execute(text("select version()")).scalar() or ""
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_version = str(exc)
    register_all()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": {"ok": db_ok, "dialect": "postgresql" if settings.is_postgres else "sqlite",
                     "version": db_version[:80]},
        "storage": {"backend": settings.storage_backend, "root": str(settings.storage_path)},
        "engines": {
            "ffmpeg": {"available": engine.ffmpeg_available(), "bin": settings.ffmpeg_bin},
            "ffprobe": {"available": engine.ffprobe_available()},
            "say": {"available": engine.Path(engine.SAY).exists()},
        },
        "workers": {"running": runner.running, "count": runner.workers},
        "handlers": registered_handlers(),
        "providers": len(provider_catalog()),
    }


@router.get("/system/info")
def system_info(db: Session = Depends(get_db)) -> dict[str, Any]:
    register_all()
    projects = db.scalar(select(func.count(Project.id))) or 0
    tasks = db.scalar(select(func.count(Task.id))) or 0
    logs = db.scalar(select(func.count(AgentLog.id))) or 0
    return {
        "app": settings.app_name,
        "api_prefix": settings.api_prefix,
        "database_url": settings.database_url.split("@")[-1],
        "storage_root": str(settings.storage_path),
        "counts": {"projects": projects, "tasks": tasks, "agent_logs": logs},
        "workflow_steps": [{"step_key": k, "name": n} for k, n, _ in WORKFLOW_STEPS],
        "handlers": registered_handlers(),
        "providers": provider_catalog(),
        "defaults": {k: settings.__dict__.get(f"default_provider_{k}") for k in
                     ("image", "video", "tts", "music", "sfx", "subtitle", "enhance")},
    }


@router.get("/projects/{project_id}/stream")
def project_stream(project_id: str, interval: float = 1.0) -> StreamingResponse:
    """SSE：实时推送 Agent 日志与任务状态，Web UI 用它做『实时流水』。"""

    def event_stream() -> Iterator[str]:
        last_log_ts = ""
        last_task_sig = ""
        heartbeat = 0
        for _ in range(1800):  # 最长约 30 分钟，前端会自动重连
            try:
                with session_scope() as db:
                    logs = db.execute(
                        select(AgentLog).where(AgentLog.project_id == project_id)
                        .order_by(AgentLog.created_at.desc()).limit(25)
                    ).scalars().all()
                    new_logs = []
                    for row in logs:
                        ts = S.iso(row.created_at)
                        if ts <= last_log_ts:
                            continue
                        new_logs.append({
                            "id": row.id, "ts": ts, "actor": row.actor, "event": row.event,
                            "level": row.level, "message": row.message, "task_id": row.task_id,
                            "shot_id": row.shot_id, "detail": row.detail or {},
                        })
                    if logs:
                        last_log_ts = max(S.iso(x.created_at) for x in logs)

                    rows = db.execute(
                        select(Task).where(Task.project_id == project_id)
                        .order_by(Task.updated_at.desc()).limit(40)
                    ).scalars().all()
                    task_payload = [
                        {"task_id": t.id, "type": t.type, "name": t.name, "status": t.status,
                         "progress": t.progress, "shot_id": t.shot_id, "error": t.error,
                         "updated_at": S.iso(t.updated_at)}
                        for t in rows
                    ]
            except Exception as exc:  # noqa: BLE001
                yield f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n"
                time.sleep(2)
                continue

            sig = json.dumps([[t["task_id"], t["status"], t["progress"]] for t in task_payload])
            if new_logs:
                yield f"event: logs\ndata: {json.dumps({'logs': new_logs}, ensure_ascii=False)}\n\n"
            if sig != last_task_sig:
                last_task_sig = sig
                yield f"event: tasks\ndata: {json.dumps({'tasks': task_payload}, ensure_ascii=False)}\n\n"
            heartbeat += 1
            if heartbeat % 15 == 0:
                yield ": keep-alive\n\n"
            time.sleep(max(interval, 0.3))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
