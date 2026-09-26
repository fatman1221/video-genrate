"""Provider 运行时配置层。

**要解决的问题**：过去的默认值只存在于两处易失的地方 ——

1. provider 实例在进程启动时构造一次，``__init__`` 里读 ``settings``；
2. ``settings.default_provider_*`` 由 ``.env`` 固定。

于是「在设置页切换引擎」既不能即时生效，重启后也必然丢失。
本模块把数据库里的 ``providers`` 表当作唯一真相来源：

    优先级：数据库配置  >  环境变量（.env）  >  代码默认值

``providers`` 表里与用户相关的是三项，同步时不会被 ``sync_providers_table`` 覆盖：

- ``is_default``  —— 该类能力用哪个引擎
- ``config.model`` —— 该引擎下使用的具体模型名
- ``config.credentials`` —— 云端 Base URL / API Key

provider 实现改为**每次调用时**向本模块查询（见 ``resolve``），
因此设置页改完立刻生效，无需重启。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("studio.providers")

#: kind -> 该类能力的默认 provider 名
_defaults: dict[str, str] = {}
#: "kind:name" -> {"model": str, "credentials": dict, "enabled": bool}
_entries: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


def _key(kind: str, name: str) -> str:
    return f"{kind}:{name}"


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def entry(kind: str, name: str) -> dict[str, Any]:
    """返回某 provider 的运行时配置（不存在则为空配置）。"""
    with _lock:
        return dict(_entries.get(_key(kind, name)) or {})


def credentials_of(kind: str, name: str) -> dict[str, str]:
    return dict(entry(kind, name).get("credentials") or {})


def model_of(kind: str, name: str) -> str:
    return str(entry(kind, name).get("model") or "")


def resolve(kind: str, name: str, key: str, env_value: str = "") -> str:
    """取一项连接配置：数据库里的值优先，其次环境变量，最后空串。"""
    value = credentials_of(kind, name).get(key)
    if value not in (None, ""):
        return str(value)
    return env_value or ""


def is_enabled(kind: str, name: str) -> bool:
    """未登记过的 provider 视为启用（保持注册表行为不变）。"""
    info = entry(kind, name)
    return bool(info.get("enabled", True))


def default_of(kind: str) -> str:
    with _lock:
        return _defaults.get(kind, "")


def snapshot() -> dict[str, Any]:
    with _lock:
        return {"defaults": dict(_defaults), "entries": {k: dict(v) for k, v in _entries.items()}}


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def set_runtime(
    kind: str, name: str, *,
    model: str | None = None,
    credentials: dict[str, str] | None = None,
    merge_credentials: bool = True,
) -> None:
    """更新内存中的运行时配置（调用方负责持久化到数据库）。"""
    with _lock:
        info = _entries.setdefault(_key(kind, name), {"model": "", "credentials": {}, "enabled": True})
        if model is not None:
            info["model"] = model
        if credentials:
            if merge_credentials:
                merged = dict(info.get("credentials") or {})
                # 空字符串表示「清除该项」
                for k, v in credentials.items():
                    if v == "":
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                info["credentials"] = merged
            else:
                info["credentials"] = {k: v for k, v in credentials.items() if v}


def set_default(kind: str, name: str) -> None:
    """设置某类能力的默认引擎，并同步回写 settings（保持新旧读路径一致）。"""
    from ..config import settings

    with _lock:
        _defaults[kind] = name
    field = f"default_provider_{kind}"
    if hasattr(settings, field):
        try:
            setattr(settings, field, name)
        except Exception as exc:  # noqa: BLE001  pydantic 校验失败不该影响主流程
            logger.warning("回写 settings.%s 失败：%s", field, exc)


def clear() -> None:
    with _lock:
        _defaults.clear()
        _entries.clear()


# --------------------------------------------------------------------------- #
# 与数据库 / 注册表对接
# --------------------------------------------------------------------------- #
def load_from_db(db) -> int:
    """读 providers 表 → 填充内存配置。返回读取的行数。"""
    from sqlalchemy import select

    from ..models import ProviderRecord

    rows = list(db.execute(select(ProviderRecord)).scalars())
    with _lock:
        _defaults.clear()
        _entries.clear()
        for row in rows:
            cfg = dict(row.config or {})
            _entries[_key(row.kind, row.name)] = {
                "model": cfg.get("model") or "",
                "credentials": dict(cfg.get("credentials") or {}),
                "enabled": bool(row.enabled),
            }
            if row.is_default:
                _defaults[row.kind] = row.name
    logger.info("Provider 运行时配置已加载：%s 条，默认值 %s", len(rows), dict(_defaults))
    return len(rows)


def apply_to_registry() -> None:
    """把内存里的默认值写进 ProviderRegistry（生成任务时按它选引擎）。"""
    from . import register_all, registry

    register_all()
    for kind, name in snapshot()["defaults"].items():
        try:
            registry.set_default(kind, name)
            set_default(kind, name)   # 同时回写 settings
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复默认 Provider 失败 %s=%s：%s", kind, name, exc)


def reload_from_db(db) -> int:
    """加载 + 应用，供启动流程与设置页写入后调用。"""
    count = load_from_db(db)
    apply_to_registry()
    return count
