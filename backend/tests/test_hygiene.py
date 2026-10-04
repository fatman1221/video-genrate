"""正文卫生 + 文字政策两层映射。

对应测试清单第 10/11 类。两条都是**编译期硬失败**（不产生半成品），
因为这两类问题一旦漏进正文，模型会照单全收地把垃圾当指令。
"""
from __future__ import annotations

import pytest

from app.services import prompt_compiler as pc
from app.services import visual_bible as vb

OPTS = {"provider": "comfyui", "resolution": "2K"}


# --------------------------------------------------------------------------- #
# 正文卫生
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("a cabin scene --ar 16:9", "引擎参数语法"),
        ("a cabin scene --v 6", "引擎参数语法"),
        ("weighted::2 term", "引擎权重语法"),
        ("scene with " + "a" * 64, "64 位十六进制哈希"),
        ("see compiled_from for details", "JSON 键名/字段路径"),
        ("render LOCK-3 for this shot", "内部代号"),
        ("read C:/Users/x/out.png", "文件路径/盘符"),
        ("/storage/images/a.png", "文件路径"),
        ("请模型务必保持构图一致", "流程说明"),
        ("这是第 3 次尝试", "重试历史"),
    ],
)
def test_hygiene_rejects(text, why):
    problems = pc.validate_hygiene(text)
    assert problems, f"应被拒（{why}）却通过了：{text!r}"


def test_hygiene_accepts_clean_prompt():
    clean = ("A quiet spacecraft cabin at night, cold side light, "
             "dark grey zip-up stand collar jacket, subtle metal and fabric texture.")
    assert pc.validate_hygiene(clean) == []


def test_dirty_body_fails_compilation(db, fx):
    """卫生不达标 → 编译失败，且**不留下半成品版本**。

    用镜头描述投毒（它会直接进正向正文第 1 段），而不是 ``raw_prompt``
    —— 后者只作「版本差异」比对的基线，不进正文。
    """
    fx.shot.description = "a quiet cabin --ar 16:9"
    db.flush()
    with pytest.raises(pc.CompileError):
        pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))
    from sqlalchemy import select

    from app import models as m
    prompts = db.execute(
        select(m.Prompt).where(m.Prompt.shot_id == fx.shot.id)
    ).scalars().all()
    assert prompts == [], "编译失败却留下了 Prompt/版本记录（半成品）"


# --------------------------------------------------------------------------- #
# 文字政策两层映射
# --------------------------------------------------------------------------- #
def test_exact_readable_without_text_rejected(db, fx):
    prop = vb.create_prop(db, fx.project, name="终端机",
                          text_policy={"mode": "exact_readable"})
    vb.set_shot_bindings(db, fx.shot, [
        {"kind": "character", "id": fx.character.id, "variant_id": fx.look.id, "role": "主角"},
        {"kind": "prop", "id": prop.id, "role": "关键道具"},
    ])
    db.flush()
    with pytest.raises(pc.CompileError, match="精确文字"):
        pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))


def test_exact_readable_vs_global_no_text_conflict(db, fx):
    """``readable`` 与「全局无文字约束」**不能共存** —— 政策自相矛盾要报错。"""
    prop = vb.create_prop(db, fx.project, name="终端机", text_policy={
        "mode": "exact_readable", "text": "SYSTEM ONLINE",
    })
    vb.set_shot_bindings(db, fx.shot, [
        {"kind": "character", "id": fx.character.id, "variant_id": fx.look.id, "role": "主角"},
        {"kind": "prop", "id": prop.id, "role": "关键道具"},
    ])
    bible = vb.get_bible(db, fx.project.id)
    bible.text_policy = {"readable_text_allowed": False}
    db.flush()
    with pytest.raises(pc.CompileError, match="不可共存"):
        pc.compile_prompt(db, fx.shot, prompt_type="image", options=dict(OPTS))


def test_text_policy_two_layer_mapping():
    """模式 → 呈现方式的多值映射（``exact_readable`` 允许 readable + 后期制作两条路）。"""
    from app.services.prompt_compiler import TEXT_POLICY_PRESENTATION
    assert "blank" in TEXT_POLICY_PRESENTATION["no_readable_text"]
    assert "symbolic_unreadable" in TEXT_POLICY_PRESENTATION["no_readable_text"]
    assert TEXT_POLICY_PRESENTATION["graphic_only"] == ("symbolic",)
    assert TEXT_POLICY_PRESENTATION["pending_creator_text"] == ("postproduction",)
    assert set(TEXT_POLICY_PRESENTATION["exact_readable"]) == {"readable", "postproduction"}


def test_text_policy_vocabulary_is_closed():
    from app.services.prompt_compiler import TEXT_POLICY_PRESENTATION
    assert set(TEXT_POLICY_PRESENTATION) == {
        "exact_readable", "graphic_only", "no_readable_text", "pending_creator_text",
    }
