"""Skill 层基础设施。

设计目标：WorkBuddy / Codex 等 Agent 只需要读 `/api/skills` 拿到清单与 JSON Schema，
就能调用本项目全部能力；不需要读源码、也不需要浏览器。

每个 Skill 有：
  name / description / category / input_schema / output_schema / async / tags
调用后统一返回：
  { ok, data, error, task_id, status }
长时间任务一律返回 taskId，Agent 通过 get_task_status 轮询，绝不阻塞。
"""
from __future__ import annotations

import inspect
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from ..models import Task
from ..services import agent_log


@dataclass
class SkillContext:
    db: Session
    actor: str = "agent"
    skill_name: str = ""
    started: float = field(default_factory=time.time)

    def log(self, message: str, level: str = "INFO", project_id: str | None = None,
            detail: dict[str, Any] | None = None) -> None:
        agent_log.log_event(
            self.db, project_id=project_id, actor=self.actor,
            event=f"skill.{self.skill_name}", message=message, level=level, detail=detail or {},
        )


@dataclass
class SkillResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_detail: str = ""
    task_id: str | None = None
    status: str = "OK"
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "error_detail": self.error_detail if not self.ok else "",
            "task_id": self.task_id,
            "elapsed_ms": self.elapsed_ms,
        }


class SkillError(Exception):
    def __init__(self, message: str, *, code: str = "BAD_INPUT", detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class Skill:
    def __init__(
        self, *, name: str, description: str, handler: Callable[..., Any],
        category: str = "general", input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None, is_async: bool = False,
        tags: tuple[str, ...] = (), examples: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.name = name
        self.description = description
        self.handler = handler
        self.category = category
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        self.output_schema = output_schema or {"type": "object"}
        self.is_async = is_async
        self.tags = tags
        self.examples = examples
        self._validator = Draft202012Validator(self.input_schema)
        self.signature = str(inspect.signature(handler))

    def invoke(self, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        started = time.time()
        errors = sorted(self._validator.iter_errors(args or {}), key=lambda e: list(e.path))
        if errors:
            message = "; ".join(
                f"{'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}" for e in errors[:6]
            )
            return SkillResult(
                ok=False, error=f"参数校验失败：{message}", status="INVALID_INPUT",
                elapsed_ms=int((time.time() - started) * 1000),
            )
        try:
            result = self.handler(ctx, **(args or {}))
        except SkillError as exc:
            return SkillResult(ok=False, error=str(exc), error_detail=exc.detail,
                               status=exc.code, elapsed_ms=int((time.time() - started) * 1000))
        except TypeError as exc:
            return SkillResult(ok=False, error=f"参数不匹配：{exc}", status="INVALID_INPUT",
                               error_detail=traceback.format_exc()[-1500:],
                               elapsed_ms=int((time.time() - started) * 1000))
        except Exception as exc:  # noqa: BLE001
            return SkillResult(ok=False, error=str(exc), status="ERROR",
                               error_detail=traceback.format_exc()[-2000:],
                               elapsed_ms=int((time.time() - started) * 1000))

        elapsed = int((time.time() - started) * 1000)
        if isinstance(result, Task):
            return SkillResult(
                ok=True, status="ACCEPTED", task_id=result.id,
                data={
                    "taskId": result.id, "task_id": result.id, "status": result.status,
                    "type": result.type, "name": result.name,
                    "note": "任务已入队，请通过 get_task_status 轮询进度",
                },
                elapsed_ms=elapsed,
            )
        data = result if isinstance(result, dict) else {"result": result}
        return SkillResult(ok=True, data=data, elapsed_ms=elapsed)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "async": self.is_async,
            "tags": list(self.tags),
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "examples": list(self.examples),
            "signature": self.signature,
        }


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> Skill:
        if skill.name in self._skills:
            raise ValueError(f"Skill 重名: {skill.name}")
        self._skills[skill.name] = skill
        return skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list(self, category: str | None = None, keyword: str | None = None) -> list[Skill]:
        items = list(self._skills.values())
        if category:
            items = [s for s in items if s.category == category]
        if keyword:
            kw = keyword.lower()
            items = [s for s in items if kw in s.name.lower() or kw in s.description.lower()]
        return sorted(items, key=lambda s: (s.category, s.name))

    def categories(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for skill in self._skills.values():
            out[skill.category] = out.get(skill.category, 0) + 1
        return dict(sorted(out.items()))


registry = SkillRegistry()


def skill(
    *, name: str, description: str, category: str = "general",
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    is_async: bool = False, tags: tuple[str, ...] = (),
    examples: tuple[dict[str, Any], ...] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        registry.register(Skill(
            name=name, description=description, handler=func, category=category,
            input_schema=input_schema, output_schema=output_schema, is_async=is_async,
            tags=tags, examples=examples,
        ))
        return func
    return decorator


def invoke_skill(db: Session, name: str, args: dict[str, Any], *, actor: str = "agent") -> dict[str, Any]:
    skill_obj = registry.get(name)
    if skill_obj is None:
        return {
            "ok": False, "status": "NOT_FOUND", "data": {},
            "error": f"未找到 Skill: {name}",
            "error_detail": "", "task_id": None, "elapsed_ms": 0,
        }
    ctx = SkillContext(db=db, actor=actor, skill_name=name)
    result = skill_obj.invoke(ctx, args or {})
    if result.ok:
        db.commit()
    else:
        db.rollback()
    agent_log.log_event(
        db, actor=actor, event=f"skill.{name}",
        level="INFO" if result.ok else "ERROR",
        message=(f"Skill 调用成功：{name}" if result.ok else f"Skill 调用失败：{name} — {result.error[:160]}"),
        detail={"args": {k: v for k, v in (args or {}).items() if not k.startswith("_")},
                "status": result.status},
    )
    db.commit()
    return result.to_dict()
