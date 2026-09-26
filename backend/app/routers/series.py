"""连续剧（Series）REST 端点。

读操作直接走 service；写操作统一走 Skill 层（与项目/内容端点保持一致的约定）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Series
from ..services import projects as projects_svc
from ..services import serializers as S
from ..services import series as series_svc
from ..skills import invoke_skill

router = APIRouter(prefix="/api", tags=["series"])


def _series_or_404(db: Session, series_id: str) -> Series:
    series = db.get(Series, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="系列不存在")
    return series


@router.get("/series")
def list_series(
    status: str | None = None, keyword: str | None = None,
    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Series).order_by(Series.updated_at.desc())
    if status:
        stmt = stmt.where(Series.status == status)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(Series.name.ilike(like) | Series.requirement.ilike(like))
    rows = list(db.execute(stmt.offset(offset).limit(limit)).scalars())
    return {"items": [series_svc.summarize_series(db, s) for s in rows], "total": len(rows)}


@router.post("/series")
def create_series(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    result = invoke_skill(db, "create_series", payload or {})
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/series/{series_id}")
def get_series(series_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    series = _series_or_404(db, series_id)
    return {"series": series_svc.series_overview(db, series)}


@router.patch("/series/{series_id}")
def update_series(series_id: str, payload: dict[str, Any] = Body(...),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    _series_or_404(db, series_id)
    result = invoke_skill(db, "update_series", {**(payload or {}), "series_id": series_id})
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.delete("/series/{series_id}")
def delete_series(series_id: str, delete_episodes: bool = False,
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    """删除系列。delete_episodes=false 时各集降级为独立项目，数据保留。"""
    _series_or_404(db, series_id)
    result = invoke_skill(db, "delete_series", {
        "series_id": series_id, "confirm": True, "delete_episodes": delete_episodes,
    })
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/series/{series_id}/episodes")
def list_episodes(series_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    _series_or_404(db, series_id)
    episodes = series_svc.list_episodes(db, series_id)
    return {
        "items": [
            {**projects_svc.summarize_project(db, p), "episode_no": p.episode_no}
            for p in episodes
        ],
        "total": len(episodes),
    }


@router.post("/series/{series_id}/episodes")
def create_episode(series_id: str, payload: dict[str, Any] = Body(default_factory=dict),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    _series_or_404(db, series_id)
    result = invoke_skill(db, "create_episode", {**(payload or {}), "series_id": series_id})
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/series/{series_id}/characters")
def list_series_characters(series_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """系列级角色库（跨集复用）。"""
    _series_or_404(db, series_id)
    chars = series_svc.series_characters(db, series_id)
    return {"items": [S.serialize_character(c, db=db) for c in chars], "total": len(chars)}
