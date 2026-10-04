"""内容端点：脚本 / 分镜 / 场景 / 镜头 / 角色。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.constants import AssetType
from ..database import get_db
from ..models import Asset, Character, Project, Scene, Script, ScriptSection, Shot, Storyboard
from ..services import characters as characters_svc
from ..services import references as references_svc
from ..services import script_sections as sections_svc
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


# --------------------------------------------------------------------------- #
# 参考素材（外部上传的创作依据：文本 / 图片 / 音视频）
# --------------------------------------------------------------------------- #
def _require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.get("/projects/{project_id}/references")
def list_references(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """列出项目的参考素材（文本 / 图片 / 视频 / 音频）。"""
    _require_project(db, project_id)
    rows = references_svc.list_references(db, project_id)
    return {"items": [S.asset_brief(a) for a in rows], "total": len(rows)}


@router.post("/projects/{project_id}/references")
async def upload_reference(
    project_id: str,
    file: UploadFile = File(..., description="参考文件：大纲/小说/文案、参考图、样片或小样"),
    kind: str = Form("", description="显式指定子类：text/image/video/audio，留空则自动判断"),
    note: str = Form("", description="这份素材的用途说明，会进入生成上下文"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """上传一份参考素材。只作创作依据，不参与成片。"""
    project = _require_project(db, project_id)
    data = await file.read()
    try:
        asset = references_svc.save_reference(
            db, project,
            filename=file.filename or "reference", data=data,
            content_type=file.content_type or "", kind=kind, note=note, actor="human",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"ok": True, "asset": S.asset_brief(asset)}


@router.delete("/projects/{project_id}/references/{asset_id}")
def delete_reference(project_id: str, asset_id: str,
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    """删除参考素材（同时清理磁盘文件）。"""
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project_id or asset.type != AssetType.REFERENCE:
        raise HTTPException(status_code=404, detail="参考素材不存在")
    removed = references_svc.delete_reference(db, asset, actor="human")
    db.commit()
    return {"ok": True, "file_removed": removed}


# --------------------------------------------------------------------------- #
# 脚本分段（按幕写作，逐段生成、上下文连贯）
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/script-sections")
def list_script_sections(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """列出脚本分段。写操作（新增/改写/删除/重规划）统一走 Skill 层。"""
    project = _require_project(db, project_id)
    rows = sections_svc.list_sections(db, project_id)
    script = sections_svc.latest_script(db, project_id)
    return {
        "items": [S.serialize_script_section(s) for s in rows],
        "total": len(rows),
        "script": S.serialize_script(script) if script else None,
        "written_count": sum(1 for s in rows if (s.content or "").strip()),
    }


@router.get("/projects/{project_id}/script-sections/plan-prompt")
def build_plan_prompt(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """打包「请帮我规划分幕结构」的提示词（不写正文，只切结构）。"""
    project = _require_project(db, project_id)
    return sections_svc.build_plan_prompt(db, project)


@router.get("/projects/{project_id}/script-sections/{section_id}/prompt")
def build_section_prompt(project_id: str, section_id: str,
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    """打包「写这一幕」所需的完整上下文，交给 Agent 生成正文。

    返回的 ``prompt`` 可直接复制给 Agent；写回用 Skill ``upsert_script_section``。
    """
    project = _require_project(db, project_id)
    section = sections_svc.get_section(db, section_id)
    if section is None or section.project_id != project_id:
        raise HTTPException(status_code=404, detail="脚本分段不存在")
    try:
        return sections_svc.build_generation_prompt(db, project, section)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
