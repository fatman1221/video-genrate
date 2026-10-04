"""系统设置端点：模型引擎选择 / 模型名 / 云端连接凭证。

面向「生图模型 / 图生视频模型 / 语音生成模型」三类设置，
前端设置页只用 ``GET /api/settings/providers`` 一个接口即可完成渲染，
引擎需要填哪些字段由后端 ``CREDENTIAL_SCHEMA`` 描述，前端不写死。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings as app_settings
from ..database import get_db
from ..providers.audio_providers import available_voices, music_styles, tts_engines
from ..services import settings as settings_svc

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/providers")
def get_provider_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    """三类模型的全部设置与可选项（凭证以掩码形式返回）。"""
    return settings_svc.settings_overview(db)


@router.patch("/providers")
def update_provider_settings(payload: dict[str, Any] = Body(...),
                             db: Session = Depends(get_db)) -> dict[str, Any]:
    """保存设置：选引擎 + 模型名 + 凭证。写入数据库并即时生效（无需重启）。"""
    kind = str(payload.get("kind") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not kind or not name:
        raise HTTPException(status_code=400, detail="必须提供 kind 与 name")
    credentials = payload.get("credentials")
    if credentials is not None and not isinstance(credentials, dict):
        raise HTTPException(status_code=400, detail="credentials 必须是对象")
    try:
        view = settings_svc.update_setting(
            db, kind, name,
            model=payload.get("model"),
            credentials={str(k): str(v) for k, v in credentials.items()} if credentials else None,
            make_default=bool(payload.get("make_default", True)),
            actor=str(payload.get("actor") or "human"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "provider": view, "overview": settings_svc.settings_overview(db)}


@router.get("/voices")
def list_tts_voices(limit: int = Query(200, ge=1, le=1000),
                    refresh: bool = Query(False, description="忽略缓存，重新枚举本机音色")) -> dict[str, Any]:
    """本机可用的 TTS 音色与引擎清单（配音调音台 / 素材中心生成语音时使用）。

    - ``voices``：扁平音色名列表（兼容旧调用方）
    - ``engines``：按引擎分组的音色，含 ``supports_instruct`` 与默认音色，
      前端据此渲染音色下拉与「情感指令」输入框，不硬编码任何音色名
    - ``music_styles``：BGM 可选曲风（对应 scripts/gen_bgm.py 的 --style）
    """
    voices = available_voices(refresh=refresh)
    engines = tts_engines(refresh=refresh)
    return {
        "voices": voices[:limit],
        "total": len(voices),
        "default": app_settings.tts_voice,
        "engines": engines,
        "default_engine": engines[0]["name"] if engines else "",
        "music_styles": music_styles(),
    }
