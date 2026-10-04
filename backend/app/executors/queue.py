"""异步任务队列。

- 数据库即队列：Task 表就是 job 表，服务重启后遗留的 RUNNING 任务会被回收重跑
- 支持并发 worker、优先级、进度回写、取消、失败重试（指数退避）、错误详情
- 长任务不阻塞 Agent：Agent 提交后拿 taskId，靠 get_task_status 轮询
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..core.constants import TaskStatus
from ..database import session_scope
from ..models import Project, Task
from ..services import agent_log

UTC = timezone.utc

Handler = Callable[["TaskContext"], dict[str, Any]]
_HANDLERS: dict[str, Handler] = {}


class TaskCancelled(Exception):
    """任务被取消。"""


def task_handler(task_type: str) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        _HANDLERS[task_type] = func
        return func
    return decorator


def get_handler(task_type: str) -> Handler | None:
    return _HANDLERS.get(task_type)


def registered_handlers() -> list[str]:
    return sorted(_HANDLERS)


class TaskContext:
    """传给 handler 的上下文：进度、日志、取消检查。"""

    def __init__(self, db: Session, task: Task) -> None:
        self.db = db
        self.task = task
        self._last_flush = 0.0

    def progress(self, pct: int, message: str = "") -> None:
        task = self.task
        task.progress = max(0, min(99, int(pct)))
        if message:
            task.append_log(message)
        now = time.time()
        if now - self._last_flush > 1.0:
            self._last_flush = now
            self.db.commit()
        self.check_cancel()

    def log(self, message: str, level: str = "INFO") -> None:
        self.task.append_log(message, level)
        self.db.commit()

    def event(self, key: str, message: str, level: str = "INFO", **detail: Any) -> None:
        agent_log.log_event(
            self.db, project_id=self.task.project_id, task_id=self.task.id,
            shot_id=self.task.shot_id, event=key, message=message, level=level,
            detail=detail, actor=self.task.created_by,
        )
        self.db.commit()

    def check_cancel(self) -> None:
        payload = self.task.payload or {}
        if payload.get("_cancel_requested"):
            raise TaskCancelled("任务已被取消")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def claim_next(db: Session) -> Task | None:
    """领取一个可执行任务。

    注意：**必须用「条件 UPDATE + 校验影响行数」来抢占**，不能只靠
    SELECT 之后再改状态。SQLite 没有 SELECT ... FOR UPDATE，
    多个 worker 线程会同时 SELECT 到同一个 PENDING 任务、各自把它置为
    RUNNING，然后**同一个任务被并行执行两遍** —— 表现为产物凭空翻倍
    （例如一次出 8 张定妆候选，库里却多了 16 张），而任务状态是 SUCCESS，
    日志因为双方互相覆盖只留一份，极难察觉。

    Postgres 分支保留 skip_locked 以减少无谓争抢，但正确性同样依赖下面的
    原子 UPDATE。
    """
    stmt = (
        select(Task.id)
        .where(Task.status == TaskStatus.PENDING)
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(1)
    )
    if settings.is_postgres:
        stmt = stmt.with_for_update(skip_locked=True)
    task_id = db.execute(stmt).scalars().first()
    if task_id is None:
        return None

    claimed = db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == TaskStatus.PENDING)
        .values(
            status=TaskStatus.RUNNING,
            started_at=_utcnow(),
            worker=f"{settings.app_name}-{threading.get_ident()}",
            attempts=Task.attempts + 1,
            progress=1,
        )
    )
    db.commit()
    if claimed.rowcount != 1:
        # 已被别的 worker 抢走，本轮当作没活干，避免重复执行
        return None

    task = db.get(Task, task_id)
    if task is None:  # pragma: no cover
        return None
    task.append_log("任务开始执行")
    db.commit()
    db.refresh(task)
    return task


def revive_retrying(db: Session) -> int:
    """把到期的 RETRYING 任务放回队列。"""
    now = time.time()
    rows = db.execute(select(Task).where(Task.status == TaskStatus.RETRYING)).scalars().all()
    revived = 0
    for task in rows:
        retry_after = float((task.payload or {}).get("_retry_after") or 0)
        if retry_after <= now:
            task.status = TaskStatus.PENDING
            payload = dict(task.payload or {})
            payload.pop("_retry_after", None)
            task.payload = payload
            revived += 1
    if revived:
        db.commit()
    return revived


def reclaim_stale(db: Session, *, stale_seconds: int = 1800) -> int:
    """把服务重启后遗留的 RUNNING 任务回收为 PENDING（断点恢复）。"""
    cutoff = datetime.now(UTC).timestamp() - stale_seconds
    rows = db.execute(select(Task).where(Task.status == TaskStatus.RUNNING)).scalars().all()
    reclaimed = 0
    for task in rows:
        started = task.started_at.timestamp() if task.started_at else 0
        if started < cutoff:
            task.status = TaskStatus.PENDING
            task.progress = 0
            task.append_log("检测到中断的任务，已重新排队（断点恢复）", "WARN")
            reclaimed += 1
    if reclaimed:
        db.commit()
    return reclaimed


def execute_task(db: Session, task: Task) -> None:
    """执行单个任务并回写状态。"""
    ctx = TaskContext(db, task)
    handler = get_handler(task.type)
    started = time.time()
    if handler is None:
        task.status = TaskStatus.FAILED
        task.error = f"没有注册 {task.type} 的处理器"
        task.finished_at = _utcnow()
        db.commit()
        return

    agent_log.log_event(
        db, project_id=task.project_id, task_id=task.id, shot_id=task.shot_id,
        event="task.started", message=f"开始执行：{task.name or task.type}", actor=task.created_by,
    )
    db.commit()

    try:
        result = handler(ctx) or {}
        task.status = TaskStatus.SUCCESS
        task.progress = 100
        task.result = result
        task.error = ""
        task.error_detail = ""
        task.finished_at = _utcnow()
        task.append_log("执行成功")
        elapsed = int((time.time() - started) * 1000)
        agent_log.log_event(
            db, project_id=task.project_id, task_id=task.id, shot_id=task.shot_id,
            event="task.finished", message=f"完成：{task.name or task.type}",
            duration_ms=elapsed, detail={"result": _summarize(result)},
        )
        db.commit()
    except TaskCancelled as exc:
        task.status = TaskStatus.CANCELLED
        task.error = str(exc)
        task.finished_at = _utcnow()
        task.append_log(f"已取消：{exc}", "WARN")
        db.commit()
    except Exception as exc:  # noqa: BLE001
        detail = traceback.format_exc()
        retryable = getattr(exc, "retryable", True)
        can_retry = retryable and (task.attempts or 0) < (task.max_attempts or 3)
        task.error = str(exc)[:1000]
        task.error_detail = detail[-4000:]
        if can_retry:
            backoff = min(60 * (2 ** ((task.attempts or 1) - 1)), 600)
            payload = dict(task.payload or {})
            payload["_retry_after"] = time.time() + backoff
            payload["_last_error"] = str(exc)[:400]
            task.payload = payload
            task.status = TaskStatus.RETRYING
            task.append_log(f"执行失败，{backoff}s 后重试（第 {task.attempts} 次）：{exc}", "WARN")
            agent_log.log_event(
                db, project_id=task.project_id, task_id=task.id, shot_id=task.shot_id,
                event="task.retry", level="WARN",
                message=f"任务失败，第 {task.attempts} 次重试：{str(exc)[:160]}",
                detail={"backoff": backoff},
            )
        else:
            task.status = TaskStatus.FAILED
            task.finished_at = _utcnow()
            task.append_log(f"执行失败：{exc}", "ERROR")
            agent_log.log_event(
                db, project_id=task.project_id, task_id=task.id, shot_id=task.shot_id,
                event="task.failed", level="ERROR",
                message=f"任务最终失败：{task.name or task.type} — {str(exc)[:200]}",
                detail={"error": str(exc)[:500]},
            )
        db.commit()


def _summarize(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (result or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[key] = f"[{len(value)} items]"
        elif isinstance(value, dict):
            out[key] = f"{{{', '.join(list(value)[:5])}}}"
    return out


class TaskRunner:
    """后台 worker 池。"""

    def __init__(self, workers: int | None = None) -> None:
        self.workers = workers or settings.task_workers
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._tick = 0.0
        self.running = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self.running = True
            for idx in range(self.workers):
                thread = threading.Thread(
                    target=self._loop, name=f"task-worker-{idx}", daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop.set()
            self.running = False
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    def _loop(self) -> None:
        while not self._stop.is_set():
            did_work = False
            try:
                with session_scope() as db:
                    now = time.time()
                    if now - self._tick > 5:
                        self._tick = now
                        revive_retrying(db)
                    task = claim_next(db)
                if task is not None:
                    with session_scope() as db:
                        fresh = db.get(Task, task.id)
                        if fresh is not None and fresh.status == TaskStatus.RUNNING:
                            execute_task(db, fresh)
                    did_work = True
            except Exception:  # noqa: BLE001  队列自身不能挂
                time.sleep(1.0)
            if not did_work:
                self._stop.wait(0.6)


runner = TaskRunner()


def maintenance(db: Session) -> dict[str, int]:
    """启动时维护：回收中断任务。

    进程刚启动时不可能有任务真的在跑，因此所有遗留的 RUNNING 任务都视为
    孤儿（例如服务被强杀导致 ffmpeg 卡死），无条件回收重排，实现断点恢复。
    """
    return {"reclaimed": reclaim_stale(db, stale_seconds=0), "revived": revive_retrying(db)}


def next_actions_for_project(db: Session, project: Project) -> list[str]:
    """给 Agent / UI 的下一步建议（不替代 Agent 决策，只做状态提示）。"""
    from ..services import projects as projects_svc

    status = projects_svc.project_status(db, project)
    return status["next_actions"]
