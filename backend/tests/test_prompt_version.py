"""Prompt 版本：只增不改、版本递增、单向兼容投影。

对应用户要求第 2 类：**改设定是否产生新版本**。
"""
from __future__ import annotations

from sqlalchemy import select

from app import models as m
from app.services import prompt_compiler as pc

OPTS = {"provider": "comfyui", "resolution": "2K"}


def test_first_compile_creates_v1(db, fx):
    version = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    assert version.version == 1
    assert version.status == "READY"
    assert version.compiled_prompt.strip()
    prompt = db.execute(select(m.Prompt).where(m.Prompt.shot_id == fx.shot.id)).scalars().one()
    assert prompt.latest_version == 1
    assert prompt.current_version_id == version.id
    assert prompt.code.startswith("IMG-")


def test_recompile_creates_v2_and_keeps_v1(db, fx):
    v1 = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    v1_text = v1.compiled_prompt

    db.flush()
    v2 = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))

    assert v2.version == 2
    assert v2.id != v1.id
    fresh_v1 = db.get(m.PromptVersion, v1.id)
    assert fresh_v1.compiled_prompt == v1_text, "v1 被就地修改 —— 违反「只增不改」"
    assert fresh_v1.version == 1

    prompt = db.execute(select(m.Prompt).where(m.Prompt.shot_id == fx.shot.id)).scalars().one()
    assert prompt.latest_version == 2
    assert prompt.current_version_id == v2.id


def test_old_version_not_deleted(db, fx):
    ids = []
    for _ in range(3):
        ids.append(pc.compile_prompt(db, fx.shot, prompt_type="image",
                                     options=dict(OPTS)).id)
        db.flush()
    for vid in ids:
        assert db.get(m.PromptVersion, vid) is not None


def test_mirror_writes_legacy_field(db, fx):
    """单向兼容投影：新层是唯一事实来源，旧字段被回写一份供老消费者读。"""
    version = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    db.flush()
    assert fx.shot.image_prompt == version.compiled_prompt


def test_mirror_can_be_disabled(db, fx):
    fx.shot.image_prompt = "手工老提示词"
    db.flush()
    pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS), mirror=False)
    db.flush()
    assert fx.shot.image_prompt == "手工老提示词"


def test_video_prompt_is_separate_type(db, fx):
    img = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    db.flush()
    vid = pc.compile_prompt(db, fx.shot, prompt_type="video", options=dict(OPTS))
    assert img.id != vid.id
    prompts = db.execute(
        select(m.Prompt).where(m.Prompt.shot_id == fx.shot.id)
    ).scalars().all()
    assert {p.type for p in prompts} == {"image", "video"}


def test_compile_is_idempotent_in_content(db, fx):
    """同输入重复编译：内容一致（版本号不同），便于比对"改了哪几字"。"""
    v1 = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    db.flush()
    v2 = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    assert v1.compiled_prompt == v2.compiled_prompt
