"""项目相关 REST 端点（读为主，写操作统一走 Skill 层）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.constants import WORKFLOW_STEPS
from ..database import get_db
from ..models import Project, Script, Storyboard
from ..services import agent_log, projects as projects_svc, quality as quality_svc
from ..services import pipeline as pipeline_svc
from ..services import serializers as S, tasks as tasks_svc, workflow as workflow_svc
from ..skills import invoke_skill
from ..storage import get_storage

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects")
def list_projects(
    status: str | None = None, keyword: str | None = None,
    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Project).order_by(Project.updated_at.desc())
    if status:
        stmt = stmt.where(Project.status == status)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(Project.name.ilike(like) | Project.requirement.ilike(like))
    rows = list(db.execute(stmt.offset(offset).limit(limit)).scalars())
    return {"items": [projects_svc.summarize_project(db, p) for p in rows], "total": len(rows)}


@router.post("/projects")
def create_project(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    result = invoke_skill(db, "create_project", payload)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"project": S.serialize_project(project, db=db)}


@router.get("/projects/{project_id}/status")
def project_status(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return projects_svc.project_status(db, project)


@router.get("/projects/{project_id}/overview")
def project_overview(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """项目详情页一次性聚合数据（项目信息 + 生产进度 + 各模块摘要 + 成片）。"""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")

    script = db.execute(
        select(Script).where(Script.project_id == project_id).order_by(Script.created_at.desc())
    ).scalars().first()
    storyboard = db.execute(
        select(Storyboard).where(Storyboard.project_id == project_id)
        .order_by(Storyboard.created_at.desc())
    ).scalars().first()
    final_asset = projects_svc.final_output_asset(db, project_id)

    return {
        "project": S.serialize_project(project, db=db),
        "status": projects_svc.project_status(db, project),
        "steps_definition": [{"step_key": k, "name": n, "description": d} for k, n, d in WORKFLOW_STEPS],
        "script": S.serialize_script(script) if script else None,
        "storyboard": S.serialize_storyboard(storyboard, db=db, include_shots=False) if storyboard else None,
        "final_output": S.asset_brief(final_asset),
        "quality": quality_svc.latest_run(db, project_id),
        "task_stats": tasks_svc.task_stats(db, project_id),
        "recent_logs": [
            {"id": r.id, "ts": S.iso(r.created_at), "actor": r.actor, "event": r.event,
             "level": r.level, "message": r.message, "task_id": r.task_id, "shot_id": r.shot_id}
            for r in agent_log.list_logs(db, project_id=project_id, limit=30)
        ],
    }


@router.get("/projects/{project_id}/workflow")
def project_workflow(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return workflow_svc.workflow_summary(db, project)


@router.get("/projects/{project_id}/pipeline")
def project_pipeline(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """节点式生产流水线视图：状态 / 产出缩略图 / 审核 / 可执行动作。"""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return pipeline_svc.node_view(db, project)


@router.get("/projects/{project_id}/quality")
def project_quality(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    report = quality_svc.latest_run(db, project_id)
    return {"report": report}


@router.get("/projects/{project_id}/outputs")
def project_outputs(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """列出该项目历史上产出的所有成片，供「版本对比」使用。

    每次合成都会在 ``storage/outputs/{project_id}/`` 落一个新文件，
    因此这里直接扫目录即可拿到全部版本；当前生效的那一版用 ``overview.final_output``
    的 URL 做匹配并标记 ``is_current``。
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")

    storage = get_storage()
    rel_dir = f"outputs/{project_id}"
    abs_dir = storage.abs_path(rel_dir)
    # 当前生效的成片（DB 里的 PROJECT_OUTPUT 资产），用于给列表打 is_current 标记。
    # 注意：资产 URL 在 Windows 上可能是反斜杠形式，比较前统一成正斜杠。
    current_asset = projects_svc.final_output_asset(db, project_id)
    current_url = (current_asset.url if current_asset else "") or ""
    current_norm = current_url.replace("\\", "/")

    items: list[dict[str, Any]] = []
    if abs_dir.is_dir():
        for path in abs_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
                continue
            stat = path.stat()
            url = storage.url_for(f"{rel_dir}/{path.name}")
            items.append({
                "name": path.name,
                "url": url,
                "is_current": bool(current_norm) and current_norm.endswith("/" + path.name),
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
                "mtime": stat.st_mtime,
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    for item in items:
        item.pop("mtime", None)
    return {"items": items, "total": len(items), "current_url": current_norm}


@router.get("/projects/{project_id}/logs")
def project_logs(
    project_id: str, level: str | None = None,
    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = agent_log.list_logs(db, project_id=project_id, level=level, limit=limit, offset=offset)
    return {"items": [
        {"id": r.id, "ts": S.iso(r.created_at), "actor": r.actor, "event": r.event,
         "level": r.level, "message": r.message, "detail": r.detail or {},
         "task_id": r.task_id, "shot_id": r.shot_id, "duration_ms": r.duration_ms}
        for r in rows
    ], "total": len(rows)}


@router.get("/projects/{project_id}/tasks")
def project_tasks(
    project_id: str, status: str | None = None, type: str | None = None,
    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = tasks_svc.list_tasks(db, project_id=project_id, status=status, type=type,
                               limit=limit, offset=offset)
    return {"items": [S.serialize_task(t) for t in rows], "total": len(rows),
            "stats": tasks_svc.task_stats(db, project_id)}
