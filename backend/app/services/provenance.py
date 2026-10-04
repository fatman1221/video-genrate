"""血缘反查 —— 从一张图（或一条 Prompt）向上还原完整生成链路。

用户把这条能力定为**最高优先级**：任何一张图，都要能回答

    哪个项目 / 哪个剧本段 / 哪个镜头 / 哪个角色 / 哪个造型 /
    哪版 Prompt / 哪个模型 / 哪些参考图 / 哪次任务

链路：

    Asset
      ├─ prompt_version_id ──> PromptVersion
      │     ├─ compiled_from ──> VisualBible / VisualStyle / Character+Look /
      │     │                     Location+View / Prop+State / ContinuityLock
      │     ├─ reference_assets ──> Asset（参考图）──> 可再向上反查
      │     └─ model / provider / parameters / resolution
      ├─ prompt_id ──> Prompt ──> shot_id ──> Shot ──> Scene ──> Storyboard
      │                                          └─> Project ──> Series
      ├─ task_id ──> Task ──> payload / provider / attempts
      ├─ generation_plan_item_id ──> GenerationPlanItem ──> GenerationPlan
      └─ parent_asset_id ──> Asset（上一版）

``compiled_from`` 里存的是**实体 id + 版本号**，不是哈希 ——
drama-skills 清点时发现 331 个手填哈希全部与字节对不上，手工哈希必然腐烂。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Asset, Character, CharacterLook, ContinuityLock, GenerationPlan, GenerationPlanItem,
    Location, LocationView, Project, Prompt, PromptVersion, Prop, PropState, Scene, Series,
    Shot, Storyboard, Task, VisualBible, VisualStyle,
)

#: 反查结果里「必须存在」的环节 —— 缺任一即认为链路不完整
REQUIRED_CHAIN = (
    "project", "prompt", "prompt_version", "shot", "scene", "storyboard", "task",
)


def asset_provenance(db: Session, asset: Asset, *, depth: int = 0) -> dict[str, Any]:
    """从素材反查完整生成链路。``depth`` 控制参考图递归层数（默认只下钻一层）。"""
    chain: dict[str, Any] = {}
    missing: list[str] = []

    chain["asset"] = _asset_ref(asset)

    version = db.get(PromptVersion, asset.prompt_version_id) if asset.prompt_version_id else None
    prompt = db.get(Prompt, version.prompt_id) if version is not None else None
    if version is not None:
        chain["prompt_version"] = _prompt_version_ref(version)
    else:
        missing.append("prompt_version")
    if prompt is not None:
        chain["prompt"] = {
            "id": prompt.id, "code": prompt.code, "type": prompt.type, "name": prompt.name,
            "latest_version": prompt.latest_version,
        }
    else:
        missing.append("prompt")

    shot = db.get(Shot, prompt.shot_id) if (prompt is not None and prompt.shot_id) else None
    if shot is None and asset.shot_id:
        shot = db.get(Shot, asset.shot_id)
    if shot is not None:
        chain["shot"] = {
            "id": shot.id, "code": shot.code, "sequence": shot.sequence,
            "scene_id": shot.scene_id, "duration": shot.duration,
            "description": shot.description[:200],
        }
    else:
        missing.append("shot")

    scene = db.get(Scene, shot.scene_id) if shot is not None else None
    if scene is not None:
        chain["scene"] = {"id": scene.id, "code": scene.code, "title": scene.title,
                          "location": scene.location}
    else:
        missing.append("scene")

    storyboard = db.get(Storyboard, scene.storyboard_id) if scene is not None else None
    if storyboard is not None:
        chain["storyboard"] = {"id": storyboard.id, "title": storyboard.title,
                               "visual_style": storyboard.visual_style}
    else:
        missing.append("storyboard")

    project = db.get(Project, asset.project_id) if asset.project_id else None
    if project is not None:
        chain["project"] = {
            "id": project.id, "name": project.name, "series_id": project.series_id,
            "episode_no": project.episode_no, "workflow_state": project.workflow_state,
        }
        if project.series_id:
            series = db.get(Series, project.series_id)
            if series is not None:
                chain["series"] = {"id": series.id, "name": series.name}
    else:
        missing.append("project")

    task = db.get(Task, asset.task_id) if asset.task_id else None
    if task is not None:
        chain["task"] = {
            "id": task.id, "type": task.type, "status": task.status,
            "provider": task.provider, "attempts": task.attempts,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        }
    else:
        missing.append("task")

    plan_item = (
        db.get(GenerationPlanItem, asset.generation_plan_item_id)
        if asset.generation_plan_item_id else None
    )
    if plan_item is not None:
        plan = db.get(GenerationPlan, plan_item.plan_id)
        chain["plan_item"] = {
            "id": plan_item.id, "ordinal": plan_item.ordinal, "modality": plan_item.modality,
            "status": plan_item.status,
        }
        if plan is not None:
            chain["generation_plan"] = {
                "id": plan.id, "name": plan.name, "plan_type": plan.plan_type,
                "status": plan.status,
                "confirmed_at": plan.confirmed_at.isoformat() if plan.confirmed_at else None,
                "confirmed_by": plan.confirmed_by,
            }

    if asset.parent_asset_id:
        parent = db.get(Asset, asset.parent_asset_id)
        if parent is not None:
            chain["parent_asset"] = _asset_ref(parent)

    # 编译输入（视觉设定 / 主体 / 锁）
    if version is not None:
        chain["compiled_from"] = _compiled_from_detail(db, version)

    # 参考图（可再向上反查一层）
    references: list[dict[str, Any]] = []
    if version is not None:
        for slot in version.reference_assets or []:
            entry = dict(slot)
            ref_asset_id = slot.get("asset_id")
            if ref_asset_id:
                ref_asset = db.get(Asset, ref_asset_id)
                if ref_asset is not None:
                    entry["resolved"] = _asset_ref(ref_asset)
                    if depth < 1:
                        entry["provenance"] = asset_provenance(
                            db, ref_asset, depth=depth + 1
                        ).get("chain", {})
            references.append(entry)
    chain["references"] = references

    return {
        "chain": chain,
        "missing": missing,
        "complete": not [m for m in missing if m in REQUIRED_CHAIN],
        "summary": _summary(chain, missing),
    }


def prompt_provenance(db: Session, prompt: Prompt) -> dict[str, Any]:
    """从 Prompt 向上反查（不涉及具体素材）。"""
    versions = list(db.execute(
        select(PromptVersion).where(PromptVersion.prompt_id == prompt.id)
        .order_by(PromptVersion.version.asc())
    ).scalars().all())
    current = db.get(PromptVersion, prompt.current_version_id) if prompt.current_version_id else None

    assets = list(db.execute(
        select(Asset).where(Asset.prompt_version_id.in_([v.id for v in versions]))
    ).scalars().all()) if versions else []

    shot = db.get(Shot, prompt.shot_id) if prompt.shot_id else None
    chain: dict[str, Any] = {
        "prompt": {"id": prompt.id, "code": prompt.code, "type": prompt.type,
                   "latest_version": prompt.latest_version},
        "versions": [
            {"id": v.id, "version": v.version, "status": v.status,
             "provider": v.provider, "model": v.model,
             "created_at": v.created_at.isoformat() if v.created_at else None}
            for v in versions
        ],
        "compiled_from": _compiled_from_detail(db, current) if current else {},
        "produced_assets": [_asset_ref(a) for a in assets],
    }
    if shot is not None:
        chain["shot"] = {"id": shot.id, "code": shot.code, "scene_id": shot.scene_id}
        scene = db.get(Scene, shot.scene_id)
        if scene is not None:
            chain["scene"] = {"id": scene.id, "code": scene.code}
    return {"chain": chain, "summary": f"Prompt {prompt.code} 共 {len(versions)} 个版本，"
                                       f"产出 {len(assets)} 个素材"}


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _asset_ref(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id, "type": asset.type, "name": asset.name,
        "role": asset.role, "status": asset.status,
        "subject_type": asset.subject_type, "subject_id": asset.subject_id,
        "variant_id": asset.variant_id,
        "file_path": asset.file_path, "width": asset.width, "height": asset.height,
    }


def _prompt_version_ref(version: PromptVersion) -> dict[str, Any]:
    """血缘链里的「这一版提示词」节点。

    除了元数据也带上**正文本身**：反查的意义就是回答"当时到底发了什么指令"，
    只给一个 id 还得再去查一次，等于没闭环。
    """
    return {
        "id": version.id, "version": version.version,
        "provider": version.provider, "model": version.model,
        "width": version.width, "height": version.height,
        "aspect_ratio": version.aspect_ratio, "resolution": version.resolution,
        "parameters": version.parameters or {},
        "recipe": version.recipe or {},
        "status": version.status,
        "compiled_prompt": version.compiled_prompt or "",
        "negative_prompt": version.negative_prompt or "",
        # 编译输入快照（实体 id + 版本，不是哈希）：用来判断"当时依据的设定是哪一版"
        "compiled_from": version.compiled_from or {},
        "continuity_lock_ids": list(version.continuity_lock_ids or []),
        # 参考图槽位（含 REF/PLAN/IMG 三态与准入状态）
        "reference_assets": list(version.reference_assets or []),
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


def _compiled_from_detail(db: Session, version: PromptVersion) -> dict[str, Any]:
    """把 ``compiled_from`` 里的 id 展开成可读实体（**再校验一次存在性**）。"""
    snapshot = version.compiled_from or {}
    detail: dict[str, Any] = {"snapshot": snapshot, "resolved": []}

    bible_ref = snapshot.get("bible")
    if bible_ref:
        bible = db.get(VisualBible, bible_ref["id"])
        detail["bible"] = {"id": bible_ref["id"], "version": bible_ref.get("version"),
                           "exists": bible is not None,
                           "title": bible.title if bible else None}
    style_ref = snapshot.get("style")
    if style_ref:
        style = db.get(VisualStyle, style_ref["id"])
        detail["style"] = {"id": style_ref["id"], "version": style_ref.get("version"),
                           "exists": style is not None,
                           "name": style.name if style else None,
                           "form_card": style.form_card if style else None}

    model_by_kind = {"character": Character, "location": Location, "prop": Prop}
    for item in snapshot.get("subjects") or []:
        model = model_by_kind.get(item.get("kind"))
        if model is None:
            continue
        entity = db.get(model, item["id"])
        entry: dict[str, Any] = {
            "kind": item["kind"], "id": item["id"], "version": item.get("version"),
            "exists": entity is not None,
            "name": getattr(entity, "name", None) if entity else None,
        }
        variant_ref = item.get("variant")
        if variant_ref:
            variant = db.get(
                {"character": CharacterLook, "location": LocationView, "prop": PropState}[item["kind"]],
                variant_ref["id"],
            )
            entry["variant"] = {
                "id": variant_ref["id"], "code": variant_ref.get("code"),
                "exists": variant is not None,
                "name": getattr(variant, "name", None) if variant else None,
            }
        detail["resolved"].append(entry)

    detail["locks"] = []
    for lock_ref in snapshot.get("locks") or []:
        lock = db.get(ContinuityLock, lock_ref["id"])
        detail["locks"].append({
            "id": lock_ref["id"], "code": lock_ref.get("code"),
            "surface": lock_ref.get("surface"),
            "exists": lock is not None,
            "surface_changed": bool(lock is not None and lock.surface != lock_ref.get("surface")),
        })
    return detail


def _summary(chain: dict[str, Any], missing: list[str]) -> str:
    bits = []
    if chain.get("project"):
        bits.append(chain["project"]["name"])
    if chain.get("shot"):
        bits.append(f"镜头 {chain['shot']['code']}")
    if chain.get("prompt_version"):
        bits.append(f"Prompt v{chain['prompt_version']['version']}")
    if chain.get("asset"):
        bits.append(chain["asset"]["type"])
    head = " / ".join(bits) or "（链路不完整）"
    if missing:
        head += f"（缺：{', '.join(missing)}）"
    return head
