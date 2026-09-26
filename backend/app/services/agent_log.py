"""Agent 执行日志服务。

Web UI 通过这些日志实时展示 Agent 的执行过程，例如：
[10:01:02] Agent 创建项目 / [10:01:05] 生成脚本 / [10:01:25] 拆分 32 个镜头 ...
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentLog


def log_event(
    db: Session,
    *,
    project_id: str | None = None,
    event: str = "",
    message: str = "",
    level: str = "INFO",
    actor: str = "agent",
    task_id: str | None = None,
    shot_id: str | None = None,
    detail: dict[str, Any] | None = None,
    duration_ms: int = 0,
    commit: bool = False,
) -> AgentLog:
    row = AgentLog(
        project_id=project_id, task_id=task_id, shot_id=shot_id, actor=actor,
        event=event, level=level, message=message, detail=detail or {}, duration_ms=duration_ms,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def list_logs(
    db: Session, *, project_id: str | None = None, task_id: str | None = None,
    level: str | None = None, limit: int = 200, offset: int = 0, after_id: str | None = None,
) -> list[AgentLog]:
    stmt = select(AgentLog).order_by(AgentLog.created_at.desc(), AgentLog.id.desc())
    if project_id:
        stmt = stmt.where(AgentLog.project_id == project_id)
    if task_id:
        stmt = stmt.where(AgentLog.task_id == task_id)
    if level:
        stmt = stmt.where(AgentLog.level == level)
    stmt = stmt.offset(offset).limit(limit)
    rows = list(db.execute(stmt).scalars())
    return rows


LOG_TEMPLATES = {
    "project.created": "Agent 创建项目：{name}",
    "script.generated": "脚本生成完成（{length} 字）",
    "storyboard.generated": "拆分 {scenes} 个场景 / {shots} 个镜头",
    "character.generated": "Character 完成：{name}",
    "image.started": "开始生成 Shot {code} 关键帧",
    "image.finished": "Shot {code} 关键帧生成完成",
    "video.started": "开始生成 Shot {code} 视频",
    "video.finished": "Shot {code} 视频生成完成",
    "voice.started": "开始生成配音 Shot {code}",
    "voice.finished": "Shot {code} 配音完成",
    "music.finished": "背景音乐生成完成",
    "subtitle.finished": "字幕生成完成（{count} 条）",
    "enhance.finished": "画质增强完成：{applied}",
    "compose.finished": "合成完成",
    "quality.started": "开始质量检测",
    "quality.finished": "质量检测完成：{passed}/{total} 通过",
    "task.retry": "任务失败，第 {attempt} 次重试",
    "task.failed": "任务失败：{error}",
    "workflow.transition": "工作流状态 {src} → {dst}",
}


def log_template(db: Session, key: str, **kwargs: Any) -> AgentLog:
    template = LOG_TEMPLATES.get(key, key)
    return log_event(db, event=key, message=template.format(**kwargs) if kwargs else template, **{
        k: v for k, v in kwargs.items() if k in ("project_id", "task_id", "shot_id", "level", "actor", "detail")
    })
