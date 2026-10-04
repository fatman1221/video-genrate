"""出图链路端到端（不碰 GPU）：用 ``local`` Provider 真的跑一遍任务。

这里是**唯一**验证「编译产物真的作用到产物上」的地方 ——
单测能证明 ``compile_prompt`` 写对了，但证明不了 ``handlers`` 真的读了它。
用 PIL 渲染的 ``local`` provider，秒级完成，无外部依赖。

⚠️ 需要 worker 真的跑任务，因此本文件自己起一个临时 ``TaskRunner``
（全局 runner 在测试里绝不启动 —— 那会真去调 ComfyUI 出图）。
"""
from __future__ import annotations

import time

import pytest

from app.database import SessionLocal
from app.services import prompt_compiler as pc
from tests.conftest import skill


@pytest.fixture()
def worker():
    from app.executors.queue import TaskRunner

    runner = TaskRunner(workers=2)
    runner.start()
    try:
        yield runner
    finally:
        runner.stop()


def _wait_task(task_id: str, timeout: float = 90.0) -> str:
    from app import models as m

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
        time.sleep(0.4)
    raise AssertionError(f"任务超时未结束：{task_id}")


def _shot_asset(shot_id: str):
    from app import models as m

    db = SessionLocal()
    try:
        shot = db.get(m.Shot, shot_id)
        return db.get(m.Asset, shot.image_asset_id) if shot and shot.image_asset_id else None
    finally:
        db.close()


def test_compiled_prompt_wins_over_tampered_legacy(client, worker):
    """新层是唯一事实来源：老字段被投毒也不影响实际出图用的提示词。"""
    pid = skill(client, "bootstrap_project", {
        "name": "出图链路-新层优先", "requirement": "深夜办公室，一个人趴在桌上",
        "target_duration": 30, "shot_duration": 5,
        "create_characters": False, "generate_references": False,
    })["project_id"]
    shot_id = skill(client, "list_shots", {"project_id": pid})["shots"][0]["shot_id"]

    skill(client, "set_image_provider", {"project_id": pid, "image_provider": "local"})
    compiled = skill(client, "compile_image_prompt",
                     {"shot_id": shot_id, "provider": "local", "resolution": "720p"})
    body = compiled["compiled_prompt"]
    assert body

    # 投毒：把老字段改成完全无关的内容
    skill(client, "update_shot", {"shot_id": shot_id,
                                  "image_prompt": "投毒的老提示词：一只紫色独角兽"})

    task = skill(client, "generate_image", {"shot_id": shot_id, "provider": "local",
                                            "resolution": "720p", "aspect_ratio": "16:9"})
    assert _wait_task(task["task_id"]) == "SUCCESS"

    asset = _shot_asset(shot_id)
    assert asset is not None, "任务成功但没产出素材"
    assert "紫色独角兽" not in (asset.prompt or ""), "老字段越权覆盖了新层"
    assert body[:30] in (asset.prompt or ""), "产物用的不是编译正文"


def test_asset_carries_provenance_back_to_prompt_version(client, worker):
    """产物必须挂到产生它的 PromptVersion 上 —— 血缘闭环的落地点。"""
    pid = skill(client, "bootstrap_project", {
        "name": "出图链路-血缘", "requirement": "深夜办公室，一个人趴在桌上",
        "target_duration": 30, "shot_duration": 5,
        "create_characters": False, "generate_references": False,
    })["project_id"]
    shot_id = skill(client, "list_shots", {"project_id": pid})["shots"][0]["shot_id"]
    skill(client, "set_image_provider", {"project_id": pid, "image_provider": "local"})
    skill(client, "compile_image_prompt",
          {"shot_id": shot_id, "provider": "local", "resolution": "720p"})

    task = skill(client, "generate_image", {"shot_id": shot_id, "provider": "local",
                                            "resolution": "720p"})
    assert _wait_task(task["task_id"]) == "SUCCESS"

    asset = _shot_asset(shot_id)
    assert asset.prompt_version_id, "产物未挂 prompt_version_id"
    assert asset.role == "keyframe"
    assert (asset.provenance or {}).get("source") == "compiled"

    prov = client.get(f"/api/assets/{asset.id}/provenance").json()
    assert prov["complete"] is True, prov.get("missing")
    node = prov["chain"]["prompt_version"]
    assert node["compiled_prompt"] == asset.prompt


def test_legacy_fallback_when_shot_uncompiled(client, worker):
    """没有编译产物的镜头仍要走老链路（向后兼容，一行都不用改）。"""
    pid = skill(client, "bootstrap_project", {
        "name": "出图链路-老链路兜底", "requirement": "深夜办公室，一个人趴在桌上",
        "target_duration": 30, "shot_duration": 5,
        "create_characters": False, "generate_references": False,
    })["project_id"]
    shot_id = skill(client, "list_shots", {"project_id": pid})["shots"][0]["shot_id"]
    skill(client, "set_image_provider", {"project_id": pid, "image_provider": "local"})
    skill(client, "update_shot", {"shot_id": shot_id,
                                  "image_prompt": "老链路提示词：锈蚀舱室，蓝色应急灯"})

    task = skill(client, "generate_image", {"shot_id": shot_id, "provider": "local"})
    assert _wait_task(task["task_id"]) == "SUCCESS"

    asset = _shot_asset(shot_id)
    assert "老链路提示词" in (asset.prompt or ""), "老链路兜底失效"
    assert (asset.provenance or {}).get("source") == "legacy_shot_fields"


def test_resolution_lands_on_asset(client, worker):
    """分辨率档位必须真的落到产物尺寸上（不只是回个提示）。"""
    pid = skill(client, "bootstrap_project", {
        "name": "出图链路-尺寸", "requirement": "深夜办公室，一个人趴在桌上",
        "target_duration": 30, "shot_duration": 5,
        "create_characters": False, "generate_references": False,
    })["project_id"]
    shot_id = skill(client, "list_shots", {"project_id": pid})["shots"][0]["shot_id"]
    skill(client, "set_image_provider", {"project_id": pid, "image_provider": "local"})

    task = skill(client, "generate_image", {
        "shot_id": shot_id, "provider": "local",
        "resolution": "1080p", "aspect_ratio": "16:9",
    })
    assert task["size"]["resolution"] == "1080p"
    assert (task["size"]["width"], task["size"]["height"]) == (1920, 1080)
    assert _wait_task(task["task_id"]) == "SUCCESS"

    asset = _shot_asset(shot_id)
    assert (asset.width, asset.height) == (1920, 1080), \
        f"产物尺寸 {asset.width}x{asset.height} 与请求档位不符"
