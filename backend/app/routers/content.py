"""内容端点：脚本 / 分镜 / 场景 / 镜头 / 角色。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Character, Project, Scene, Script, Shot, Storyboard
from ..services import characters as characters_svc
from ..services import serializers as S

router = APIRouter(prefix="/api", tags=["content"])


@router.get("/projects/{project_id}/script")
def get_script(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    script = db.execute(
        select(Script).where(Script.project_id == project_id).order_by(Script.created_at.desc())
    ).scalars().first()
    return {"script": S.serialize_script(script) if script else None}


@router.get("/projects/{project_id}/storyboard")
def get_storyboard(project_id: str, include_shots: bool = True,
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    board = db.execute(
        select(Storyboard).where(Storyboard.project_id == project_id)
        .order_by(Storyboard.created_at.desc())
    ).scalars().first()
    if board is None:
        return {"storyboard": None}
    return {"storyboard": S.serialize_storyboard(board, db=db, include_shots=include_shots)}


@router.get("/projects/{project_id}/scenes")
def list_scenes(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(
        select(Scene).where(Scene.project_id == project_id).order_by(Scene.sequence.asc())
    ).scalars().all()
    return {"items": [S.serialize_scene(s) for s in rows], "total": len(rows)}


@router.get("/projects/{project_id}/shots")
def list_shots(project_id: str, scene_id: str | None = None, status: str | None = None,
               db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(Shot).where(Shot.project_id == project_id).order_by(Shot.sequence.asc())
    if scene_id:
        stmt = stmt.where(Shot.scene_id == scene_id)
    if status:
        stmt = stmt.where(Shot.status == status)
    rows = list(db.execute(stmt).scalars())
    return {"items": [S.serialize_shot(s, db=db) for s in rows], "total": len(rows)}


@router.get("/shots/{shot_id}")
def get_shot(shot_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="镜头不存在")
    return {"shot": S.serialize_shot(shot, db=db)}


@router.get("/projects/{project_id}/characters")
def list_characters(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """列出该集可用角色：本集角色 + 所属连续剧的系列级角色。"""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    rows = characters_svc.character_pool(db, project)
    return {"items": [S.serialize_character(c, db=db) for c in rows], "total": len(rows)}


@router.get("/characters/{character_id}")
def get_character(character_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    char = db.get(Character, character_id)
    if char is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return {"character": S.serialize_character(char, db=db)}
