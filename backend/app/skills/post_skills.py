"""后期与编排类 Skill：视频处理 / 合成 / 质检 / 工作流 / 素材 / 日志 / Provider / 浏览器 / 一键编排。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..core.constants import (
    AssetGroup, AssetType, ModelSettingKind, ShotStatus, TaskStatus, TaskType, WorkflowState,
)
from ..models import Asset, BrowserTask, Project, Shot
from ..providers import register_all, registry
from ..services import agent_log
from ..services import assets as assets_svc
from ..services import planner, projects as projects_svc, quality as quality_svc
from ..services import serializers as S, settings as settings_svc
from ..services import tasks as tasks_svc, workflow as workflow_svc
from .base import SkillContext, SkillError, skill


def _project(db, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
    return project


def _submit(ctx: SkillContext, *, project_id: str, type: str, name: str, **kwargs: Any):
    return tasks_svc.create_task(ctx.db, project_id=project_id, type=type, name=name,
                                 created_by=ctx.actor, commit=False, **kwargs)


# --------------------------------------------------------------------------- #
# 视频处理
# --------------------------------------------------------------------------- #
@skill(
    name="enhance_video", category="processing", is_async=True,
    description="画质增强（超分/补帧/降噪/锐化/调色/音频增强，可自由组合算子）。",
    tags=("video", "enhance"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "asset_id": {"type": "string"},
        "shot_id": {"type": "string"},
        "operations": {"type": "array", "items": {"type": "object"},
                       "description": "如 [{op:upscale,scale:1.5},{op:interpolate,fps:48}]"},
        "provider": {"type": "string"}, "width": {"type": "integer"},
        "height": {"type": "integer"}, "fps": {"type": "integer"}},
        "required": ["project_id"]},
)
def enhance_video(ctx: SkillContext, *, project_id: str, asset_id: str | None = None,
                  shot_id: str | None = None, operations: list[dict[str, Any]] | None = None,
                  provider: str | None = None, width: int | None = None,
                  height: int | None = None, fps: int | None = None):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.ENHANCE_VIDEO, name="画质增强",
                   shot_id=shot_id, provider=provider or "",
                   payload={"asset_id": asset_id, "operations": operations,
                            "width": width, "height": height, "fps": fps})


@skill(
    name="upscale_video", category="processing", is_async=True,
    description="视频超分辨率（放大 + 锐化）。",
    tags=("video", "enhance"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "asset_id": {"type": "string"},
        "shot_id": {"type": "string"}, "scale": {"type": "number", "default": 1.5},
        "width": {"type": "integer"}, "height": {"type": "integer"}}, "required": ["project_id"]},
)
def upscale_video(ctx: SkillContext, *, project_id: str, asset_id: str | None = None,
                  shot_id: str | None = None, scale: float = 1.5,
                  width: int | None = None, height: int | None = None):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.UPSCALE_VIDEO, name="视频超分",
                   shot_id=shot_id,
                   payload={"asset_id": asset_id, "scale": scale, "width": width, "height": height})


@skill(
    name="interpolate_video", category="processing", is_async=True,
    description="视频补帧（提升帧率，让运动更顺滑）。",
    tags=("video", "enhance"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "asset_id": {"type": "string"},
        "shot_id": {"type": "string"}, "fps": {"type": "integer", "default": 48}},
        "required": ["project_id"]},
)
def interpolate_video(ctx: SkillContext, *, project_id: str, asset_id: str | None = None,
                      shot_id: str | None = None, fps: int = 48):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.INTERPOLATE_VIDEO, name="视频补帧",
                   shot_id=shot_id, payload={"asset_id": asset_id, "fps": fps})


@skill(
    name="merge_video", category="processing", is_async=True,
    description="把所有已完成镜头的视频按顺序拼接为粗剪。",
    tags=("video", "compose"),
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}},
                  "required": ["project_id"]},
)
def merge_video(ctx: SkillContext, *, project_id: str):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.MERGE_VIDEO, name="拼接镜头")


@skill(
    name="edit_video", category="processing", is_async=True,
    description=("通用视频编辑操作。operation 支持：trim_video / scale_video / add_voice / "
                 "add_music / add_sfx / mix_audio / add_subtitle / extract_audio / make_thumbnail。"),
    tags=("video", "edit"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "operation": {"type": "string"},
        "inputs": {"type": "array", "items": {"type": "string"},
                   "description": "输入文件绝对路径；不传则自动取项目最新产物"},
        "params": {"type": "object"}}, "required": ["project_id", "operation"]},
)
def edit_video(ctx: SkillContext, *, project_id: str, operation: str,
               inputs: list[str] | None = None, params: dict[str, Any] | None = None):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.EDIT_VIDEO,
                   name=f"视频处理：{operation}",
                   payload={"operation": operation, "inputs": inputs, "params": params or {}})


@skill(
    name="add_voice", category="processing", is_async=True,
    description="给视频加配音音轨（单条音轨，覆盖式）。",
    tags=("video", "audio"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "video_asset_id": {"type": "string"},
        "voice_asset_id": {"type": "string"}, "volume": {"type": "number", "default": 1.0}},
        "required": ["project_id"]},
)
def add_voice(ctx: SkillContext, *, project_id: str, video_asset_id: str | None = None,
              voice_asset_id: str | None = None, volume: float = 1.0):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.EDIT_VIDEO, name="加配音",
                   payload={"operation": "add_voice", "params": {"volume": volume},
                            "video_asset_id": video_asset_id, "voice_asset_id": voice_asset_id})


@skill(
    name="add_music", category="processing", is_async=True,
    description="给视频混入背景音乐（循环 + 压低音量）。",
    tags=("video", "audio"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "video_asset_id": {"type": "string"},
        "music_asset_id": {"type": "string"}, "volume": {"type": "number", "default": 0.16}},
        "required": ["project_id"]},
)
def add_music(ctx: SkillContext, *, project_id: str, video_asset_id: str | None = None,
              music_asset_id: str | None = None, volume: float = 0.16):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.EDIT_VIDEO, name="加背景音乐",
                   payload={"operation": "add_music", "params": {"volume": volume},
                            "video_asset_id": video_asset_id, "music_asset_id": music_asset_id})


@skill(
    name="add_sfx", category="processing", is_async=True,
    description="给视频混入音效。",
    tags=("video", "audio"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "video_asset_id": {"type": "string"},
        "sfx_asset_id": {"type": "string"}, "volume": {"type": "number", "default": 0.5},
        "delay": {"type": "number", "default": 0.0}}, "required": ["project_id"]},
)
def add_sfx(ctx: SkillContext, *, project_id: str, video_asset_id: str | None = None,
            sfx_asset_id: str | None = None, volume: float = 0.5, delay: float = 0.0):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.EDIT_VIDEO, name="加音效",
                   payload={"operation": "add_sfx", "params": {"volume": volume, "delay": delay},
                            "video_asset_id": video_asset_id, "sfx_asset_id": sfx_asset_id})


@skill(
    name="add_subtitle", category="processing", is_async=True,
    description="给视频加字幕（burn=烧录进画面 / soft=软字幕轨道）。",
    tags=("video", "subtitle"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "video_asset_id": {"type": "string"},
        "subtitle_asset_id": {"type": "string"}, "mode": {"type": "string", "default": "burn"},
        "font_size": {"type": "integer", "default": 22}}, "required": ["project_id"]},
)
def add_subtitle(ctx: SkillContext, *, project_id: str, video_asset_id: str | None = None,
                 subtitle_asset_id: str | None = None, mode: str = "burn",
                 font_size: int = 22):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.EDIT_VIDEO, name="加字幕",
                   payload={"operation": "add_subtitle",
                            "params": {"mode": mode, "font_size": font_size},
                            "video_asset_id": video_asset_id,
                            "subtitle_asset_id": subtitle_asset_id})


@skill(
    name="compose_video", category="processing", is_async=True,
    description=("合成最终成片：主视频 + 配音 + 配乐 + 音效 + 字幕，一步完成；"
                 "完成后自动提交质量检查。"),
    tags=("video", "compose", "final"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "video_asset_id": {"type": "string"},
        "with_music": {"type": "boolean", "default": True},
        "with_sfx": {"type": "boolean", "default": False},
        "with_subtitle": {"type": "boolean", "default": True},
        "music_volume": {"type": "number", "default": 0.16},
        "voice_volume": {"type": "number", "default": 1.0},
        "font_size": {"type": "integer", "default": 22},
        "auto_quality_check": {"type": "boolean", "default": True}}, "required": ["project_id"]},
    examples=({"project_id": "proj_xxx", "with_music": True, "with_subtitle": True},),
)
def compose_video(ctx: SkillContext, *, project_id: str, video_asset_id: str | None = None,
                  with_music: bool = True, with_sfx: bool = False, with_subtitle: bool = True,
                  music_volume: float = 0.16, voice_volume: float = 1.0, font_size: int = 22,
                  auto_quality_check: bool = True):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.COMPOSE_VIDEO, name="合成最终成片",
                   payload={"video_asset_id": video_asset_id, "with_music": with_music,
                            "with_sfx": with_sfx, "with_subtitle": with_subtitle,
                            "music_volume": music_volume, "voice_volume": voice_volume,
                            "font_size": font_size, "auto_quality_check": auto_quality_check})


# --------------------------------------------------------------------------- #
# 质检
# --------------------------------------------------------------------------- #
@skill(
    name="run_quality_check", category="quality", is_async=True,
    description="对项目执行质量检查；auto_repair=true 时失败会自动派发最小粒度的修复任务。",
    tags=("quality", "check"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "asset_id": {"type": "string"},
        "auto_repair": {"type": "boolean", "default": True}}, "required": ["project_id"]},
)
def run_quality_check(ctx: SkillContext, *, project_id: str, asset_id: str | None = None,
                      auto_repair: bool = True):
    _project(ctx.db, project_id)
    return _submit(ctx, project_id=project_id, type=TaskType.QUALITY_CHECK, name="质量检查",
                   payload={"asset_id": asset_id, "auto_repair": auto_repair})


@skill(
    name="get_quality_report", category="quality",
    description="获取最近一次质量检查报告（含逐项结果与修复建议）。",
    tags=("quality", "read"),
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}},
                  "required": ["project_id"]},
)
def get_quality_report(ctx: SkillContext, *, project_id: str) -> dict[str, Any]:
    report = quality_svc.latest_run(ctx.db, project_id)
    if report is None:
        return {"report": None, "message": "尚未执行质量检查"}
    return {"report": report}


# --------------------------------------------------------------------------- #
# 工作流
# --------------------------------------------------------------------------- #
@skill(
    name="get_workflow", category="workflow",
    description="获取项目工作流状态机：当前状态、各步骤状态、历史迁移记录。",
    tags=("workflow", "read"),
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}},
                  "required": ["project_id"]},
)
def get_workflow(ctx: SkillContext, *, project_id: str) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    return workflow_svc.workflow_summary(ctx.db, project)


@skill(
    name="advance_workflow", category="workflow",
    description="把工作流推进到正常流程的下一个状态。",
    tags=("workflow", "write"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["project_id"]},
)
def advance_workflow(ctx: SkillContext, *, project_id: str, reason: str = "") -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    return workflow_svc.advance(ctx.db, project, actor=ctx.actor, reason=reason)


@skill(
    name="set_workflow_state", category="workflow",
    description=("设置工作流状态，支持 Agent 动态跳转（如 VIDEO_GENERATED → QUALITY_CHECK）。"
                 "非相邻跳转需要 force=true。"),
    tags=("workflow", "write"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "state": {"type": "string"},
        "reason": {"type": "string"}, "force": {"type": "boolean", "default": False}},
        "required": ["project_id", "state"]},
)
def set_workflow_state(ctx: SkillContext, *, project_id: str, state: str, reason: str = "",
                       force: bool = False) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    valid = set(WorkflowState.FLOW) | set(WorkflowState.ANY_TO)
    if state not in valid:
        raise SkillError(f"未知状态 {state}。可用状态：{', '.join(sorted(valid))}", code="BAD_INPUT")
    return workflow_svc.transition(ctx.db, project, state, actor=ctx.actor, reason=reason, force=force)


@skill(
    name="update_workflow_step", category="workflow",
    description="手动标记某个工作流步骤的状态（用于断点恢复与人工干预）。",
    tags=("workflow", "write"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "step_key": {"type": "string"},
        "state": {"type": "string", "description": "PENDING / RUNNING / SUCCESS / FAILED / SKIPPED"}},
        "required": ["project_id", "step_key", "state"]},
)
def update_workflow_step(ctx: SkillContext, *, project_id: str, step_key: str,
                         state: str) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    if state == "SUCCESS":
        step = workflow_svc.complete_step(ctx.db, project, step_key, actor=ctx.actor)
    elif state == "FAILED":
        step = workflow_svc.fail_step(ctx.db, project, step_key, "人工标记为失败")
    elif state == "PENDING":
        step = workflow_svc.reset_step(ctx.db, project, step_key)
    else:
        step = workflow_svc.start_step(ctx.db, project, step_key, actor=ctx.actor)
        ctx.db.commit()
    if step is None:
        raise SkillError(f"未知步骤: {step_key}", code="NOT_FOUND")
    projects_svc.refresh_progress(ctx.db, project)
    return {"step": S.serialize_workflow_step(step)}


# --------------------------------------------------------------------------- #
# 素材中心
# --------------------------------------------------------------------------- #
@skill(
    name="list_assets", category="asset",
    description="查询素材（按项目 / 类型 / 镜头 / 关键词过滤）。",
    tags=("asset", "read"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "type": {"type": "string",
        "description": "CHARACTER / IMAGE / VIDEO / VOICE / MUSIC / SFX / SUBTITLE / PROJECT_OUTPUT / TEMP"},
        "shot_id": {"type": "string"}, "scene_id": {"type": "string"},
        "status": {"type": "string"}, "keyword": {"type": "string"},
        "limit": {"type": "integer", "default": 200}, "offset": {"type": "integer", "default": 0}}},
)
def list_assets(ctx: SkillContext, *, project_id: str | None = None, type: str | None = None,
                shot_id: str | None = None, scene_id: str | None = None,
                status: str | None = None, keyword: str | None = None,
                limit: int = 200, offset: int = 0) -> dict[str, Any]:
    rows = assets_svc.list_assets(ctx.db, project_id=project_id, asset_type=type, shot_id=shot_id,
                                 scene_id=scene_id, status=status, keyword=keyword,
                                 limit=limit, offset=offset)
    stats = assets_svc.asset_stats(ctx.db, project_id) if project_id else {}
    return {"assets": [S.asset_brief(a) for a in rows], "count": len(rows), "stats": stats}


@skill(
    name="get_asset_center", category="asset",
    description=("素材中心总览：按「人物图 / 场景图 / 音频 / 图片 / 视频」分类返回素材与统计。"
                 "不传 project_id 即跨项目聚合；unassigned=true 只看不属于任何项目的独立素材。"),
    tags=("asset", "read"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string", "description": "留空 = 跨项目聚合"},
        "unassigned": {"type": "boolean", "default": False,
                       "description": "true = 只看不属于任何项目的独立素材"},
        "group": {"type": "string",
                  "description": "character / scene / audio / image / video；留空返回全部"},
        "keyword": {"type": "string"},
        "limit": {"type": "integer", "default": 200}, "offset": {"type": "integer", "default": 0}}},
)
def get_asset_center(ctx: SkillContext, *, project_id: str | None = None,
                     unassigned: bool = False, group: str = "",
                     keyword: str | None = None,
                     limit: int = 200, offset: int = 0) -> dict[str, Any]:
    types = assets_svc.types_of_group(group) if group else None
    rows = assets_svc.list_assets(
        ctx.db, project_id=project_id, asset_types=types, unassigned=unassigned,
        keyword=keyword or None, limit=limit, offset=offset,
    )
    return {
        "assets": [S.asset_brief(a) for a in rows],
        "count": len(rows),
        "stats": assets_svc.center_stats(ctx.db, project_id=project_id, unassigned=unassigned),
    }


@skill(
    name="generate_standalone_voice", category="audio",
    description=("独立合成一段语音并存入素材库 —— 不需要项目、不需要镜头。"
                 "适合试音、旁白素材、配音片段；产物出现在素材中心的「音频」分区。"),
    tags=("audio", "tts", "generate", "asset"),
    input_schema={"type": "object", "properties": {
        "text": {"type": "string", "description": "要合成的文本"},
        "voice": {"type": "string", "description": "音色名，留空用默认音色"},
        "rate": {"type": "integer", "description": "语速，留空用默认"},
        "name": {"type": "string", "description": "素材名，留空自动截取文本生成"},
        "provider": {"type": "string", "description": "tts 引擎（local / cloud），留空用当前默认"},
        "project_id": {"type": "string", "description": "可选：把素材归属到某个项目"}},
        "required": ["text"]},
)
def generate_standalone_voice(ctx: SkillContext, *, text: str, voice: str = "", rate: int = 0,
                              name: str = "", provider: str | None = None,
                              project_id: str | None = None) -> dict[str, Any]:
    if not text.strip():
        raise SkillError("文本为空，无法合成语音", code="BAD_INPUT")
    register_all()
    try:
        tts = registry.get("tts", provider or None)
    except Exception as exc:  # noqa: BLE001
        raise SkillError(f"未找到可用的 TTS 引擎：{exc}", code="NOT_FOUND") from exc

    result = tts.synthesize(text=text, voice=voice, rate=int(rate or 0))
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project_id, result=result, asset_type=AssetType.VOICE,
        name=name or f"语音 {text.strip()[:14]}",
        extra={"role": "standalone_voice", "voice": voice or "(默认)", "text": text},
    )
    ctx.log(f"独立语音已生成并入库：{asset.id}（{result.duration:.1f}s / {result.provider}）")
    return {
        "asset_id": asset.id, "name": asset.name, "url": asset.url,
        "duration": asset.duration, "provider": result.provider, "model": result.model,
        "project_id": asset.project_id,
    }


@skill(
    name="get_asset", category="asset",
    description="获取单个素材的完整信息（含 prompt / model / provider / workflow / parameters）。",
    tags=("asset", "read"),
    input_schema={"type": "object", "properties": {"asset_id": {"type": "string"}},
                  "required": ["asset_id"]},
)
def get_asset(ctx: SkillContext, *, asset_id: str) -> dict[str, Any]:
    asset = ctx.db.get(Asset, asset_id)
    if asset is None:
        raise SkillError(f"素材不存在: {asset_id}", code="NOT_FOUND")
    return {"asset": S.asset_brief(asset), "source_chain": _asset_chain(ctx.db, asset)}


def _asset_chain(db, asset: Asset) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    current: Asset | None = asset
    depth = 0
    while current is not None and depth < 10:
        chain.append({"asset_id": current.id, "type": current.type, "name": current.name,
                      "provider": current.provider, "model": current.model,
                      "task_id": current.task_id, "created_at": S.iso(current.created_at)})
        current = db.get(Asset, current.parent_asset_id) if current.parent_asset_id else None
        depth += 1
    return chain


@skill(
    name="delete_asset", category="asset",
    description="删除素材及其文件。",
    tags=("asset", "destructive"),
    input_schema={"type": "object", "properties": {
        "asset_id": {"type": "string"}, "confirm": {"type": "boolean", "default": False}},
        "required": ["asset_id", "confirm"]},
)
def delete_asset(ctx: SkillContext, *, asset_id: str, confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise SkillError("删除素材属于危险操作，请显式传 confirm=true", code="CONFIRM_REQUIRED")
    asset = ctx.db.get(Asset, asset_id)
    if asset is None:
        raise SkillError(f"素材不存在: {asset_id}", code="NOT_FOUND")
    shot_updates: list[str] = []
    for shot in ctx.db.execute(select(Shot).where(Shot.project_id == asset.project_id)).scalars():
        if shot.image_asset_id == asset_id:
            shot.image_asset_id = None
            shot.image_status = "PENDING"
            shot_updates.append(f"{shot.code}.image")
        if shot.video_asset_id == asset_id:
            shot.video_asset_id = None
            shot.video_status = "PENDING"
            shot_updates.append(f"{shot.code}.video")
        if shot.voice_asset_id == asset_id:
            shot.voice_asset_id = None
            shot.voice_status = "PENDING"
            shot_updates.append(f"{shot.code}.voice")
        if shot.subtitle_asset_id == asset_id:
            shot.subtitle_asset_id = None
            shot_updates.append(f"{shot.code}.subtitle")
    ok = assets_svc.delete_asset(ctx.db, asset_id)
    ctx.db.commit()
    return {"deleted": ok, "asset_id": asset_id, "unlinked": shot_updates}


@skill(
    name="regenerate_asset", category="asset", is_async=True,
    description="按素材来源重新生成：自动判断应触发 image / video / voice 的重跑。",
    tags=("asset", "retry"),
    input_schema={"type": "object", "properties": {
        "asset_id": {"type": "string"}, "provider": {"type": "string"}}, "required": ["asset_id"]},
)
def regenerate_asset(ctx: SkillContext, *, asset_id: str, provider: str | None = None):
    asset = ctx.db.get(Asset, asset_id)
    if asset is None:
        raise SkillError(f"素材不存在: {asset_id}", code="NOT_FOUND")
    if not asset.shot_id:
        raise SkillError("该素材没有关联镜头，无法按镜头重生成；请改用对应的生成 Skill",
                         code="BAD_INPUT")
    shot = ctx.db.get(Shot, asset.shot_id)
    if shot is None:
        raise SkillError("素材关联的镜头已不存在", code="NOT_FOUND")
    mapping = {
        AssetType.IMAGE: (Shot.image_asset_id, "image_asset_id", TaskType.GENERATE_IMAGE),
        AssetType.VIDEO: (Shot.video_asset_id, "video_asset_id", TaskType.GENERATE_VIDEO),
        AssetType.VOICE: (Shot.voice_asset_id, "voice_asset_id", TaskType.GENERATE_VOICE),
    }
    entry = mapping.get(asset.type)
    if entry is None:
        raise SkillError(f"暂不支持重生成 {asset.type} 类型素材", code="BAD_INPUT")
    _, field, task_type = entry
    setattr(shot, field, None)
    shot.retry_count = (shot.retry_count or 0) + 1
    return tasks_svc.create_task(
        ctx.db, project_id=shot.project_id, type=task_type,
        name=f"重生成：{shot.code} {asset.type}", shot_id=shot.id,
        payload={"provider": provider, "regenerate": True, "source_asset_id": asset_id},
        created_by=ctx.actor, commit=False,
    )


# --------------------------------------------------------------------------- #
# 日志 / Provider / 浏览器
# --------------------------------------------------------------------------- #
@skill(
    name="list_agent_logs", category="log",
    description="查询 Agent 执行日志（Web UI 的『Agent 执行日志』页数据源）。",
    tags=("log", "read"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "task_id": {"type": "string"},
        "level": {"type": "string"}, "limit": {"type": "integer", "default": 200},
        "offset": {"type": "integer", "default": 0}}},
)
def list_agent_logs(ctx: SkillContext, *, project_id: str | None = None, task_id: str | None = None,
                    level: str | None = None, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    rows = agent_log.list_logs(ctx.db, project_id=project_id, task_id=task_id, level=level,
                               limit=limit, offset=offset)
    return {"logs": [{"id": r.id, "ts": S.iso(r.created_at), "actor": r.actor, "event": r.event,
                      "level": r.level, "message": r.message, "detail": r.detail or {},
                      "task_id": r.task_id, "shot_id": r.shot_id, "duration_ms": r.duration_ms}
                     for r in rows], "count": len(rows)}


@skill(
    name="log_agent_event", category="log",
    description="由 Agent 主动写入一条执行日志（用于把 Agent 自己的思考过程同步到 Web UI）。",
    tags=("log", "write"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "event": {"type": "string"},
        "message": {"type": "string"}, "level": {"type": "string", "default": "INFO"},
        "task_id": {"type": "string"}, "shot_id": {"type": "string"},
        "detail": {"type": "object"}}, "required": ["message"]},
)
def log_agent_event(ctx: SkillContext, *, message: str, project_id: str | None = None,
                    event: str = "agent.note", level: str = "INFO", task_id: str | None = None,
                    shot_id: str | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    row = agent_log.log_event(ctx.db, project_id=project_id, event=event, message=message,
                              level=level, actor=ctx.actor, task_id=task_id, shot_id=shot_id,
                              detail=detail or {}, commit=True)
    return {"log_id": row.id, "ts": S.iso(row.created_at)}


@skill(
    name="list_providers", category="provider",
    description="列出所有执行引擎 Provider 及其能力与可用状态。",
    tags=("provider", "read"),
    input_schema={"type": "object", "properties": {"kind": {"type": "string"}}},
)
def list_providers(ctx: SkillContext, *, kind: str | None = None) -> dict[str, Any]:
    register_all()
    return {"providers": registry.list(kind), "defaults": {
        k: registry.default_name(k) for k in ("image", "video", "tts", "music", "sfx",
                                              "subtitle", "enhance", "processing", "browser")
    }}


@skill(
    name="set_default_provider", category="provider",
    description=("切换某类能力的默认引擎（生图 / 图生视频 / 语音生成），"
                 "并可同时设置模型名与连接凭证（Base URL、API Key）。"
                 "写入数据库，重启后依然生效。"),
    tags=("provider", "write", "settings"),
    input_schema={"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(ModelSettingKind.ALL)},
        "name": {"type": "string", "description": "引擎名，如 local / comfyui / cloud"},
        "model": {"type": "string", "description": "该引擎下使用的具体模型名，可留空"},
        "credentials": {"type": "object",
                        "description": "如 {base_url, api_key}；字段传空字符串表示清除该项"}},
        "required": ["kind", "name"]},
)
def set_default_provider(ctx: SkillContext, *, kind: str, name: str,
                         model: str | None = None,
                         credentials: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        view = settings_svc.update_setting(
            ctx.db, kind, name, model=model, credentials=credentials, actor=ctx.actor,
        )
    except ValueError as exc:
        raise SkillError(str(exc), code="NOT_FOUND") from exc
    return {"kind": kind, "default": view["name"], "model": view["model"],
            "is_default": view["is_default"], "ready": view["ready"]}


@skill(
    name="list_provider_settings", category="provider",
    description=("查看「生图 / 图生视频 / 语音生成」三类模型的当前设置："
                 "可选的本地与云端引擎、各自是否可用、模型名、凭证是否已配置（密钥掩码返回）。"),
    tags=("provider", "read", "settings"),
    input_schema={"type": "object", "properties": {}},
)
def list_provider_settings(ctx: SkillContext) -> dict[str, Any]:
    return settings_svc.settings_overview(ctx.db)


@skill(
    name="create_browser_task", category="browser",
    description=("登记一个浏览器自动化任务（用于没有 API 的第三方平台）。"
                 "Backend 只登记与派发 playbook，实际执行由 Agent 完成。"),
    tags=("browser", "write"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "instruction": {"type": "string"},
        "target_platform": {"type": "string"}, "shot_id": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "object"}}},
        "required": ["project_id", "instruction"]},
)
def create_browser_task(ctx: SkillContext, *, project_id: str, instruction: str,
                        target_platform: str = "", shot_id: str | None = None,
                        steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    _project(ctx.db, project_id)
    register_all()
    provider = registry.get("browser", "agent_browser")
    plan = steps or provider.plan(instruction, target_platform)
    row = BrowserTask(project_id=project_id, shot_id=shot_id, target_platform=target_platform,
                      instruction=instruction, steps=plan, status="PENDING", executor="agent")
    ctx.db.add(row)
    ctx.db.flush()
    task = tasks_svc.create_task(
        ctx.db, project_id=project_id, type=TaskType.BROWSER_TASK,
        name=f"浏览器任务：{instruction[:40]}", shot_id=shot_id,
        payload={"instruction": instruction, "platform": target_platform,
                 "browser_task_id": row.id, "provider": "agent_browser"},
        created_by=ctx.actor, commit=False,
    )
    ctx.db.commit()
    return {"browser_task_id": row.id, "task_id": task.id, "playbook": plan,
            "note": "任务已登记，请由 Agent 执行浏览器动作后调用 complete_browser_task 回写"}


@skill(
    name="complete_browser_task", category="browser",
    description="Agent 完成浏览器任务后回写结果；如有下载产物，可指定 asset 目录或文件登记为素材。",
    tags=("browser", "write"),
    input_schema={"type": "object", "properties": {
        "browser_task_id": {"type": "string"}, "status": {"type": "string", "default": "SUCCESS"},
        "result": {"type": "object"},
        "downloaded_files": {"type": "array", "items": {"type": "string"},
                             "description": "本地文件绝对路径，将登记到 Asset Center"},
        "shot_id": {"type": "string"}, "asset_type": {"type": "string", "default": "VIDEO"}},
        "required": ["browser_task_id"]},
)
def complete_browser_task(ctx: SkillContext, *, browser_task_id: str, status: str = "SUCCESS",
                          result: dict[str, Any] | None = None,
                          downloaded_files: list[str] | None = None,
                          shot_id: str | None = None, asset_type: str = "VIDEO") -> dict[str, Any]:
    row = ctx.db.get(BrowserTask, browser_task_id)
    if row is None:
        raise SkillError(f"浏览器任务不存在: {browser_task_id}", code="NOT_FOUND")
    row.status = status
    row.result = result or {}
    registered: list[dict[str, Any]] = []
    for path in downloaded_files or []:
        from pathlib import Path as _P

        from ..providers.base import GenerationResult

        p = _P(path)
        if not p.exists():
            continue
        info: dict[str, Any] = {}
        if p.suffix.lower() in (".mp4", ".mov", ".webm"):
            from ..providers import local_engine as engine

            try:
                info = engine.ffprobe(p)
            except Exception:  # noqa: BLE001
                info = {}
        gen = GenerationResult(
            file_path=str(p), provider="external_platform", model=row.target_platform or "browser",
            workflow="browser_automation", width=info.get("width", 0), height=info.get("height", 0),
            duration=info.get("duration", 0.0), fps=info.get("fps", 0.0),
            format=p.suffix.lstrip("."), size_bytes=p.stat().st_size,
            parameters={"browser_task_id": browser_task_id},
        )
        asset = assets_svc.ingest_result(
            ctx.db, project_id=row.project_id, result=gen, asset_type=asset_type,
            name=f"{row.target_platform or 'browser'} 产物", shot_id=shot_id or row.shot_id,
            source="external", extra={"browser_task_id": browser_task_id},
        )
        if (shot_id or row.shot_id) and asset_type == AssetType.VIDEO:
            shot = ctx.db.get(Shot, shot_id or row.shot_id)
            if shot is not None:
                shot.video_asset_id = asset.id
                shot.video_status = "READY"
                shot.status = ShotStatus.READY
        registered.append({"asset_id": asset.id, "url": asset.url})
    task = tasks_svc.list_tasks(ctx.db, project_id=row.project_id, type=TaskType.BROWSER_TASK, limit=200)
    for t in task:
        if (t.payload or {}).get("browser_task_id") == browser_task_id:
            t.status = TaskStatus.SUCCESS if status == "SUCCESS" else TaskStatus.FAILED
            t.result = {"status": status, **(result or {}), "assets": registered}
            t.progress = 100
    ctx.db.commit()
    return {"browser_task_id": browser_task_id, "status": status, "assets": registered}


# --------------------------------------------------------------------------- #
# 一键编排（Agent 便利入口，不替代 Agent 决策）
# --------------------------------------------------------------------------- #
@skill(
    name="plan_project", category="orchestration",
    description=("使用本地规划引擎产出一份可执行的制作方案（脚本大纲 + 分镜 + 角色），"
                 "结果不落库，供 Agent 参考或改写后写入。"),
    tags=("orchestration", "planner"),
    input_schema={"type": "object", "properties": {
        "requirement": {"type": "string"}, "style": {"type": "string", "default": "漫画教学风格"},
        "target_duration": {"type": "number", "default": 300},
        "shot_duration": {"type": "number", "default": 5},
        "include_storyboard": {"type": "boolean", "default": False}},
        "required": ["requirement"]},
)
def plan_project(ctx: SkillContext, *, requirement: str, style: str = "漫画教学风格",
                 target_duration: float = 300.0, shot_duration: float = 5.0,
                 include_storyboard: bool = False) -> dict[str, Any]:
    script = planner.plan_script(requirement=requirement, style=style,
                                target_duration=target_duration)
    result: dict[str, Any] = {"script": script, "characters": planner.plan_characters(requirement, style)}
    if include_storyboard:
        result["storyboard"] = planner.plan_storyboard(
            requirement=requirement, style=style, target_duration=target_duration,
            shot_duration=shot_duration,
        )
    return result


@skill(
    name="bootstrap_project", category="orchestration", is_async=True,
    description=("一键搭起项目骨架：创建项目 → 生成脚本 → 拆分镜 → 创建角色。"
                 "适用于 Agent 快速起步；内容仍可由 Agent 后续覆盖。"),
    tags=("orchestration", "setup"),
    input_schema={"type": "object", "properties": {
        "name": {"type": "string"}, "requirement": {"type": "string"},
        "style": {"type": "string", "default": "漫画教学风格"},
        "target_duration": {"type": "number", "default": 300},
        "shot_duration": {"type": "number", "default": 5},
        "width": {"type": "integer", "default": 1280},
        "height": {"type": "integer", "default": 720},
        "fps": {"type": "integer", "default": 24},
        "create_characters": {"type": "boolean", "default": True},
        "generate_references": {"type": "boolean", "default": False}},
        "required": ["name"]},
    examples=({"name": "AI Agent 教学视频",
               "requirement": "制作一个 5 分钟的 AI Agent 教学视频，漫画教学风格",
               "target_duration": 300},),
)
def bootstrap_project(ctx: SkillContext, *, name: str, requirement: str = "",
                      style: str = "漫画教学风格", target_duration: float = 300.0,
                      shot_duration: float = 5.0, width: int = 1280, height: int = 720,
                      fps: int = 24, create_characters: bool = True,
                      generate_references: bool = False, **extra: Any) -> dict[str, Any]:
    from ..models import Character as CharacterModel
    from ..models import Scene, Script as ScriptModel, Storyboard

    requirement = requirement or name
    project = Project(
        name=name, requirement=requirement, description=requirement, style=style,
        target_duration=float(target_duration), width=int(width), height=int(height), fps=int(fps),
    )
    ctx.db.add(project)
    ctx.db.flush()
    workflow_svc.ensure_workflow(ctx.db, project)
    agent_log.log_event(ctx.db, project_id=project.id, event="project.created", actor=ctx.actor,
                        message=f"Agent 创建项目：{project.name}", detail={"requirement": requirement})

    # 角色
    characters = []
    if create_characters:
        for spec in planner.plan_characters(requirement, style):
            char = CharacterModel(project_id=project.id, name=spec["name"], role=spec["role"],
                                 description=spec["description"], appearance=spec["appearance"],
                                 personality=spec["personality"], voice_style=spec["voice_style"],
                                 reference_prompt=spec["reference_prompt"], status="PENDING")
            ctx.db.add(char)
            characters.append(char)
        ctx.db.flush()

    # 脚本
    planned_script = planner.plan_script(requirement=requirement, style=style,
                                         target_duration=target_duration)
    script = ScriptModel(project_id=project.id, title=planned_script["title"],
                        content=planned_script["content"], outline=planned_script["outline"],
                        style=style, status="READY", provider=planned_script["provider"],
                        model=planned_script["model"], parameters=planned_script["parameters"])
    ctx.db.add(script)
    ctx.db.flush()
    workflow_svc.complete_step(ctx.db, project, "script",
                               output={"script_id": script.id}, commit=False)

    # 分镜
    planned_board = planner.plan_storyboard(
        requirement=requirement, style=style, target_duration=target_duration,
        shot_duration=shot_duration,
        characters=[{"name": c.name, "id": c.id} for c in characters] or None,
    )
    board = Storyboard(project_id=project.id, title=planned_board["title"],
                       synopsis=planned_board["synopsis"], visual_style=style, status="READY",
                       provider=planned_board["provider"], model=planned_board["model"],
                       parameters=planned_board["parameters"])
    ctx.db.add(board)
    ctx.db.flush()
    shot_count = 0
    for s_idx, raw_scene in enumerate(planned_board["scenes"], start=1):
        scene = Scene(project_id=project.id, storyboard_id=board.id, sequence=s_idx,
                      code=raw_scene.get("code", f"Scene {s_idx:02d}"),
                      title=raw_scene.get("title", ""), summary=raw_scene.get("summary", ""),
                      location=raw_scene.get("location", ""), mood=raw_scene.get("mood", ""))
        ctx.db.add(scene)
        ctx.db.flush()
        for shot_index, raw_shot in enumerate(raw_scene.get("shots") or [], start=1):
            seq = int(raw_shot.get("sequence") or shot_index)
            char_ids = [
                c.id for c in characters
                if c.name in [str(v) for v in (raw_shot.get("character_ids") or [])]
            ]
            ctx.db.add(Shot(
                project_id=project.id, scene_id=scene.id, sequence=seq,
                code=raw_shot.get("code") or f"Shot {seq:03d}",
                duration=float(raw_shot.get("duration") or shot_duration),
                description=raw_shot.get("description", ""), camera=raw_shot.get("camera", ""),
                location=raw_shot.get("location", ""), visual_style=raw_shot.get("visual_style", style),
                character_ids=char_ids, image_prompt=raw_shot.get("image_prompt", ""),
                video_prompt=raw_shot.get("video_prompt", ""),
                negative_prompt=raw_shot.get("negative_prompt", ""),
                voice_script=raw_shot.get("voice_script", ""),
                subtitle_text=raw_shot.get("subtitle_text") or raw_shot.get("voice_script", ""),
                extra=raw_shot.get("extra") or {},
            ))
            shot_count += 1
    board.scene_count = len(planned_board["scenes"])
    board.shot_count = shot_count
    workflow_svc.complete_step(ctx.db, project, "storyboard",
                               output={"scenes": board.scene_count, "shots": shot_count}, commit=False)
    if create_characters:
        workflow_svc.complete_step(ctx.db, project, "character",
                                   output={"characters": len(characters)}, commit=False)
    projects_svc.refresh_progress(ctx.db, project, commit=False)
    ctx.db.commit()

    reference_tasks = []
    if generate_references:
        register_all()
        for char in characters:
            task = tasks_svc.create_task(
                ctx.db, project_id=project.id, type=TaskType.GENERATE_CHARACTER_REFERENCE,
                name=f"生成角色参考图：{char.name}", character_id=char.id,
                created_by=ctx.actor, commit=False,
            )
            reference_tasks.append(task.id)
        ctx.db.commit()

    return {
        "project_id": project.id,
        "project": S.serialize_project(project, db=ctx.db),
        "script_id": script.id,
        "storyboard_id": board.id,
        "scene_count": board.scene_count,
        "shot_count": shot_count,
        "character_ids": [c.id for c in characters],
        "reference_task_ids": reference_tasks,
        "status": projects_svc.project_status(ctx.db, project),
        "message": "项目骨架已就绪，建议下一步调用 generate_all_images 生成关键帧",
    }


@skill(
    name="run_pipeline", category="orchestration", is_async=True,
    description=("推进项目流水线：自动判断当前缺什么并批量提交对应任务"
                 "（关键帧 → 视频 → 配音 → 字幕 → 配乐 → 合成 → 质检）。"),
    tags=("orchestration", "pipeline"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"},
        "stages": {"type": "array", "items": {"type": "string"},
                   "description": "限定要推进的阶段；不传则自动判断"},
        "image_provider": {"type": "string"}, "video_provider": {"type": "string"},
        "auto_quality_check": {"type": "boolean", "default": True},
        "compose": {"type": "boolean", "default": True}}, "required": ["project_id"]},
)
def run_pipeline(ctx: SkillContext, *, project_id: str, stages: list[str] | None = None,
                 image_provider: str | None = None, video_provider: str | None = None,
                 auto_quality_check: bool = True, compose: bool = True) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    shots = ctx.db.execute(select(Shot).where(Shot.project_id == project_id)).scalars().all()
    if not shots:
        raise SkillError("项目还没有镜头，请先 create_storyboard", code="BAD_INPUT")

    want = set(stages or [])
    submitted: dict[str, list[str]] = {}
    notes: list[str] = []
    fields = {"image": "image_asset_id", "video": "video_asset_id",
              "voice": "voice_asset_id", "subtitle": "subtitle_asset_id"}

    def need(kind: str) -> list[Shot]:
        return [s for s in shots if getattr(s, fields[kind]) is None]

    if not want or "image" in want:
        todo = need("image")
        if todo:
            ids = []
            for shot in todo:
                ids.append(tasks_svc.create_task(
                    ctx.db, project_id=project_id, type=TaskType.GENERATE_IMAGE,
                    name=f"生成关键帧 {shot.code}", shot_id=shot.id,
                    payload={"provider": image_provider, "batch": "pipeline"},
                    created_by=ctx.actor, commit=False).id)
            submitted["image"] = ids
        else:
            notes.append("关键帧已齐全")

    if not want or "video" in want:
        todo = need("video")
        if todo:
            ids = []
            for shot in todo:
                ids.append(tasks_svc.create_task(
                    ctx.db, project_id=project_id, type=TaskType.GENERATE_VIDEO,
                    name=f"生成视频 {shot.code}", shot_id=shot.id,
                    payload={"provider": video_provider, "batch": "pipeline"},
                    created_by=ctx.actor, commit=False).id)
            submitted["video"] = ids
        else:
            notes.append("视频已齐全")

    if not want or "voice" in want:
        todo = need("voice")
        if todo:
            task = tasks_svc.create_task(
                ctx.db, project_id=project_id, type=TaskType.GENERATE_VOICE,
                name="批量生成配音", payload={}, created_by=ctx.actor, commit=False)
            submitted["voice"] = [task.id]
        else:
            notes.append("配音已齐全")

    music_exists = ctx.db.execute(select(Asset).where(
        Asset.project_id == project_id, Asset.type == AssetType.MUSIC)).scalars().first()
    if (not want or "music" in want) and music_exists is None:
        task = tasks_svc.create_task(
            ctx.db, project_id=project_id, type=TaskType.GENERATE_MUSIC, name="生成背景音乐",
            payload={"duration": project.target_duration, "mood": "calm"},
            created_by=ctx.actor, commit=False)
        submitted["music"] = [task.id]

    if (not want or "subtitle" in want) and need("subtitle"):
        task = tasks_svc.create_task(
            ctx.db, project_id=project_id, type=TaskType.GENERATE_SUBTITLE, name="生成全片字幕",
            payload={"format": "srt"}, created_by=ctx.actor, commit=False)
        submitted["subtitle"] = [task.id]

    has_failed = ctx.db.execute(select(Shot).where(
        Shot.project_id == project_id, Shot.status == ShotStatus.FAILED)).scalars().first()
    if has_failed:
        notes.append(f"存在失败镜头（{has_failed.code}），可先调用 retry_failed_tasks")

    all_videos_ready = not need("video")
    if compose and all_videos_ready and (not want or "compose" in want):
        task = tasks_svc.create_task(
            ctx.db, project_id=project_id, type=TaskType.COMPOSE_VIDEO, name="合成最终成片",
            payload={"auto_quality_check": auto_quality_check}, created_by=ctx.actor, commit=False)
        submitted["compose"] = [task.id]

    ctx.db.commit()
    return {
        "project_id": project_id,
        "submitted": submitted,
        "task_ids": [tid for ids in submitted.values() for tid in ids],
        "counts": {k: len(v) for k, v in submitted.items()},
        "notes": notes,
        "message": "任务已批量入队，请通过 list_tasks 或 get_task_status 观察进度",
    }


@skill(
    name="resume_project", category="orchestration",
    description="断点恢复：检查项目当前卡在哪一步，给出下一步动作，可选自动补提交。",
    tags=("orchestration", "recovery"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "auto_submit": {"type": "boolean", "default": False}},
        "required": ["project_id"]},
)
def resume_project(ctx: SkillContext, *, project_id: str, auto_submit: bool = False) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    status = projects_svc.project_status(ctx.db, project)
    stuck = ctx.db.execute(select(Task).where(
        Task.project_id == project_id, Task.status == TaskStatus.RUNNING)).scalars().all()
    failed = ctx.db.execute(select(Task).where(
        Task.project_id == project_id, Task.status == TaskStatus.FAILED)).scalars().all()
    result: dict[str, Any] = {
        "project_id": project_id,
        "workflow_state": status["workflow_state"],
        "progress": status["progress"],
        "stages": status["stages"],
        "next_actions": status["next_actions"],
        "running_tasks": [t.id for t in stuck],
        "failed_tasks": [t.id for t in failed],
    }
    if auto_submit:
        result["pipeline"] = run_pipeline(ctx, project_id=project_id)  # type: ignore[arg-type]
    return result
