"""生成类 Skill：Image / Video / Voice / Music / SFX / Subtitle / Task。

统一约定：所有生成类 Skill 都是异步的，返回 taskId，Agent 用 get_task_status 轮询。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..core.constants import AssetType, ShotStatus, TaskStatus, TaskType
from ..models import Asset, Project, Shot
from ..config import settings
from ..services import assets as assets_svc
from ..services import generation_plans as plans_svc
from ..services import projects as projects_svc, serializers as S, tasks as tasks_svc
from ..services import resolution as res_svc
from .base import SkillContext, SkillError, skill


def _project(db, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
    return project


def _shot(db, shot_id: str) -> Shot:
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise SkillError(f"镜头不存在: {shot_id}", code="NOT_FOUND")
    return shot


def _compiled_prompt_exists(db, shot: Shot, prompt_type: str) -> bool:
    """该镜头是否已有编译产物（新层是唯一事实来源）。"""
    from ..models import Prompt

    return db.execute(
        select(Prompt.id).where(
            Prompt.shot_id == shot.id, Prompt.type == prompt_type,
            Prompt.current_version_id.isnot(None),
        ).limit(1)
    ).first() is not None


def _legacy_override_warning(db, shot: Shot, prompt_type: str, prompt: str | None) -> str | None:
    """老字段 ``prompt`` 覆盖的守卫：返回警告文本表示**本次覆盖无效**。

    ``generate_image`` / ``generate_video`` 上的 ``prompt`` 参数是**老链路**的入口
    （直接写 ``Shot.image_prompt``）。一旦该镜头编译过提示词，处理器只会读编译产物，
    老字段彻底退化为「只写不读」的兼容投影 —— 此时：

    * 覆盖**不参与生成**（新层优先）；
    * 连投影也**不写**：写进去会让 Shot 老字段与实际生成依据不一致，
      后来人反查时会被误导。

    两条都不做静默处理，而是把警告随返回值带回给调用方。
    """
    if prompt and _compiled_prompt_exists(db, shot, prompt_type):
        return (f"该镜头已编译过 {prompt_type} 提示词（新层为唯一事实来源），"
                f"传入的 prompt 不参与生成、也未写入兼容投影；"
                f"要改提示词请用 compile_{prompt_type}_prompt 重新编译。")
    return None


def _prepare_image_size(db, project: Project, *, provider: str | None,
                        resolution: Any, aspect_ratio: Any,
                        width: int | None, height: int | None) -> dict[str, Any]:
    """把（resolution, aspect_ratio, width, height）定成一组具体参数并做能力校验。

    优先级：显式 width/height > resolution×aspect_ratio > 项目默认尺寸。
    分辨率由 **Provider 能力声明**校验（模型不写死在这里）；超出模型原生档位只发
    warning，让调用方看得见，不静默降级。
    """
    provider_name = (provider or (project.extra or {}).get("image_provider")
                     or settings.default_provider_image)
    try:
        info = res_svc.describe(resolution, aspect_ratio,
                                fallback=(int(width or project.width or 1280),
                                          int(height or project.height or 720)))
    except res_svc.ResolutionError as exc:
        raise SkillError(str(exc), code="BAD_RESOLUTION") from exc

    if width or height:
        info["width"] = int(width or info["width"])
        info["height"] = int(height or info["height"])
        info["megapixels"] = round(info["width"] * info["height"] / 1_000_000, 2)

    ok, note = plans_svc.check_provider_capability(
        db, "image", provider_name, info.get("resolution") or ""
    )
    if not ok:
        raise SkillError(note, code="RESOLUTION_UNSUPPORTED")
    info["provider"] = provider_name
    info["provider_note"] = note
    return info


# --------------------------------------------------------------------------- #
# Image
# --------------------------------------------------------------------------- #
@skill(
    name="generate_image", category="image", is_async=True,
    description="为指定镜头生成关键帧（异步）。返回 taskId，用 get_image_generation_status 查询。",
    tags=("image", "generate"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "shot_id": {"type": "string"},
        "provider": {"type": "string", "description": "local / comfyui / cloud"},
        "prompt": {"type": "string", "description": "覆盖镜头的提示词（既有链路字段；新链路请用 compile_image_prompt）"},
        "workflow_name": {"type": "string",
                          "description": "ComfyUI 工作流模板名（如 qwen_image_scene）。provider=comfyui 时使用"},
        "workflow_json": {"type": "object", "description": "直接传入 API 格式工作流，优先于 workflow_name"},
        "resolution": {"type": "string", "enum": ["720p", "1080p", "2K", "4K"],
                       "description": "分辨率档位。由 Provider 能力声明校验；不传则用项目默认尺寸"},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1", "4:3", "3:2"],
                         "description": "画幅。仅在与 resolution 同传时生效"},
        "width": {"type": "integer"}, "height": {"type": "integer"}, "seed": {"type": "integer"}},
        "required": ["shot_id"]},
)
def generate_image(ctx: SkillContext, *, shot_id: str, project_id: str | None = None,
                   provider: str | None = None, prompt: str | None = None,
                   workflow_name: str | None = None, workflow_json: dict[str, Any] | None = None,
                   resolution: str | None = None, aspect_ratio: str | None = None,
                   width: int | None = None, height: int | None = None,
                   seed: int | None = None):
    shot = _shot(ctx.db, shot_id)
    project = _project(ctx.db, project_id or shot.project_id)
    size = _prepare_image_size(ctx.db, project, provider=provider, resolution=resolution,
                               aspect_ratio=aspect_ratio, width=width, height=height)
    override_warning = _legacy_override_warning(ctx.db, shot, "image", prompt)
    if prompt and override_warning is None:
        # ⚠️ 兼容投影：既有链路仍从 Shot 读提示词；新链路请走 compile_image_prompt
        shot.image_prompt = prompt
    task = tasks_svc.create_task(
        ctx.db, project_id=project.id, type=TaskType.GENERATE_IMAGE,
        name=f"生成关键帧 {shot.code}", shot_id=shot.id,
        payload={"provider": provider, "seed": seed,
                 "width": size["width"], "height": size["height"],
                 "resolution": size["resolution"], "aspect_ratio": size["aspect_ratio"],
                 "workflow_name": workflow_name, "workflow_json": workflow_json},
        created_by=ctx.actor, commit=False,
    )
    # 返回 (Task, 附加信息)：task_id 契约不变，另外把解析后的尺寸与 warning 一并回给调用方
    return task, {
        "shot_id": shot.id,
        "size": {k: size[k] for k in ("resolution", "aspect_ratio", "width", "height",
                                      "megapixels", "above_native")},
        "provider": size["provider"], "provider_note": size["provider_note"],
        "warnings": list(size["warnings"]) + ([override_warning] if override_warning else []),
    }


@skill(
    name="regenerate_image", category="image", is_async=True,
    description="重新生成某个镜头的关键帧：清空旧产物并重新入队（旧素材保留可对比）。",
    tags=("image", "generate", "retry"),
    input_schema={"type": "object", "properties": {
        "shot_id": {"type": "string"}, "provider": {"type": "string"},
        "prompt": {"type": "string"}, "seed": {"type": "integer"},
        "workflow_name": {"type": "string"},
        "workflow_json": {"type": "object"},
        "resolution": {"type": "string", "enum": ["720p", "1080p", "2K", "4K"]},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1", "4:3", "3:2"]},
        "width": {"type": "integer"}, "height": {"type": "integer"},
        "keep_old": {"type": "boolean", "default": True}},
        "required": ["shot_id"]},
)
def regenerate_image(ctx: SkillContext, *, shot_id: str, provider: str | None = None,
                     prompt: str | None = None, seed: int | None = None,
                     workflow_name: str | None = None,
                     workflow_json: dict[str, Any] | None = None,
                     resolution: str | None = None, aspect_ratio: str | None = None,
                     width: int | None = None, height: int | None = None,
                     keep_old: bool = True):
    shot = _shot(ctx.db, shot_id)
    project = _project(ctx.db, shot.project_id)
    size = _prepare_image_size(ctx.db, project, provider=provider, resolution=resolution,
                               aspect_ratio=aspect_ratio, width=width, height=height)
    override_warning = _legacy_override_warning(ctx.db, shot, "image", prompt)
    if prompt and override_warning is None:
        shot.image_prompt = prompt
    shot.image_asset_id = None
    shot.image_status = "PENDING"
    shot.status = ShotStatus.PENDING
    task = tasks_svc.create_task(
        ctx.db, project_id=shot.project_id, type=TaskType.GENERATE_IMAGE,
        name=f"重生成关键帧 {shot.code}", shot_id=shot.id,
        payload={"provider": provider, "seed": seed, "regenerate": True, "keep_old": keep_old,
                 "width": size["width"], "height": size["height"],
                 "resolution": size["resolution"], "aspect_ratio": size["aspect_ratio"],
                 "workflow_name": workflow_name, "workflow_json": workflow_json},
        created_by=ctx.actor, commit=False,
    )
    return task, {
        "shot_id": shot.id,
        "size": {k: size[k] for k in ("resolution", "aspect_ratio", "width", "height",
                                      "megapixels", "above_native")},
        "provider": size["provider"], "provider_note": size["provider_note"],
        "warnings": list(size["warnings"]) + ([override_warning] if override_warning else []),
    }


@skill(
    name="get_image_generation_status", category="image",
    description="查询关键帧生成状态：按 task_id / shot_id / project_id 查询。",
    tags=("image", "status", "read"),
    input_schema={"type": "object", "properties": {
        "task_id": {"type": "string"}, "shot_id": {"type": "string"},
        "project_id": {"type": "string"}}},
)
def get_image_generation_status(ctx: SkillContext, *, task_id: str | None = None,
                                shot_id: str | None = None,
                                project_id: str | None = None) -> dict[str, Any]:
    from ..models import Task

    if task_id:
        task = ctx.db.get(Task, task_id)
        if task is None:
            raise SkillError(f"任务不存在: {task_id}", code="NOT_FOUND")
        return {"task": S.serialize_task(task)}
    if shot_id:
        shot = _shot(ctx.db, shot_id)
        asset = ctx.db.get(Asset, shot.image_asset_id) if shot.image_asset_id else None
        return {"shot_id": shot_id, "status": shot.image_status,
                "asset": S.asset_brief(asset), "shot_status": shot.status}
    if project_id:
        rows = tasks_svc.list_tasks(ctx.db, project_id=project_id, type=TaskType.GENERATE_IMAGE, limit=50)
        done = ctx.db.query(Shot).filter(Shot.project_id == project_id,
                                         Shot.image_asset_id.isnot(None)).count()
        total = ctx.db.query(Shot).filter(Shot.project_id == project_id).count()
        return {"tasks": [S.serialize_task(t, include_logs=False) for t in rows],
                "progress": {"done": done, "total": total}}
    raise SkillError("需要提供 task_id / shot_id / project_id 之一", code="BAD_INPUT")


# --------------------------------------------------------------------------- #
# Video
# --------------------------------------------------------------------------- #
@skill(
    name="generate_video", category="video", is_async=True,
    description="为指定镜头生成视频片段（异步）。若该镜头还没有关键帧会自动先生成。",
    tags=("video", "generate"),
    input_schema={"type": "object", "properties": {
        "shot_id": {"type": "string"}, "project_id": {"type": "string"},
        "provider": {"type": "string", "description": "local / comfyui / cloud"},
        "prompt": {"type": "string"}, "duration": {"type": "number"},
        "fps": {"type": "integer"}, "motion": {"type": "string"},
        "seed": {"type": "integer"}}, "required": ["shot_id"]},
    examples=({"shot_id": "shot_xxx", "provider": "local", "duration": 5},),
)
def generate_video(ctx: SkillContext, *, shot_id: str, project_id: str | None = None,
                   provider: str | None = None, prompt: str | None = None,
                   duration: float | None = None, fps: int | None = None,
                   motion: str | None = None, seed: int | None = None):
    shot = _shot(ctx.db, shot_id)
    override_warning = _legacy_override_warning(ctx.db, shot, "video", prompt)
    if prompt and override_warning is None:
        shot.video_prompt = prompt
    task = tasks_svc.create_task(
        ctx.db, project_id=project_id or shot.project_id, type=TaskType.GENERATE_VIDEO,
        name=f"生成视频 {shot.code}", shot_id=shot.id,
        payload={"provider": provider, "duration": duration, "fps": fps,
                 "motion": motion, "seed": seed},
        created_by=ctx.actor, commit=False,
    )
    return task, {"shot_id": shot.id,
                  "warnings": [override_warning] if override_warning else []}


@skill(
    name="regenerate_video", category="video", is_async=True,
    description="只重新生成某个镜头的视频，不影响其它镜头（核心能力：单镜头重跑）。",
    tags=("video", "generate", "retry"),
    input_schema={"type": "object", "properties": {
        "shot_id": {"type": "string"}, "provider": {"type": "string"},
        "prompt": {"type": "string"}, "duration": {"type": "number"},
        "motion": {"type": "string"}, "seed": {"type": "integer"},
        "reason": {"type": "string", "description": "重生成原因，会记入 Agent 日志"}},
        "required": ["shot_id"]},
)
def regenerate_video(ctx: SkillContext, *, shot_id: str, provider: str | None = None,
                     prompt: str | None = None, duration: float | None = None,
                     motion: str | None = None, seed: int | None = None,
                     reason: str = ""):
    shot = _shot(ctx.db, shot_id)
    override_warning = _legacy_override_warning(ctx.db, shot, "video", prompt)
    if prompt and override_warning is None:
        shot.video_prompt = prompt
    shot.video_asset_id = None
    shot.video_status = "PENDING"
    shot.status = ShotStatus.PENDING
    shot.retry_count = (shot.retry_count or 0) + 1
    shot.last_error = reason
    task = tasks_svc.create_task(
        ctx.db, project_id=shot.project_id, type=TaskType.GENERATE_VIDEO,
        name=f"重生成视频 {shot.code}", shot_id=shot.id,
        payload={"provider": provider, "duration": duration, "motion": motion,
                 "seed": seed, "regenerate": True, "reason": reason},
        created_by=ctx.actor, commit=False,
    )
    return task, {"shot_id": shot.id,
                  "warnings": [override_warning] if override_warning else []}


@skill(
    name="get_video_generation_status", category="video",
    description="查询视频生成状态：按 task_id / shot_id / project_id 查询。",
    tags=("video", "status", "read"),
    input_schema={"type": "object", "properties": {
        "task_id": {"type": "string"}, "shot_id": {"type": "string"},
        "project_id": {"type": "string"}}},
)
def get_video_generation_status(ctx: SkillContext, *, task_id: str | None = None,
                                shot_id: str | None = None,
                                project_id: str | None = None) -> dict[str, Any]:
    from ..models import Task

    if task_id:
        task = ctx.db.get(Task, task_id)
        if task is None:
            raise SkillError(f"任务不存在: {task_id}", code="NOT_FOUND")
        return {"task": S.serialize_task(task)}
    if shot_id:
        shot = _shot(ctx.db, shot_id)
        return {
            "shot_id": shot_id, "status": shot.video_status, "shot_status": shot.status,
            "asset": S.asset_brief(ctx.db.get(Asset, shot.video_asset_id) if shot.video_asset_id else None),
            "retry_count": shot.retry_count, "last_error": shot.last_error,
        }
    if project_id:
        rows = ctx.db.execute(
            select(Shot).where(Shot.project_id == project_id).order_by(Shot.sequence.asc())
        ).scalars().all()
        tasks = tasks_svc.list_tasks(ctx.db, project_id=project_id,
                                     type=TaskType.GENERATE_VIDEO, limit=100)
        return {
            "shots": [{"shot_id": s.id, "code": s.code, "status": s.video_status,
                       "asset_id": s.video_asset_id, "retry_count": s.retry_count} for s in rows],
            "tasks": [S.serialize_task(t, include_logs=False) for t in tasks],
            "progress": {
                "done": sum(1 for s in rows if s.video_asset_id),
                "total": len(rows),
                "failed": sum(1 for s in rows if s.status == ShotStatus.FAILED),
            },
        }
    raise SkillError("需要提供 task_id / shot_id / project_id 之一", code="BAD_INPUT")


# --------------------------------------------------------------------------- #
# 批量生成（Agent 一步推进）
# --------------------------------------------------------------------------- #
@skill(
    name="generate_all_images", category="image", is_async=True,
    description="为项目中所有缺关键帧的镜头批量提交生成任务。",
    tags=("image", "batch"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "provider": {"type": "string"},
        "workflow_name": {"type": "string", "description": "ComfyUI 工作流模板名，如 qwen_image_scene"},
        "resolution": {"type": "string", "enum": ["720p", "1080p", "2K", "4K"],
                       "description": "分辨率档位，整批统一。由 Provider 能力声明校验"},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1", "4:3", "3:2"]},
        "width": {"type": "integer"}, "height": {"type": "integer"},
        "concurrency": {"type": "integer", "default": 1,
                        "description": "并发数。本地 ComfyUI 串行更稳，建议 1"},
        "only_missing": {"type": "boolean", "default": True},
        "force": {"type": "boolean", "default": False,
                  "description": "True 时忽略已有关键帧强制重生成（切换工作流后全量重跑）"}}, "required": ["project_id"]},
)
def generate_all_images(ctx: SkillContext, *, project_id: str, provider: str | None = None,
                        workflow_name: str | None = None,
                        resolution: str | None = None, aspect_ratio: str | None = None,
                        width: int | None = None, height: int | None = None,
                        concurrency: int = 1,
                        only_missing: bool = True, force: bool = False) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    size = _prepare_image_size(ctx.db, project, provider=provider, resolution=resolution,
                               aspect_ratio=aspect_ratio, width=width, height=height)
    shots = projects_svc.pending_shots(ctx.db, project_id, kind="image") if only_missing else \
        list(ctx.db.execute(select(Shot).where(Shot.project_id == project_id)).scalars())
    if not shots:
        return {"submitted": [], "count": 0, "message": "所有镜头都已有关键帧",
                "size": {"resolution": size["resolution"], "width": size["width"],
                         "height": size["height"]},
                "warnings": size["warnings"]}
    parent = tasks_svc.create_task(
        ctx.db, project_id=project_id, type=TaskType.CUSTOM, name="批量生成关键帧",
        payload={"batch": "images", "count": len(shots)}, created_by=ctx.actor, commit=False,
    )
    ids = []
    for shot in shots:
        task = tasks_svc.create_task(
            ctx.db, project_id=project_id, type=TaskType.GENERATE_IMAGE,
            name=f"生成关键帧 {shot.code}", shot_id=shot.id,
            payload={"provider": provider, "workflow_name": workflow_name, "force": force,
                     "width": size["width"], "height": size["height"],
                     "resolution": size["resolution"], "aspect_ratio": size["aspect_ratio"]},
            parent_task_id=parent.id, created_by=ctx.actor, commit=False,
        )
        ids.append(task.id)
    ctx.db.commit()
    return {"batch_task_id": parent.id, "task_ids": ids, "count": len(ids),
            "size": {k: size[k] for k in ("resolution", "aspect_ratio", "width", "height",
                                          "megapixels", "above_native")},
            "provider": size["provider"], "provider_note": size["provider_note"],
            "warnings": size["warnings"],
            "message": f"已提交 {len(ids)} 个关键帧任务"}


@skill(
    name="list_image_workflows", category="image",
    description="列出可用的 ComfyUI 工作流模板（含用到的模型文件名，便于排查模型是否已下载）。",
    tags=("image", "comfyui", "read"),
    input_schema={"type": "object", "properties": {"kind": {"type": "string",
                  "description": "image / video，不传则列出全部"}}},
)
def list_image_workflows(ctx: SkillContext, *, kind: str | None = None) -> dict[str, Any]:
    from ..workflows import list_templates

    items = [t.to_meta() for t in list_templates(kind)]
    return {"templates": items, "count": len(items),
            "note": "用 workflow_name 指定模板；模板文件放 backend/app/workflows/templates/"} 



@skill(
    name="generate_all_videos", category="video", is_async=True,
    description="为项目中所有缺视频的镜头批量提交生成任务。",
    tags=("video", "batch"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "provider": {"type": "string"},
        "only_missing": {"type": "boolean", "default": True}}, "required": ["project_id"]},
)
def generate_all_videos(ctx: SkillContext, *, project_id: str, provider: str | None = None,
                        only_missing: bool = True) -> dict[str, Any]:
    _project(ctx.db, project_id)
    shots = projects_svc.pending_shots(ctx.db, project_id, kind="video") if only_missing else \
        list(ctx.db.execute(select(Shot).where(Shot.project_id == project_id)).scalars())
    if not shots:
        return {"submitted": [], "count": 0, "message": "所有镜头都已有视频"}
    parent = tasks_svc.create_task(
        ctx.db, project_id=project_id, type=TaskType.CUSTOM, name="批量生成视频",
        payload={"batch": "videos", "count": len(shots)}, created_by=ctx.actor, commit=False,
    )
    ids = []
    for shot in shots:
        task = tasks_svc.create_task(
            ctx.db, project_id=project_id, type=TaskType.GENERATE_VIDEO,
            name=f"生成视频 {shot.code}", shot_id=shot.id, payload={"provider": provider},
            parent_task_id=parent.id, created_by=ctx.actor, commit=False,
        )
        ids.append(task.id)
    ctx.db.commit()
    return {"batch_task_id": parent.id, "task_ids": ids, "count": len(ids),
            "message": f"已提交 {len(ids)} 个视频任务"}


# --------------------------------------------------------------------------- #
# Voice / Music / SFX / Subtitle
# --------------------------------------------------------------------------- #
@skill(
    name="generate_voice", category="audio", is_async=True,
    description="生成配音。指定 shot_id 生成单条；不指定则批量为所有缺配音的镜头生成。",
    tags=("audio", "tts", "generate"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "shot_id": {"type": "string"},
        "text": {"type": "string"}, "voice": {"type": "string"},
        "speaker": {"type": "string", "description": "Qwen3-TTS 音色名"},
        "instruct": {"type": "string", "description": "情感指令"},
        "force": {"type": "boolean", "description": "项目级批量时：true=全部镜头重做，false=只补缺配音的"},
        "rate": {"type": "integer"}, "provider": {"type": "string"}},
        "required": ["project_id"]},
)
def generate_voice(ctx: SkillContext, *, project_id: str, shot_id: str | None = None,
                   text: str | None = None, voice: str | None = None, rate: int | None = None,
                   provider: str | None = None, speaker: str | None = None,
                   instruct: str | None = None, force: bool = False):
    _project(ctx.db, project_id)
    if shot_id:
        shot = _shot(ctx.db, shot_id)
        if text:
            shot.voice_script = text
            shot.subtitle_text = shot.subtitle_text or text
        return tasks_svc.create_task(
            ctx.db, project_id=project_id, type=TaskType.GENERATE_VOICE,
            name=f"生成配音 {shot.code}", shot_id=shot.id,
            payload={"text": text, "voice": voice, "rate": rate, "provider": provider,
                     "speaker": speaker, "instruct": instruct},
            created_by=ctx.actor, commit=False,
        )
    return tasks_svc.create_task(
        ctx.db, project_id=project_id, type=TaskType.GENERATE_VOICE, name="批量生成配音",
        payload={"voice": voice, "rate": rate, "provider": provider,
                 "speaker": speaker, "instruct": instruct, "force": bool(force)},
        created_by=ctx.actor, commit=False,
    )


@skill(
    name="regenerate_voice", category="audio", is_async=True,
    description="重新生成某个镜头的配音。",
    tags=("audio", "tts", "retry"),
    input_schema={"type": "object", "properties": {
        "shot_id": {"type": "string"}, "text": {"type": "string"},
        "voice": {"type": "string", "description": "音色（Qwen3-TTS speaker 名）"},
        "speaker": {"type": "string", "description": "同 voice，语义更明确；两者都给时 speaker 优先"},
        "instruct": {"type": "string", "description": "情感指令（自然语言，控制语气/情绪）"},
        "voice_engine": {"type": "string", "description": "tts 引擎名，如 qwen3tts / local；留空用项目配置"},
        "rate": {"type": "integer"}}, "required": ["shot_id"]},
)
def regenerate_voice(ctx: SkillContext, *, shot_id: str, text: str | None = None,
                     voice: str | None = None, rate: int | None = None,
                     speaker: str | None = None, instruct: str | None = None,
                     voice_engine: str | None = None):
    shot = _shot(ctx.db, shot_id)
    if text is not None:
        shot.voice_script = text
    # 音色 / 情感指令写回镜头，让「重生成一次」与「项目级重跑」结果一致
    if speaker or voice:
        shot.voice_speaker = speaker or voice or ""
    if instruct is not None:
        shot.voice_instruct = instruct
    shot.voice_asset_id = None
    shot.voice_status = "PENDING"
    return tasks_svc.create_task(
        ctx.db, project_id=shot.project_id, type=TaskType.GENERATE_VOICE,
        name=f"重生成配音 {shot.code}", shot_id=shot.id,
        payload={"text": text, "voice": speaker or voice, "rate": rate,
                 "speaker": speaker or voice, "instruct": instruct,
                 "provider": voice_engine, "regenerate": True},
        created_by=ctx.actor, commit=False,
    )


@skill(
    name="list_voices", category="audio",
    description="列出本机可用的 TTS 音色。",
    tags=("audio", "tts", "read"),
    input_schema={"type": "object", "properties": {"limit": {"type": "integer", "default": 80}}},
)
def list_voices(ctx: SkillContext, *, limit: int = 80) -> dict[str, Any]:
    from ..providers.audio_providers import available_voices

    voices = available_voices()
    zh = [v for v in voices if v in ("Tingting", "Sinji", "Meijia", "Samantha")]
    return {"voices": voices[:limit], "count": len(voices), "cjk_voices": zh}


@skill(
    name="generate_music", category="audio", is_async=True,
    description="为项目生成背景音乐。",
    tags=("audio", "music", "generate"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "prompt": {"type": "string"},
        "duration": {"type": "number"}, "mood": {"type": "string",
        "description": "calm / warm / happy / tense / epic / tech"},
        "style": {"type": "string", "description": "BGM 曲风：pop（流行律动 108BPM）/ warm（温暖钢琴 76BPM）"},
        "peak_db": {"type": "number", "description": "输出峰值 dBFS，默认 -9（合成时还会再乘 0.16）"},
        "provider": {"type": "string"}}, "required": ["project_id"]},
)
def generate_music(ctx: SkillContext, *, project_id: str, prompt: str = "",
                   duration: float | None = None, mood: str = "calm",
                   provider: str | None = None, style: str = "pop",
                   peak_db: float | None = None):
    project = _project(ctx.db, project_id)
    return tasks_svc.create_task(
        ctx.db, project_id=project_id, type=TaskType.GENERATE_MUSIC,
        name=f"生成背景音乐（{style}）",
        payload={"prompt": prompt, "mood": mood, "style": style, "peak_db": peak_db,
                 "duration": duration or project.target_duration,
                 "provider": provider},
        created_by=ctx.actor, commit=False,
    )


@skill(
    name="generate_sfx", category="audio", is_async=True,
    description="生成音效（转场 whoosh / 提示 ding / 点击 click）。",
    tags=("audio", "sfx", "generate"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "prompt": {"type": "string"},
        "duration": {"type": "number"}, "shot_id": {"type": "string"}},
        "required": ["project_id"]},
)
def generate_sfx(ctx: SkillContext, *, project_id: str, prompt: str = "whoosh",
                 duration: float = 1.5, shot_id: str | None = None):
    _project(ctx.db, project_id)
    return tasks_svc.create_task(
        ctx.db, project_id=project_id, type=TaskType.GENERATE_SFX,
        name=f"生成音效：{prompt}", shot_id=shot_id,
        payload={"prompt": prompt, "duration": duration}, created_by=ctx.actor, commit=False,
    )


@skill(
    name="generate_subtitle", category="subtitle", is_async=True,
    description="按镜头时间轴与旁白生成全片字幕（SRT/ASS/VTT）。",
    tags=("subtitle", "generate"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "format": {"type": "string", "default": "srt"},
        "provider": {"type": "string"}}, "required": ["project_id"]},
)
def generate_subtitle(ctx: SkillContext, *, project_id: str, format: str = "srt",
                      provider: str | None = None):
    _project(ctx.db, project_id)
    return tasks_svc.create_task(
        ctx.db, project_id=project_id, type=TaskType.GENERATE_SUBTITLE,
        name="生成全片字幕", payload={"format": format, "provider": provider},
        created_by=ctx.actor, commit=False,
    )


# --------------------------------------------------------------------------- #
# Task
# --------------------------------------------------------------------------- #
@skill(
    name="get_task_status", category="task",
    description="查询任务状态、进度、结果与错误信息。长任务请轮询本 Skill，不要阻塞。",
    tags=("task", "status", "read"),
    input_schema={"type": "object", "properties": {
        "task_id": {"type": "string"}, "include_logs": {"type": "boolean", "default": True}},
        "required": ["task_id"]},
    output_schema={"type": "object", "properties": {
        "task_id": {"type": "string"}, "status": {"type": "string"},
        "progress": {"type": "integer"}, "result": {"type": "object"}, "error": {"type": "string"}}},
    examples=({"task_id": "task_xxx"},),
)
def get_task_status(ctx: SkillContext, *, task_id: str, include_logs: bool = True) -> dict[str, Any]:
    from ..models import Task

    task = ctx.db.get(Task, task_id)
    if task is None:
        raise SkillError(f"任务不存在: {task_id}", code="NOT_FOUND")
    data = S.serialize_task(task, include_logs=include_logs)
    data["task_id"] = task.id
    data["terminal"] = task.status in TaskStatus.TERMINAL
    return data


@skill(
    name="list_tasks", category="task",
    description="列出任务（可按项目 / 状态 / 类型过滤），用于观察整体执行情况。",
    tags=("task", "read"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "status": {"type": "string"},
        "type": {"type": "string"}, "shot_id": {"type": "string"},
        "limit": {"type": "integer", "default": 100}, "offset": {"type": "integer", "default": 0}}},
)
def list_tasks_skill(ctx: SkillContext, *, project_id: str | None = None, status: str | None = None,
                     type: str | None = None, shot_id: str | None = None,
                     limit: int = 100, offset: int = 0) -> dict[str, Any]:
    rows = tasks_svc.list_tasks(ctx.db, project_id=project_id, status=status, type=type,
                                shot_id=shot_id, limit=limit, offset=offset)
    stats = tasks_svc.task_stats(ctx.db, project_id) if project_id else {}
    return {"tasks": [S.serialize_task(t, include_logs=False) for t in rows],
            "count": len(rows), "stats": stats}


@skill(
    name="cancel_task", category="task",
    description="取消一个未完成的任务。",
    tags=("task", "write"),
    input_schema={"type": "object", "properties": {"task_id": {"type": "string"}},
                  "required": ["task_id"]},
)
def cancel_task(ctx: SkillContext, *, task_id: str) -> dict[str, Any]:
    ok, message = tasks_svc.cancel_task(ctx.db, task_id)
    return {"ok": ok, "message": message, "task_id": task_id}


@skill(
    name="retry_task", category="task",
    description="重新入队一个失败或已完成的任务。",
    tags=("task", "retry"),
    input_schema={"type": "object", "properties": {
        "task_id": {"type": "string"}, "reset_attempts": {"type": "boolean", "default": False}},
        "required": ["task_id"]},
)
def retry_task(ctx: SkillContext, *, task_id: str, reset_attempts: bool = False) -> dict[str, Any]:
    ok, message = tasks_svc.retry_task(ctx.db, task_id, reset_attempts=reset_attempts)
    return {"ok": ok, "message": message, "task_id": task_id}


@skill(
    name="retry_failed_tasks", category="task",
    description="批量重试项目内所有失败任务。",
    tags=("task", "retry"),
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}},
                  "required": ["project_id"]},
)
def retry_failed_tasks(ctx: SkillContext, *, project_id: str) -> dict[str, Any]:
    rows = tasks_svc.list_tasks(ctx.db, project_id=project_id, status=TaskStatus.FAILED, limit=200)
    requeued = []
    for task in rows:
        ok, _ = tasks_svc.retry_task(ctx.db, task.id, reset_attempts=True)
        if ok:
            requeued.append(task.id)
    return {"requeued": requeued, "count": len(requeued)}


# --------------------------------------------------------------------------- #
# ComfyUI 出图配置（项目级）
# --------------------------------------------------------------------------- #
@skill(
    name="set_image_provider", category="image",
    description=(
        "为项目固化出图配置：把图片 Provider 与 ComfyUI 工作流模板写到项目上，"
        "之后该项目所有关键帧 / 角色参考图都自动使用，无需每次传参。"
        "典型用法：切到本地 ComfyUI + Qwen-Image 出人物图与场景图。"
    ),
    tags=("image", "comfyui", "config", "write"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"},
        "image_provider": {"type": "string", "description": "local / comfyui / cloud"},
        "scene_workflow_name": {"type": "string", "description": "场景图工作流模板名"},
        "character_workflow_name": {"type": "string", "description": "人物图工作流模板名"},
        "video_provider": {"type": "string", "description": "镜头视频 Provider：local / comfyui / cloud"},
        "video_workflow_name": {"type": "string", "description": "镜头视频工作流模板名（如 minimax_h3_i2v）"},
        "scene_prompt_prefix": {"type": "string", "description": "场景图 prompt 统一前缀（画风锚定）"},
        "character_prompt_prefix": {"type": "string", "description": "人物图 prompt 统一前缀"},
        "negative_prompt": {"type": "string", "description": "统一负向词"},
    }, "required": ["project_id"]},
)
def set_image_provider(ctx: SkillContext, *, project_id: str, image_provider: str | None = None,
                       scene_workflow_name: str | None = None,
                       character_workflow_name: str | None = None,
                       video_provider: str | None = None,
                       video_workflow_name: str | None = None,
                       scene_prompt_prefix: str | None = None,
                       character_prompt_prefix: str | None = None,
                       negative_prompt: str | None = None) -> dict[str, Any]:
    project = _project(ctx.db, project_id)

    # 校验模板名，避免写进去一个拼错的名字、跑到出图时才发现
    if scene_workflow_name or character_workflow_name or video_workflow_name:
        from ..workflows import has_template

        for label, name in (("scene_workflow_name", scene_workflow_name),
                            ("character_workflow_name", character_workflow_name),
                            ("video_workflow_name", video_workflow_name)):
            if name and not has_template(name):
                raise SkillError(f"工作流模板不存在：{name}（{label}）", code="BAD_INPUT")

    extra = dict(project.extra or {})
    changes: dict[str, Any] = {}
    for key, value in (("image_provider", image_provider),
                       ("scene_workflow_name", scene_workflow_name),
                       ("character_workflow_name", character_workflow_name),
                       ("video_provider", video_provider),
                       ("video_workflow_name", video_workflow_name),
                       ("scene_prompt_prefix", scene_prompt_prefix),
                       ("character_prompt_prefix", character_prompt_prefix),
                       ("negative_prompt", negative_prompt)):
        if value is not None:
            extra[key] = value
            changes[key] = value
    project.extra = extra
    ctx.db.commit()
    return {"project_id": project_id, "image_config": extra, "changed": changes}
