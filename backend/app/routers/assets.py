"""素材中心端点：列表 / 详情 / 下载 / 删除。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
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
    project_id: str | None = None, type: str | None = None, shot_id: str | None = None,
    scene_id: str | None = None, status: str | None = None, keyword: str | None = None,
    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = assets_svc.list_assets(db, project_id=project_id, asset_type=type, shot_id=shot_id,
                                  scene_id=scene_id, status=status, keyword=keyword,
                                  limit=limit, offset=offset)
    stats = assets_svc.asset_stats(db, project_id) if project_id else {}
    return {"items": [S.asset_brief(a) for a in rows], "total": len(rows), "stats": stats}


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
