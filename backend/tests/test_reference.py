"""参考图（Reference）：槽位三态、用途边界、准入状态。

对应用户要求第 3 类：**Shot 能否找到 Character/Location 的 Reference**。

三态记号：
- ``IMG-`` 提示词条目（可作 job 来源）
- ``REF-`` 项目内真实存在（可投产）
- ``PLAN-`` 创作者自备、项目内不存在（**不可作生产输入**）
"""
from __future__ import annotations

from app import models as m
from app.services import prompt_compiler as pc

OPTS = {"provider": "comfyui", "resolution": "2K"}


def _ready_asset(db, project_id: str, *, path: str, name: str = "定妆图") -> m.Asset:
    asset = m.Asset(id=m.new_id("ast"), project_id=project_id, type="IMAGE",
                    name=name, file_path=path, status="READY")
    db.add(asset)
    db.flush()
    return asset


def test_no_reference_no_fake_slot(db, fx):
    """没有参考图就不该产生空槽位（假槽位会让下游以为有图可依）。"""
    version = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    assert version.reference_assets == []


def test_character_reference_becomes_ready_slot(db, fx):
    asset = _ready_asset(db, fx.project.id, path=f"images/{fx.character.id}.png")
    fx.character.reference_asset_id = asset.id
    db.flush()

    version = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    slots = version.reference_assets
    assert len(slots) == 1
    slot = slots[0]
    assert slot["kind"] == "REF"
    assert slot["admission_status"] == "ready"
    assert slot["asset_id"] == asset.id
    assert "identity" in " ".join(slot["may_control"]).lower() or slot["role"]


def test_missing_asset_is_flagged_not_silently_dropped(db, fx):
    """基准图被删了 —— 要标 ``missing_asset``，而不是当它不存在悄悄跳过。"""
    fx.character.reference_asset_id = "ast_does_not_exist"
    db.flush()
    version = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    assert version.reference_assets[0]["admission_status"] == "missing_asset"


def test_not_ready_asset_blocked(db, fx):
    asset = _ready_asset(db, fx.project.id, path=f"images/{fx.character.id}.png")
    asset.status = "PENDING"
    fx.character.reference_asset_id = asset.id
    db.flush()
    version = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    assert version.reference_assets[0]["admission_status"] == "not_ready"


def test_slot_declares_control_boundary(db, fx):
    """每个槽位必须声明它能控制什么、绝不能控制什么 —— 否则参考图会串味。"""
    asset = _ready_asset(db, fx.project.id, path=f"images/{fx.character.id}.png")
    fx.character.reference_asset_id = asset.id
    db.flush()
    version = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    slot = version.reference_assets[0]
    assert slot["may_control"], "未声明 may_control"
    assert slot["must_not_control"], "未声明 must_not_control"


def test_reference_vocabulary_is_closed():
    """用途与记号都是封闭词表 —— 不允许自由发挥（自由文本无法被校验）。"""
    from app.services.visual_bible import REFERENCE_KINDS, REFERENCE_ROLES
    assert len(REFERENCE_ROLES) == 9, REFERENCE_ROLES
    assert len(set(REFERENCE_ROLES)) == 9, "用途词表有重复"
    # 三态记号含义完全不同，禁止混用
    assert tuple(REFERENCE_KINDS) == ("REF", "IMG", "PLAN")
