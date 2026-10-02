"""任务处理器：把 Skill / API 提交的任务，转成真实执行动作。

每个 handler 都是幂等的：重复执行同一个任务只会产生新版本资产，
并通过 shot.xxx_asset_id 指向最新产物，不会破坏已有数据。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..core.constants import (
    AssetType, SceneStatus, ShotStatus, TaskType,
)
from ..models import Asset, Character, Project, Scene, Shot
from ..providers import register_all, registry
from ..providers import local_engine as engine
from ..services import assets as assets_svc
from ..services import planner, projects as projects_svc, quality as quality_svc
from ..services import tasks as tasks_svc
from ..services import workflow as workflow_svc
from .queue import TaskContext, registered_handlers, task_handler


# --------------------------------------------------------------------------- #
# 公共辅助
# --------------------------------------------------------------------------- #
def _project(ctx: TaskContext) -> Project:
    project = ctx.db.get(Project, ctx.task.project_id)
    if project is None:
        raise ValueError(f"项目不存在: {ctx.task.project_id}")
    workflow_svc.ensure_workflow(ctx.db, project)
    return project


def _shot(ctx: TaskContext, shot_id: str | None = None) -> Shot:
    sid = shot_id or ctx.task.shot_id or (ctx.task.payload or {}).get("shot_id")
    if not sid:
        raise ValueError("缺少 shot_id")
    shot = ctx.db.get(Shot, sid)
    if shot is None:
        raise ValueError(f"镜头不存在: {sid}")
    return shot


def _provider(kind: str, name: str | None):
    register_all()
    return registry.get(kind, name or None)


def _refresh_scene(db, scene_id: str) -> None:
    scene = db.get(Scene, scene_id)
    if scene is None:
        return
    shots = list(db.query(Shot).filter(Shot.scene_id == scene_id).all())
    if not shots:
        return
    if any(s.status == ShotStatus.FAILED for s in shots):
        scene.status = SceneStatus.FAILED
    elif all(s.status in (ShotStatus.READY, ShotStatus.VIDEO_READY) for s in shots):
        scene.status = SceneStatus.READY
    elif any(s.image_asset_id or s.video_asset_id for s in shots):
        scene.status = SceneStatus.GENERATING


def _maybe_complete_step(db, project: Project, kind: str) -> None:
    """某个阶段的所有产物齐了，就把工作流步骤标记为完成。"""
    column = {
        "image": Shot.image_asset_id,
        "video": Shot.video_asset_id,
        "voice": Shot.voice_asset_id,
        "subtitle": Shot.subtitle_asset_id,
    }.get(kind)
    if column is None:
        return
    total = db.query(Shot).filter(Shot.project_id == project.id).count()
    if not total:
        return
    done = db.query(Shot).filter(Shot.project_id == project.id, column.isnot(None)).count()
    if done >= total:
        workflow_svc.complete_step(db, project, kind, output={"shots": total}, commit=False)
        projects_svc.refresh_progress(db, project, commit=False)


def _ensure_shot_image(ctx: TaskContext, shot: Shot, project: Project) -> Asset:
    """确保镜头有关键帧：没有就先按同一套参数生成。

    payload.force=True 时忽略已有关键帧强制重生成（如切换人物一致性工作流后全量重跑）。
    """
    if not (ctx.task.payload or {}).get("force") and shot.image_asset_id:
        existing = ctx.db.get(Asset, shot.image_asset_id)
        if existing and Path(existing.file_path).exists():
            return existing

    payload = ctx.task.payload or {}
    provider_name = payload.get("image_provider") or settings.default_provider_image
    provider = _provider("image", provider_name)
    ctx.event("image.started", f"开始生成 Shot {shot.code} 关键帧")
    ctx.progress(10, "生成关键帧")

    # 项目级画风锚定：set_image_provider 写进 project.extra 的前缀/负向词在此生效。
    # 没有它的话，22 个镜头各自成图、色调与质感会明显跳。
    proj_extra = project.extra if isinstance(project.extra, dict) else {}
    prefix = (proj_extra.get("scene_prompt_prefix") or "").strip()
    prompt = shot.image_prompt or shot.description or shot.code
    if prefix and not prompt.startswith(prefix):
        prompt = f"{prefix}, {prompt}"
    negative = shot.negative_prompt or proj_extra.get("negative_prompt") or ""
    # 人物一致性：镜头若绑定角色，取该角色最新的定妆图作为参考图
    # （IPAdapter / Qwen-Image-Edit 类工作流用 {{reference_image}} 占位符接收）
    reference_image = None
    char_ids = list(shot.character_ids or [])
    if char_ids:
        for cid in char_ids:
            char = ctx.db.get(Character, cid) if cid else None
            ref_asset_id = getattr(char, "reference_asset_id", None) if char else None
            if ref_asset_id:
                ref_asset = ctx.db.get(Asset, ref_asset_id)
                if ref_asset and Path(ref_asset.file_path).exists():
                    reference_image = ref_asset.file_path
                    break
    result = provider.generate(
        prompt=prompt, negative_prompt=negative,
        width=project.width, height=project.height,
        seed=payload.get("seed"),
        reference_image=reference_image,
        parameters={
            "title": shot.extra.get("point") if isinstance(shot.extra, dict) else "",
            "body": shot.description,
            "badge": shot.code,
            "style_tag": shot.visual_style or project.style,
            "shot_code": shot.code,
            "workdir": str(_workdir(ctx, "images")),
            # ComfyUI：把工作流模板名透传到 Provider（缺了它 comfyui 通道不可用）
            "workflow_name": _resolve_workflow_name(payload, project, kind="scene"),
            "workflow_json": payload.get("workflow_json"),
            "timeout": settings.comfyui_timeout,
        },
    )
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=AssetType.IMAGE,
        name=f"{shot.code} 关键帧", scene_id=shot.scene_id, shot_id=shot.id,
        task_id=ctx.task.id, extra={"role": "keyframe"},
    )
    shot.image_asset_id = asset.id
    shot.image_status = "READY"
    if shot.status in (ShotStatus.PENDING, ShotStatus.QUEUED, ShotStatus.GENERATING):
        shot.status = ShotStatus.IMAGE_READY
    _refresh_scene(ctx.db, shot.scene_id)
    ctx.db.commit()
    ctx.event("image.finished", f"Shot {shot.code} 关键帧生成完成", asset_id=asset.id, url=asset.url)
    _maybe_complete_step(ctx.db, project, "image")
    return asset


def _workdir(ctx: TaskContext, sub: str) -> Path:
    root = settings.storage_path / "temp" / ctx.task.project_id / sub
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_workflow_name(payload: dict[str, Any], project: Project, *, kind: str) -> str | None:
    """决定本次出图用哪个 ComfyUI 工作流模板。

    优先级：
    1. 任务 payload 里显式指定（单次调用覆盖）
    2. 项目 extra 里固化（`set_shot_image_workflow` 写入，整项目统一风格）
    3. settings 默认（人物图 / 场景图各有默认模板）

    kind="character" 取人物图模板，kind="scene" 取场景图模板。
    """
    explicit = payload.get("workflow_name")
    if explicit:
        return explicit
    project_extra = project.extra if isinstance(project.extra, dict) else {}
    key = {"character": "character_workflow_name",
           "scene": "scene_workflow_name",
           "video": "video_workflow_name"}.get(kind)
    from_project = project_extra.get(key) if key else None
    if from_project:
        return from_project
    if kind == "character":
        return settings.comfyui_workflow_character
    if kind == "scene":
        return settings.comfyui_workflow_scene
    return None


def _latest_asset(db, project_id: str, asset_type: str) -> Asset | None:
    return db.query(Asset).filter(
        Asset.project_id == project_id, Asset.type == asset_type
    ).order_by(Asset.created_at.desc()).first()


def _ordered_shot_videos(db, project_id: str) -> list[tuple[Shot, Asset]]:
    """按镜头顺序取出可用的镜头视频。

    若某镜头已有增强版（enhanced_video_asset_id），优先采用增强版，
    这样合成成片会自然带上画质增强结果。
    """
    shots = db.query(Shot).filter(Shot.project_id == project_id).order_by(Shot.sequence.asc()).all()
    out: list[tuple[Shot, Asset]] = []
    for shot in shots:
        chosen: Asset | None = None
        if shot.enhanced_video_asset_id:
            enhanced = db.get(Asset, shot.enhanced_video_asset_id)
            if enhanced and Path(enhanced.file_path).exists():
                chosen = enhanced
        if chosen is None and shot.video_asset_id:
            base = db.get(Asset, shot.video_asset_id)
            if base and Path(base.file_path).exists():
                chosen = base
        if chosen is not None:
            out.append((shot, chosen))
    return out


# --------------------------------------------------------------------------- #
# 图像 / 角色
# --------------------------------------------------------------------------- #
@task_handler(TaskType.GENERATE_IMAGE)
def handle_generate_image(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    shot = _shot(ctx)
    asset = _ensure_shot_image(ctx, shot, project)
    return {
        "asset_id": asset.id, "url": asset.url, "shot_id": shot.id,
        "provider": asset.provider, "width": asset.width, "height": asset.height,
    }


@task_handler(TaskType.GENERATE_CHARACTER_REFERENCE)
def handle_character_reference(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    char_id = ctx.task.character_id or (ctx.task.payload or {}).get("character_id")
    char = ctx.db.get(Character, char_id)
    if char is None:
        raise ValueError(f"角色不存在: {char_id}")
    payload = ctx.task.payload or {}
    provider = _provider("image", payload.get("provider") or settings.default_provider_image)

    ctx.event("character.started", f"开始生成 Character: {char.name}")
    ctx.progress(15, f"生成角色参考图：{char.name}")
    proj_extra = project.extra if isinstance(project.extra, dict) else {}
    prompt = char.reference_prompt or (
        f"{project.style}，角色设定图：{char.name}，{char.appearance}，正面半身，干净扁平配色"
    )
    prefix = (proj_extra.get("character_prompt_prefix") or "").strip()
    if prefix and not prompt.startswith(prefix):
        prompt = f"{prefix}, {prompt}"
    result = provider.generate(
        prompt=prompt,
        negative_prompt=char.negative_prompt or proj_extra.get("negative_prompt") or "",
        width=project.width // 2 * 1, height=project.height,
        seed=payload.get("seed"),
        parameters={
            "title": char.name, "body": char.appearance, "badge": "CHARACTER",
            "style_tag": project.style, "shot_code": char.name,
            "workdir": str(_workdir(ctx, "characters")),
            # ComfyUI：人物设定图走 character 模板
            "workflow_name": _resolve_workflow_name(payload, project, kind="character"),
            "workflow_json": payload.get("workflow_json"),
            "timeout": settings.comfyui_timeout,
        },
    )
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=AssetType.CHARACTER,
        name=f"{char.name} 参考图", character_id=char.id, task_id=ctx.task.id,
        extra={"role": "reference"},
    )
    char.reference_asset_id = asset.id
    char.status = "READY"
    char.provider = result.provider
    char.model = result.model
    char.parameters = result.parameters
    ctx.db.commit()
    ctx.event("character.finished", f"Character 完成：{char.name}", asset_id=asset.id, url=asset.url)
    return {"character_id": char.id, "asset_id": asset.id, "url": asset.url}


# --------------------------------------------------------------------------- #
# 视频
# --------------------------------------------------------------------------- #
@task_handler(TaskType.GENERATE_VIDEO)
def handle_generate_video(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    shot = _shot(ctx)
    payload = ctx.task.payload or {}

    frame = _ensure_shot_image(ctx, shot, project)
    ctx.event("video.started", f"开始生成 Shot {shot.code} 视频")

    provider = _provider("video", payload.get("provider") or settings.default_provider_video)
    ctx.progress(20, f"渲染 {shot.code} 视频")
    result = provider.generate(
        prompt=shot.video_prompt or shot.description or shot.code,
        image_path=frame.file_path,
        width=project.width, height=project.height,
        duration=float(payload.get("duration") or shot.duration or settings.default_shot_duration),
        fps=int(payload.get("fps") or project.fps),
        seed=payload.get("seed"),
        parameters={
            "motion": payload.get("motion") or "auto",
            "camera": shot.camera,
            "title": shot.extra.get("point") if isinstance(shot.extra, dict) else "",
            "subtitle": shot.voice_script,
            "badge": shot.code,
            "style_tag": shot.visual_style or project.style,
            "shot_code": shot.code,
            "workdir": str(_workdir(ctx, "videos")),
            # ComfyUI 视频工作流模板（如 minimax_h3_i2v）：单次 > 项目 extra > 无
            "workflow_name": _resolve_workflow_name(payload, project, kind="video"),
            "workflow_json": payload.get("workflow_json"),
            # H3 一段 5~15 秒要几分钟，默认超时给足
            "timeout": payload.get("timeout") or 3600,
        },
        progress_cb=lambda pct, msg: ctx.progress(20 + int(pct * 0.75), msg),
    )
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=AssetType.VIDEO,
        name=f"{shot.code} 视频", scene_id=shot.scene_id, shot_id=shot.id,
        task_id=ctx.task.id, parent_asset_id=frame.id, extra={"role": "shot"},
    )
    shot.video_asset_id = asset.id
    shot.video_status = "READY"
    shot.last_error = ""
    if not shot.voice_script or shot.voice_asset_id:
        shot.status = ShotStatus.READY
    else:
        shot.status = ShotStatus.VIDEO_READY
    _refresh_scene(ctx.db, shot.scene_id)
    ctx.db.commit()
    ctx.event("video.finished", f"Shot {shot.code} 视频生成完成",
              asset_id=asset.id, url=asset.url, duration=asset.duration)
    _maybe_complete_step(ctx.db, project, "video")
    return {
        "asset_id": asset.id, "url": asset.url, "shot_id": shot.id,
        "duration": asset.duration, "width": asset.width, "height": asset.height,
        "provider": asset.provider, "source_image_asset_id": frame.id,
    }


# --------------------------------------------------------------------------- #
# 音频 / 字幕
# --------------------------------------------------------------------------- #
@task_handler(TaskType.GENERATE_VOICE)
def handle_generate_voice(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    payload = ctx.task.payload or {}
    proj_extra = project.extra if isinstance(project.extra, dict) else {}
    # 引擎优先级：本次调用 > 项目 extra 的 voice_engine > 全局默认
    provider = _provider("tts", payload.get("provider") or proj_extra.get("voice_engine")
                         or settings.default_provider_tts)

    if ctx.task.shot_id:
        shot = _shot(ctx)
        text = payload.get("text") or shot.voice_script or shot.description
        if not text.strip():
            raise ValueError(f"Shot {shot.code} 没有旁白文本")
        # 音色与情感指令：本次调用 > 镜头自身字段 > provider 默认
        speaker = str(payload.get("speaker") or shot.voice_speaker or "")
        instruct = payload.get("instruct")
        if instruct is None:
            instruct = shot.voice_instruct or ""
        ctx.event("voice.started", f"开始生成配音 Shot {shot.code}")
        ctx.progress(20, f"合成旁白：{shot.code}")
        result = provider.synthesize(
            text=text, voice=speaker or payload.get("voice") or "",
            rate=int(payload.get("rate") or settings.tts_rate),
            parameters={
                "workdir": str(_workdir(ctx, "voices")),
                "speaker": speaker, "instruct": instruct,
                **payload.get("extra", {}),
            },
        )
        asset = assets_svc.ingest_result(
            ctx.db, project_id=project.id, result=result, asset_type=AssetType.VOICE,
            name=f"{shot.code} 旁白", scene_id=shot.scene_id, shot_id=shot.id,
            task_id=ctx.task.id,
            extra={"role": "shot_voice", "voice": speaker or payload.get("voice"),
                   "speaker": speaker, "instruct": instruct},
        )
        shot.voice_asset_id = asset.id
        shot.voice_status = "READY"
        if shot.video_asset_id:
            shot.status = ShotStatus.READY
        ctx.db.commit()
        ctx.event("voice.finished", f"Shot {shot.code} 配音完成",
                  asset_id=asset.id, url=asset.url, duration=asset.duration)
        _maybe_complete_step(ctx.db, project, "voice")
        return {"asset_id": asset.id, "url": asset.url, "duration": asset.duration,
                "shot_id": shot.id, "speaker": speaker, "instruct": instruct}

    # 项目级：force=True 则全部镜头重做（调音台改完音色/情感指令后一键重跑），
    # 否则只补缺配音的镜头。
    if payload.get("force"):
        shots = list(ctx.db.scalars(
            select(Shot).where(Shot.project_id == project.id).order_by(Shot.sequence)
        ))
    else:
        shots = projects_svc.pending_shots(ctx.db, project.id, kind="voice")
    created = [tasks_svc.create_task(
        ctx.db, project_id=project.id, type=TaskType.GENERATE_VOICE,
        name=f"配音 {s.code}", shot_id=s.id,
        payload={"provider": payload.get("provider"),
                 "voice": payload.get("voice") or s.voice_speaker,
                 "speaker": payload.get("speaker") or s.voice_speaker,
                 "instruct": payload.get("instruct") if payload.get("instruct") is not None
                             else s.voice_instruct},
        parent_task_id=ctx.task.id, created_by=ctx.task.created_by, commit=False,
    ).id for s in shots]
    ctx.db.commit()
    return {"submitted": created, "count": len(created)}


@task_handler(TaskType.GENERATE_MUSIC)
def handle_generate_music(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    payload = ctx.task.payload or {}
    provider = _provider("music", payload.get("provider") or settings.default_provider_music)
    duration = float(payload.get("duration") or project.target_duration or 60)
    ctx.progress(20, "生成背景音乐")
    result = provider.generate_music(
        prompt=payload.get("prompt") or "",
        duration=duration,
        mood=payload.get("mood") or "calm",
        parameters={"workdir": str(_workdir(ctx, "music")),
                    "style": payload.get("style") or "pop",
                    "peak_db": payload.get("peak_db"),
                    **payload.get("extra", {})},
    )
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=AssetType.MUSIC,
        name=payload.get("name") or "背景音乐", task_id=ctx.task.id, extra={"role": "bgm"},
    )
    ctx.db.commit()
    ctx.event("music.finished", "背景音乐生成完成", asset_id=asset.id, url=asset.url,
              duration=asset.duration)
    workflow_svc.complete_step(ctx.db, project, "music", output={"asset_id": asset.id}, commit=False)
    projects_svc.refresh_progress(ctx.db, project, commit=False)
    ctx.db.commit()
    return {"asset_id": asset.id, "url": asset.url, "duration": asset.duration}


@task_handler(TaskType.GENERATE_SFX)
def handle_generate_sfx(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    payload = ctx.task.payload or {}
    provider = _provider("sfx", payload.get("provider") or settings.default_provider_sfx)
    ctx.progress(20, "生成音效")
    result = provider.generate_sfx(
        prompt=payload.get("prompt") or "whoosh",
        duration=float(payload.get("duration") or 1.5),
        parameters={"workdir": str(_workdir(ctx, "sfx"))},
    )
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=AssetType.SFX,
        name=payload.get("name") or "转场音效", task_id=ctx.task.id,
        shot_id=ctx.task.shot_id, extra={"role": "sfx"},
    )
    ctx.db.commit()
    ctx.event("sfx.finished", "音效生成完成", asset_id=asset.id, url=asset.url)
    return {"asset_id": asset.id, "url": asset.url, "duration": asset.duration}


@task_handler(TaskType.GENERATE_SUBTITLE)
def handle_generate_subtitle(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    payload = ctx.task.payload or {}
    provider = _provider("subtitle", payload.get("provider") or settings.default_provider_subtitle)

    shots = ctx.db.query(Shot).filter(Shot.project_id == project.id).order_by(Shot.sequence.asc()).all()
    if not shots:
        raise ValueError("项目还没有镜头，无法生成字幕")

    ctx.progress(20, "汇总时间轴")
    segments: list[dict[str, Any]] = []
    cursor = 0.0
    for idx, shot in enumerate(shots, start=1):
        text = shot.subtitle_text or shot.voice_script or ""
        duration = float(shot.duration or settings.default_shot_duration)
        if shot.voice_asset_id:
            voice = ctx.db.get(Asset, shot.voice_asset_id)
            if voice and voice.duration:
                duration = max(duration, float(voice.duration))
        segments.append({
            "index": idx, "start": round(cursor, 3), "end": round(cursor + duration, 3),
            "text": text.strip(), "shot_id": shot.id, "code": shot.code,
        })
        cursor += duration

    result = provider.generate(
        segments=segments, language=project.language,
        fmt=payload.get("format") or "srt",
        parameters={
            "workdir": str(_workdir(ctx, "subtitles")),
            "width": project.width, "height": project.height,
        },
    )
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=AssetType.SUBTITLE,
        name="全片字幕", task_id=ctx.task.id,
        extra={"role": "full", "segment_count": len(segments), "timeline": segments},
    )
    for shot in shots:
        shot.subtitle_asset_id = asset.id
        shot.subtitle_status = "READY"
    ctx.db.commit()
    ctx.event("subtitle.finished", f"字幕生成完成（{len(segments)} 条）",
              asset_id=asset.id, url=asset.url)
    _maybe_complete_step(ctx.db, project, "subtitle")
    return {
        "asset_id": asset.id, "url": asset.url, "segment_count": len(segments),
        "duration": asset.duration, "timeline": segments,
    }


# --------------------------------------------------------------------------- #
# 视频处理 / 合成
# --------------------------------------------------------------------------- #
@task_handler(TaskType.MERGE_VIDEO)
def handle_merge_video(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    pairs = _ordered_shot_videos(ctx.db, project.id)
    if not pairs:
        raise ValueError("没有任何镜头视频可以拼接")
    ctx.progress(10, f"拼接 {len(pairs)} 个镜头")
    processing = _provider("processing", "ffmpeg")
    result = processing.run(
        operation="merge_video", inputs=[a.file_path for _, a in pairs],
        output_name=f"{project.id}_merged",
        params={"workdir": str(_workdir(ctx, "videos"))},
        progress_cb=lambda pct, msg: ctx.progress(10 + int(pct * 0.85), msg),
    )
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=AssetType.VIDEO,
        name="拼接粗剪", task_id=ctx.task.id,
        extra={"role": "merged", "shot_count": len(pairs)},
    )
    ctx.db.commit()
    ctx.event("merge.finished", f"镜头拼接完成（{len(pairs)} 个镜头）",
              asset_id=asset.id, url=asset.url, duration=asset.duration)
    return {"asset_id": asset.id, "url": asset.url, "duration": asset.duration,
            "shot_count": len(pairs)}


@task_handler(TaskType.ENHANCE_VIDEO)
@task_handler(TaskType.UPSCALE_VIDEO)
@task_handler(TaskType.INTERPOLATE_VIDEO)
def handle_enhance_video(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    payload = ctx.task.payload or {}
    provider = _provider("enhance", payload.get("provider") or settings.default_provider_enhance)

    source_asset: Asset | None = None
    if payload.get("asset_id"):
        source_asset = ctx.db.get(Asset, payload["asset_id"])
    elif ctx.task.shot_id:
        shot = _shot(ctx)
        source_asset = ctx.db.get(Asset, shot.video_asset_id) if shot.video_asset_id else None
    if source_asset is None:
        # 项目级增强：优先作用于"粗剪成片"（全部镜头拼接结果），
        # 否则退化为最新视频素材。避免把单镜头片段当作整片增强。
        candidates = (
            ctx.db.query(Asset)
            .filter(Asset.project_id == project.id, Asset.type == AssetType.VIDEO)
            .order_by(Asset.created_at.desc()).all()
        )
        merged = [a for a in candidates if (a.extra or {}).get("role") == "merged"]
        source_asset = merged[0] if merged else (candidates[0] if candidates else None)
    if source_asset is None:
        raise ValueError("找不到可增强的视频资产")

    ops = payload.get("operations")
    if not ops:
        if ctx.task.type == TaskType.UPSCALE_VIDEO:
            ops = [{"op": "upscale", "scale": payload.get("scale", 1.5)},
                   {"op": "sharpen", "amount": 0.9}]
        elif ctx.task.type == TaskType.INTERPOLATE_VIDEO:
            ops = [{"op": "interpolate", "fps": payload.get("fps", 48)}]
        else:
            ops = [{"op": "denoise", "strength": 1.2}, {"op": "color"},
                   {"op": "sharpen", "amount": 0.8}, {"op": "audio", "denoise": True, "normalize": True}]

    ctx.progress(10, "画质增强中")
    result = provider.enhance(
        video_path=source_asset.file_path, operations=ops,
        width=int(payload.get("width") or 0), height=int(payload.get("height") or 0),
        fps=int(payload.get("fps") or 0),
        progress_cb=lambda pct, msg: ctx.progress(10 + int(pct * 0.85), msg),
    )
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=AssetType.VIDEO,
        name=f"增强版（{', '.join(a.split(':')[0] for a in result.extra.get('applied', []))}）",
        task_id=ctx.task.id, parent_asset_id=source_asset.id, shot_id=ctx.task.shot_id,
        extra={"role": "enhanced", "operations": ops, **result.extra},
    )
    if ctx.task.shot_id:
        shot = _shot(ctx)
        shot.enhanced_video_asset_id = asset.id
    ctx.db.commit()
    ctx.event("enhance.finished",
              f"画质增强完成：{', '.join(result.extra.get('applied', [])) or 'n/a'}",
              asset_id=asset.id, url=asset.url, skipped=result.extra.get("skipped", []))
    workflow_svc.complete_step(ctx.db, project, "enhancement",
                               output={"asset_id": asset.id}, commit=False)
    projects_svc.refresh_progress(ctx.db, project, commit=False)
    ctx.db.commit()
    return {"asset_id": asset.id, "url": asset.url,
            "applied": result.extra.get("applied", []),
            "skipped": result.extra.get("skipped", []),
            "source_asset_id": source_asset.id}


@task_handler(TaskType.EDIT_VIDEO)
def handle_edit_video(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    payload = ctx.task.payload or {}
    operation = payload.get("operation") or "trim_video"
    inputs = list(payload.get("inputs") or [])
    if not inputs:
        src = _latest_asset(ctx.db, project.id, AssetType.VIDEO)
        if src is None:
            raise ValueError("找不到可编辑的视频资产")
        inputs = [src.file_path]
        if operation in ("add_voice", "add_music", "add_sfx", "mix_audio", "add_subtitle"):
            kind = {"add_voice": AssetType.VOICE, "add_music": AssetType.MUSIC,
                    "add_sfx": AssetType.SFX, "add_subtitle": AssetType.SUBTITLE}.get(operation)
            if kind:
                extra_asset = _latest_asset(ctx.db, project.id, kind)
                if extra_asset is None:
                    raise ValueError(f"找不到 {kind} 资产，无法执行 {operation}")
                inputs.append(extra_asset.file_path)

    processing = _provider("processing", payload.get("provider") or "ffmpeg")
    ctx.progress(10, f"视频处理：{operation}")
    result = processing.run(
        operation=operation, inputs=inputs,
        output_name=f"{project.id}_{operation}",
        params={**payload.get("params", {}), "workdir": str(_workdir(ctx, "videos"))},
        progress_cb=lambda pct, msg: ctx.progress(10 + int(pct * 0.85), msg),
    )
    asset_type = AssetType.SUBTITLE if result.format in ("srt", "ass", "vtt") else (
        AssetType.VOICE if result.format == "mp3" and operation == "extract_audio" else AssetType.VIDEO
    )
    asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=asset_type,
        name=f"{operation} 结果", task_id=ctx.task.id, extra={"operation": operation},
    )
    ctx.db.commit()
    ctx.event("edit.finished", f"视频处理完成：{operation}", asset_id=asset.id, url=asset.url)
    return {"asset_id": asset.id, "url": asset.url, "operation": operation}


@task_handler(TaskType.COMPOSE_VIDEO)
def handle_compose_video(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    payload = ctx.task.payload or {}
    processing = _provider("processing", payload.get("provider") or "ffmpeg")
    ctx.progress(5, "开始合成最终成片")

    # 1) 主视频：必须是"全部镜头按序拼接"的结果
    #    注意：不能简单取"最新一条 VIDEO 素材"——增强/重生成会产出单镜头片段，
    #    直接采用会导致成片只剩一个镜头（时长严重偏短）。
    main_asset: Asset | None = None
    if payload.get("video_asset_id"):
        main_asset = ctx.db.get(Asset, payload["video_asset_id"])

    if main_asset is None:
        pairs = _ordered_shot_videos(ctx.db, project.id)
        if not pairs:
            raise ValueError("没有可合成的主视频（所有镜头均无可用视频素材）")
        source_ids = [a.id for _, a in pairs]

        merged_asset: Asset | None = None
        # 若已有拼接结果且来源完全一致，则复用，避免重复编码
        existing = (
            ctx.db.query(Asset)
            .filter(Asset.project_id == project.id, Asset.type == AssetType.VIDEO)
            .order_by(Asset.created_at.desc()).all()
        )
        for candidate in existing:
            extra = candidate.extra or {}
            if extra.get("role") == "merged" and extra.get("source_asset_ids") == source_ids:
                if Path(candidate.file_path).exists():
                    merged_asset = candidate
                    break

        if merged_asset is None:
            ctx.progress(8, f"拼接 {len(pairs)} 个镜头作为主视频")
            merge_result = processing.run(
                operation="merge_video", inputs=[a.file_path for _, a in pairs],
                output_name=f"{project.id}_merged",
                params={"workdir": str(_workdir(ctx, "videos"))},
                progress_cb=lambda pct, msg: ctx.progress(8 + int(pct * 0.25), msg),
            )
            merged_asset = assets_svc.ingest_result(
                ctx.db, project_id=project.id, result=merge_result, asset_type=AssetType.VIDEO,
                name=f"拼接粗剪（{len(pairs)} 镜头）", task_id=ctx.task.id,
                extra={"role": "merged", "source_asset_ids": source_ids,
                       "shot_count": len(pairs)},
            )
            # 合成内部完成了「拼接」，同步把「剪辑」节点标记为完成，
            # 避免 Web UI 上出现「拼接片段已产出、剪辑节点却仍是待执行」的状态漂移。
            workflow_svc.complete_step(
                ctx.db, project, "editing",
                output={"asset_id": merged_asset.id, "shots": len(pairs)}, commit=False,
            )
            ctx.db.commit()

        # 工作流中 ENHANCEMENT 先于 COMPOSING：若该粗剪已有增强版，则采用增强版
        # 作为主视频，使画质增强结果真正进入成片。
        enhanced_main = (
            ctx.db.query(Asset)
            .filter(Asset.project_id == project.id, Asset.type == AssetType.VIDEO,
                    Asset.parent_asset_id == merged_asset.id)
            .order_by(Asset.created_at.desc()).first()
        )
        if enhanced_main is not None and Path(enhanced_main.file_path).exists():
            ctx.log(f"采用粗剪的增强版作为主视频：{enhanced_main.id}")
            main_asset = enhanced_main
        else:
            main_asset = merged_asset

    # 2) 配音：按镜头时间轴对齐（保证音画同步，且不会与下一镜头旁白重叠）
    voice_path: str | None = None
    ordered_shots = (
        ctx.db.query(Shot).filter(Shot.project_id == project.id).order_by(Shot.sequence.asc()).all()
    )
    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    for shot in ordered_shots:
        slot = float(shot.duration or 0.0)
        if shot.voice_asset_id:
            voice_asset = ctx.db.get(Asset, shot.voice_asset_id)
            if voice_asset and Path(voice_asset.file_path).exists():
                timeline.append({"path": voice_asset.file_path, "start": round(cursor, 3), "slot": slot})
        cursor += slot

    if payload.get("voice_asset_id"):
        single = ctx.db.get(Asset, payload["voice_asset_id"])
        voice_path = single.file_path if single else None
    elif timeline:
        ctx.progress(32, f"对齐 {len(timeline)} 段旁白到镜头时间轴")
        aligned = processing.run(
            operation="build_voice_timeline",
            inputs=[seg["path"] for seg in timeline],
            output_name=f"{project.id}_voice_aligned",
            params={
                "timeline": timeline,
                "total_duration": round(cursor, 3),
                "workdir": str(_workdir(ctx, "voices")),
                "ext": "mp3",
            },
            progress_cb=lambda pct, msg: ctx.progress(32 + int(pct * 0.18), msg),
        )
        voice_path = aligned.file_path

    music = _latest_asset(ctx.db, project.id, AssetType.MUSIC)
    sfx = _latest_asset(ctx.db, project.id, AssetType.SFX)
    subtitle = _latest_asset(ctx.db, project.id, AssetType.SUBTITLE)

    ctx.progress(55, "混合音轨并烧录字幕")
    result = processing.run(
        operation="compose_video",
        inputs=[main_asset.file_path]
        + ([voice_path] if voice_path else [None])  # type: ignore[list-item]
        + ([music.file_path] if (music and payload.get("with_music", True)) else [None])  # type: ignore[list-item]
        + ([sfx.file_path] if (sfx and payload.get("with_sfx", False)) else [None]),  # type: ignore[list-item]
        output_name=f"{project.id}_final",
        params={
            "workdir": str(_workdir(ctx, "outputs")),
            "subtitle_path": subtitle.file_path if (subtitle and payload.get("with_subtitle", True)) else None,
            "music_volume": float(payload.get("music_volume") or 0.16),
            "voice_volume": float(payload.get("voice_volume") or 1.0),
            "sfx_volume": float(payload.get("sfx_volume") or 0.5),
            "font_size": int(payload.get("font_size") or 22),
            "soft_fallback": True,
        },
        progress_cb=lambda pct, msg: ctx.progress(55 + int(pct * 0.35), msg),
    )
    final_asset = assets_svc.ingest_result(
        ctx.db, project_id=project.id, result=result, asset_type=AssetType.PROJECT_OUTPUT,
        name=f"{project.name} 成片", task_id=ctx.task.id, parent_asset_id=main_asset.id,
        extra={"role": "final", "has_voice": bool(voice_path), "has_music": bool(music),
               "has_subtitle": bool(subtitle)},
    )

    # 3) 封面
    try:
        poster = processing.run(
            operation="make_thumbnail", inputs=[final_asset.file_path],
            output_name=f"{project.id}_poster",
            params={"at": 1.0, "workdir": str(_workdir(ctx, "images")), "ext": "jpg"},
        )
        assets_svc.ingest_result(
            ctx.db, project_id=project.id, result=poster, asset_type=AssetType.IMAGE,
            name="成片封面", task_id=ctx.task.id, parent_asset_id=final_asset.id,
            extra={"role": "poster"},
        )
    except Exception as exc:  # noqa: BLE001  封面失败不影响成片
        ctx.log(f"封面生成失败（忽略）：{exc}", "WARN")

    ctx.db.commit()
    ctx.event("compose.finished", "最终成片合成完成",
              asset_id=final_asset.id, url=final_asset.url, duration=final_asset.duration)
    workflow_svc.start_step(ctx.db, project, "composing")
    workflow_svc.complete_step(ctx.db, project, "composing",
                               output={"asset_id": final_asset.id, "duration": final_asset.duration},
                               commit=False)
    projects_svc.refresh_progress(ctx.db, project, commit=False)
    ctx.db.commit()

    auto_qc = bool(payload.get("auto_quality_check", True))
    qc_task_id = None
    if auto_qc:
        qc_task = tasks_svc.create_task(
            ctx.db, project_id=project.id, type=TaskType.QUALITY_CHECK,
            name="成片质量检查", payload={"asset_id": final_asset.id},
            parent_task_id=ctx.task.id, created_by=ctx.task.created_by, commit=False,
        )
        ctx.db.commit()
        qc_task_id = qc_task.id

    return {
        "asset_id": final_asset.id, "url": final_asset.url,
        "duration": final_asset.duration, "width": final_asset.width, "height": final_asset.height,
        "size_bytes": final_asset.size_bytes,
        "quality_check_task_id": qc_task_id,
        "sources": {"main": main_asset.id, "voice": bool(voice_path),
                    "music": music.id if music else None,
                    "subtitle": subtitle.id if subtitle else None},
    }


# --------------------------------------------------------------------------- #
# 质检 / 自愈 / 通用
# --------------------------------------------------------------------------- #
@task_handler(TaskType.QUALITY_CHECK)
def handle_quality_check(ctx: TaskContext) -> dict[str, Any]:
    project = _project(ctx)
    payload = ctx.task.payload or {}
    ctx.progress(20, "执行质量检查")
    workflow_svc.start_step(ctx.db, project, "quality_check", actor=ctx.task.created_by)
    ctx.db.commit()
    report = quality_svc.run_project_quality_check(ctx.db, project, commit=False)

    if report["failed"] == 0:
        # 没有硬失败即视为可交付。WARN 只代表"需人工/VLM 复核"（例如人物一致性
        # 需要视觉模型），不应阻塞项目完成，否则项目永远停在中间态。
        workflow_svc.complete_step(ctx.db, project, "quality_check",
                                   output={"score": report["score"], "run_id": report["run_id"],
                                           "warned": report["warned"]},
                                   commit=False)
        workflow_svc.transition(
            ctx.db, project, "COMPLETED", actor=ctx.task.created_by,
            reason=f"质检通过（{report['passed']} 项通过，{report['warned']} 项提示）", commit=False,
        )
        project.status = "COMPLETED"
        project.progress = 100
    elif payload.get("auto_repair", True) and report["failed"]:
        workflow_svc.transition(ctx.db, project, "FAILED", actor=ctx.task.created_by,
                                reason=f"质检未通过（{report['failed']} 项失败）", commit=False)
        workflow_svc.transition(ctx.db, project, "ANALYZE", actor=ctx.task.created_by,
                                reason="分析失败原因", commit=False)
        ctx.db.commit()
        repair = _auto_repair(ctx, project, report)
        report["repair"] = repair
    ctx.db.commit()
    return {
        "run_id": report["run_id"], "status": report["status"], "score": report["score"],
        "passed": report["passed"], "failed": report["failed"], "warned": report["warned"],
        "items": [{"name": i["name"], "status": i["status"], "message": i["message"]}
                  for i in report["items"]],
        "repair_hints": report["repair_hints"],
        "repair": report.get("repair"),
    }


def _auto_repair(ctx: TaskContext, project: Project, report: dict[str, Any]) -> dict[str, Any]:
    """自愈：定位失败原因，重跑最小单位（单镜头优先），而不是整个项目重来。"""
    failed_checks = [i for i in report["items"] if i["status"] == "FAIL"]
    needs_recompose = False
    retried_shots: list[str] = []
    resubmitted: list[str] = []
    skipped: list[str] = []

    for item in failed_checks:
        key = item["check_key"]
        if key in ("shot_video_playable", "shot_video_exists"):
            shot = ctx.db.get(Shot, item["target_id"])
            if shot is not None:
                shot.video_asset_id = None
                shot.video_status = "PENDING"
                shot.status = ShotStatus.PENDING
                shot.retry_count = (shot.retry_count or 0) + 1
                shot.last_error = item["message"]
                task = tasks_svc.create_task(
                    ctx.db, project_id=project.id, type=TaskType.GENERATE_VIDEO,
                    name=f"重生成 {shot.code}", shot_id=shot.id,
                    payload={"reason": "quality_check_failed", "attempt": shot.retry_count},
                    parent_task_id=ctx.task.id, created_by="agent", commit=False,
                )
                ctx.db.commit()
                retried_shots.append(shot.code)
                resubmitted.append(task.id)
                needs_recompose = True
        elif key == "all_shots_done":
            missing = ctx.db.query(Shot).filter(
                Shot.project_id == project.id, Shot.video_asset_id.is_(None)
            ).all()
            for shot in missing:
                task = tasks_svc.create_task(
                    ctx.db, project_id=project.id, type=TaskType.GENERATE_VIDEO,
                    name=f"补齐 {shot.code}", shot_id=shot.id, payload={"reason": "missing"},
                    parent_task_id=ctx.task.id, created_by="agent", commit=False,
                )
                resubmitted.append(task.id)
            if missing:
                ctx.db.commit()
                needs_recompose = True
        elif key in ("video_integrity", "resolution", "audio_integrity", "subtitle_integrity", "duration"):
            needs_recompose = True
        elif key in ("no_failed_tasks",):
            failed = ctx.db.query(Task).filter(
                Task.project_id == project.id, Task.status == "FAILED", Task.id != ctx.task.id
            ).all()
            for task in failed:
                tasks_svc.retry_task(ctx.db, task.id, reset_attempts=True)
                resubmitted.append(task.id)
        else:
            skipped.append(key)

    if needs_recompose:
        task = tasks_svc.create_task(
            ctx.db, project_id=project.id, type=TaskType.COMPOSE_VIDEO, name="重新合成成片",
            payload={"reason": "quality_repair", "auto_quality_check": True},
            parent_task_id=ctx.task.id, created_by="agent", commit=False,
        )
        ctx.db.commit()
        resubmitted.append(task.id)

    workflow_svc.transition(ctx.db, project, "REGENERATE", actor="agent",
                            reason=f"自动修复：重跑 {len(retried_shots)} 个镜头", commit=True)
    ctx.event("repair.dispatched",
              f"自动修复已派发：重跑 {len(retried_shots)} 个镜头，"
              f"{len(resubmitted)} 个任务重新入队",
              level="WARN", retried_shots=retried_shots, resubmitted=resubmitted)
    return {"retried_shots": retried_shots, "resubmitted_tasks": resubmitted, "skipped": skipped}


@task_handler(TaskType.BROWSER_TASK)
def handle_browser_task(ctx: TaskContext) -> dict[str, Any]:
    """浏览器任务不在 Backend 写死：只登记并交给 Agent 执行。"""
    payload = ctx.task.payload or {}
    provider = _provider("browser", payload.get("provider") or "agent_browser")
    plan = provider.plan(payload.get("instruction", ""), payload.get("platform", ""))
    ctx.event("browser.dispatched",
              f"浏览器任务已登记，等待 Agent 执行：{payload.get('instruction', '')[:80]}",
              level="WARN", steps=len(plan))
    return {
        "status": "PENDING",
        "executor": "agent",
        "playbook": plan,
        "note": "Backend 不下发浏览器动作；请由 Agent 执行后调用 browser.complete 回写结果。",
    }


@task_handler(TaskType.CUSTOM)
def handle_custom(ctx: TaskContext) -> dict[str, Any]:
    payload = ctx.task.payload or {}
    # 批量分组任务：仅作为分组容器与进度锚点，本身没有实际执行动作
    if payload.get("batch"):
        return {
            "batch": payload["batch"],
            "count": payload.get("count", 0),
            "note": "批量分组任务，子任务已分别入队执行",
        }
    action = payload.get("action")
    if action == "plan_script":
        script = planner.plan_script(
            requirement=payload.get("requirement", ""), style=payload.get("style", "漫画教学风格"),
            target_duration=float(payload.get("target_duration") or 300),
        )
        return {k: v for k, v in script.items() if k in ("title", "shot_count", "scene_count")}
    raise ValueError(f"未知 CUSTOM action: {action}")


def load_handlers() -> list[str]:
    """触发 handler 注册并返回已注册的任务类型。"""
    register_all()
    return registered_handlers()
