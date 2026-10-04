"""Skill 层入口：导入即注册全部 Skill。"""
from __future__ import annotations

from .base import (  # noqa: F401
    Skill, SkillContext, SkillError, SkillRegistry, SkillResult, invoke_skill, registry, skill,
)
from . import (  # noqa: F401
    content_skills, generation_skills, pipeline_skills, post_skills, series_skills,
    studio_skills,
)

__all__ = [
    "registry", "skill", "Skill", "SkillContext", "SkillResult", "SkillError",
    "invoke_skill", "SkillRegistry",
]
