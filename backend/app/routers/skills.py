"""Skill 与 Provider 端点：Agent 接入本项目的入口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..providers import provider_catalog, register_all, registry, sync_providers_table
from ..skills import invoke_skill, registry as skill_registry

router = APIRouter(prefix="/api", tags=["skills"])


@router.get("/skills")
def list_skills(
    category: str | None = None, keyword: str | None = None, detail: bool = False,
) -> dict[str, Any]:
    """列出全部 Skill。detail=true 时返回完整 JSON Schema（Agent 自描述用）。"""
    items = skill_registry.list(category=category, keyword=keyword)
    if detail:
        return {"skills": [s.describe() for s in items],
                "categories": skill_registry.categories(), "total": len(items)}
    return {
        "skills": [{"name": s.name, "description": s.description, "category": s.category,
                    "async": s.is_async, "tags": list(s.tags)} for s in items],
        "categories": skill_registry.categories(),
        "total": len(items),
    }


@router.get("/skills/{name}")
def describe_skill(name: str) -> dict[str, Any]:
    obj = skill_registry.get(name)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"未找到 Skill: {name}")
    return obj.describe()


@router.post("/skills/{name}/invoke")
def invoke(name: str, payload: dict[str, Any] = Body(default_factory=dict),
           actor: str = Query("agent"), db: Session = Depends(get_db)) -> dict[str, Any]:
    """统一 Skill 调用入口。长任务返回 taskId，请用 get_task_status 轮询。"""
    if skill_registry.get(name) is None:
        raise HTTPException(status_code=404, detail=f"未找到 Skill: {name}")
    return invoke_skill(db, name, payload or {}, actor=actor)


@router.get("/providers")
def list_providers(kind: str | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    register_all()
    catalog = provider_catalog()
    if kind:
        catalog = [p for p in catalog if p["kind"] == kind]
    return {
        "providers": catalog,
        "defaults": {k: registry.default_name(k) for k in
                     ("image", "video", "tts", "music", "sfx", "subtitle",
                      "enhance", "processing", "browser")},
    }


@router.post("/providers/sync")
def sync_providers(db: Session = Depends(get_db)) -> dict[str, Any]:
    count = sync_providers_table(db)
    return {"synced": count}


@router.post("/providers/default")
def set_default(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    result = invoke_skill(db, "set_default_provider",
                          {"kind": payload.get("kind", ""), "name": payload.get("name", "")})
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
