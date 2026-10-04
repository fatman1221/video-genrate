"""真实 ComfyUI 出图（默认不跑，需显式 ``-m gpu``）。

    ../.venv/Scripts/python.exe -m pytest -m gpu -s

⚠️ 会真实占用 GPU、写真实的 storage 目录。先释放显存（ComfyUI 的 ``/free``），
并优先用 2K（模型原生档）验证通路；4K 属超采样，慢且不提升成片画质。

设计取舍：这条用例**不能**进默认套件 —— 默认套件必须能在没有 GPU、
没有 ComfyUI 的机器上全绿，否则 CI 形同虚设。
"""
from __future__ import annotations

import socket
import time

import pytest

from tests.conftest import skill

pytestmark = pytest.mark.gpu


def _comfy_reachable() -> bool:
    from urllib.parse import urlparse

    from app.config import settings

    parsed = urlparse(settings.comfyui_base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8188
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return True
    except OSError:
        return False


@pytest.fixture()
def worker():
    from app.executors.queue import TaskRunner

    runner = TaskRunner(workers=1)
    runner.start()
    try:
        yield runner
    finally:
        runner.stop()


def _wait_task(task_id: str, timeout: float = 2400.0) -> str:
    from app import models as m

    from app.database import SessionLocal

    deadline = time.time() + timeout
    while time.time() < deadline:
        db = SessionLocal()
        try:
            task = db.get(m.Task, task_id)
            status = str(task.status) if task else ""
        finally:
            db.close()
        if status in ("SUCCESS", "FAILED", "CANCELLED"):
            return status
        time.sleep(5.0)
    raise AssertionError(f"任务超时未结束：{task_id}")


@pytest.mark.skipif(not _comfy_reachable(), reason="ComfyUI 未运行（127.0.0.1:8188）")
def test_comfy_image_2k(client, worker):
    pid = skill(client, "bootstrap_project", {
        "name": "GPU 出图验证 2K", "requirement": "深夜办公室，一个人趴在桌上，屏幕堆满标签页",
        "target_duration": 30, "shot_duration": 5,
        "create_characters": False, "generate_references": False,
    })["project_id"]
    shot_id = skill(client, "list_shots", {"project_id": pid})["shots"][0]["shot_id"]

    skill(client, "set_image_provider", {
        "project_id": pid, "image_provider": "comfyui",
        "scene_workflow_name": "qwen_image_scene",
        "negative_prompt": "text, watermark, lowres, blurry, extra fingers",
    })
    skill(client, "compile_image_prompt",
          {"shot_id": shot_id, "resolution": "2K", "aspect_ratio": "16:9"})

    task = skill(client, "generate_image", {"shot_id": shot_id, "provider": "comfyui",
                                            "resolution": "2K", "aspect_ratio": "16:9"})
    assert task["size"]["above_native"] is False
    assert _wait_task(task["task_id"]) == "SUCCESS"

    rows = skill(client, "list_shots", {"project_id": pid})["shots"]
    row = next(s for s in rows if s["shot_id"] == shot_id)
    asset = skill(client, "get_asset", {"asset_id": row["image_asset_id"]})["asset"]

    # 档位 → 模板 → 产物尺寸，三环必须一致
    assert (asset["width"], asset["height"]) == (2752, 1536)
    assert asset["prompt_version_id"], "产物未挂血缘"
    assert asset["provider"] == "comfyui"
