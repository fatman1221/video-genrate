"""节点级流水线 Skill：观察节点、回退节点、重生成节点、审核节点。

这一组 Skill 让 Agent（WorkBuddy / Codex）与人类共用同一套「节点」心智模型：
每个生产阶段都是一个可观察、可回退、可重跑、可审核的节点，而不是一根黑盒进度条。
"""
from __future__ import annotations

from typing import Any

from ..core.constants import STAGE_LABELS
from ..models import Project
from ..services import pipeline as pipeline_svc
from .base import SkillContext, SkillError, skill

STAGE_HELP = "节点 key：" + "、".join(f"{k}({v})" for k, v in STAGE_LABELS.items())


def _project(db, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise SkillError(f"项目不存在: {project_id}", code="NOT_FOUND")
    return project


@skill(
    name="get_pipeline",
    category="workflow",
    description=("获取项目的节点式生产流水线：每个节点的状态、真实产出比例、"
                 "产出缩略图、审核结论与可执行动作。Web UI 的进度节点图数据源。"),
    tags=("workflow", "read", "pipeline"),
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}},
                  "required": ["project_id"]},
)
def get_pipeline(ctx: SkillContext, *, project_id: str) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    return pipeline_svc.node_view(ctx.db, project)


@skill(
    name="rollback_stage",
    category="workflow",
    description=("回退到某个节点：该节点与全部下游节点重置为待执行，下游产物引用被清空"
                 "（文件保留在素材中心可对比）。用于「回到那一步重来」。"),
    tags=("workflow", "write", "rollback"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"},
        "step_key": {"type": "string", "description": STAGE_HELP},
        "reason": {"type": "string"},
        "purge_products": {"type": "boolean", "default": True,
                           "description": "是否清空下游产物引用（关掉则只改状态）"},
        "keep_review": {"type": "boolean", "default": False}},
        "required": ["project_id", "step_key"]},
    examples=({"project_id": "proj_xxx", "step_key": "image", "reason": "画面风格不满意"},
              ),
)
def rollback_stage(ctx: SkillContext, *, project_id: str, step_key: str, reason: str = "",
                   purge_products: bool = True, keep_review: bool = False) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    try:
        return pipeline_svc.rollback_to(
            ctx.db, project, step_key, actor=ctx.actor, reason=reason,
            purge_products=purge_products, keep_review=keep_review,
        )
    except KeyError as exc:
        raise SkillError(str(exc), code="NOT_FOUND") from exc


@skill(
    name="regenerate_stage",
    category="workflow", is_async=True,
    description=("重新生产某个节点的产出，不影响上游节点。默认会先清空该节点产物再全量重跑，"
                 "并自动把下游节点置为待执行，避免出现半新半旧的脏状态。"),
    tags=("workflow", "write", "regenerate"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"},
        "step_key": {"type": "string", "description": STAGE_HELP},
        "reset": {"type": "boolean", "default": True,
                  "description": "true=全量重跑；false=只补缺"},
        "auto_rollback": {"type": "boolean", "default": True,
                          "description": "是否同时重置下游节点"},
        "reason": {"type": "string"},
        "mood": {"type": "string"}, "operations": {"type": "array", "items": {"type": "object"}},
        "with_music": {"type": "boolean"}, "with_subtitle": {"type": "boolean"},
        "auto_repair": {"type": "boolean"}},
        "required": ["project_id", "step_key"]},
    examples=({"project_id": "proj_xxx", "step_key": "video", "reason": "运镜不自然"},),
)
def regenerate_stage(ctx: SkillContext, *, project_id: str, step_key: str, reset: bool = True,
                     auto_rollback: bool = True, reason: str = "", **params: Any) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    try:
        return pipeline_svc.regenerate_stage(
            ctx.db, project, step_key, actor=ctx.actor, reset=reset,
            reason=reason, auto_rollback=auto_rollback, **params,
        )
    except KeyError as exc:
        raise SkillError(str(exc), code="NOT_FOUND") from exc


@skill(
    name="review_stage",
    category="workflow",
    description=("审核某个节点的产出：APPROVED 通过 / REJECTED 驳回。"
                 "驳回时可选择一并回退（auto_rollback）并立即重跑（regenerate）。"),
    tags=("workflow", "write", "review"),
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"},
        "step_key": {"type": "string", "description": STAGE_HELP},
        "decision": {"type": "string", "enum": ["APPROVED", "REJECTED"]},
        "comment": {"type": "string"},
        "auto_rollback": {"type": "boolean", "default": False},
        "regenerate": {"type": "boolean", "default": False}},
        "required": ["project_id", "step_key", "decision"]},
    examples=({"project_id": "proj_xxx", "step_key": "video", "decision": "REJECTED",
               "comment": "角色不一致", "auto_rollback": True, "regenerate": True},),
)
def review_stage(ctx: SkillContext, *, project_id: str, step_key: str, decision: str,
                 comment: str = "", auto_rollback: bool = False,
                 regenerate: bool = False) -> dict[str, Any]:
    project = _project(ctx.db, project_id)
    try:
        return pipeline_svc.review_stage(
            ctx.db, project, step_key, decision=decision, actor=ctx.actor,
            comment=comment, auto_rollback=auto_rollback, regenerate=regenerate,
        )
    except (KeyError, ValueError) as exc:
        raise SkillError(str(exc), code="BAD_INPUT") from exc
