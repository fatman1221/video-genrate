"""Provider 包：统一注册入口。"""
from __future__ import annotations

from .base import (  # noqa: F401
    BrowserProvider, EnhanceProvider, GenerationResult, ImageProvider, MusicProvider,
    ProcessingProvider, Provider, ProviderError, ProviderRegistry, SFXProvider,
    SubtitleProvider, TTSProvider, VideoProvider, registry,
)
from . import (  # noqa: F401
    audio_providers, browser_providers, enhance_providers, image_providers,
    processing_providers, subtitle_providers, video_providers,
)
from . import runtime  # noqa: F401  运行时配置层（默认引擎 / 模型名 / 云端凭证）
from .runtime import (  # noqa: F401
    apply_to_registry, credentials_of, load_from_db, model_of, reload_from_db, resolve,
    set_runtime, snapshot,
)

_MODULES = (
    image_providers,
    video_providers,
    audio_providers,
    subtitle_providers,
    enhance_providers,
    processing_providers,
    browser_providers,
)

_registered = False


def register_all() -> ProviderRegistry:
    global _registered
    if not _registered:
        for module in _MODULES:
            module.register()
        _registered = True
    return registry


def sync_providers_table(db) -> int:
    """把注册表中的 Provider 同步进数据库，便于 UI 展示与运行时开关。

    ⚠️ 同步只负责「代码侧派生字段」（名称、能力、可用性），
    **绝不覆盖用户在设置页写的三项**：``is_default``（默认引擎）、
    ``config.model``（模型名）、``config.credentials``（云端凭证）。
    否则每次重启都会把用户的配置冲回代码默认值。
    """
    from ..models import ProviderRecord

    register_all()
    count = 0
    for info in registry.list():
        pid = f"{info['kind']}:{info['name']}"
        row = db.get(ProviderRecord, pid)
        derived = {
            "kind": info["kind"],
            "name": info["name"],
            "display_name": info.get("display_name") or info["name"],
            "requires_api_key": info.get("requires_api_key", False),
            "capabilities": info.get("capabilities", []),
        }
        meta = {"functional": info.get("functional", True), "doc": info.get("doc", "")}
        if row is None:
            row = ProviderRecord(
                id=pid, enabled=True, is_default=info.get("is_default", False),
                config={**meta, "model": "", "credentials": {}}, **derived,
            )
            db.add(row)
        else:
            for key, value in derived.items():
                setattr(row, key, value)
            cfg = dict(row.config or {})
            keep = {k: cfg[k] for k in ("model", "credentials") if k in cfg}
            row.config = {**meta, **keep}
            # is_default 保持不动：它是用户的选择
        count += 1
    db.commit()
    return count


def provider_catalog() -> list[dict]:
    """返回全部 Provider 清单（含健康状态）。"""
    register_all()
    return registry.list()
