"""系统设置服务：模型引擎选择 / 模型名 / 云端凭证。

对用户可见的是三类「模型」，每类对应一个 Provider kind：

===========  ============  ==========================================
设置项        Provider kind 可选引擎
===========  ============  ==========================================
生图模型      image         local / comfyui / cloud
图生视频模型  video         local / comfyui / cloud
语音生成模型  tts           local / cloud
===========  ============  ==========================================

**唯一真相来源**：``providers`` 表的 ``is_default`` / ``config.model`` /
``config.credentials``。用户改完立刻写库并刷新内存（``providers.runtime``），
因此即时生效、重启保留。

``CREDENTIAL_SCHEMA`` 描述「某个引擎需要填哪些连接参数」，前端据此渲染表单，
不在前端写死字段 —— 以后新增引擎只需改这里。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.constants import PUBLIC_MASK
from ..models import ProviderRecord
from ..providers import register_all, registry
from ..providers import runtime as provider_runtime

logger = logging.getLogger("studio.settings")

#: 用户可见的三类模型设置（按界面顺序）
MANAGED_KINDS: tuple[str, ...] = ("image", "video", "tts")

KIND_LABELS: dict[str, str] = {
    "image": "生图模型",
    "video": "图生视频模型",
    "tts": "语音生成模型",
}

KIND_HINTS: dict[str, str] = {
    "image": "分镜关键帧、人物参考图、场景图的生成引擎",
    "video": "把关键帧转成视频片段的引擎（图生视频）",
    "tts": "旁白与独立语音素材的合成引擎",
}

#: 各 kind 下每个引擎需要填写的连接参数。
#: secret=True 的字段在接口返回时会被掩码，前端只显示后 4 位。
CREDENTIAL_SCHEMA: dict[str, dict[str, list[dict[str, Any]]]] = {
    "image": {
        "local": [],
        "comfyui": [
            {"key": "base_url", "label": "ComfyUI 地址", "secret": False,
             "placeholder": "http://127.0.0.1:8188"},
            {"key": "api_key", "label": "API Key（可选）", "secret": True, "placeholder": ""},
        ],
        "cloud": [
            {"key": "base_url", "label": "云端接口地址", "secret": False,
             "placeholder": "https://api.example.com/v1/images/generations"},
            {"key": "api_key", "label": "API Key", "secret": True, "placeholder": "sk-..."},
        ],
    },
    "video": {
        "local": [],
        "comfyui": [
            {"key": "base_url", "label": "ComfyUI 地址", "secret": False,
             "placeholder": "http://127.0.0.1:8188"},
            {"key": "api_key", "label": "API Key（可选）", "secret": True, "placeholder": ""},
        ],
        "cloud": [
            {"key": "base_url", "label": "云端接口地址", "secret": False,
             "placeholder": "https://api.example.com/v1/videos"},
            {"key": "api_key", "label": "API Key", "secret": True, "placeholder": "sk-..."},
        ],
    },
    "tts": {
        "local": [],
        "cloud": [
            {"key": "base_url", "label": "云端接口地址", "secret": False,
             "placeholder": "https://api.example.com/v1/audio/speech"},
            {"key": "api_key", "label": "API Key", "secret": True, "placeholder": "sk-..."},
        ],
    },
}


def _mask(value: str) -> str:
    """凭证掩码：只暴露后 4 位，避免设置页把密钥原样吐回浏览器。"""
    if not value:
        return ""
    if len(value) <= 4:
        return PUBLIC_MASK * len(value)
    return f"{PUBLIC_MASK * 4}{value[-4:]}"


def _row(db: Session, kind: str, name: str) -> ProviderRecord | None:
    return db.get(ProviderRecord, f"{kind}:{name}")


def _ensure_row(db: Session, kind: str, name: str) -> ProviderRecord:
    row = _row(db, kind, name)
    if row is None:
        register_all()
        info = next((i for i in registry.list() if i["kind"] == kind and i["name"] == name), None)
        if info is None:
            raise ValueError(f"未注册的 Provider：{kind}:{name}")
        row = ProviderRecord(
            id=f"{kind}:{name}", kind=kind, name=name,
            display_name=info.get("display_name") or name,
            requires_api_key=info.get("requires_api_key", False),
            capabilities=info.get("capabilities", []),
            config={"functional": info.get("functional", True), "doc": info.get("doc", ""),
                    "model": "", "credentials": {}},
        )
        db.add(row)
        db.flush()
    return row


def provider_view(db: Session, kind: str, name: str) -> dict[str, Any]:
    """单个可选引擎的展示信息（含掩码后的凭证状态）。"""
    register_all()
    info = next((i for i in registry.list() if i["kind"] == kind and i["name"] == name), None)
    row = _row(db, kind, name)
    cfg = dict((row.config if row else {}) or {})
    creds = dict(cfg.get("credentials") or {})
    schema = CREDENTIAL_SCHEMA.get(kind, {}).get(name, [])

    fields: list[dict[str, Any]] = []
    for field in schema:
        raw = str(creds.get(field["key"]) or "")
        fields.append({
            **field,
            "value": _mask(raw) if field.get("secret") else raw,
            "configured": bool(raw),
        })

    return {
        "kind": kind,
        "name": name,
        "display_name": info.get("display_name") if info else name,
        "doc": (info or {}).get("doc", "") or cfg.get("doc", ""),
        "functional": bool(info.get("functional")) if info else bool(cfg.get("functional")),
        "requires_api_key": bool(info.get("requires_api_key")) if info else False,
        "capabilities": (info or {}).get("capabilities", []),
        "is_default": bool(row.is_default) if row else False,
        "model": cfg.get("model") or "",
        "fields": fields,
        # 该引擎是否「可立即使用」：需要凭证的必须都填了
        "ready": _is_ready(info, creds, schema),
    }


def _is_ready(info: dict[str, Any] | None, creds: dict[str, str],
              schema: list[dict[str, Any]]) -> bool:
    if info is not None and not info.get("functional"):
        # 需要凭证但没配齐 —— functional 已反映这个判断
        if not any(s["key"] == "base_url" for s in schema):
            return False
    if not schema:
        return True
    return all(bool(creds.get(s["key"])) for s in schema if s["key"] == "base_url")


def settings_overview(db: Session, kinds: tuple[str, ...] = MANAGED_KINDS) -> dict[str, Any]:
    """三类模型设置的总览，前端设置页直接用这一个接口渲染。"""
    register_all()
    groups: list[dict[str, Any]] = []
    for kind in kinds:
        names = [i["name"] for i in registry.list(kind)]
        options = [provider_view(db, kind, n) for n in names]
        current = provider_runtime.default_of(kind) or next(
            (o["name"] for o in options if o["is_default"]),
            options[0]["name"] if options else "",
        )
        groups.append({
            "kind": kind,
            "label": KIND_LABELS.get(kind, kind),
            "hint": KIND_HINTS.get(kind, ""),
            "current": current,
            "current_model": provider_runtime.model_of(kind, current) if current else "",
            "options": options,
        })
    return {"groups": groups}


def update_setting(
    db: Session, kind: str, name: str, *,
    model: str | None = None,
    credentials: dict[str, str] | None = None,
    make_default: bool = True,
    actor: str = "agent",
) -> dict[str, Any]:
    """保存某类模型的设置：选引擎 + 模型名 + 凭证。写库后立即刷新内存。"""
    if kind not in MANAGED_KINDS:
        raise ValueError(f"不支持的设置类别：{kind}（可选：{', '.join(MANAGED_KINDS)}）")
    register_all()
    if not registry.has(kind, name):
        available = ", ".join(i["name"] for i in registry.list(kind))
        raise ValueError(f"未知引擎：{name}（{kind} 可选：{available}）")

    row = _ensure_row(db, kind, name)
    cfg = dict(row.config or {})
    if model is not None:
        cfg["model"] = model
    if credentials:
        merged = dict(cfg.get("credentials") or {})
        for key, value in credentials.items():
            if value == "":
                merged.pop(key, None)     # 传空字符串 = 清除该项
            else:
                merged[key] = value
        cfg["credentials"] = merged
    row.config = cfg

    if make_default:
        others = db.execute(
            select(ProviderRecord).where(ProviderRecord.kind == kind,
                                        ProviderRecord.id != row.id)
        ).scalars()
        for other in others:
            other.is_default = False
        row.is_default = True

    db.commit()

    # 刷新内存：默认引擎 + 模型名 + 凭证
    provider_runtime.set_runtime(kind, name, model=model, credentials=credentials)
    if make_default:
        provider_runtime.set_default(kind, name)
        provider_runtime.apply_to_registry()

    logger.info("设置更新 %s → %s（model=%r，默认=%s，操作者=%s）",
                kind, name, model or "-", make_default, actor)
    return provider_view(db, kind, name)


def provider_for_kind(db: Session, kind: str) -> str:
    """当前生效的引擎名（供任务侧查询）。"""
    return provider_runtime.default_of(kind) or registry.default_name(kind)
