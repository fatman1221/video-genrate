"""素材中心端点：列表 / 详情 / 下载 / 删除 / 独立语音生成。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Asset
from ..services import assets as assets_svc
from ..services import serializers as S
from ..skills import invoke_skill

router = APIRouter(prefix="/api/assets", tags=["assets"])


@router.get("")
def list_assets(
    project_id: str | None = None, type: str | None = None,
    group: str | None = Query(None, description="素材中心分类：character / scene / audio / image / video"),
    unassigned: bool = Query(False, description="只看不属于任何项目的独立素材"),
    shot_id: str | None = None, scene_id: str | None = None,
    status: str | None = None, keyword: str | None = None,
    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """列出素材。

    不传 project_id = 跨项目聚合（素材中心的默认视图）；
    传 group 按用户视角分类过滤（一个分类可含多种 AssetType）。
    """
    types = assets_svc.types_of_group(group) if group else None
    rows = assets_svc.list_assets(db, project_id=project_id, asset_type=type,
                                  asset_types=types, unassigned=unassigned,
                                  shot_id=shot_id, scene_id=scene_id, status=status,
                                  keyword=keyword, limit=limit, offset=offset)
    stats = assets_svc.center_stats(db, project_id=project_id, unassigned=unassigned)
    return {"items": [S.asset_brief(a) for a in rows], "total": len(rows), "stats": stats}


# ---- 注意：以下静态路径必须定义在 /{asset_id} 之前，否则会被它吞掉 ---- #
@router.post("/generate/voice")
def generate_voice_asset(payload: dict[str, Any] = Body(default_factory=dict),
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    """素材中心：独立合成一段语音并存入素材库（不需要项目与镜头）。

    走 Skill 层，因此与 Agent 调用共享同一套实现与日志。
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    # 注意：Skill 的 JSON Schema 校验不接受 None，可选参数必须「不传」而不是传 null
    args: dict[str, Any] = {
        "text": text,
        "voice": str(payload.get("voice") or ""),
        "rate": int(payload.get("rate") or 0),
        "name": str(payload.get("name") or ""),
    }
    # 音色 / 情感指令：调音台试音与素材中心都要用（只有 qwen3tts 认识，其它引擎忽略）
    for key in ("speaker", "instruct"):
        if payload.get(key):
            args[key] = str(payload[key])
    if payload.get("provider"):
        args["provider"] = str(payload["provider"])
    if payload.get("project_id"):
        args["project_id"] = str(payload["project_id"])
    result = invoke_skill(db, "generate_standalone_voice", args, actor="human")
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result.get("error") or "语音生成失败")
    return result


@router.get("/{asset_id}")
def get_asset(asset_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="素材不存在")
    return {"asset": S.asset_brief(asset)}


@router.get("/{asset_id}/download")
def download_asset(asset_id: str, db: Session = Depends(get_db)) -> FileResponse:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="素材不存在")
    path = Path(asset.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="素材文件已丢失")
    filename = asset.name if "." in (asset.name or "") else f"{asset.name or asset.id}.{asset.format or 'bin'}"
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@router.delete("/{asset_id}")
def delete_asset(asset_id: str, confirm: bool = Query(False),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    result = invoke_skill(db, "delete_asset", {"asset_id": asset_id, "confirm": confirm})
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
