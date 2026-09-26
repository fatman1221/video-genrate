"""Asset Center 服务。

- 所有生成结果统一经由 ingest_result 落地：写文件 + 建记录 + 保存完整元数据
- 记录关联：project_id / scene_id / shot_id / character_id
- 支持查看 / 删除 / 下载 / 重新生成 / 查看生成参数与来源
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.constants import AssetStatus, AssetType
from ..models import Asset
from ..providers.base import GenerationResult
from ..storage import (
    checksum_of, get_storage, guess_format, type_dir,
)


_EXT_BY_FORMAT = {
    "png": ".png", "jpg": ".jpg", "jpeg": ".jpeg", "webp": ".webp",
    "mp4": ".mp4", "mov": ".mov", "webm": ".webm",
    "mp3": ".mp3", "wav": ".wav", "aac": ".aac", "m4a": ".m4a",
    "srt": ".srt", "ass": ".ass", "vtt": ".vtt",
}


def ingest_result(
    db: Session,
    *,
    project_id: str,
    result: GenerationResult,
    asset_type: str,
    name: str = "",
    scene_id: str | None = None,
    shot_id: str | None = None,
    character_id: str | None = None,
    task_id: str | None = None,
    source: str = "generated",
    parent_asset_id: str | None = None,
    move: bool = False,
    extra: dict[str, Any] | None = None,
) -> Asset:
    """把 Provider 产物落到存储并登记为 Asset。"""
    src = Path(result.file_path)
    if not src.exists():
        raise FileNotFoundError(f"生成结果文件不存在: {result.file_path}")

    storage = get_storage()
    ext = _EXT_BY_FORMAT.get(result.format.lower()) or src.suffix or ".bin"
    filename = f"{asset_type.lower()}_{uuid.uuid4().hex[:10]}{ext}"
    rel_dir = type_dir(asset_type, project_id)
    if move:
        rel_path, url = storage.save_bytes(rel_dir, filename, src.read_bytes())
        try:
            src.unlink()
        except OSError:
            pass
    else:
        rel_path, url = storage.save_file(rel_dir, filename, src)

    abs_path = storage.abs_path(rel_path)
    fmt = result.format or guess_format(abs_path)
    asset = Asset(
        project_id=project_id,
        scene_id=scene_id,
        shot_id=shot_id,
        character_id=character_id,
        type=asset_type,
        name=name or filename,
        file_path=str(abs_path),
        url=url,
        storage_backend=storage.name,
        status=AssetStatus.READY,
        prompt=result.prompt or "",
        negative_prompt=result.negative_prompt or "",
        model=result.model or "",
        provider=result.provider or "",
        workflow=result.workflow or "",
        parameters=result.parameters or {},
        width=result.width or 0,
        height=result.height or 0,
        duration=result.duration or 0.0,
        fps=result.fps or 0.0,
        format=fmt,
        size_bytes=result.size_bytes or abs_path.stat().st_size,
        checksum=checksum_of(abs_path),
        source=source,
        parent_asset_id=parent_asset_id,
        task_id=task_id,
        extra={**(result.extra or {}), **(extra or {})},
    )
    db.add(asset)
    db.flush()
    return asset


def get_asset(db: Session, asset_id: str) -> Asset | None:
    return db.get(Asset, asset_id)


def list_assets(
    db: Session, *, project_id: str | None = None, asset_type: str | None = None,
    shot_id: str | None = None, scene_id: str | None = None,
    status: str | None = None, keyword: str | None = None,
    limit: int = 200, offset: int = 0,
) -> list[Asset]:
    stmt = select(Asset).order_by(Asset.created_at.desc())
    if project_id:
        stmt = stmt.where(Asset.project_id == project_id)
    if asset_type:
        stmt = stmt.where(Asset.type == asset_type)
    if shot_id:
        stmt = stmt.where(Asset.shot_id == shot_id)
    if scene_id:
        stmt = stmt.where(Asset.scene_id == scene_id)
    if status:
        stmt = stmt.where(Asset.status == status)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(Asset.name.ilike(like) | Asset.prompt.ilike(like))
    return list(db.execute(stmt.offset(offset).limit(limit)).scalars())


def asset_stats(db: Session, project_id: str) -> dict[str, Any]:
    rows = db.execute(
        select(Asset.type, func.count(Asset.id), func.coalesce(func.sum(Asset.size_bytes), 0))
        .where(Asset.project_id == project_id)
        .group_by(Asset.type)
    ).all()
    by_type = {r[0]: {"count": r[1], "size_bytes": int(r[2] or 0)} for r in rows}
    total = sum(v["count"] for v in by_type.values())
    total_size = sum(v["size_bytes"] for v in by_type.values())
    return {"total": total, "total_size_bytes": total_size, "by_type": by_type}


def delete_asset(db: Session, asset_id: str, *, remove_file: bool = True) -> bool:
    asset = db.get(Asset, asset_id)
    if asset is None:
        return False
    if remove_file:
        try:
            storage = get_storage()
            rel = Path(asset.file_path).relative_to(storage.root) if storage.name == "local" else None
            if rel is not None:
                storage.delete(str(rel))
            else:
                Path(asset.file_path).unlink(missing_ok=True)
        except (ValueError, OSError):
            Path(asset.file_path).unlink(missing_ok=True)
    db.delete(asset)
    db.flush()
    return True


def latest_primary_videos(db: Session, project_id: str) -> list[Asset]:
    """按 Shot 顺序返回主视频资产（未增强优先）。"""
    stmt = (
        select(Asset)
        .where(Asset.project_id == project_id, Asset.type == AssetType.VIDEO)
        .order_by(Asset.created_at.asc())
    )
    return list(db.execute(stmt).scalars())
