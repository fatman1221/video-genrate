"""Phase 4–7 冒烟测试（隔离临时库，不碰主库、不启动 worker）。

跑法（在 backend/ 下）：
    unset PYTHONPATH && ../.venv/Scripts/python.exe _smoke_services.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="vg_smoke_"))
os.environ["DATABASE_URL"] = f"sqlite:///{TMP / 'smoke.db'}"
os.environ["STORAGE_ROOT"] = str(TMP / "storage")
sys.path.insert(0, ".")

from sqlalchemy import func, select  # noqa: E402

from app import models as m  # noqa: E402
from app.database import init_db, session_scope  # noqa: E402
from app.services import continuity, generation_plans as gp, prompt_compiler as pc  # noqa: E402
from app.services import provenance  # noqa: E402
from app.services import visual_bible as vb  # noqa: E402

PASS: list[str] = []


def check(label: str, condition: bool, extra: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{('  → ' + extra) if extra else ''}")
    if condition:
        PASS.append(label)
    else:
        raise AssertionError(label + (" " + extra if extra else ""))


def main() -> None:
    init_db()
    with session_scope() as db:
        # ---------- 构造最小项目 ---------- #
        proj = m.Project(id="proj_t", name="冒烟项目", width=1280, height=720, aspect_ratio="16:9")
        db.add(proj); db.flush()
        sb = m.Storyboard(id="sb_t", project_id=proj.id, title="分镜")
        db.add(sb); db.flush()
        scn = m.Scene(id="scn_t", project_id=proj.id, storyboard_id=sb.id, sequence=1, code="SC001")
        db.add(scn); db.flush()
        shot = m.Shot(id="shot_t", project_id=proj.id, scene_id=scn.id, sequence=1, code="001",
                      description="林野在舱内舷窗前回头", camera="缓慢推近", duration=7.0)
        ch = m.Character(id="chr_t", project_id=proj.id, name="林野", appearance="三十岁男性，短寸头")
        db.add_all([shot, ch]); db.flush()

        print("\n=== 1. 视觉设定体系 ===")
        vb.update_bible(
            db, proj, visual_logline="冷调静谧的深空舱内",
            global_rules={"stage_policy": "环境内展示，保持舷窗可见"},
            text_policy={"readable_text_allowed": True},
        )
        bible = vb.get_bible(db, proj.id)
        check("视觉设定已建立且版本递增", bible is not None and bible.version >= 2, f"v{bible.version}")

        style = vb.create_style(
            db, proj, name="写实电影感", form_card="live_action",
            rendering={"surface": "细腻金属与织物纹理"},
            lighting={"key": "冷调侧逆光"}, palette={"primary": "青灰", "accent": "暖橙"},
            set_current=True,
        )
        check("风格已建且被设为当前", style.is_current and vb.current_style(db, proj.id).id == style.id)

        try:
            vb.create_style(db, proj, name="瞎写的", form_card="not_a_card")
            check("非法形态卡应被拒绝", False)
        except ValueError:
            check("非法形态卡被拒绝", True)

        ch.identity_anchors = ["方额窄下颌", "后颈发际收成尖角"]
        ch.code = "CHAR-LINYE"
        look = vb.create_look(
            db, ch, name="常服", differences={"wardrobe_layers": ["深灰立领拉链夹克", "黑色工装裤"]},
            is_current=True,
        )
        loc = vb.create_location(
            db, proj, name="守望者舱",
            spatial_identity={"shape": "长方形舱室", "fixed_anchors": ["圆形舷窗", "控制台"]},
        )
        view = vb.create_location_view(
            db, loc, name="舷窗北向夜", orientation={"toward": "舷窗"},
            state_differences={"time": "深夜", "light": ["冷白屏幕光"]}, is_current=True,
        )
        check("造型/地点/视图均已建立", all([look.id, loc.id, view.id]))

        print("\n=== 2. 绑定（新字段为真相 + 旧字段单向回写）===")
        vb.set_shot_bindings(db, shot, [{"kind": "character", "id": ch.id, "variant_id": look.id, "role": "主角"}])
        vb.set_shot_location(db, shot, loc)
        check("asset_bindings 写入", shot.asset_bindings[0]["id"] == ch.id)
        check("character_ids 单向回写（必须是角色 id，供 handlers 解析）",
              shot.character_ids == [ch.id])
        check("location_id + location 单向回写", shot.location_id == loc.id and shot.location == "守望者舱")

        print("\n=== 3. 连续性锁 ===")
        for bad, why in (
            ("half-finished blue sweater in her hands", "含状态/位置词"),
            ("blue sweater, dark grey", "含标点"),
            ("镜头 03 的蓝毛衣 特写", "含镜头信息"),
        ):
            try:
                continuity.create_lock(db, proj, name="坏锁", surface=bad)
                check(f"非法锁面应被拒绝（{why}）", False)
            except ValueError:
                check(f"非法锁面被拒绝（{why}）", True)

        lock = continuity.create_lock(
            db, proj, name="深灰夹克", surface="dark grey zip-up stand collar jacket",
            subject_type="character", subject_id=ch.id,
        )
        check("合法锁面建立成功", lock.id and lock.surface == "dark grey zip-up stand collar jacket")

        print("\n=== 4. 锁面匹配的两种「假命中」 ===")
        check("粘在词上的不算", not continuity.surface_hits("unchipped white enamel mug", "chipped white enamel mug"))
        check("正常出现算命中", continuity.surface_hits("a chipped white enamel mug on the table", "chipped white enamel mug"))
        check("大小写不敏感", continuity.surface_hits("Dark Grey Zip-Up Stand Collar Jacket", lock.surface))
        check("换行当空格", continuity.surface_hits("dark grey\nzip-up stand collar jacket", lock.surface))

        print("\n=== 5. Prompt Compiler ===")
        v1 = pc.compile_prompt(
            db, shot, prompt_type="image",
            options={"provider": "comfyui", "width": 2752, "height": 1536, "resolution": "2K"},
        )
        check("编译产出 v1", v1.version == 1 and v1.status == "READY")
        check("锁面逐字出现在正向正文", lock.surface in v1.compiled_prompt)
        check("锁面未落进负向正文", lock.surface not in (v1.negative_prompt or ""))
        check("compiled_from 记录锁快照（id+version，非哈希）",
              v1.compiled_from["locks"][0]["surface"] == lock.surface
              and "version" in v1.compiled_from["locks"][0])
        check("正文卫生通过（无引擎语法/路径/哈希）", pc.validate_hygiene(v1.compiled_prompt) == [])
        check("单向回写 Shot.image_prompt", shot.image_prompt == v1.compiled_prompt)
        check("参考图槽位：无参考图时不产生假槽位", v1.reference_assets == [])

        prompt = db.execute(select(m.Prompt).where(m.Prompt.shot_id == shot.id)).scalars().first()
        check("Prompt 逻辑单元已建立", prompt is not None and prompt.latest_version == 1)

        print("\n=== 6. 文字政策冲突 ===")
        prop = vb.create_prop(db, proj, name="终端机", text_policy={"mode": "exact_readable"})
        vb.set_shot_bindings(db, shot, [
            {"kind": "character", "id": ch.id, "variant_id": look.id, "role": "主角"},
            {"kind": "prop", "id": prop.id, "role": "关键道具"},
        ])
        try:
            pc.compile_prompt(db, shot, prompt_type="image")
            check("exact_readable 缺精确文字应报错", False)
        except pc.CompileError as exc:
            check("exact_readable 缺精确文字被拒绝", "没有给精确文字" in str(exc))

        prop.text_policy = {"mode": "exact_readable", "text": "SYSTEM ONLINE"}
        bible.text_policy = {"readable_text_allowed": False}
        db.flush()
        try:
            pc.compile_prompt(db, shot, prompt_type="image")
            check("全局无文字 + exact_readable 应报错", False)
        except pc.CompileError as exc:
            check("文字政策冲突被拒绝", "不可共存" in str(exc))

        bible.text_policy = {"readable_text_allowed": True}
        db.flush()

        print("\n=== 7. 版本只增不改 + staleness ===")
        v1_text = v1.compiled_prompt
        v2 = pc.compile_prompt(db, shot, prompt_type="image",
                               options={"provider": "comfyui", "resolution": "2K"})
        check("再次编译产生 v2", v2.version == 2)
        v1_again = db.get(m.PromptVersion, v1.id)
        check("v1 未被覆盖（只增不改）",
              v1_again.compiled_prompt == v1_text and v1_again.version == 1)

        stale_before = pc.check_prompt_staleness(db, prompt)
        check("刚编译完不算 stale", stale_before["stale"] is False, str(stale_before["reasons"]))

        ch.identity_anchors = ["方额窄下颌", "后颈发际收成尖角", "眉骨有旧疤"]
        db.flush()
        stale_after = pc.check_prompt_staleness(db, prompt)
        check("改角色设定后判定 stale", stale_after["stale"] is True, stale_after["reasons"][0][:60])

        lock_ref = db.get(m.ContinuityLock, lock.id)
        lock_ref.surface = "dark grey zip-up stand collar jacket with fleece lining"
        lock_ref.version = 2
        db.flush()
        stale_lock = pc.check_prompt_staleness(db, prompt)
        check("锁面变化也判定 stale",
              stale_lock["stale"] and any("锁面" in r for r in stale_lock["reasons"]))

        print("\n=== 8. 生成计划：Preview → Confirm → Produce ===")
        plan = gp.create_plan(
            db, proj, name="批量出图", items=[
                {"shot_id": shot.id, "modality": "image", "prompt_version_id": v2.id,
                 "provider": "comfyui", "resolution": "2K"},
            ],
        )
        check("计划建立为 DRAFT", plan.status == "DRAFT")

        before_tasks = db.scalar(
            select(func.count()).select_from(m.Task).where(m.Task.project_id == proj.id)
        )
        gp.preview_plan(db, plan)
        after_tasks = db.scalar(
            select(func.count()).select_from(m.Task).where(m.Task.project_id == proj.id)
        )
        check("预览状态为 PREVIEWED", plan.status == "PREVIEWED")
        check("预览不产生任何任务", before_tasks == after_tasks == 0, f"{before_tasks} → {after_tasks}")
        check("预览算出指纹与汇总", bool(plan.fingerprint) and plan.summary["item_count"] == 1)

        try:
            gp.confirm_plan(db, plan, "deadbeef")
            check("错误指纹应被拒绝", False)
        except gp.PlanError:
            check("错误指纹被拒绝", True)

        gp.confirm_plan(db, plan, plan.fingerprint)
        check("确认成功且进入 CONFIRMED", plan.status == "CONFIRMED" and plan.confirmed_at is not None)

        ids = gp.materialize_plan(db, plan)
        check("物化创建了任务", len(ids) == 1 and plan.status == "RUNNING")
        item = gp.list_plan_items(db, plan.id)[0]
        check("计划项回写 TASK_CREATED + task_id", item.status == "TASK_CREATED" and item.task_id == ids[0])

        try:
            gp.materialize_plan(db, plan)
            check("重复物化应被拒绝（确认一次性）", False)
        except gp.PlanError:
            check("重复物化被拒绝（确认一次性）", True)

        print("\n=== 9. 指纹失效 ===")
        plan2 = gp.create_plan(db, proj, name="指纹测试", items=[
            {"shot_id": shot.id, "modality": "image", "prompt_version_id": v2.id},
        ])
        gp.preview_plan(db, plan2)
        old_fp = plan2.fingerprint
        plan2.parameters = {"changed": True}
        db.flush()
        try:
            gp.confirm_plan(db, plan2, old_fp)
            check("计划变更后旧指纹应失效", False)
        except gp.PlanError:
            check("计划变更后旧指纹失效", True)

        print("\n=== 10. PLAN 态资产不可投产 ===")
        plan3 = gp.create_plan(db, proj, name="PLAN态测试", items=[
            {"shot_id": shot.id, "modality": "image", "prompt_version_id": v2.id,
             "reference_assets": [{"slot": "REF-X", "kind": "PLAN", "label": "创作者自备图"}]},
        ])
        try:
            gp.preview_plan(db, plan3)
            check("PLAN 态参考图应被拒绝", False)
        except gp.PlanError as exc:
            check("PLAN 态参考图被拒绝", "不能作生产输入" in str(exc))

        print("\n=== 11. 血缘反查 ===")
        asset = m.Asset(
            id="ast_t", project_id=proj.id, shot_id=shot.id, type="IMAGE", name="关键帧",
            prompt_version_id=v2.id, task_id=ids[0], role="keyframe",
            generation_plan_item_id=item.id, file_path="images/001.png",
        )
        db.add(asset); db.flush()
        prov = provenance.asset_provenance(db, asset)
        chain = prov["chain"]
        for key in ("asset", "prompt_version", "prompt", "shot", "scene", "storyboard", "project", "task"):
            check(f"血缘链路含 {key}", key in chain)
        check("血缘可回查模型与 Provider",
              chain["prompt_version"]["provider"] == "comfyui"
              and chain["prompt_version"]["resolution"] == "2K")
        check("血缘可回查编译输入（角色/锁）",
              any(x["kind"] == "character" for x in chain["compiled_from"]["resolved"])
              and len(chain["compiled_from"]["locks"]) == 1)
        check("血缘可回查计划", chain.get("generation_plan", {}).get("id") == plan.id)
        check("链路判定为完整", prov["complete"] is True, str(prov["missing"]))

        db.rollback()

    print(f"\n=== 冒烟测试通过 {len(PASS)} 项 ===")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
