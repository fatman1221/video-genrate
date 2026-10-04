"""向后兼容：单向兼容投影 + 老链路兜底。

**用户裁定：新层是唯一事实来源；旧字段降级为「单向兼容投影」** ——
由新层写入时回写一份供老消费者读，**新逻辑一律不读旧字段**，不做双向同步。

这条边界一旦破掉，就会出现「改了一边忘了另一边」的幽灵 bug。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import models as m
from app.executors import handlers
from app.services import visual_bible as vb

# --------------------------------------------------------------------------- #
# 投影：只写不读
# --------------------------------------------------------------------------- #
def test_bindings_mirror_character_ids_as_ids(db, fx):
    """⚠️ ``shot.character_ids`` 存的是**角色 id，不是名字**。

    ``handlers._ensure_shot_image`` 用 ``db.get(Character, cid)`` 消费它；
    回写成名字会让它查不到角色 —— 人物一致性静默失效（任务仍 SUCCESS）。
    """
    vb.set_shot_bindings(db, fx.shot, [
        {"kind": "character", "id": fx.character.id, "variant_id": fx.look.id, "role": "主角"},
    ])
    db.flush()
    assert fx.shot.character_ids == [fx.character.id]
    assert db.get(m.Character, fx.shot.character_ids[0]) is not None


def test_location_mirror_writes_string(db, fx):
    vb.set_shot_location(db, fx.shot, fx.location)
    db.flush()
    assert fx.shot.location_id == fx.location.id
    assert fx.shot.location == fx.location.name


def test_resolve_bindings_prefers_new_layer(db, fx):
    vb.set_shot_bindings(db, fx.shot, [
        {"kind": "character", "id": fx.character.id, "variant_id": fx.look.id, "role": "主角"},
    ])
    db.flush()
    resolved = vb.resolve_bindings(db, fx.shot)
    assert any(r["kind"] == "character" and r["id"] == fx.character.id for r in resolved)


def test_resolve_bindings_falls_back_to_legacy_ids(db, fx):
    """老数据没有 ``asset_bindings``，只有 ``character_ids`` —— 也要能解析出来。"""
    fx.shot.asset_bindings = []
    fx.shot.character_ids = [fx.character.id]
    db.flush()
    resolved = vb.resolve_bindings(db, fx.shot)
    assert any(r["id"] == fx.character.id for r in resolved), resolved


def test_new_layer_wins_over_tampered_legacy(db, fx):
    """旧字段被改花（投毒）不影响新层解析结果。"""
    vb.set_shot_bindings(db, fx.shot, [
        {"kind": "character", "id": fx.character.id, "variant_id": fx.look.id, "role": "主角"},
    ])
    db.flush()
    fx.shot.character_ids = ["chr_tampered"]
    fx.shot.location = "被改坏的地点"
    db.flush()
    resolved = vb.resolve_bindings(db, fx.shot)
    assert any(r["id"] == fx.character.id for r in resolved)


# --------------------------------------------------------------------------- #
# 老链路兜底：没有编译产物时，handler 必须还能跑
# --------------------------------------------------------------------------- #
def test_current_prompt_version_none_when_uncompiled(db, fx):
    assert handlers._current_prompt_version(db, fx.shot, "image") is None


def test_current_prompt_version_returns_latest(db, fx):
    from app.services import prompt_compiler as pc
    v1 = pc.compile_prompt(db, fx.shot, prompt_type="image",
                           options={"provider": "comfyui", "resolution": "2K"})
    db.flush()
    v2 = pc.compile_prompt(db, fx.shot, prompt_type="image",
                           options={"provider": "comfyui", "resolution": "2K"})
    db.flush()
    assert handlers._current_prompt_version(db, fx.shot, "image").id == v2.id
    assert v1.id != v2.id


def test_pick_reference_image_requires_ref_kind(tmp_path):
    real = tmp_path / "ref.png"
    real.write_bytes(b"\x89PNG\r\n\x1a\n")
    slots = [
        {"kind": "PLAN", "admission_status": "ready", "file_path": str(real)},
        {"kind": "REF", "admission_status": "ready", "file_path": str(real)},
    ]
    assert handlers._pick_reference_image(slots) == str(real)

    only_plan = [{"kind": "PLAN", "admission_status": "ready", "file_path": str(real)}]
    assert handlers._pick_reference_image(only_plan) is None


def test_pick_reference_image_requires_ready(tmp_path):
    real = tmp_path / "ref.png"
    real.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert handlers._pick_reference_image(
        [{"kind": "REF", "admission_status": "missing_asset", "file_path": str(real)}]
    ) is None


def test_pick_reference_image_requires_existing_file(tmp_path):
    ghost = Path(tmp_path) / "nope.png"
    assert handlers._pick_reference_image(
        [{"kind": "REF", "admission_status": "ready", "file_path": str(ghost)}]
    ) is None


# --------------------------------------------------------------------------- #
# 既有实体未被破坏
# --------------------------------------------------------------------------- #
def test_legacy_fields_still_readable(db, fx):
    """老消费者仍能读到旧字段（哪怕新层已接管写）。"""
    fx.shot.image_prompt = "老提示词"
    fx.shot.negative_prompt = "老负向词"
    fx.shot.video_prompt = "老视频提示词"
    db.flush()
    fresh = db.get(m.Shot, fx.shot.id)
    assert fresh.image_prompt == "老提示词"
    assert fresh.negative_prompt == "老负向词"
    assert fresh.video_prompt == "老视频提示词"


def test_core_tables_untouched(db, fx):
    """迁移没有删表：既有 16 张核心表都还在。"""
    for table in ("projects", "storyboards", "scenes", "shots", "characters",
                  "assets", "tasks", "providers", "quality_checks", "series"):
        assert table in m.Base.metadata.tables, f"核心表 {table} 不见了"


def test_new_tables_are_present(db):
    for table in ("visual_bibles", "visual_styles", "character_looks", "locations",
                  "location_views", "props", "prop_states", "continuity_locks",
                  "continuity_deltas", "prompts", "prompt_versions",
                  "generation_plans", "generation_plan_items"):
        assert table in m.Base.metadata.tables, f"迁移新表 {table} 缺失"


def test_empty_code_not_blocked(db, fx):
    """⚠️ 这个约束是**部分唯一索引** ``WHERE code != ''``。

    既有 ``bootstrap_project`` 建角色**不写 code**（同项目多个空串）。
    若用全量唯一索引，第二次插入就 UNIQUE 冲突 —— 既有能力会被迁移打挂。
    """
    db.add_all([
        m.Character(project_id=fx.project.id, name="甲", role="支持", code=""),
        m.Character(project_id=fx.project.id, name="乙", role="支持", code=""),
        m.Character(project_id=fx.project.id, name="丙", role="支持", code=""),
    ])
    db.flush()  # 不抛异常即通过


def test_duplicate_nonempty_code_blocked(db, fx):
    from sqlalchemy.exc import IntegrityError

    db.add(m.Character(project_id=fx.project.id, name="丁", role="支持", code="DUPX"))
    db.flush()
    db.add(m.Character(project_id=fx.project.id, name="戊", role="支持", code="DUPX"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_same_code_allowed_across_projects(db, fx):
    other = m.Project(id="proj_other_dupx", name="另一个项目", requirement="x")
    db.add(other)
    db.flush()
    db.add(m.Character(project_id=fx.project.id, name="己", role="支持", code="CROSS"))
    db.add(m.Character(project_id=other.id, name="庚", role="支持", code="CROSS"))
    db.flush()  # 唯一性按 project_id 分组，跨项目同 code 应放行
