"""文件存储抽象层。

数据库只保存元数据；二进制文件由 StorageBackend 负责。
第一阶段使用本地磁盘，接口已按对象存储（S3 / OSS / MinIO）形状设计，
未来替换实现即可，业务层不需要改动。
"""
from __future__ import annotations

import hashlib
import mimetypes
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from .config import settings

#: 资产类型 -> 存储子目录
TYPE_DIRS: dict[str, str] = {
    "CHARACTER": "characters",
    "IMAGE": "images",
    "SCENE": "images",
    "VIDEO": "videos",
    "VOICE": "voices",
    "MUSIC": "music",
    "SFX": "sfx",
    "SUBTITLE": "subtitles",
    "PROJECT_OUTPUT": "outputs",
    "TEMP": "temp",
}


class StorageBackend(ABC):
    name = "base"

    @abstractmethod
    def save_bytes(self, rel_dir: str, filename: str, data: bytes) -> tuple[str, str]:
        """返回 (relative_path, public_url)。"""

    @abstractmethod
    def save_file(self, rel_dir: str, filename: str, src: str | Path) -> tuple[str, str]:
        ...

    @abstractmethod
    def delete(self, rel_path: str) -> bool:
        ...

    @abstractmethod
    def url_for(self, rel_path: str) -> str:
        ...

    @abstractmethod
    def abs_path(self, rel_path: str) -> Path:
        ...


class LocalStorage(StorageBackend):
    name = "local"

    def __init__(self, root: str | Path, public_base_url: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")

    def _prepare(self, rel_dir: str) -> Path:
        target = (self.root / rel_dir).resolve()
        if not str(target).startswith(str(self.root)):
            raise ValueError("非法路径，越出存储根目录")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_bytes(self, rel_dir: str, filename: str, data: bytes) -> tuple[str, str]:
        target = self._prepare(rel_dir) / filename
        target.write_bytes(data)
        rel = str(target.relative_to(self.root))
        return rel, self.url_for(rel)

    def save_file(self, rel_dir: str, filename: str, src: str | Path) -> tuple[str, str]:
        target = self._prepare(rel_dir) / filename
        src_path = Path(src)
        if src_path.resolve() != target.resolve():
            shutil.copyfile(src_path, target)
        rel = str(target.relative_to(self.root))
        return rel, self.url_for(rel)

    def delete(self, rel_path: str) -> bool:
        target = self.abs_path(rel_path)
        if target.exists():
            target.unlink()
            return True
        return False

    def url_for(self, rel_path: str) -> str:
        # 调用方常传 Windows 反斜杠路径（Path 拼出来的），URL 必须是正斜杠，
        # 否则下载文件名、复制出去的链接都会带上 `\`。
        rel = str(rel_path).replace("\\", "/").lstrip("/")
        return f"{self.public_base_url}/media/{rel.replace(' ', '%20')}"

    def abs_path(self, rel_path: str) -> Path:
        return (self.root / rel_path).resolve()


class S3Storage(StorageBackend):
    """对象存储实现骨架（接入 S3 / OSS / MinIO 时补全）。

    保留接口形状，确保业务层无需改动即可切换存储。
    """

    name = "s3"

    def __init__(self) -> None:
        if not settings.s3_bucket:
            raise RuntimeError("未配置 S3_BUCKET，无法启用对象存储")
        raise NotImplementedError(
            "S3Storage 尚未启用。安装 boto3 并在此实现 put_object / get_object 即可。"
        )

    def save_bytes(self, rel_dir: str, filename: str, data: bytes):  # pragma: no cover
        raise NotImplementedError

    def save_file(self, rel_dir: str, filename: str, src):  # pragma: no cover
        raise NotImplementedError

    def delete(self, rel_path: str):  # pragma: no cover
        raise NotImplementedError

    def url_for(self, rel_path: str):  # pragma: no cover
        raise NotImplementedError

    def abs_path(self, rel_path: str):  # pragma: no cover
        raise NotImplementedError


_storage: StorageBackend | None = None


def get_storage() -> StorageBackend:
    global _storage
    if _storage is None:
        if settings.storage_backend == "local":
            _storage = LocalStorage(settings.storage_path, settings.public_base_url)
        else:
            _storage = S3Storage()
    return _storage


#: 不属于任何项目的素材（素材中心里独立生成/保存）统一落在该子目录下，
#: 与真实 project_id 天然隔离，不会被 purge_project 的整目录清理误伤。
LIBRARY_DIR = "_library"


def type_dir(asset_type: str, project_id: str | None) -> str:
    sub = TYPE_DIRS.get(asset_type, "temp")
    return f"{sub}/{project_id or LIBRARY_DIR}"


def checksum_of(path: str | Path, limit: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        remaining = limit
        while remaining > 0:
            chunk = fh.read(min(65536, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def guess_format(path: str | Path) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or "bin"


def guess_mime(path: str | Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"
