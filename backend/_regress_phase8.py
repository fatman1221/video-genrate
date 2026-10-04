"""Phase 8 回归确认（隔离临时库 + 真实 HTTP 链路，不触发任何生成类任务）。

覆盖：
  A. 空库首次初始化后 `bootstrap_project` 能正常建项目/角色（验证部分唯一索引不再误伤）
  B. 部分唯一索引语义：空 code 放行 / 同项目重复 code 拦截 / 跨项目同 code 放行
  C. 新增 30 个 studio Skill 已在注册表内
  D. 新旧链路共存：老接口仍可用，新层可编译 prompt 且连续性锁逐字注入
  E. Preview→Confirm→Produce 闸门（preview 不建任务、错误指纹被拒）
  F. 8 个只读端点全部 200

跑法（在 backend/ 下，需后端已在隔离库上启动）：
    unset PYTHONPATH && ../.venv/Scripts/python.exe _regress_phase8.py
"""
from __future__ import annotations

import os
import sqlite3

import httpx

BASE = os.environ.get("REGRESS_BASE", "http://127.0.0.1:8077")
DB_PATH = os.environ.get(
    "REGRESS_DB",
    r"C:/Users/Administrator/WorkBuddy/video-generate/video-genrate/backend/_tmp_regress/regress.db",
)
OK: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{('  → ' + extra) if extra else ''}")
    if not cond:
        raise AssertionError(label + (" " + extra if extra else ""))
    OK.append(label)


def invoke(name: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    r = httpx.post(f"{BASE}/api/skills/{name}/invoke", json=payload or {}, timeout=timeout)
    r.raise_for_status()
    out = r.json()
    if not out.get("ok"):
        raise RuntimeError(f"[{name}] 调用失败：{out.get('error')}")
    return out.get("data") or {}


def try_invoke(name: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    r = httpx.post(f"{BASE}/api/skills/{name}/invoke", json=payload or {}, timeout=timeout)
    return r.json()


def main() -> None:
    print("\n=== A. 空库初始化 + bootstrap_project（关键回归）===")
    boot = invoke("bootstrap_project", {
        "name": "回归验证项目",
        "requirement": "验证空库初始化后项目骨架可正常搭建",
        "target_duration": 30,
        "shot_duration": 5,
        "create_characters": True,
        "generate_references": False,   # 关键：不出图
    })
    pid = boot["project_id"]
    check("bootstrap_project 成功（不再撞 characters 唯一约束）", bool(pid), pid)
    check("角色已创建", len(boot.get("character_ids") or []) > 0,
          f"{len(boot.get('character_ids') or [])} 个")
    check("分镜已创建", int(boot.get("shot_count") or 0) > 0, f"{boot.get('shot_count')} 镜")

    check("项目详情端点可用",
          httpx.get(f"{BASE}/api/projects/{pid}", timeout=30).status_code == 200)

    # 角色与镜头通过既有 Skill 读取（与前端同源）
    shots_all = invoke("list_shots", {"project_id": pid}).get("shots") or []
    check("list_shots 能读到镜头", len(shots_all) == int(boot.get("shot_count") or 0),
          f"{len(shots_all)} 镜")

    chars = []
    for cid in (boot.get("character_ids") or []):
        d = invoke("get_character", {"character_id": cid})
        chars.append(d.get("character") or d)
    check("角色可逐个读回", len(chars) == len(boot.get("character_ids") or []),
          f"{len(chars)} 个")

    print("\n=== B. 部分唯一索引语义（直查隔离库，ORM 写入）===")
    from sqlalchemy import create_engine, select as sa_select
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session as SaSession

    import app.models as models  # noqa: PLC0415

    con = sqlite3.connect(DB_PATH)
    idx = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='uq_characters_project_code_nonempty'").fetchone()
    con.close()
    check("部分唯一索引已建且带 WHERE 条件", bool(idx and "WHERE" in idx[0]),
          (idx[0] if idx else "缺失"))

    code_empty = [c.get("code") or "" for c in chars]
    check("旧流程产生的角色 code 全为空串且未被索引拦截",
          all(c == "" for c in code_empty), str(code_empty))

    eng = create_engine(f"sqlite:///{DB_PATH}")
    created: list[str] = []
    other_id: str | None = None
    with SaSession(eng) as db:
        proj_x = db.execute(sa_select(models.Project.id).limit(1)).scalar()

        a = models.Character(project_id=proj_x, name="甲", role="支持", code="DUPCODE")
        db.add(a)
        db.commit()
        created.append(a.id)

        b = models.Character(project_id=proj_x, name="乙", role="支持", code="DUPCODE")
        db.add(b)
        dup_blocked = False
        try:
            db.commit()
        except IntegrityError:
            dup_blocked = True
            db.rollback()
        check("同项目重复非空 code 仍被唯一约束拦下", dup_blocked)

        other = models.Project(name="另一项目", requirement="x")
        db.add(other)
        db.commit()
        c = models.Character(project_id=other.id, name="丙", role="支持", code="DUPCODE")
        db.add(c)
        cross_ok = False
        try:
            db.commit()
            cross_ok = True
            created.append(c.id)
            other_id = other.id
        except IntegrityError:
            db.rollback()
        check("跨项目允许相同 code（索引按 project_id 分组）", cross_ok)

        # 清理测试数据
        db.query(models.Character).filter(models.Character.id.in_(created)).delete(
            synchronize_session=False)
        if other_id:
            db.query(models.Project).filter(models.Project.id == other_id).delete(
                synchronize_session=False)
        db.commit()
    eng.dispose()

    print("\n=== C. 新 Skill 注册表检查 ===")
    skills = httpx.get(f"{BASE}/api/skills", timeout=30).json()
    names = {s["name"] for s in skills["skills"]}
    by_cat = {}
    for s in skills["skills"]:
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
    check("Skill 总数 = 124", skills["total"] == 124, str(skills["total"]))
    check("新增 visual 类目 12 个", by_cat.get("visual") == 12, str(by_cat.get("visual")))
    check("新增 plan 类目 7 个", by_cat.get("plan") == 7, str(by_cat.get("plan")))
    for n in ("create_visual_bible", "get_visual_bible", "create_style", "set_current_style",
              "set_character_identity", "create_look", "create_location", "create_location_view",
              "create_prop", "create_prop_state", "set_shot_bindings", "set_shot_location",
              "create_continuity_lock", "create_continuity_delta", "verify_continuity_locks",
              "list_continuity_locks",
              "compile_image_prompt", "compile_video_prompt", "get_prompt", "list_prompts",
              "check_prompt_staleness",
              "create_generation_plan", "preview_generation_plan", "confirm_generation_plan",
              "materialize_generation_plan", "cancel_generation_plan",
              "get_generation_plan", "list_generation_plans",
              "get_asset_provenance", "get_prompt_provenance"):
        check(f"Skill 已注册：{n}", n in names)

    print("\n=== D. 新旧链路共存（设定 → 绑定 → 编译）===")
    bible = invoke("get_visual_bible", {"project_id": pid})
    check("可读取 visual_bible", bool(bible.get("bible")),
          f"v{(bible.get('bible') or {}).get('version')}")
    check("形态卡 6 张已暴露", len(bible.get("form_cards") or []) == 6,
          str(bible.get("form_cards")))

    shot_id = shots_all[0]["shot_id"]

    check("老接口 /projects/{id}/status 仍可用",
          httpx.get(f"{BASE}/api/projects/{pid}/status", timeout=30).status_code == 200)

    loc = invoke("create_location", {"project_id": pid, "name": "废弃控制舱",
                                     "description": "锈蚀金属舱室，蓝色应急灯"})
    loc_id = (loc.get("location") or {}).get("id")
    check("create_location 成功", bool(loc_id), str(loc_id))
    bound = invoke("set_shot_location", {"shot_id": shot_id, "location_id": loc_id})
    check("set_shot_location 双向一致（新外键 + 老字符串回写）",
          bound.get("location_id") == loc_id and bool(bound.get("mirrored_location")),
          f"mirrored={bound.get('mirrored_location')!r}")

    surface = "磨砂铝合金外壳的战术手电筒"
    lock = invoke("create_continuity_lock", {"project_id": pid, "name": "战术手电",
                                             "surface": surface})
    check("create_continuity_lock 成功", bool((lock.get("lock") or {}).get("id")),
          str((lock.get("lock") or {}).get("code")))

    comp = invoke("compile_image_prompt", {"shot_id": shot_id})
    check("compile_image_prompt 成功", bool(comp.get("prompt_id")),
          f"{comp.get('code')} v{comp.get('version')}")
    text = str(comp.get("compiled_prompt") or "")
    check("编译正文非空（八段式已拼装）", len(text) > 20, f"{len(text)} 字")
    check("连续性锁锁面被逐字注入正文", surface in text)
    check("编译产物带了血缘快照", bool(comp.get("compiled_from")),
          str(list((comp.get("compiled_from") or {}).keys()))[:90])

    ver = invoke("verify_continuity_locks", {"project_id": pid})
    check("verify 判定合规（已编译镜头锁面在场）", ver.get("compliant") is True,
          f"missing_total={ver.get('missing_total')}")
    check("未编译镜头被单列，不计入违规",
          ver.get("not_compiled_total") == len(shots_all) - 1,
          f"not_compiled={ver.get('not_compiled_total')} / 共 {len(shots_all)} 镜")

    stale = invoke("check_prompt_staleness", {"project_id": pid})
    check("新编译的 prompt 未 stale", stale.get("stale_count") == 0,
          f"checked={stale.get('checked_count')}")

    print("\n=== E. Preview→Confirm→Produce 闸门 ===")
    plan = invoke("create_generation_plan", {
        "project_id": pid, "name": "回归计划", "plan_type": "batch_image",
        "items": [{"shot_id": shot_id, "modality": "image"}],
    })
    plan_id = (plan.get("plan") or {}).get("id")
    check("create_generation_plan 成功", bool(plan_id), str(plan_id))
    prev = invoke("preview_generation_plan", {"plan_id": plan_id})
    p = prev.get("plan") or {}
    check("preview 成功且产出指纹", bool(p.get("fingerprint")), str(p.get("fingerprint"))[:16])
    check("preview 阶段不建任务",
          httpx.get(f"{BASE}/api/projects/{pid}/tasks", timeout=30).json().get("total", 0) == 0)
    bad = try_invoke("confirm_generation_plan", {"plan_id": plan_id, "fingerprint": "deadbeef"})
    check("错误指纹被拒绝确认", bad.get("ok") is False, str(bad.get("error"))[:80])
    still = httpx.get(f"{BASE}/api/generation-plans/{plan_id}", timeout=30).json()
    check("计划仍停留在 PREVIEWED（未被误确认）",
          (still.get("plan") or still).get("status") == "PREVIEWED",
          str((still.get("plan") or still).get("status")))

    print("\n=== F. 只读端点 ===")
    for path in (f"/api/projects/{pid}/visual-bible",
                 f"/api/projects/{pid}/continuity-locks",
                 f"/api/projects/{pid}/prompts",
                 f"/api/projects/{pid}/generation-plans",
                 f"/api/prompts/{comp['prompt_id']}",
                 f"/api/prompts/{comp['prompt_id']}/provenance",
                 f"/api/generation-plans/{plan_id}"):
        r = httpx.get(BASE + path, timeout=30)
        check(f"GET {path} → 200", r.status_code == 200, f"HTTP {r.status_code}")

    print(f"\n全部 {len(OK)} 项断言通过。project_id={pid}")


if __name__ == "__main__":
    main()
