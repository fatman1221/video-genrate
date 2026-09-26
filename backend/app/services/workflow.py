"""Workflow 服务：状态机 + 步骤追踪 + 动态跳转。

既支持正常流水线推进，也支持 Agent 决策后的任意跳转
（FAILED -> ANALYZE -> REGENERATE -> QUALITY_CHECK）。
每一步的尝试次数、错误、输出都会被记录，用于断点恢复。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.constants import (
    STEP_DONE_STATE, WORKFLOW_STEPS, WorkflowState, transition_allowed,
)
from ..models import Project, Workflow, WorkflowStep
from . import agent_log


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_workflow(db: Session, project: Project) -> Workflow:
    """确保项目有且仅有一个默认工作流，并初始化全部步骤。"""
    wf = db.execute(
        select(Workflow).where(Workflow.project_id == project.id).order_by(Workflow.created_at.asc())
    ).scalars().first()
    if wf is None:
        wf = Workflow(project_id=project.id, state=WorkflowState.PROJECT_CREATED)
        db.add(wf)
        db.flush()
        for idx, (key, name, desc) in enumerate(WORKFLOW_STEPS):
            db.add(WorkflowStep(
                workflow_id=wf.id, project_id=project.id, step_key=key,
                name=name, description=desc, order_index=idx, state="PENDING",
            ))
        db.flush()
    else:
        existing = {s.step_key for s in wf.steps}
        for idx, (key, name, desc) in enumerate(WORKFLOW_STEPS):
            if key not in existing:
                db.add(WorkflowStep(
                    workflow_id=wf.id, project_id=project.id, step_key=key,
                    name=name, description=desc, order_index=idx, state="PENDING",
                ))
        db.flush()
    return wf


def get_workflow(db: Session, project_id: str) -> Workflow | None:
    return db.execute(
        select(Workflow).where(Workflow.project_id == project_id).order_by(Workflow.created_at.asc())
    ).scalars().first()


def get_step(db: Session, workflow: Workflow, step_key: str) -> WorkflowStep | None:
    for step in workflow.steps:
        if step.step_key == step_key:
            return step
    return None


def transition(
    db: Session, project: Project, target: str, *,
    actor: str = "agent", reason: str = "", force: bool = False, commit: bool = True,
) -> dict[str, Any]:
    """迁移项目工作流状态。force=True 时允许 Agent 强制跳转（会记录警告）。"""
    wf = ensure_workflow(db, project)
    current = wf.state
    if current == target:
        return {"changed": False, "state": current, "message": "状态未变化"}
    if not force and not transition_allowed(current, target):
        return {
            "changed": False, "state": current, "allowed": False,
            "message": f"非法状态迁移 {current} → {target}；如需强制跳转请传 force=true",
        }

    wf.previous_state = current
    wf.state = target
    wf.history = list(wf.history or []) + [{
        "ts": utcnow().isoformat(), "from": current, "to": target,
        "actor": actor, "reason": reason, "forced": force and current != target,
    }]
    project.workflow_state = target
    agent_log.log_event(
        db, project_id=project.id, event="workflow.transition", actor=actor,
        message=f"工作流状态 {current} → {target}" + (f"（{reason}）" if reason else ""),
        level="WARN" if force else "INFO",
        detail={"from": current, "to": target, "reason": reason},
    )
    if commit:
        db.commit()
    return {"changed": True, "state": target, "from": current, "message": "状态已迁移"}


def advance(db: Session, project: Project, *, actor: str = "agent", reason: str = "") -> dict[str, Any]:
    """推进到正常流程的下一个状态。"""
    from ..core.constants import next_state

    wf = ensure_workflow(db, project)
    target = next_state(wf.state)
    if target is None:
        return {"changed": False, "state": wf.state, "message": "已是终态"}
    return transition(db, project, target, actor=actor, reason=reason)


def start_step(db: Session, project: Project, step_key: str, *, actor: str = "agent") -> WorkflowStep:
    wf = ensure_workflow(db, project)
    step = get_step(db, wf, step_key)
    if step is None:
        raise KeyError(f"未知工作流步骤: {step_key}")
    step.state = "RUNNING"
    step.started_at = utcnow()
    step.attempts = (step.attempts or 0) + 1
    step.error = ""
    db.flush()
    return step


def complete_step(
    db: Session, project: Project, step_key: str, *,
    output: dict[str, Any] | None = None, actor: str = "agent", commit: bool = True,
) -> WorkflowStep | None:
    wf = ensure_workflow(db, project)
    step = get_step(db, wf, step_key)
    if step is None:
        return None
    step.state = "SUCCESS"
    step.finished_at = utcnow()
    step.output = {**(step.output or {}), **(output or {})}
    done_state = STEP_DONE_STATE.get(step_key)
    if done_state:
        result = transition(db, project, done_state, actor=actor,
                            reason=f"步骤 {step_key} 完成", commit=False)
        # 回退后重跑时会跳跃式推进（例如从 SCRIPT_GENERATED 直接到 IMAGE_GENERATED），
        # 这属于自愈行为，用 force 补齐状态而不是静默失败。
        if not result.get("changed") and not result.get("allowed", True):
            transition(db, project, done_state, actor=actor, force=True,
                       reason=f"步骤 {step_key} 完成后补齐状态", commit=False)
    if commit:
        db.commit()
    return step


def fail_step(
    db: Session, project: Project, step_key: str, error: str, *, commit: bool = True,
) -> WorkflowStep | None:
    wf = ensure_workflow(db, project)
    step = get_step(db, wf, step_key)
    if step is None:
        return None
    step.state = "FAILED"
    step.finished_at = utcnow()
    step.error = error[:2000]
    agent_log.log_event(
        db, project_id=project.id, event="step.failed", level="ERROR",
        message=f"步骤「{step.name}」失败：{error[:200]}",
    )
    if commit:
        db.commit()
    return step


def reset_step(db: Session, project: Project, step_key: str, *, commit: bool = True) -> WorkflowStep | None:
    """把步骤重置为 PENDING（用于重跑 / 断点恢复）。"""
    wf = ensure_workflow(db, project)
    step = get_step(db, wf, step_key)
    if step is None:
        return None
    step.state = "PENDING"
    step.finished_at = None
    step.error = ""
    if commit:
        db.commit()
    return step


def workflow_summary(db: Session, project: Project) -> dict[str, Any]:
    wf = ensure_workflow(db, project)
    steps = [
        {
            "step_key": s.step_key, "name": s.name, "description": s.description,
            "state": s.state, "attempts": s.attempts, "error": s.error,
            "output": s.output or {}, "order_index": s.order_index,
            "started_at": s.started_at, "finished_at": s.finished_at,
        }
        for s in sorted(wf.steps, key=lambda x: x.order_index)
    ]
    current_index = next((i for i, s in enumerate(steps) if s["state"] != "SUCCESS"), len(steps))
    return {
        "id": wf.id,
        "state": wf.state,
        "previous_state": wf.previous_state,
        "status": wf.status,
        "paused": wf.paused,
        "steps": steps,
        "current_step": steps[current_index]["step_key"] if current_index < len(steps) else "done",
        "history": (wf.history or [])[-50:],
    }
