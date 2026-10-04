"""对外契约面：Skill 注册表 + 只读端点 + 既有链路可用性。

对应测试清单第 13 类（向后兼容的对外半面）。这里只断言**能力存在且可用**，
不扫描文档文本 —— 文档里的清单容易与实现对不上。
"""
from __future__ import annotations

from tests.conftest import skill

NEW_SKILLS = (
    # 视觉设定
    "create_visual_bible", "get_visual_bible", "create_style", "set_current_style",
    # 身份 / 变体
    "set_character_identity", "create_look", "create_location", "create_location_view",
    "create_prop", "create_prop_state", "set_shot_bindings", "set_shot_location",
    # 连续性
    "create_continuity_lock", "create_continuity_delta", "verify_continuity_locks",
    "list_continuity_locks",
    # Prompt
    "compile_image_prompt", "compile_video_prompt", "get_prompt", "list_prompts",
    "check_prompt_staleness",
    # 计划
    "create_generation_plan", "preview_generation_plan", "confirm_generation_plan",
    "materialize_generation_plan", "cancel_generation_plan", "get_generation_plan",
    "list_generation_plans",
    # 血缘
    "get_asset_provenance", "get_prompt_provenance",
)


def test_all_new_skills_registered(client):
    catalog = client.get("/api/skills").json()
    names = {s["name"] for s in catalog["skills"]}
    missing = [n for n in NEW_SKILLS if n not in names]
    assert not missing, f"未注册：{missing}"
    assert len(NEW_SKILLS) == 30


def test_skill_total_grew(client):
    catalog = client.get("/api/skills").json()
    assert catalog["total"] >= 124, f"Skill 总数只有 {catalog['total']}"


def test_every_skill_has_schema(client):
    """每个 Skill 都必须声明 input_schema —— 否则 Agent 无法可靠调用。"""
    catalog = client.get("/api/skills", params={"detail": "true"}).json()
    bad = [s["name"] for s in catalog["skills"]
           if not (s.get("input_schema") or {}).get("type")]
    assert not bad, f"缺 schema：{bad[:10]}"


def test_bootstrap_project_works_on_clean_db(client):
    """⚠️ 索引回归的门禁：既有 bootstrap_project 建角色**不写 code**。

    若 ``characters(project_id, code)`` 是全量唯一索引，这一步会
    ``UNIQUE constraint failed`` —— 迁移把既有能力打挂。必须是部分索引。
    """
    boot = skill(client, "bootstrap_project", {
        "name": "回归门禁项目", "requirement": "验证既有链路可用",
        "target_duration": 30, "shot_duration": 5,
        "create_characters": True, "generate_references": False,
    })
    assert boot["project_id"]
    assert boot["shot_count"] > 0
    assert boot["character_ids"]


def test_readonly_endpoints_all_200(client, api_project):
    pid = api_project["project_id"]
    for path in (
        f"/api/projects/{pid}/visual-bible",
        f"/api/projects/{pid}/continuity-locks",
        f"/api/projects/{pid}/prompts",
        f"/api/projects/{pid}/generation-plans",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} → {resp.status_code}"


def test_visual_bible_endpoint_shape(client, api_project):
    pid = api_project["project_id"]
    body = client.get(f"/api/projects/{pid}/visual-bible").json()
    assert isinstance(body, dict)


def test_prompt_endpoints_after_compile(client, api_project):
    pid = api_project["project_id"]
    shots = skill(client, "list_shots", {"project_id": pid})["shots"]
    shot_id = shots[0]["shot_id"]

    skill(client, "compile_image_prompt",
          {"shot_id": shot_id, "options": {"provider": "comfyui", "resolution": "2K"}})

    listed = client.get(f"/api/projects/{pid}/prompts").json()
    assert listed["count"] >= 1
    assert "stale_count" in listed
    prompt_id = listed["prompts"][0]["id"]

    detail = client.get(f"/api/prompts/{prompt_id}").json()
    assert detail["prompt"]["latest_version"] >= 1
    assert detail["prompt"]["versions"]
    assert detail["staleness"]["stale"] is False

    prov = client.get(f"/api/prompts/{prompt_id}/provenance")
    assert prov.status_code == 200


def test_compile_skill_accepts_optional_fields(client, api_project):
    """⚠️ 回归门禁：``compile_image_prompt`` 传任意可选字段都不能崩。

    字段必须打包进 ``options`` 再交给 compiler；直接 ``**fields`` 展开会
    "unexpected keyword argument"（只在传了可选字段时才炸，极易漏测）。
    """
    pid = api_project["project_id"]
    shot_id = skill(client, "list_shots", {"project_id": pid})["shots"][0]["shot_id"]
    skill(client, "compile_image_prompt", {
        "shot_id": shot_id, "provider": "comfyui", "resolution": "2K",
        "aspect_ratio": "16:9", "mirror": True,
    })
    listed = client.get(f"/api/projects/{pid}/prompts",
                        params={"prompt_type": "image"}).json()
    prompt_id = listed["prompts"][0]["id"]
    detail = client.get(f"/api/prompts/{prompt_id}").json()
    v = detail["prompt"]["versions"][0]
    assert v["width"] and v["height"], "档位未固化成具体像素"
    assert (v["width"], v["height"]) == (2752, 1536)


def test_generation_plan_endpoints(client, api_project):
    pid = api_project["project_id"]
    shots = skill(client, "list_shots", {"project_id": pid})["shots"]
    created = skill(client, "create_generation_plan", {
        "project_id": pid, "name": "端点验证",
        "items": [{"shot_id": shots[0]["shot_id"], "modality": "image",
                   "provider": "local", "resolution": "720p"}],
    })
    plan_id = created["plan"]["id"]
    assert client.get(f"/api/generation-plans/{plan_id}").status_code == 200
    listed = client.get(f"/api/projects/{pid}/generation-plans").json()
    assert listed is not None


def test_legacy_skills_still_callable(client, api_project):
    """旧能力没被迁移破坏：抽查几个既有 Skill 仍可调用。"""
    pid = api_project["project_id"]
    assert skill(client, "list_projects")["projects"], "list_projects 返回空"
    assert skill(client, "list_shots", {"project_id": pid})["shots"]
    assert skill(client, "get_project", {"project_id": pid})["project"]["id"] == pid
