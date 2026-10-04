"""连续性（Continuity）：锁面卫生、在场判定、锁进正文、锁快照。

对应用户要求第 1 类：**角色连续性信息是否正确注入 Prompt**。
"""
from __future__ import annotations

import pytest

from app.services import continuity as ct
from app.services import prompt_compiler as pc

SURFACE = "dark grey zip-up stand collar jacket"


# --------------------------------------------------------------------------- #
# 锁面卫生：不合规必须直接报错（不静默放过）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("surface", "why"),
    [
        ("half-finished blue sweater in her hands", "含状态/位置词"),
        ("blue sweater, dark grey", "含标点"),
        ("镜头 03 的蓝毛衣 特写", "含镜头信息"),
        ("", "空"),
    ],
)
def test_illegal_surface_rejected(db, fx, surface, why):
    with pytest.raises(ValueError):
        ct.create_lock(db, fx.project, name=f"坏锁-{why}", surface=surface)


def test_legal_surface_accepted(db, fx):
    lock = ct.create_lock(db, fx.project, name="深灰夹克", surface=SURFACE,
                          subject_type="character", subject_id=fx.character.id)
    assert lock.surface == SURFACE
    assert lock.code.startswith("LOCK-")
    assert lock.version == 1


def test_duplicate_surface_rejected(db, fx):
    ct.create_lock(db, fx.project, name="锁A", surface=SURFACE)
    with pytest.raises(ValueError, match="已存在"):
        ct.create_lock(db, fx.project, name="锁B", surface=SURFACE)


# --------------------------------------------------------------------------- #
# 两种「假命中」—— 这是锁机制最容易失效的地方
# --------------------------------------------------------------------------- #
def test_surface_not_stuck_on_word():
    """粘在别的词上不算命中：``unchipped ...`` 不满足 ``chipped ...``。"""
    assert ct.surface_hits("unchipped white enamel mug", "chipped white enamel mug") is False


def test_surface_present_normally():
    assert ct.surface_hits("a chipped white enamel mug on the table",
                           "chipped white enamel mug") is True


def test_surface_case_and_newline_insensitive():
    assert ct.surface_hits("Dark Grey Zip-Up Stand Collar Jacket", SURFACE) is True
    assert ct.surface_hits("dark grey\nzip-up stand collar jacket", SURFACE) is True
    assert ct.surface_hits("dark   grey  zip-up stand collar jacket", SURFACE) is True


def test_surface_in_negative_is_not_evidence(db, fx):
    """负面提示词里的命中**不算在场证据**，但会被标出来便于排查。"""
    lock = ct.create_lock(db, fx.project, name="深灰夹克", surface=SURFACE)
    problems = ct.verify_surfaces("a quiet cabin, no text", f"blurry, {SURFACE}", [lock])
    assert len(problems) == 1
    assert problems[0]["reason"] == "missing_in_positive"
    assert problems[0]["in_negative"] is True


# --------------------------------------------------------------------------- #
# 编译时注锁
# --------------------------------------------------------------------------- #
def test_lock_injected_into_positive_only(db, fx):
    lock = ct.create_lock(db, fx.project, name="深灰夹克", surface=SURFACE,
                          subject_type="character", subject_id=fx.character.id)
    version = pc.compile_prompt(db, fx.shot, prompt_type="image",
                                options={"provider": "comfyui", "resolution": "2K"})
    assert lock.surface in version.compiled_prompt
    assert lock.surface not in (version.negative_prompt or "")


def test_compiled_from_records_lock_snapshot_not_hash(db, fx):
    """``compiled_from`` 存实体 id + 版本号，**不是哈希**。

    手工哈希必然腐烂（drama-skills 清点时 331 个手填哈希全部与字节对不上）。
    """
    lock = ct.create_lock(db, fx.project, name="深灰夹克", surface=SURFACE)
    version = pc.compile_prompt(db, fx.shot, prompt_type="image",
                                options={"provider": "comfyui", "resolution": "2K"})
    snap = version.compiled_from["locks"][0]
    assert snap["surface"] == lock.surface
    assert snap["version"] == lock.version
    assert snap["id"] == lock.id
    # 任何 32/64 位十六进制串都不应出现在快照里
    import re
    assert not re.search(r"\b[0-9a-f]{32,64}\b", str(snap))


def test_lock_out_of_scope_not_injected(db, fx):
    """锁只作用于 in-scope 镜头 —— 作用域外的锁不该被强行注入。"""
    ct.create_lock(db, fx.project, name="作用域外", surface="rusty iron railing",
                   shot_scope=["another-shot-id"])
    version = pc.compile_prompt(db, fx.shot, prompt_type="image",
                                options={"provider": "comfyui", "resolution": "2K"})
    assert "rusty iron railing" not in version.compiled_prompt
