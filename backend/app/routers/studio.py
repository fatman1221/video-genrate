"""视觉设定 / 连续性 / Prompt / 生成计划的**只读**端点。

写操作一律走 Skill 层（与现有约定一致）；这里只提供服务端渲染与 Agent 快速查看所需的读接口。

端点清单：

- ``GET /api/projects/{id}/visual-bible``      视觉设定全量 + 风格列表
- ``GET /api/projects/{id}/continuity-locks``  锁与增量
- ``GET /api/projects/{id}/prompts``           Prompt 列表（含 stale 标记）
- ``GET /api/prompts/{id}``                    Prompt 详情 + 全部版本
- ``GET /api/prompts/{id}/provenance``         血缘反查（从 Prompt 向上）
- ``GET /api/assets/{id}/provenance``          血缘反查（从素材向上）
- ``GET /api/projects/{id}/generation-plans``  计划列表
- ``GET /api/generation-plans/{id}``           计划详情 + 条目
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Asset, ContinuityDelta, ContinuityLock, GenerationPlan, Project, Prompt, PromptVersion
from ..services import continuity as continuity_svc
from ..services import generation_plans as plans_svc
from ..services import prompt_compiler as compiler_svc
from ..services import provenance as provenance_svc
from ..services import visual_bible as vb_svc

router = APIRouter(prefix="/api", tags=["studio"])


def _require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    return project


# --------------------------------------------------------------------------- #
# 视觉设定
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/visual-bible")
def get_visual_bible(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """视觉设定全量：总纲 + 全部风格 + 当前生效风格 + 词表。"""
    project = _require_project(db, project_id)
    bible = vb_svc.get_or_create_bible(db, project.id)
    styles = list(db.execute(
        select(vb_svc.VisualStyle).where(vb_svc.VisualStyle.project_id == project.id)
    ).scalars().all())
    current = vb_svc.current_style(db, project.id)

    characters = db.execute(
        select(vb_svc.Character).where(vb_svc.Character.project_id == project.id)
    ).scalars().all()
    locations = db.execute(
        select(vb_svc.Location).where(vb_svc.Location.project_id == project.id)
    ).scalars().all()
    props = db.execute(
        select(vb_svc.Prop).where(vb_svc.Prop.project_id == project.id)
    ).scalars().all()

    return {
        "bible": vb_svc.bible_brief(bible, styles=styles),
        "current_style": vb_svc.style_brief(current) if current else None,
        "form_cards": list(vb_svc.FORM_CARDS),
        "reference_roles": list(vb_svc.REFERENCE_ROLES),
        "counts": {
            "styles": len(styles), "characters": len(characters),
            "locations": len(locations), "props": len(props),
        },
        "characters": [
            {
                "id": c.id, "name": c.name, "code": c.code, "role": c.role,
                "reference_asset_id": c.reference_asset_id,
                "identity_anchors": c.identity_anchors or [],
                "not_identity": c.not_identity or [],
                "voice_direction": c.voice_direction or {},
                "looks": [
                    vb_svc.look_brief(look) for look in db.execute(
                        select(vb_svc.CharacterLook).where(vb_svc.CharacterLook.character_id == c.id)
                    ).scalars().all()
                ],
            }
            for c in characters
        ],
        "locations": [
            {
                **vb_svc.location_brief(loc),
                "views": [
                    vb_svc.location_view_brief(view) for view in db.execute(
                        select(vb_svc.LocationView).where(vb_svc.LocationView.location_id == loc.id)
                    ).scalars().all()
                ],
            }
            for loc in locations
        ],
        "props": [
            {
                **vb_svc.prop_brief(prop),
                "states": [
                    vb_svc.prop_state_brief(state) for state in db.execute(
                        select(vb_svc.PropState).where(vb_svc.PropState.prop_id == prop.id)
                    ).scalars().all()
                ],
            }
            for prop in props
        ],
    }


# --------------------------------------------------------------------------- #
# 连续性
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/continuity-locks")
def list_continuity_locks(project_id: str, only_active: bool = True,
                          db: Session = Depends(get_db)) -> dict[str, Any]:
    """连续性锁与增量（两者是**不同**机制，分列返回）。"""
    _require_project(db, project_id)
    locks = (
        continuity_svc.list_project_locks(db, project_id, only_active=only_active)
        if only_active else
        list(db.execute(
            select(ContinuityLock).where(ContinuityLock.project_id == project_id)
            .order_by(ContinuityLock.created_at.asc())
        ).scalars().all())
    )
    deltas = list(db.execute(
        select(ContinuityDelta).where(ContinuityDelta.project_id == project_id)
        .order_by(ContinuityDelta.created_at.asc())
    ).scalars().all())
    return {
        "lock_count": len(locks),
        "locks": [continuity_svc.lock_brief(lock) for lock in locks],
        "delta_count": len(deltas),
        "deltas": [continuity_svc.delta_brief(delta) for delta in deltas],
        "hint": "一集里的锁通常是个位数；把每条识别锚点都上锁会挤掉本镜真正要执行的动作",
    }


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/prompts")
def list_prompts(project_id: str, prompt_type: str = Query("", description="image/video/voice/music"),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    """Prompt 列表（含版本数与 stale 标记），用于判断「哪些镜头需要重出」。"""
    _require_project(db, project_id)
    query = select(Prompt).where(Prompt.project_id == project_id)
    if prompt_type:
        query = query.where(Prompt.type == prompt_type)
    prompts = list(db.execute(query.order_by(Prompt.created_at.asc())).scalars().all())

    items: list[dict[str, Any]] = []
    stale_count = 0
    for prompt in prompts:
        staleness = compiler_svc.check_prompt_staleness(db, prompt)
        if staleness["stale"]:
            stale_count += 1
        items.append({
            **compiler_svc.prompt_brief(prompt),
            "stale": staleness["stale"],
            "stale_reasons": staleness["reasons"],
        })
    return {"count": len(items), "stale_count": stale_count, "prompts": items}


@router.get("/prompts/{prompt_id}")
def get_prompt(prompt_id: str, include_compiled: bool = True,
               db: Session = Depends(get_db)) -> dict[str, Any]:
    """Prompt 详情 + 全部历史版本 + stale 判定。"""
    prompt = db.get(Prompt, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt 不存在：{prompt_id}")
    versions = list(db.execute(
        select(PromptVersion).where(PromptVersion.prompt_id == prompt_id)
        .order_by(PromptVersion.version.asc())
    ).scalars().all())
    return {
        "prompt": compiler_svc.prompt_brief(
            prompt, versions=versions, include_prompt=include_compiled
        ),
        "staleness": compiler_svc.check_prompt_staleness(db, prompt),
    }


@router.get("/prompts/{prompt_id}/provenance")
def get_prompt_provenance(prompt_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """血缘反查：从 Prompt 向上。"""
    prompt = db.get(Prompt, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt 不存在：{prompt_id}")
    return provenance_svc.prompt_provenance(db, prompt)


@router.get("/assets/{asset_id}/provenance")
def get_asset_provenance(asset_id: str, depth: int = 1, db: Session = Depends(get_db)) -> dict[str, Any]:
    """血缘反查：从素材向上还原完整生成链路。"""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"素材不存在：{asset_id}")
    return provenance_svc.asset_provenance(db, asset, depth=max(0, min(depth, 2)))


# --------------------------------------------------------------------------- #
# 生成计划
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/generation-plans")
def list_generation_plans(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """生成计划列表。"""
    _require_project(db, project_id)
    plans = plans_svc.list_plans(db, project_id)
    return {"count": len(plans), "plans": [plans_svc.plan_brief(p) for p in plans]}


@router.get("/generation-plans/{plan_id}")
def get_generation_plan(plan_id: str, include_items: bool = True,
                        db: Session = Depends(get_db)) -> dict[str, Any]:
    """计划详情 + 条目。``confirm_phrase`` 是可直接回传给确认 Skill 的短语。"""
    plan = db.get(GenerationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"生成计划不存在：{plan_id}")
    items = plans_svc.list_plan_items(db, plan.id) if include_items else None
    return {"plan": plans_svc.plan_brief(plan, items=items)}
