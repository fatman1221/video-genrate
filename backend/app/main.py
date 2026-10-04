"""AI Video Agent Studio —— 后端入口。

启动顺序：
1. 建表（PostgreSQL / SQLite 均可）
2. 注册 Provider 与 Task Handler
3. 同步 Provider 注册表到数据库
4. 回收上次中断的任务（断点恢复）
5. 启动后台任务 worker
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db, session_scope
from .executors import load_handlers, maintenance, runner
from .providers import register_all, reload_from_db, snapshot, sync_providers_table
from .routers import (
    assets, content, projects, series, settings as settings_router, skills, studio, system, tasks,
)
from .skills import registry as skill_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("studio")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("初始化数据库：%s", settings.database_url.split("@")[-1])
    init_db()
    register_all()
    handlers = load_handlers()
    with session_scope() as db:
        count = sync_providers_table(db)
        # 恢复用户在「系统设置」里保存的选择（默认引擎 / 模型名 / 云端凭证）。
        # 必须在 worker 启动前完成，否则首个任务会拿到代码默认值。
        restored = reload_from_db(db)
        result = maintenance(db)
    logger.info("Provider 已同步 %s 个，运行时配置恢复 %s 条，当前默认：%s",
                count, restored, snapshot().get("defaults"))
    logger.info("任务处理器 %s 个：%s", len(handlers), ", ".join(handlers))
    logger.info("启动维护：%s", result)
    logger.info("Skill 已注册 %s 个，分类：%s", len(skill_registry.list()),
                ", ".join(f"{k}={v}" for k, v in skill_registry.categories().items()))
    settings.storage_path.mkdir(parents=True, exist_ok=True)
    runner.start()
    logger.info("任务 worker 已启动（%s 个）", runner.workers)
    try:
        yield
    finally:
        runner.stop()
        logger.info("任务 worker 已停止")


app = FastAPI(
    title=settings.app_name,
    description=(
        "工程化的 AI 视频生产基础设施：Agent（WorkBuddy / Codex）负责思考与决策，"
        "本服务负责状态管理、数据持久化与执行能力。全部能力以 Skill 形式对外暴露。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态素材服务（图片 / 视频 / 音频 / 字幕）
app.mount("/media", StaticFiles(directory=str(settings.storage_path), html=False), name="media")

app.include_router(system.router)
app.include_router(projects.router)
app.include_router(series.router)
app.include_router(content.router)
app.include_router(assets.router)
app.include_router(settings_router.router)
app.include_router(tasks.router)
app.include_router(skills.router)
app.include_router(studio.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": str(exc), "path": request.url.path},
    )


@app.get("/")
def root() -> dict:
    return {
        "app": settings.app_name,
        "version": "1.0.0",
        "docs": "/docs",
        "skills": f"{settings.api_prefix}/skills",
        "health": f"{settings.api_prefix}/health",
        "message": "Agent 请先 GET /api/skills 获取能力清单，再 POST /api/skills/{name}/invoke 调用",
    }
