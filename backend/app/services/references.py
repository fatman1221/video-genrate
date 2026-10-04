"""项目参考素材：外部上传的创作依据（文本 / 图片 / 音视频）。

与「素材」（Asset 里的 IMAGE / VIDEO / VOICE 等）刻意区分：
那些是参与成片的产物，而参考素材不进成片、不参与质检，只用于喂给
脚本 / 分镜 /Prompt 的生成上下文。

因此参考素材统一 ``type=REFERENCE``，用 ``extra.kind`` 区分子类
（text / image / video / audio），落在 ``storage/references/{project_id}/``，
且会被 ``asset_digest()`` 汇总成一段可直接塞进提示词的文字说明。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.constants import AssetStatus, AssetType
from ..models import Asset, Project
from ..storage import checksum_of, get_storage, guess_format, type_dir
from . import agent_log

#: 单个参考文件大小上限 —— 参考素材是给人看的依据，不该往里塞巨型资产
MAX_REFERENCE_BYTES = 50 * 1024 * 1024
#: 文本类参考抽取进 ``extra.text_excerpt`` 的字符上限（超出部分只留在文件里）
TEXT_EXCERPT_CHARS = 20000
#: 文本参考直接塞进提示词时的单文件字符上限（防止把上下文撑爆）
TEXT_PROMPT_CHARS = 4000


_KIND_BY_SUFFIX: dict[str, str] = {
    "txt": "text", "md": "text", "markdown": "text", "csv": "text", "tsv": "text",
    "json": "text", "srt": "text", "vtt": "text", "log": "text", "yml": "text", "yaml": "text",
    "png": "image", "jpg": "image", "jpeg": "image", "webp": "image", "gif": "image",
    "bmp": "image", "tif": "image", "tiff": "image",
    "mp4": "video", "mov": "video", "mkv": "video", "webm": "video", "avi": "video", "m4v": "video",
    "mp3": "audio", "wav": "audio", "m4a": "audio", "aac": "audio", "flac": "audio",
    "ogg": "audio", "opus": "audio",
}


def classify_kind(filename: str, content_type: str = "", kind: str = "") -> str:
    """判断参考素材的子类。显式传入的 ``kind`` 优先，其次 MIME，最后扩展名。"""
    if kind in AssetType.REFERENCE_KINDS:
        return kind
    ctype = (content_type or "").lower()
    for prefix, mapped in (("text/", "text"), ("image/", "image"),
                           ("video/", "video"), ("audio/", "audio")):
        if ctype.startswith(prefix):
            return mapped
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    return _KIND_BY_SUFFIX.get(suffix, "text")


def _safe_filename(name: str) -> str:
    """只保留基名并剔除路径分隔符，避免上传时穿越目录。"""
    base = Path(name or "").name or "reference"
    cleaned = "".join(ch for ch in base if ch not in '\\/:*?"<>|').strip()
    return cleaned or "reference"


def _read_text_head(path: Path, limit: int = TEXT_EXCERPT_CHARS) -> str:
    """读取文本文件开头若干字符。

    上传的文案可能是 UTF-8，也可能是 Windows 记事本存出来的 GBK，
    所以按常见编码依次尝试，全都失败就返回空串（不影响上传本身）。
    """
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, errors="strict") as fh:
                return fh.read(limit)
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return ""
    # 最后兜底：宽松解码，保证总能拿到点东西
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _probe_image(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:  # noqa: BLE001  探测失败不影响上传
        return 0, 0


def _probe_media(path: Path) -> dict[str, Any]:
    """音视频探测（时长 / 尺寸 / 采样率）。失败一律返回空值，不阻断上传。"""
    try:
        from ..providers.local_engine import ffprobe

        return ffprobe(path) or {}
    except Exception:  # noqa: BLE001
        return {}


def save_reference(
    db: Session, project: Project, *, filename: str, data: bytes,
    content_type: str = "", kind: str = "", note: str = "", actor: str = "human",
) -> Asset:
    """保存一份上传的参考素材并登记为 Asset(type=REFERENCE)。"""
    if not data:
        raise ValueError("上传内容为空")
    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError(
            f"文件过大（{len(data) / 1048576:.1f}MB），参考素材上限 {MAX_REFERENCE_BYTES // 1048576}MB"
        )

    resolved_kind = classify_kind(filename, content_type, kind)
    safe_name = _safe_filename(filename)

    storage = get_storage()
    rel_path, url = storage.save_bytes(type_dir(AssetType.REFERENCE, project.id), safe_name, data)
    abs_path = storage.abs_path(rel_path)

    extra: dict[str, Any] = {
        "kind": resolved_kind,
        "note": (note or "").strip(),
        "original_name": filename,
    }
    width = height = 0
    duration = 0.0
    fps = 0.0

    if resolved_kind == "text":
        excerpt = _read_text_head(abs_path)
        extra["text_excerpt"] = excerpt
        extra["truncated"] = len(excerpt) >= TEXT_EXCERPT_CHARS
    elif resolved_kind == "image":
        width, height = _probe_image(abs_path)
    else:  # video / audio
        info = _probe_media(abs_path)
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        duration = float(info.get("duration") or 0.0)
        fps = float(info.get("fps") or 0.0)

    asset = Asset(
        project_id=project.id,
        type=AssetType.REFERENCE,
        name=Path(safe_name).stem or safe_name,
        file_path=rel_path,
        url=url,
        storage_backend=storage.name,
        status=AssetStatus.READY,
        source="uploaded",
        format=guess_format(abs_path),
        size_bytes=len(data),
        checksum=checksum_of(abs_path),
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        extra=extra,
    )
    db.add(asset)
    db.flush()
    agent_log.log_event(
        db, project_id=project.id, event="reference.uploaded", actor=actor,
        message=f"上传参考素材「{asset.name}」（{resolved_kind}）",
        detail={"asset_id": asset.id, "kind": resolved_kind, "size_bytes": len(data)},
    )
    return asset


def list_references(db: Session, project_id: str) -> list[Asset]:
    """按类型（文本 → 图片 → 音视频）与上传时间列出参考素材。"""
    rows = db.execute(
        select(Asset).where(
            Asset.project_id == project_id,
            Asset.type == AssetType.REFERENCE,
        ).order_by(Asset.created_at.asc())
    ).scalars().all()
    order = {"text": 0, "image": 1, "video": 2, "audio": 3}
    return sorted(rows, key=lambda a: order.get((a.extra or {}).get("kind", "text"), 9))


def delete_reference(db: Session, asset: Asset, *, actor: str = "human") -> bool:
    """删除参考素材记录并尽力删除磁盘文件。"""
    storage = get_storage()
    rel_path = asset.file_path or ""
    project_id = asset.project_id
    name = asset.name
    db.delete(asset)
    db.flush()
    removed = False
    if rel_path:
        try:
            removed = storage.delete(rel_path)
        except Exception:  # noqa: BLE001  记录已删，文件清理失败不该让请求失败
            removed = False
    if project_id:
        agent_log.log_event(
            db, project_id=project_id, event="reference.deleted", actor=actor,
            message=f"删除参考素材「{name}」", detail={"file_removed": removed},
        )
    return removed


def asset_digest(references: list[Asset], *, per_text_chars: int = TEXT_PROMPT_CHARS) -> str:
    """把参考素材汇总成一段可直接放进提示词的说明。

    文本类会带上正文节选（截断到 ``per_text_chars``），图片与音视频只列名称、
    时长/尺寸和用户备注 —— 因为文字生成走的是纯文本模型，多模态内容只能当
    「已知存在这些参考」的说明来用。
    """
    if not references:
        return ""
    lines: list[str] = ["【参考素材】"]
    for idx, asset in enumerate(references, start=1):
        extra = asset.extra or {}
        kind = extra.get("kind", "text")
        note = (extra.get("note") or "").strip()
        head = f"{idx}. [{kind}] {asset.name}"
        if asset.duration:
            head += f"（{asset.duration:.1f}s）"
        elif asset.width and asset.height:
            head += f"（{asset.width}×{asset.height}）"
        if note:
            head += f" —— 用途：{note}"
        lines.append(head)
        if kind == "text":
            excerpt = (extra.get("text_excerpt") or "").strip()
            if excerpt:
                if len(excerpt) > per_text_chars:
                    excerpt = excerpt[:per_text_chars] + "…（已截断）"
                lines.extend(f"    {ln}" for ln in excerpt.splitlines() if ln.strip())
    return "\n".join(lines)
