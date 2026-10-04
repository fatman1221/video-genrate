"""Preview → Confirm → Produce：四步硬闸门。

对应用户要求第 5/6 类，外加指纹失效、确认一次性、PLAN 不可投产、能力校验。

纪律：
- **预览绝不花钱**（不建 Task、不调 Provider）
- **确认一次性消费**（重复物化必须被拒）
- **预估不承诺**（只说口径不报金额）
- **指纹不符即拒**（改了任何一项就得重新预览）
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app import models as m
from app.services import generation_plans as gp
from app.services import prompt_compiler as pc


def _compiled(db, fx):
    version = pc.compile_prompt(db, fx.shot, prompt_type="image",
                                options={"provider": "comfyui", "resolution": "2K"})
    db.flush()
    return version


def _task_count(db, project_id: str) -> int:
    return db.scalar(select(func.count()).select_from(m.Task)
                     .where(m.Task.project_id == project_id))


def _mk_plan(db, fx, **over):
    version = _compiled(db, fx)
    item = {"shot_id": fx.shot.id, "modality": "image",
            "prompt_version_id": version.id, "provider": "comfyui", "resolution": "2K"}
    item.update(over)
    return gp.create_plan(db, fx.project, name="批量出图", items=[item])


# --------------------------------------------------------------------------- #
# Preview 不花钱
# --------------------------------------------------------------------------- #
def test_preview_creates_no_task(db, fx):
    plan = _mk_plan(db, fx)
    before = _task_count(db, fx.project.id)
    gp.preview_plan(db, plan)
    after = _task_count(db, fx.project.id)
    assert plan.status == "PREVIEWED"
    assert before == after == 0, f"预览产生了任务：{before} → {after}"


def test_preview_computes_fingerprint_and_summary(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    assert plan.fingerprint and len(plan.fingerprint) == 64
    assert plan.summary["item_count"] == 1
    # 预估只描述口径，不承诺金额
    assert "est_cost_note" not in plan.summary or "元" not in str(plan.summary.get("est_cost_note"))


def test_preview_repeatable(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    gp.preview_plan(db, plan)  # 还应处于 PREVIEWED，可重复预览
    assert plan.status == "PREVIEWED"


# --------------------------------------------------------------------------- #
# Confirm 闸门
# --------------------------------------------------------------------------- #
def test_confirm_requires_preview_first(db, fx):
    plan = _mk_plan(db, fx)
    with pytest.raises(gp.PlanError):
        gp.confirm_plan(db, plan, "whatever")


def test_wrong_fingerprint_rejected(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    with pytest.raises(gp.PlanError, match="指纹"):
        gp.confirm_plan(db, plan, "deadbeef" * 8)
    assert plan.status == "PREVIEWED", "被拒后状态不该前进"


def test_correct_fingerprint_confirms(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    gp.confirm_plan(db, plan, plan.fingerprint)
    assert plan.status == "CONFIRMED"
    assert plan.confirmed_at is not None
    assert plan.expires_at is not None


def test_confirm_phrase_matches_contract(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    brief = gp.plan_brief(plan)
    assert brief["confirm_phrase"] == f"CONFIRM {plan.id} {plan.fingerprint[:12]}"


# --------------------------------------------------------------------------- #
# Produce / 一次性
# --------------------------------------------------------------------------- #
def test_materialize_creates_task(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    gp.confirm_plan(db, plan, plan.fingerprint)
    ids = gp.materialize_plan(db, plan)
    assert len(ids) == 1
    assert plan.status == "RUNNING"
    item = gp.list_plan_items(db, plan.id)[0]
    assert item.status == "TASK_CREATED"
    assert item.task_id == ids[0]
    task = db.get(m.Task, ids[0])
    assert task is not None and task.shot_id == fx.shot.id


def test_materialize_twice_rejected(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    gp.confirm_plan(db, plan, plan.fingerprint)
    gp.materialize_plan(db, plan)
    with pytest.raises(gp.PlanError):
        gp.materialize_plan(db, plan)


def test_materialize_without_confirm_rejected(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    with pytest.raises(gp.PlanError):
        gp.materialize_plan(db, plan)


def test_cancel_then_materialize_rejected(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    gp.confirm_plan(db, plan, plan.fingerprint)
    gp.cancel_plan(db, plan)
    with pytest.raises(gp.PlanError):
        gp.materialize_plan(db, plan)


# --------------------------------------------------------------------------- #
# 指纹失效
# --------------------------------------------------------------------------- #
def test_fingerprint_invalidated_by_change(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    old_fp = plan.fingerprint
    plan.parameters = {"changed": True}
    db.flush()
    with pytest.raises(gp.PlanError):
        gp.confirm_plan(db, plan, old_fp)


def test_fingerprint_stable_without_change(db, fx):
    plan = _mk_plan(db, fx)
    gp.preview_plan(db, plan)
    assert gp.compute_fingerprint(db, plan) == plan.fingerprint


# --------------------------------------------------------------------------- #
# PLAN 态资产不可投产
# --------------------------------------------------------------------------- #
def test_plan_state_reference_rejected(db, fx):
    plan = _mk_plan(db, fx, reference_assets=[
        {"slot": "REF-X", "kind": "PLAN", "label": "创作者自备图"},
    ])
    with pytest.raises(gp.PlanError, match="生产输入"):
        gp.preview_plan(db, plan)


# --------------------------------------------------------------------------- #
# Provider 能力闸门
# --------------------------------------------------------------------------- #
def test_unsupported_resolution_rejected(db, fx):
    ok, note = gp.check_provider_capability(db, "image", "comfyui", "8K")
    assert ok is False
    assert "8K" in note or "分辨率" in note


def test_above_native_is_warning_only(db, fx):
    ok, note = gp.check_provider_capability(db, "image", "comfyui", "4K")
    assert ok is True, "超原生档位应放行（用户可能就要 4K），只发 warning"
    assert note and "超采样" in note


def test_unknown_provider_not_punished(db, fx):
    """provider 没注册 → 跳过校验，不要把「查不到」当成「不支持」。"""
    ok, _note = gp.check_provider_capability(db, "image", "no-such-provider", "4K")
    assert ok is True


def test_unregistered_provider_is_skipped_not_blocked(db, fx):
    ok, _ = gp.check_provider_capability(db, "video", "local-image-only-nonexistent", "2K")
    assert ok is True  # 未注册 → 跳过（误伤比漏放更糟）
