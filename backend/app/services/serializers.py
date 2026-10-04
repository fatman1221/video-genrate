"""统一的序列化器：API 与 Skill 共用同一套返回结构，避免 UI / Agent 两套心智。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import (
    Asset, Character, Project, Scene, Script, ScriptSection, Shot, Storyboard, Series, Task,
    WorkflowStep,
)
from . import workflow as workflow_svc


def iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _media_url(url: str | None) -> str:
    """媒体地址统一成正斜杠。

    历史数据里 `asset.url` 是 Windows 反斜杠（`.../media/voices\\proj\\a.mp3`），
    浏览器虽然会自动纠正，但下载文件名、复制出去的链接都会带着 `\\`，
    所以在序列化出口处一次性抹平。
    """
    return (url or "").replace("\\", "/")


def asset_brief(asset: Asset | None) -> dict[str, Any] | None:
    if asset is None:
        return None
    return {
        "asset_id": asset.id,
        # None = 不属于任何项目的独立素材（素材中心里直接生成/保存的）
        "project_id": asset.project_id,
        "type": asset.type,
        "name": asset.name,
        "url": _media_url(asset.url),
        "file_path": asset.file_path,
        "status": asset.status,
        "format": asset.format,
        "width": asset.width,
        "height": asset.height,
        "duration": asset.duration,
        "fps": asset.fps,
        "size_bytes": asset.size_bytes,
        "provider": asset.provider,
        "model": asset.model,
        "workflow": asset.workflow,
        "prompt": asset.prompt,
        "parameters": asset.parameters or {},
        "source": asset.source,
        "parent_asset_id": asset.parent_asset_id,
        "task_id": asset.task_id,
        "shot_id": asset.shot_id,
        "scene_id": asset.scene_id,
        "character_id": asset.character_id,
        # 血缘：产物由哪一版提示词编译而来（新层字段，老数据为空 —— 只增不删）
        "prompt_version_id": asset.prompt_version_id,
        "role": asset.role,
        "subject_type": asset.subject_type,
        "subject_id": asset.subject_id,
        "variant_id": asset.variant_id,
        "provenance": asset.provenance or {},
        "extra": asset.extra or {},
        "created_at": iso(asset.created_at),
        "updated_at": iso(asset.updated_at),
    }


def serialize_project(project: Project, *, db: Session | None = None) -> dict[str, Any]:
    data = {
        "project_id": project.id,
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "requirement": project.requirement,
        "style": project.style,
        "language": project.language,
        "status": project.status,
        "workflow_state": project.workflow_state,
        "target_duration": project.target_duration,
        "aspect_ratio": project.aspect_ratio,
        "width": project.width,
        "height": project.height,
        "fps": project.fps,
        "progress": project.progress,
        "owner": project.owner,
        # 连续剧分集信息：series_id 为空表示这是独立项目
        "series_id": project.series_id,
        "episode_no": project.episode_no,
        "episode_label": f"第 {project.episode_no} 集" if project.episode_no else "",
        "is_episode": bool(project.series_id),
        "extra": project.extra or {},
        "created_at": iso(project.created_at),
        "updated_at": iso(project.updated_at),
    }
    if db is not None:
        wf = workflow_svc.get_workflow(db, project.id)
        data["current_step"] = workflow_svc.workflow_summary(db, project)["current_step"]
        data["workflow_id"] = wf.id if wf else None
        if project.series_id:
            series = db.get(Series, project.series_id)
            data["series_name"] = series.name if series is not None else ""
            data["series_episode_count"] = len(series.episodes) if series is not None else 0
    return data


def serialize_script(script: Script) -> dict[str, Any]:
    return {
        "script_id": script.id,
        "id": script.id,
        "project_id": script.project_id,
        "title": script.title,
        "content": script.content,
        "outline": script.outline,
        "style": script.style,
        "language": script.language,
        "version": script.version,
        "status": script.status,
        "provider": script.provider,
        "model": script.model,
        "parameters": script.parameters or {},
        "length": len(script.content or ""),
        "created_at": iso(script.created_at),
        "updated_at": iso(script.updated_at),
    }


def serialize_script_section(section: ScriptSection) -> dict[str, Any]:
    """脚本分段的对外结构。

    ``written`` 是给 UI 用的便捷判断（正文是否已有内容），不要求调用方再自己 strip。
    """
    content = section.content or ""
    return {
        "section_id": section.id,
        "id": section.id,
        "project_id": section.project_id,
        "script_id": section.script_id,
        "sequence": section.sequence,
        "code": section.code,
        "title": section.title,
        "summary": section.summary,
        "beat": section.beat,
        "content": content,
        "length": len(content),
        "written": bool(content.strip()),
        "target_duration": section.target_duration,
        "status": section.status,
        "provider": section.provider,
        "parameters": section.parameters or {},
        "created_at": iso(section.created_at),
        "updated_at": iso(section.updated_at),
    }


def serialize_scene(scene: Scene, *, include_shots: bool = False) -> dict[str, Any]:
    data = {
        "scene_id": scene.id,
        "id": scene.id,
        "project_id": scene.project_id,
        "storyboard_id": scene.storyboard_id,
        "sequence": scene.sequence,
        "code": scene.code,
        "title": scene.title,
        "summary": scene.summary,
        "location": scene.location,
        "mood": scene.mood,
        "status": scene.status,
        "shot_count": len(scene.shots or []),
        "created_at": iso(scene.created_at),
        "updated_at": iso(scene.updated_at),
    }
    if include_shots:
        data["shots"] = [serialize_shot(s) for s in scene.shots]
    return data


def serialize_shot(shot: Shot, *, db: Session | None = None, with_assets: bool = True) -> dict[str, Any]:
    data = {
        "shot_id": shot.id,
        "id": shot.id,
        "project_id": shot.project_id,
        "scene_id": shot.scene_id,
        "sequence": shot.sequence,
        "code": shot.code,
        "duration": shot.duration,
        "description": shot.description,
        "camera": shot.camera,
        "location": shot.location,
        "visual_style": shot.visual_style,
        "character_ids": shot.character_ids or [],
        "image_prompt": shot.image_prompt,
        "video_prompt": shot.video_prompt,
        "negative_prompt": shot.negative_prompt,
        "voice_script": shot.voice_script,
        "voice_speaker": shot.voice_speaker or "",
        "voice_instruct": shot.voice_instruct or "",
        "subtitle_text": shot.subtitle_text,
        "status": shot.status,
        "image_status": shot.image_status,
        "video_status": shot.video_status,
        "voice_status": shot.voice_status,
        "subtitle_status": shot.subtitle_status,
        "image_asset_id": shot.image_asset_id,
        "video_asset_id": shot.video_asset_id,
        "voice_asset_id": shot.voice_asset_id,
        "subtitle_asset_id": shot.subtitle_asset_id,
        "enhanced_video_asset_id": shot.enhanced_video_asset_id,
        "retry_count": shot.retry_count,
        "last_error": shot.last_error,
        "quality_score": shot.quality_score,
        "extra": shot.extra or {},
        "created_at": iso(shot.created_at),
        "updated_at": iso(shot.updated_at),
    }
    if db is not None and with_assets:
        data["assets"] = {
            "image": asset_brief(db.get(Asset, shot.image_asset_id) if shot.image_asset_id else None),
            "video": asset_brief(db.get(Asset, shot.video_asset_id) if shot.video_asset_id else None),
            "voice": asset_brief(db.get(Asset, shot.voice_asset_id) if shot.voice_asset_id else None),
            "subtitle": asset_brief(db.get(Asset, shot.subtitle_asset_id) if shot.subtitle_asset_id else None),
            "enhanced_video": asset_brief(
                db.get(Asset, shot.enhanced_video_asset_id) if shot.enhanced_video_asset_id else None
            ),
        }
    return data


def serialize_storyboard(storyboard: Storyboard, *, db: Session | None = None,
                         include_shots: bool = True) -> dict[str, Any]:
    scenes = []
    for scene in storyboard.scenes:
        scenes.append(serialize_scene(scene, include_shots=include_shots))
    if db is not None and include_shots:
        for scene_data in scenes:
            for shot_data in scene_data.get("shots", []):
                shot_obj = db.get(Shot, shot_data["id"])
                if shot_obj is not None:
                    shot_data["assets"] = serialize_shot(shot_obj, db=db)["assets"]
    return {
        "storyboard_id": storyboard.id,
        "id": storyboard.id,
        "project_id": storyboard.project_id,
        "title": storyboard.title,
        "synopsis": storyboard.synopsis,
        "visual_style": storyboard.visual_style,
        "scene_count": storyboard.scene_count,
        "shot_count": storyboard.shot_count,
        "status": storyboard.status,
        "provider": storyboard.provider,
        "model": storyboard.model,
        "parameters": storyboard.parameters or {},
        "scenes": scenes,
        "created_at": iso(storyboard.created_at),
        "updated_at": iso(storyboard.updated_at),
    }


def serialize_character(character: Character, *, db: Session | None = None) -> dict[str, Any]:
    data = {
        "character_id": character.id,
        "id": character.id,
        "project_id": character.project_id,
        # 系列级角色（series_id 有值、project_id 为空）跨集复用
        "series_id": character.series_id,
        "scope": "series" if (character.series_id and not character.project_id) else "episode",
        "name": character.name,
        "role": character.role,
        "description": character.description,
        "appearance": character.appearance,
        "personality": character.personality,
        "voice_style": character.voice_style,
        "reference_prompt": character.reference_prompt,
        "negative_prompt": character.negative_prompt,
        "reference_asset_id": character.reference_asset_id,
        "status": character.status,
        "provider": character.provider,
        "model": character.model,
        "parameters": character.parameters or {},
        "created_at": iso(character.created_at),
        "updated_at": iso(character.updated_at),
    }
    if db is not None:
        data["reference_asset"] = asset_brief(
            db.get(Asset, character.reference_asset_id) if character.reference_asset_id else None
        )
    return data


def serialize_workflow_step(step: WorkflowStep) -> dict[str, Any]:
    return {
        "step_key": step.step_key,
        "name": step.name,
        "description": step.description,
        "state": step.state,
        "attempts": step.attempts,
        "error": step.error,
        "output": step.output or {},
        "order_index": step.order_index,
        "review_status": step.review_status or "NONE",
        "reviewed_by": step.reviewed_by or "",
        "reviewed_at": iso(step.reviewed_at),
        "review_comment": step.review_comment or "",
        "rollback_count": step.rollback_count or 0,
        "rolled_back_at": iso(step.rolled_back_at),
        "started_at": iso(step.started_at),
        "finished_at": iso(step.finished_at),
    }


def serialize_task(task: Task, *, include_logs: bool = True) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "id": task.id,
        "project_id": task.project_id,
        "shot_id": task.shot_id,
        "character_id": task.character_id,
        "parent_task_id": task.parent_task_id,
        "type": task.type,
        "name": task.name,
        "status": task.status,
        "progress": task.progress,
        "priority": task.priority,
        "provider": task.provider,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "error": task.error,
        "result": task.result or {},
        "payload": {k: v for k, v in (task.payload or {}).items() if not k.startswith("_")},
        "logs": (task.logs or []) if include_logs else [],
        "started_at": iso(task.started_at),
        "finished_at": iso(task.finished_at),
        "created_at": iso(task.created_at),
        "updated_at": iso(task.updated_at),
    }
