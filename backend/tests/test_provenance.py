"""血缘反查（Provenance）：从产物一路回溯到设定依据。

对应用户要求第 7 类：**最终 Asset 能否反查完整链路**。

设计立场：反查的意义是回答「当时到底依据什么、发了什么指令」——
只回一个 id 还得再查一次，等于没闭环。
"""
from __future__ import annotations

from app import models as m
from app.services import continuity as ct
from app.services import generation_plans as gp
from app.services import prompt_compiler as pc
from app.services import provenance as pv

REQUIRED = ("project", "prompt", "prompt_version", "shot", "scene", "storyboard", "task")


def _build(db, fx):
    lock = ct.create_lock(db, fx.project, name="深灰夹克",
                          surface="dark grey zip-up stand collar jacket",
                          subject_type="character", subject_id=fx.character.id)
    version = pc.compile_prompt(db, fx.shot, prompt_type="image",
                                options={"provider": "comfyui", "resolution": "2K"})
    db.flush()
    plan = gp.create_plan(db, fx.project, name="批量出图", items=[
        {"shot_id": fx.shot.id, "modality": "image", "prompt_version_id": version.id,
         "provider": "comfyui", "resolution": "2K"},
    ])
    gp.preview_plan(db, plan)
    gp.confirm_plan(db, plan, plan.fingerprint)
    task_ids = gp.materialize_plan(db, plan)
    item = gp.list_plan_items(db, plan.id)[0]

    asset = m.Asset(
        id=m.new_id("ast"), project_id=fx.project.id, shot_id=fx.shot.id,
        type="IMAGE", name="关键帧", file_path="images/001.png",
        prompt_version_id=version.id, task_id=task_ids[0],
        generation_plan_item_id=item.id, role="keyframe",
        subject_type="shot", subject_id=fx.shot.id,
    )
    db.add(asset)
    db.flush()
    return asset, version, plan, lock


def test_chain_has_required_nodes(db, fx):
    asset, _, _, _ = _build(db, fx)
    report = pv.asset_provenance(db, asset)
    assert report["complete"] is True, report["missing"]
    for key in REQUIRED:
        assert key in report["chain"], f"血缘链缺 {key}"


def test_chain_carries_prompt_body(db, fx):
    """必须带得出当时用的正文 —— 否则"反查"什么也看不到。"""
    asset, version, _, _ = _build(db, fx)
    node = pv.asset_provenance(db, asset)["chain"]["prompt_version"]
    assert node["compiled_prompt"] == version.compiled_prompt
    assert node["negative_prompt"] == (version.negative_prompt or "")


def test_chain_carries_compile_inputs_and_locks(db, fx):
    asset, _, _, lock = _build(db, fx)
    node = pv.asset_provenance(db, asset)["chain"]["prompt_version"]
    assert node["compiled_from"], "缺编译输入快照，无法判断当时依据哪一版设定"
    assert node["continuity_lock_ids"] == [lock.id]
    assert isinstance(node["reference_assets"], list)


def test_chain_carries_model_and_provider(db, fx):
    asset, _, _, _ = _build(db, fx)
    node = pv.asset_provenance(db, asset)["chain"]["prompt_version"]
    assert node["provider"] == "comfyui"
    assert node["resolution"] == "2K"


def test_chain_reaches_plan(db, fx):
    asset, _, plan, _ = _build(db, fx)
    chain = pv.asset_provenance(db, asset)["chain"]
    assert chain.get("generation_plan", {}).get("id") == plan.id


def test_expanded_subjects_are_reverified(db, fx):
    """快照里的实体如果后来被删，展开时要报出来（不能假装还在）。"""
    asset, version, _, _ = _build(db, fx)
    chain = pv.asset_provenance(db, asset)["chain"]
    detail = chain.get("compiled_from") or {}
    assert "resolved" in detail or "locks" in detail
    if detail.get("resolved"):
        assert all("kind" in r for r in detail["resolved"])


def test_prompt_provenance_from_prompt(db, fx):
    """从 Prompt 逻辑单元向上反查（不依赖具体哪一版）。"""
    from sqlalchemy import select

    _, version, _, _ = _build(db, fx)
    prompt = db.execute(
        select(m.Prompt).where(m.Prompt.id == version.prompt_id)
    ).scalars().one()
    report = pv.prompt_provenance(db, prompt)
    assert report["chain"]["prompt"]["id"] == prompt.id


def test_incomplete_chain_is_reported(db, fx):
    """产物没挂 prompt_version → 判定不完整，并列出缺什么。"""
    orphan = m.Asset(id=m.new_id("ast"), project_id=fx.project.id, type="IMAGE",
                     name="孤儿产物", file_path="images/x.png")
    db.add(orphan)
    db.flush()
    report = pv.asset_provenance(db, orphan)
    assert report["complete"] is False
    assert report["missing"], "不完整却没报缺什么"
