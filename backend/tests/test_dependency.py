"""依赖与失效（Dependency / STALE）：改设定后老提示词必须被判 stale。

对应用户要求第 4 类：**角色修改后相关 Prompt 是否 stale**。

关键：``STALE`` 是 **service 层动态判定**（对比 ``compiled_from`` 快照里的版本号），
不是入库时写死的字段 —— 否则改设定就得回头刷新全库。
"""
from __future__ import annotations

from sqlalchemy import select

from app import models as m
from app.services import continuity as ct
from app.services import prompt_compiler as pc

OPTS = {"provider": "comfyui", "resolution": "2K"}


def _compile(db, fx):
    version = pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    db.flush()
    prompt = db.execute(select(m.Prompt).where(m.Prompt.shot_id == fx.shot.id)).scalars().one()
    return version, prompt


def test_fresh_prompt_not_stale(db, fx):
    _, prompt = _compile(db, fx)
    report = pc.check_prompt_staleness(db, prompt)
    assert report["stale"] is False, report["reasons"]


def test_character_edit_makes_prompt_stale(db, fx):
    _, prompt = _compile(db, fx)
    fx.character.identity_anchors = ["方额窄下颌", "后颈发际收成尖角", "眉骨有旧疤"]
    db.flush()
    report = pc.check_prompt_staleness(db, prompt)
    assert report["stale"] is True
    assert report["reasons"], "判 stale 却没给理由 —— 无法定位是谁变了"
    # 理由里要能看出是「角色」变了
    assert any("角色" in r or fx.character.id in r or fx.character.name in r
               for r in report["reasons"]), report["reasons"]


def test_lock_edit_makes_prompt_stale(db, fx):
    lock = ct.create_lock(db, fx.project, name="深灰夹克",
                          surface="dark grey zip-up stand collar jacket")
    _, prompt = _compile(db, fx)
    assert pc.check_prompt_staleness(db, prompt)["stale"] is False

    lock.surface = "dark grey zip-up stand collar jacket with fleece lining"
    lock.version += 1
    db.flush()
    report = pc.check_prompt_staleness(db, prompt)
    assert report["stale"] is True
    assert any("锁" in r for r in report["reasons"]), report["reasons"]


def test_bible_change_makes_prompt_stale(db, fx):
    from app.services import visual_bible as vb
    _, prompt = _compile(db, fx)
    vb.update_bible(db, fx.project, visual_logline="冷调静谧的深空舱内（改写）")
    db.flush()
    report = pc.check_prompt_staleness(db, prompt)
    assert report["stale"] is True


def test_shot_own_change_does_not_self_invalidate(db, fx):
    """回写兼容投影会 bump ``shot.updated_at``。

    若快照里带 ``shot.updated_at``，就会「刚编译完立刻判 stale」——死循环。
    这条用例就是守这个不变式。
    """
    _, prompt = _compile(db, fx)
    # 再编译一次（会再次回写 Shot 老字段 → bump updated_at）
    pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    db.flush()
    report = pc.check_prompt_staleness(db, prompt)
    assert report["stale"] is False, report["reasons"]


def test_staleness_is_readonly(db, fx):
    """查 stale 不能有副作用（不能顺手把 prompt 标记成 stale 落库）。"""
    _, prompt = _compile(db, fx)
    before = (prompt.current_version_id, prompt.latest_version, prompt.code)
    pc.check_prompt_staleness(db, prompt)
    db.flush()
    after = (prompt.current_version_id, prompt.latest_version, prompt.code)
    assert before == after
