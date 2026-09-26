"""内容类 Skill：Project / Script / Storyboard / Scene / Shot / Character。

这些是 Agent 编排生产流程时最常用的能力。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..core.constants import ProjectStatus, SceneStatus, ShotStatus, TaskType, WorkflowState
from ..models import Character, Project, Scene, Script, Shot, Storyboard, Series
from ..services import agent_log, characters as characters_svc, planner, projects as projects_svc
from ..services import serializers as S, series as series_svc
from ..services import tasks as tasks_svc, workflow as workflow_svc
from .base import SkillContext, SkillError, skill


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #
@skill(
    name="create_project", category="project", is_async=False,
    description="创建一个视频项目。项目是全部素材、镜头、任务与工作流的容器。",
    tags=("project", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "项目名称"},
            "requirement": {"type": "string", "description": "用户的原始需求，例如『制作一个 5 分钟的 AI Agent 教学视频，漫画教学风格』"},
            "description": {"type": "string"},
            "style": {"type": "string", "default": "漫画教学风格"},
            "target_duration": {"type": "number", "default": 300, "description": "目标时长（秒）"},
            "aspect_ratio": {"type": "string", "default": "16:9"},
            "width": {"type": "integer", "default": 1280},
            "height": {"type": "integer", "default": 720},
            "fps": {"type": "integer", "default": 24},
            "language": {"type": "string", "default": "zh-CN"},
            "owner": {"type": "string"},
            "series_id": {"type": "string",
                          "description": "归入某连续剧，本项即成为其中一集（自动分配集号）"},
            "episode_no": {"type": "integer", "default": 0, "description": "指定集号，0 = 自动"},
        },
        "required": ["name"],
    },
    output_schema={"type": "object", "properties": {"project": {"type": "object"}}},
    examples=({"name": "AI Agent 教学视频", "requirement": "制作一个 5 分钟的 AI Agent 教学视频，漫画教学风格",
               "target_duration": 300},),
)
def create_project(
    ctx: SkillContext, *, name: str, requirement: str = "", description: str = "",
    style: str = "漫画教学风格", target_duration: float = 300.0, aspect_ratio: str = "16:9",
    width: int = 1280, height: int = 720, fps: int = 24, language: str = "zh-CN",
    owner: str = "workbuddy", series_id: str = "", episode_no: int = 0, **extra: Any,
) -> dict[str, Any]:
    episode = int(episode_no)
    if series_id:
        series = ctx.db.get(Series, series_id)
        if series is None:
            raise SkillError(f"系列不存在: {series_id}", code="NOT_FOUND")
        if not episode:
            episode = series_svc.next_episode_no(ctx.db, series_id)

    project = Project(
        name=name, requirement=requirement, description=description or requirement,
        style=style, target_duration=float(target_duration), aspect_ratio=aspect_ratio,
        width=int(width), height=int(height), fps=int(fps), language=language,
        series_id=series_id or None, episode_no=episode,
        owner=owner, status=ProjectStatus.PLANNING,
        workflow_state=WorkflowState.PROJECT_CREATED, extra=extra or {},
    )
    ctx.db.add(project)
    ctx.db.flush()
    workflow_svc.ensure_workflow(ctx.db, project)
    agent_log.log_event(
        ctx.db, project_id=project.id, event="project.created", actor=ctx.actor,
        message=f"Agent 创建项目：{project.name}",
        detail={"requirement": requirement, "style": style, "target_duration": target_duration},
    )
    ctx.db.commit()
    return {"project": S.serialize_project(project, db=ctx.db), "project_id": project.id,
            "message": "项目已创建，建议下一步调用 create_script"}


@skill(
    name="update_project", category="project",
    description="更新项目基本信息（名称、需求、风格、画幅、帧率、状态等）。",
    tags=("project", "write"),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string"}, "description": {"type": "string"},
            "requirement": {"type": "string"}, "style": {"type": "string"},
            "status": {"type": "string"}, "target_duration": {"type": "number"},
            "aspect_ratio": {"type": "string"}, "width": {"type": "integer"},
            "height": {"type": "integer"}, "fps": {"type": "integer"},
            "language": {"type": "string"},
        },
        "required": ["project_id"],
    },
)
def update_project(ctx: SkillContext, *, project_id: str, **fields: Any) -> dict[str, Any]:
    project = ctx.db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
    allowed = {"name", "description", "requirement", "style", "status", "target_duration",
               "aspect_ratio", "width", "height", "fps", "language", "owner"}
    changed = {}
    for key, value in fields.items():
        if key in allowed and value is not None:
            setattr(project, key, value)
            changed[key] = value
    ctx.db.commit()
    return {"project": S.serialize_project(project, db=ctx.db), "changed": changed}


@skill(
    name="delete_project", category="project",
    description="删除项目及其全部脚本、分镜、镜头、素材与任务记录。",
    tags=("project", "destructive"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "confirm": {"type": "boolean", "default": False}},
        "required": ["project_id", "confirm"]},
)
def delete_project(ctx: SkillContext, *, project_id: str, confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise SkillError("删除项目属于危险操作，请显式传 confirm=true", code="CONFIRM_REQUIRED")
    try:
        return projects_svc.purge_project(ctx.db, project_id)
    except LookupError as exc:
        raise SkillError(str(exc), code="NOT_FOUND") from exc


@skill(
    name="get_project", category="project",
    description="获取项目详情，可选带状态摘要。",
    tags=("project", "read"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "include_status": {"type": "boolean", "default": True}},
        "required": ["project_id"]},
)
def get_project(ctx: SkillContext, *, project_id: str, include_status: bool = True) -> dict[str, Any]:
    project = ctx.db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
    data = {"project": S.serialize_project(project, db=ctx.db)}
    if include_status:
        data["status"] = projects_svc.project_status(ctx.db, project)
    return data


@skill(
    name="list_projects", category="project",
    description="列出项目（支持按状态过滤与关键词搜索）。",
    tags=("project", "read"),
    input_schema={"type": "object", "properties": {
        "status": {"type": "string"}, "keyword": {"type": "string"},
        "limit": {"type": "integer", "default": 50}, "offset": {"type": "integer", "default": 0}}},
)
def list_projects(ctx: SkillContext, *, status: str | None = None, keyword: str | None = None,
                  limit: int = 50, offset: int = 0) -> dict[str, Any]:
    stmt = select(Project).order_by(Project.created_at.desc())
    if status:
        stmt = stmt.where(Project.status == status)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(Project.name.ilike(like) | Project.requirement.ilike(like))
    rows = list(ctx.db.execute(stmt.offset(offset).limit(limit)).scalars())
    return {"projects": [projects_svc.summarize_project(ctx.db, p) for p in rows], "count": len(rows)}


@skill(
    name="get_project_status", category="project",
    description="获取项目生产状态：进度百分比、各阶段完成情况、镜头/素材/任务统计、下一步建议。",
    tags=("project", "read", "status"),
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}},
                  "required": ["project_id"]},
)
def get_project_status(ctx: SkillContext, *, project_id: str) -> dict[str, Any]:
    project = ctx.db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
    return projects_svc.project_status(ctx.db, project)


# --------------------------------------------------------------------------- #
# Script
# --------------------------------------------------------------------------- #
@skill(
    name="create_script", category="script",
    description="为项目创建脚本。推荐由 Agent 生成 content 后传入；未传时可用本地规划引擎兜底（use_planner=true）。",
    tags=("script", "write"), is_async=False,
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "title": {"type": "string"},
        "content": {"type": "string", "description": "完整脚本文本"},
        "outline": {"type": "string", "description": "分章大纲"},
        "style": {"type": "string"}, "language": {"type": "string"},
        "provider": {"type": "string"}, "model": {"type": "string"},
        "parameters": {"type": "object"},
        "use_planner": {"type": "boolean", "default": False,
                        "description": "为 true 且未提供 content 时，使用本地规划引擎生成"},
        "target_duration": {"type": "number"}, "version": {"type": "integer"}},
        "required": ["project_id"]},
    examples=({"project_id": "proj_xxx", "title": "AI Agent 教学视频", "content": "...", "outline": "..."},),
)
def create_script(ctx: SkillContext, *, project_id: str, title: str = "", content: str = "",
                  outline: str = "", style: str = "", language: str = "", provider: str = "agent",
                  model: str = "", parameters: dict[str, Any] | None = None,
                  use_planner: bool = False, target_duration: float | None = None,
                  version: int = 1) -> dict[str, Any]:
    project = ctx.db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")

    if not content.strip():
        if not use_planner:
            raise SkillError(
                "content 为空。若希望使用本地规划引擎兜底，请传 use_planner=true；"
                "更推荐由 Agent 生成脚本后传入。",
                code="MISSING_CONTENT",
            )
        planned = planner.plan_script(
            requirement=project.requirement or project.name,
            style=style or project.style,
            target_duration=float(target_duration or project.target_duration),
            language=language or project.language,
        )
        content = planned["content"]
        outline = outline or planned["outline"]
        title = title or planned["title"]
        provider, model = planned["provider"], planned["model"]
        parameters = {**(parameters or {}), **planned["parameters"]}

    script = Script(
        project_id=project_id, title=title or project.name, content=content, outline=outline,
        style=style or project.style, language=language or project.language, version=version,
        status="READY", provider=provider, model=model, parameters=parameters or {},
    )
    ctx.db.add(script)
    ctx.db.flush()
    agent_log.log_event(
        ctx.db, project_id=project_id, event="script.generated", actor=ctx.actor,
        message=f"脚本生成完成（{len(content)} 字）", detail={"script_id": script.id, "provider": provider},
    )
    workflow_svc.complete_step(ctx.db, project, "script",
                               output={"script_id": script.id, "chars": len(content), "provider": provider},
                               commit=False)
    projects_svc.refresh_progress(ctx.db, project, commit=False)
    ctx.db.commit()
    return {"script": S.serialize_script(script), "script_id": script.id,
            "message": "脚本已就绪，建议下一步调用 create_storyboard 拆分镜"}


@skill(
    name="update_script", category="script",
    description="更新脚本内容、大纲或元数据。",
    tags=("script", "write"),
    input_schema={"type": "object", "properties": {
        "script_id": {"type": "string"}, "title": {"type": "string"}, "content": {"type": "string"},
        "outline": {"type": "string"}, "status": {"type": "string"},
        "parameters": {"type": "object"}}, "required": ["script_id"]},
)
def update_script(ctx: SkillContext, *, script_id: str, **fields: Any) -> dict[str, Any]:
    script = ctx.db.get(Script, script_id)
    if script is None:
        raise SkillError(f"脚本不存在: {script_id}", code="NOT_FOUND")
    changed = {}
    for key in ("title", "content", "outline", "status", "parameters", "style"):
        if key in fields and fields[key] is not None:
            setattr(script, key, fields[key])
            changed[key] = fields[key] if key != "content" else f"{len(fields[key])} 字"
    ctx.db.commit()
    return {"script": S.serialize_script(script), "changed": changed}


@skill(
    name="get_script", category="script",
    description="按 project_id 或 script_id 获取脚本。",
    tags=("script", "read"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "script_id": {"type": "string"}}},
)
def get_script(ctx: SkillContext, *, project_id: str | None = None,
               script_id: str | None = None) -> dict[str, Any]:
    script: Script | None = None
    if script_id:
        script = ctx.db.get(Script, script_id)
    elif project_id:
        script = ctx.db.execute(
            select(Script).where(Script.project_id == project_id).order_by(Script.created_at.desc())
        ).scalars().first()
    if script is None:
        return {"script": None, "message": "尚未创建脚本"}
    return {"script": S.serialize_script(script)}


# --------------------------------------------------------------------------- #
# Storyboard / Scene / Shot
# --------------------------------------------------------------------------- #
def _resolve_character_ids(db, project: Project, values: list[Any]) -> list[str]:
    """把分镜里写的角色（id 或名字）解析成角色 id。

    角色池包含本集角色与所属系列的系列级角色 —— 连续剧主角建在系列层，
    每一集的分镜都要能引用到它。解析规则统一在 services/characters 里，
    避免本处与 pipeline 各写一套。
    """
    return characters_svc.resolve_character_ids(db, project, list(values or []))


def _create_shot(db, project: Project, scene: Scene, index: int, raw: dict[str, Any]) -> Shot:
    sequence = int(raw.get("sequence") or index)
    shot = Shot(
        project_id=project.id, scene_id=scene.id, sequence=sequence,
        code=raw.get("code") or f"Shot {sequence:03d}",
        duration=float(raw.get("duration") or 5.0),
        description=raw.get("description") or raw.get("voice_script") or "",
        camera=raw.get("camera") or "", location=raw.get("location") or scene.location,
        visual_style=raw.get("visual_style") or project.style,
        character_ids=_resolve_character_ids(db, project, raw.get("character_ids") or []),
        image_prompt=raw.get("image_prompt") or raw.get("description") or "",
        video_prompt=raw.get("video_prompt") or raw.get("description") or "",
        negative_prompt=raw.get("negative_prompt") or "",
        voice_script=raw.get("voice_script") or "",
        subtitle_text=raw.get("subtitle_text") or raw.get("voice_script") or "",
        status=ShotStatus.PENDING,
    )
    if raw.get("extra"):
        shot.extra = raw["extra"]
    db.add(shot)
    return shot


@skill(
    name="create_storyboard", category="storyboard",
    description=(
        "为项目创建分镜表（Scene + Shot）。推荐由 Agent 生成 scenes 结构后传入；"
        "未传时可用本地规划引擎兜底（use_planner=true）。每个 Shot 是后续生成与重试的最小单位。"
    ),
    tags=("storyboard", "write"), is_async=False,
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"},
        "title": {"type": "string"}, "synopsis": {"type": "string"}, "visual_style": {"type": "string"},
        "scenes": {
            "type": "array", "description": "场景数组，每项含 title/summary/location/mood/shots[]",
            "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "summary": {"type": "string"},
                "location": {"type": "string"}, "mood": {"type": "string"},
                "shots": {"type": "array", "items": {"type": "object"}},
            }},
        },
        "use_planner": {"type": "boolean", "default": False},
        "target_duration": {"type": "number"}, "shot_duration": {"type": "number", "default": 5},
    }, "required": ["project_id"]},
)
def create_storyboard(ctx: SkillContext, *, project_id: str, title: str = "", synopsis: str = "",
                      visual_style: str = "", scenes: list[dict[str, Any]] | None = None,
                      use_planner: bool = False, target_duration: float | None = None,
                      shot_duration: float = 5.0) -> dict[str, Any]:
    project = ctx.db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")

    provider, model, parameters = "agent", "", {}
    if not scenes:
        if not use_planner:
            raise SkillError(
                "scenes 为空。若希望使用本地规划引擎兜底，请传 use_planner=true；"
                "更推荐由 Agent 生成分镜结构后传入。",
                code="MISSING_SCENES",
            )
        characters = characters_svc.character_pool(ctx.db, project)
        planned = planner.plan_storyboard(
            requirement=project.requirement or project.name,
            style=visual_style or project.style,
            target_duration=float(target_duration or project.target_duration),
            shot_duration=shot_duration,
            characters=[{"name": c.name, "id": c.id} for c in characters] or None,
        )
        scenes = planned["scenes"]
        title = title or planned["title"]
        synopsis = synopsis or planned["synopsis"]
        visual_style = visual_style or planned["visual_style"]
        provider, model, parameters = planned["provider"], planned["model"], planned["parameters"]

    storyboard = Storyboard(
        project_id=project_id, title=title or f"{project.name} · 分镜表", synopsis=synopsis,
        visual_style=visual_style or project.style, status="READY",
        provider=provider, model=model, parameters=parameters,
    )
    ctx.db.add(storyboard)
    ctx.db.flush()

    total_shots = 0
    for s_idx, raw_scene in enumerate(scenes, start=1):
        scene = Scene(
            project_id=project_id, storyboard_id=storyboard.id, sequence=s_idx,
            code=raw_scene.get("code") or f"Scene {s_idx:02d}",
            title=raw_scene.get("title") or f"场景 {s_idx}",
            summary=raw_scene.get("summary") or "", location=raw_scene.get("location") or "",
            mood=raw_scene.get("mood") or "", status=SceneStatus.PENDING,
        )
        ctx.db.add(scene)
        ctx.db.flush()
        for shot_index, raw_shot in enumerate(raw_scene.get("shots") or [], start=1):
            _create_shot(ctx.db, project, scene, shot_index, raw_shot)
            total_shots += 1

    storyboard.scene_count = len(scenes)
    storyboard.shot_count = total_shots
    ctx.db.flush()
    agent_log.log_event(
        ctx.db, project_id=project_id, event="storyboard.generated", actor=ctx.actor,
        message=f"拆分 {len(scenes)} 个场景 / {total_shots} 个镜头",
        detail={"storyboard_id": storyboard.id, "provider": provider},
    )
    workflow_svc.complete_step(ctx.db, project, "storyboard",
                               output={"scenes": len(scenes), "shots": total_shots}, commit=False)
    projects_svc.refresh_progress(ctx.db, project, commit=False)
    ctx.db.commit()
    ctx.db.refresh(storyboard)
    return {
        "storyboard_id": storyboard.id,
        "scene_count": len(scenes), "shot_count": total_shots,
        "storyboard": S.serialize_storyboard(storyboard, db=ctx.db, include_shots=False),
        "message": "分镜已就绪，建议下一步创建人物（create_character）",
    }


@skill(
    name="update_storyboard", category="storyboard",
    description="更新分镜表的标题、简介与视觉风格。",
    tags=("storyboard", "write"),
    input_schema={"type": "object", "properties": {
        "storyboard_id": {"type": "string"}, "title": {"type": "string"},
        "synopsis": {"type": "string"}, "visual_style": {"type": "string"},
        "status": {"type": "string"}}, "required": ["storyboard_id"]},
)
def update_storyboard(ctx: SkillContext, *, storyboard_id: str, **fields: Any) -> dict[str, Any]:
    board = ctx.db.get(Storyboard, storyboard_id)
    if board is None:
        raise SkillError(f"分镜不存在: {storyboard_id}", code="NOT_FOUND")
    changed = {}
    for key in ("title", "synopsis", "visual_style", "status"):
        if key in fields and fields[key] is not None:
            setattr(board, key, fields[key])
            changed[key] = fields[key]
    ctx.db.commit()
    return {"storyboard": S.serialize_storyboard(board, db=ctx.db, include_shots=False),
            "changed": changed}


@skill(
    name="get_storyboard", category="storyboard",
    description="获取分镜表（含 Scene 与 Shot；include_shots=false 时只返回场景）。",
    tags=("storyboard", "read"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "storyboard_id": {"type": "string"},
        "include_shots": {"type": "boolean", "default": True}}},
)
def get_storyboard(ctx: SkillContext, *, project_id: str | None = None,
                   storyboard_id: str | None = None, include_shots: bool = True) -> dict[str, Any]:
    board: Storyboard | None = None
    if storyboard_id:
        board = ctx.db.get(Storyboard, storyboard_id)
    elif project_id:
        board = ctx.db.execute(
            select(Storyboard).where(Storyboard.project_id == project_id)
            .order_by(Storyboard.created_at.desc())
        ).scalars().first()
    if board is None:
        return {"storyboard": None, "message": "尚未创建分镜"}
    return {"storyboard": S.serialize_storyboard(board, db=ctx.db, include_shots=include_shots)}


@skill(
    name="create_shot", category="storyboard",
    description="在指定场景下新增一个镜头。",
    tags=("shot", "write"),
    input_schema={"type": "object", "properties": {
        "scene_id": {"type": "string"}, "sequence": {"type": "integer"},
        "duration": {"type": "number"}, "description": {"type": "string"},
        "camera": {"type": "string"}, "location": {"type": "string"},
        "visual_style": {"type": "string"}, "character_ids": {"type": "array", "items": {"type": "string"}},
        "image_prompt": {"type": "string"}, "video_prompt": {"type": "string"},
        "negative_prompt": {"type": "string"}, "voice_script": {"type": "string"},
        "subtitle_text": {"type": "string"}}, "required": ["scene_id"]},
)
def create_shot(ctx: SkillContext, *, scene_id: str, **fields: Any) -> dict[str, Any]:
    scene = ctx.db.get(Scene, scene_id)
    if scene is None:
        raise SkillError(f"场景不存在: {scene_id}", code="NOT_FOUND")
    project = ctx.db.get(Project, scene.project_id)
    if project is None:
        raise SkillError("场景所属项目不存在", code="NOT_FOUND")
    existing = ctx.db.execute(
        select(Shot).where(Shot.scene_id == scene_id).order_by(Shot.sequence.desc())
    ).scalars().first()
    default_seq = (existing.sequence + 1) if existing else 1
    shot = _create_shot(ctx.db, project, scene, default_seq, fields)
    ctx.db.flush()
    board = ctx.db.get(Storyboard, scene.storyboard_id)
    if board:
        board.shot_count = ctx.db.query(Shot).filter(Shot.project_id == project.id).count()
    ctx.db.commit()
    return {"shot": S.serialize_shot(shot, db=ctx.db), "shot_id": shot.id}


@skill(
    name="update_shot", category="storyboard",
    description="更新镜头字段。这是「只重跑某个镜头」的基础能力。",
    tags=("shot", "write"),
    input_schema={"type": "object", "properties": {
        "shot_id": {"type": "string"}, "sequence": {"type": "integer"},
        "duration": {"type": "number"}, "description": {"type": "string"},
        "camera": {"type": "string"}, "location": {"type": "string"},
        "visual_style": {"type": "string"}, "character_ids": {"type": "array", "items": {"type": "string"}},
        "image_prompt": {"type": "string"}, "video_prompt": {"type": "string"},
        "negative_prompt": {"type": "string"}, "voice_script": {"type": "string"},
        "subtitle_text": {"type": "string"}, "status": {"type": "string"}},
        "required": ["shot_id"]},
)
def update_shot(ctx: SkillContext, *, shot_id: str, **fields: Any) -> dict[str, Any]:
    shot = ctx.db.get(Shot, shot_id)
    if shot is None:
        raise SkillError(f"镜头不存在: {shot_id}", code="NOT_FOUND")
    changed = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key == "character_ids":
            owner = ctx.db.get(Project, shot.project_id)
            if owner is not None:
                value = _resolve_character_ids(ctx.db, owner, value)
        if key in {"sequence", "duration", "description", "camera", "location", "visual_style",
                   "character_ids", "image_prompt", "video_prompt", "negative_prompt",
                   "voice_script", "subtitle_text", "status"}:
            setattr(shot, key, value)
            changed[key] = value
    ctx.db.commit()
    return {"shot": S.serialize_shot(shot, db=ctx.db), "changed": list(changed)}


@skill(
    name="get_shot", category="storyboard",
    description="获取单个镜头详情（含各产物的 Asset 元数据与 URL）。",
    tags=("shot", "read"),
    input_schema={"type": "object", "properties": {"shot_id": {"type": "string"}},
                  "required": ["shot_id"]},
)
def get_shot(ctx: SkillContext, *, shot_id: str) -> dict[str, Any]:
    shot = ctx.db.get(Shot, shot_id)
    if shot is None:
        raise SkillError(f"镜头不存在: {shot_id}", code="NOT_FOUND")
    return {"shot": S.serialize_shot(shot, db=ctx.db)}


@skill(
    name="list_shots", category="storyboard",
    description="按项目（可再按场景或状态）列出镜头。",
    tags=("shot", "read"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "scene_id": {"type": "string"},
        "status": {"type": "string"}, "missing": {"type": "string",
        "description": "只看缺某类产物的镜头：image / video / voice / subtitle"},
        "limit": {"type": "integer", "default": 200}}, "required": ["project_id"]},
)
def list_shots(ctx: SkillContext, *, project_id: str, scene_id: str | None = None,
               status: str | None = None, missing: str | None = None,
               limit: int = 200) -> dict[str, Any]:
    stmt = select(Shot).where(Shot.project_id == project_id).order_by(Shot.sequence.asc())
    if scene_id:
        stmt = stmt.where(Shot.scene_id == scene_id)
    if status:
        stmt = stmt.where(Shot.status == status)
    column = {"image": Shot.image_asset_id, "video": Shot.video_asset_id,
              "voice": Shot.voice_asset_id, "subtitle": Shot.subtitle_asset_id}.get(missing or "")
    if column is not None:
        stmt = stmt.where(column.is_(None))
    rows = list(ctx.db.execute(stmt.limit(limit)).scalars())
    return {"shots": [S.serialize_shot(s, db=ctx.db) for s in rows], "count": len(rows)}


# --------------------------------------------------------------------------- #
# Character
# --------------------------------------------------------------------------- #
def _character_host_project_id(db, char: Character) -> str:
    """角色参考图生成任务的归属集。

    本集角色归本集；**系列级角色**不隶属于任何一集，归属于该系列的第一集
    （任务必须有宿主项目，否则无法挂载与追踪）。
    """
    if char.project_id:
        return char.project_id
    if not char.series_id:
        raise SkillError(
            f"角色 {char.name} 既不属于任何项目也不属于任何系列，无法生成参考图",
            code="ORPHAN_CHARACTER",
        )
    first = db.execute(
        select(Project).where(Project.series_id == char.series_id)
        .order_by(Project.episode_no.asc(), Project.created_at.asc())
    ).scalars().first()
    if first is None:
        raise SkillError("该系列还没有任何一集，请先创建一集再生成角色参考图", code="NO_EPISODE")
    return first.id


@skill(
    name="create_character", category="character",
    description=(
        "创建角色设定。传 project_id 则该角色属于这一集；只传 series_id 则创建"
        "**系列级角色**，供连续剧各集共享（保证主角跨集形象一致）。"
        "auto_reference=true 时同时提交参考图生成任务。"
    ),
    tags=("character", "write"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string", "description": "所属集（可选）"},
        "series_id": {"type": "string", "description": "所属连续剧；只传它即为系列级角色"},
        "name": {"type": "string"},
        "role": {"type": "string", "default": "supporting"},
        "description": {"type": "string"}, "appearance": {"type": "string"},
        "personality": {"type": "string"}, "voice_style": {"type": "string"},
        "reference_prompt": {"type": "string"}, "negative_prompt": {"type": "string"},
        "parameters": {"type": "object"},
        "auto_reference": {"type": "boolean", "default": False},
        "provider": {"type": "string"}},
        "required": ["name"]},
)
def create_character(ctx: SkillContext, *, name: str, project_id: str = "", series_id: str = "",
                     role: str = "supporting",
                     description: str = "", appearance: str = "", personality: str = "",
                     voice_style: str = "", reference_prompt: str = "", negative_prompt: str = "",
                     parameters: dict[str, Any] | None = None, auto_reference: bool = False,
                     provider: str | None = None) -> dict[str, Any]:
    if not project_id and not series_id:
        raise SkillError("需要提供 project_id（本集角色）或 series_id（系列级角色）",
                         code="BAD_INPUT")

    style = ""
    if project_id:
        project = ctx.db.get(Project, project_id)
        if project is None:
            raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
        style = project.style
    else:
        series = ctx.db.get(Series, series_id)
        if series is None:
            raise SkillError(f"系列不存在: {series_id}", code="NOT_FOUND")
        style = series.style

    char = Character(
        project_id=project_id or None, series_id=series_id or None,
        name=name, role=role, description=description,
        appearance=appearance, personality=personality, voice_style=voice_style,
        reference_prompt=reference_prompt or (
            f"{style}，角色设定图：{name}，{appearance}，正面半身，干净扁平配色"
        ),
        negative_prompt=negative_prompt, parameters=parameters or {}, status="PENDING",
    )
    ctx.db.add(char)
    ctx.db.flush()
    task_id = None
    if auto_reference:
        task = tasks_svc.create_task(
            ctx.db, project_id=_character_host_project_id(ctx.db, char),
            type=TaskType.GENERATE_CHARACTER_REFERENCE,
            name=f"生成角色参考图：{name}", character_id=char.id,
            payload={"provider": provider}, created_by=ctx.actor, commit=False,
        )
        task_id = task.id
    ctx.db.commit()
    return {"character": S.serialize_character(char, db=ctx.db), "character_id": char.id,
            "reference_task_id": task_id}


@skill(
    name="update_character", category="character",
    description="更新角色设定。",
    tags=("character", "write"),
    input_schema={"type": "object", "properties": {
        "character_id": {"type": "string"}, "name": {"type": "string"}, "role": {"type": "string"},
        "description": {"type": "string"}, "appearance": {"type": "string"},
        "personality": {"type": "string"}, "voice_style": {"type": "string"},
        "reference_prompt": {"type": "string"}, "negative_prompt": {"type": "string"},
        "parameters": {"type": "object"}, "status": {"type": "string"}},
        "required": ["character_id"]},
)
def update_character(ctx: SkillContext, *, character_id: str, **fields: Any) -> dict[str, Any]:
    char = ctx.db.get(Character, character_id)
    if char is None:
        raise SkillError(f"角色不存在: {character_id}", code="NOT_FOUND")
    changed = []
    for key in ("name", "role", "description", "appearance", "personality", "voice_style",
                "reference_prompt", "negative_prompt", "parameters", "status"):
        if key in fields and fields[key] is not None:
            setattr(char, key, fields[key])
            changed.append(key)
    ctx.db.commit()
    return {"character": S.serialize_character(char, db=ctx.db), "changed": changed}


@skill(
    name="get_character", category="character",
    description="获取角色详情，或列出项目全部角色。",
    tags=("character", "read"),
    input_schema={"type": "object", "properties": {
        "character_id": {"type": "string"}, "project_id": {"type": "string"}}},
)
def get_character(ctx: SkillContext, *, character_id: str | None = None,
                  project_id: str | None = None) -> dict[str, Any]:
    if character_id:
        char = ctx.db.get(Character, character_id)
        if char is None:
            raise SkillError(f"角色不存在: {character_id}", code="NOT_FOUND")
        return {"character": S.serialize_character(char, db=ctx.db)}
    if not project_id:
        raise SkillError("需要提供 character_id 或 project_id", code="BAD_INPUT")
    project = ctx.db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
    # 返回「可用角色池」：本集角色 + 所属系列的系列级角色
    rows = characters_svc.character_pool(ctx.db, project)
    items = []
    for char in rows:
        data = S.serialize_character(char, db=ctx.db)
        data["scope"] = "series" if char.series_id and not char.project_id else "episode"
        items.append(data)
    return {"characters": items, "count": len(items)}


@skill(
    name="generate_character_reference", category="character", is_async=True,
    description="为角色生成参考图（异步任务）。",
    tags=("character", "generate"),
    input_schema={"type": "object", "properties": {
        "character_id": {"type": "string"}, "provider": {"type": "string"}},
        "required": ["character_id"]},
)
def generate_character_reference(ctx: SkillContext, *, character_id: str,
                                 provider: str | None = None):
    char = ctx.db.get(Character, character_id)
    if char is None:
        raise SkillError(f"角色不存在: {character_id}", code="NOT_FOUND")
    host_project_id = _character_host_project_id(ctx.db, char)
    return tasks_svc.create_task(
        ctx.db, project_id=host_project_id, type=TaskType.GENERATE_CHARACTER_REFERENCE,
        name=f"生成角色参考图：{char.name}", character_id=char.id,
        payload={"provider": provider}, created_by=ctx.actor, commit=False,
    )
