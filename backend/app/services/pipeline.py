"""节点级流水线服务：把「生产进度」从一根进度条升级为可交互的节点图。

对每个节点（对应 WORKFLOW_STEPS 中的一步）提供三件事：

* **观察** `node_view` —— 状态、产出缩略图、真实产出比例、耗时、审核结论
* **回退** `rollback_to` —— 回到某个节点：该节点与全部下游重置为待执行，
  下游产物引用被清空（文件保留在素材中心，可对比），工作流状态一并回退
* **重生成** `regenerate_stage` —— 只重跑某一个节点的产出，不影响上游
* **审核** `review_stage` —— 人工/Agent 对节点产出给出通过或驳回结论

设计原则：上游产物变化 ⇒ 下游一律失效。这样「点一下节点就能回到那一刻重来」
才不会出现半新半旧的脏状态。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.constants import (
    AssetType, STAGE_DOWNSTREAM, STAGE_LABELS, STAGE_REVIEWABLE, STAGE_SHOT_FIELDS,
    ReviewStatus, TaskStatus, TaskType, WORKFLOW_STEPS, WorkflowState, STEP_DONE_STATE,
)
from ..models import (
    Asset, Character, Project, QualityCheck, Scene, Script, Shot, Storyboard, Task, WorkflowStep,
)
from . import agent_log, characters as characters_svc, planner
from . import projects as projects_svc
from . import serializers as S
from . import tasks as tasks_svc
from . import workflow as workflow_svc

STAGE_KEYS: tuple[str, ...] = tuple(key for key, _, _ in WORKFLOW_STEPS)

#: 节点顺序索引
STAGE_INDEX: dict[str, int] = {key: idx for idx, key in enumerate(STAGE_KEYS)}

#: 产物型的节点（用「已完成镜头数 / 总镜头数」表达真实进度）
SHOT_PRODUCT_STAGES: dict[str, str] = {
    "image": "image_asset_id",
    "video": "video_asset_id",
    "voice": "voice_asset_id",
    "subtitle": "subtitle_asset_id",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 观察：节点视图
# --------------------------------------------------------------------------- #
def _preview_items(db: Session, project: Project, step_key: str, *, limit: int = 6) -> list[dict[str, Any]]:
    """节点的产出预览（缩略图 / 文件名）。前端据此渲染缩略图墙，避免满屏文字。"""
    pid = project.id
    items: list[Asset] = []

    def _assets(asset_type: str, **where: Any) -> list[Asset]:
        stmt = select(Asset).where(Asset.project_id == pid, Asset.type == asset_type)
        for key, value in where.items():
            stmt = stmt.where(getattr(Asset, key) == value)
        return list(db.execute(stmt.order_by(Asset.created_at.asc()).limit(limit)).scalars())

    if step_key == "character":
        items = _assets(AssetType.CHARACTER)
    elif step_key == "image":
        items = _assets(AssetType.IMAGE)
    elif step_key == "video":
        items = _assets(AssetType.VIDEO)
    elif step_key == "voice":
        items = _assets(AssetType.VOICE)
    elif step_key == "music":
        items = _assets(AssetType.MUSIC)
    elif step_key == "subtitle":
        items = _assets(AssetType.SUBTITLE)
    elif step_key in ("enhancement", "editing"):
        items = list(db.execute(
            select(Asset).where(Asset.project_id == pid, Asset.type == AssetType.VIDEO)
            .order_by(Asset.created_at.desc()).limit(limit)
        ).scalars())
    elif step_key in ("composing", "quality_check"):
        items = list(db.execute(
            select(Asset).where(Asset.project_id == pid, Asset.type == AssetType.PROJECT_OUTPUT)
            .order_by(Asset.created_at.desc()).limit(limit)
        ).scalars())

    # 视频用「所属镜头的关键帧」当封面，避免实时抽帧带来的开销
    poster_by_shot: dict[str, str] = {}
    if step_key in ("video", "enhancement", "editing"):
        for shot in db.execute(select(Shot).where(Shot.project_id == pid)).scalars():
            if shot.image_asset_id:
                img = db.get(Asset, shot.image_asset_id)
                if img is not None:
                    poster_by_shot[shot.id] = img.url

    out: list[dict[str, Any]] = []
    for asset in items:
        entry = {
            "asset_id": asset.id, "name": asset.name, "type": asset.type,
            "url": asset.url, "kind": asset.type.lower(),
            "duration": asset.duration, "size_bytes": asset.size_bytes,
            "created_at": S.iso(asset.created_at),
        }
        if asset.shot_id and asset.shot_id in poster_by_shot:
            entry["poster"] = poster_by_shot[asset.shot_id]
        if asset.type == AssetType.SUBTITLE:
            entry["text"] = (asset.extra or {}).get("preview", "")
        out.append(entry)
    return out


def _count_videos_by_role(db: Session, project_id: str, role: str) -> int:
    """按 extra.role 统计视频资产（跨方言用 Python 过滤，避免 JSON 方言差异）。"""
    rows = db.execute(
        select(Asset).where(Asset.project_id == project_id, Asset.type == AssetType.VIDEO)
    ).scalars()
    return sum(1 for a in rows if (a.extra or {}).get("role") == role)


def _stage_stat(db: Session, project: Project, step_key: str) -> tuple[str, float]:
    """返回 (人类可读的产出摘要, 0~1 真实完成比例)。"""
    pid = project.id
    shot_total = db.scalar(select(func.count(Shot.id)).where(Shot.project_id == pid)) or 0

    if step_key in SHOT_PRODUCT_STAGES:
        column = getattr(Shot, SHOT_PRODUCT_STAGES[step_key])
        done = db.scalar(
            select(func.count(Shot.id)).where(Shot.project_id == pid, column.isnot(None))
        ) or 0
        if not shot_total:
            return "—", 0.0
        return f"{done}/{shot_total}", done / shot_total

    if step_key == "script":
        script = db.execute(
            select(Script).where(Script.project_id == pid).order_by(Script.created_at.desc())
        ).scalars().first()
        if script is None:
            return "未生成", 0.0
        return f"{len(script.content or '')} 字", 1.0

    if step_key == "storyboard":
        scenes = db.scalar(select(func.count(Scene.id)).where(Scene.project_id == pid)) or 0
        if not shot_total:
            return "未拆解", 0.0
        return f"{scenes} 场 / {shot_total} 镜", 1.0

    if step_key == "character":
        # 统计「本集可用角色池」：含系列级角色，主角有参考图即算设定完成
        project = db.get(Project, pid)
        pool = characters_svc.character_pool(db, project) if project is not None else []
        if not pool:
            return "未设定", 0.0
        with_ref = sum(1 for c in pool if c.reference_asset_id)
        return f"{len(pool)} 人 / {with_ref} 图", 1.0 if with_ref else 0.5

    if step_key == "music":
        count = db.scalar(select(func.count(Asset.id)).where(
            Asset.project_id == pid, Asset.type == AssetType.MUSIC)) or 0
        return (f"{count} 条", 1.0) if count else ("未生成", 0.0)

    if step_key == "enhancement":
        count = _count_videos_by_role(db, pid, "enhanced")
        return (f"{count} 段", 1.0) if count else ("未增强", 0.0)

    if step_key == "editing":
        count = _count_videos_by_role(db, pid, "merged")
        return (f"{count} 段", 1.0) if count else ("未剪辑", 0.0)

    if step_key == "composing":
        count = db.scalar(select(func.count(Asset.id)).where(
            Asset.project_id == pid, Asset.type == AssetType.PROJECT_OUTPUT)) or 0
        return (f"{count} 个", 1.0) if count else ("未合成", 0.0)

    if step_key == "quality_check":
        rows = list(db.execute(
            select(QualityCheck).where(QualityCheck.project_id == pid)
            .order_by(QualityCheck.created_at.desc()).limit(50)
        ).scalars())
        if not rows:
            return "未检查", 0.0
        run_id = rows[0].run_id
        checks = [r for r in rows if r.run_id == run_id]
        passed = sum(1 for c in checks if c.status in ("PASS", "SKIPPED"))
        ratio = passed / len(checks) if checks else 0.0
        return f"{passed}/{len(checks)} 项", ratio

    return "—", 0.0


def _allowed_actions(step: WorkflowStep | None) -> list[str]:
    """节点上可执行的动作（前端据此决定按钮的可见性）。"""
    if step is None or step.state == "PENDING":
        return ["regenerate"]
    actions = ["regenerate"]
    if step.state == "FAILED":
        actions.append("retry")
    if step.state == "SUCCESS":
        actions.append("rollback")
        actions.append("review")
    return actions


def node_view(db: Session, project: Project) -> dict[str, Any]:
    """返回全部节点的视图数据（Web UI 节点图 + Agent 观察的唯一数据源）。"""
    wf = workflow_svc.ensure_workflow(db, project)
    steps = {s.step_key: s for s in wf.steps}

    nodes: list[dict[str, Any]] = []
    for index, (key, label, desc) in enumerate(WORKFLOW_STEPS):
        step = steps.get(key)
        stat, ratio = _stage_stat(db, project, key)
        state = step.state if step else "PENDING"
        if state == "SUCCESS":
            ratio = 1.0
        elif state == "RUNNING":
            ratio = max(ratio, 0.08)
        else:
            # 已产出但节点状态未标记完成时，不显示「满环」以免误导
            ratio = min(ratio, 0.9)

        duration_ms = 0
        if step and step.started_at and step.finished_at:
            duration_ms = int((step.finished_at - step.started_at).total_seconds() * 1000)

        nodes.append({
            "step_key": key,
            "index": index,
            "label": label,
            "description": desc,
            "state": state,
            "ratio": round(ratio, 3),
            "stat": stat,
            "attempts": step.attempts if step else 0,
            "error": (step.error or "") if step else "",
            "review_status": (step.review_status or ReviewStatus.NONE) if step else ReviewStatus.NONE,
            "review_comment": (step.review_comment or "") if step else "",
            "reviewed_by": (step.reviewed_by or "") if step else "",
            "reviewed_at": S.iso(step.reviewed_at) if step else None,
            "reviewable": key in STAGE_REVIEWABLE,
            "rollback_count": (step.rollback_count or 0) if step else 0,
            "rolled_back_at": S.iso(step.rolled_back_at) if step else None,
            "duration_ms": duration_ms,
            "started_at": S.iso(step.started_at) if step else None,
            "finished_at": S.iso(step.finished_at) if step else None,
            "updated_at": S.iso(step.updated_at) if step else None,
            "previews": _preview_items(db, project, key),
            "actions": _allowed_actions(step),
        })

    # 当前节点 = 第一个未成功的节点
    current = next((n["step_key"] for n in nodes if n["state"] != "SUCCESS"), "done")
    done_count = sum(1 for n in nodes if n["state"] == "SUCCESS")
    approved = sum(1 for n in nodes if n["review_status"] == ReviewStatus.APPROVED)
    rejected = sum(1 for n in nodes if n["review_status"] == ReviewStatus.REJECTED)

    return {
        "project_id": project.id,
        "state": wf.state,
        "previous_state": wf.previous_state,
        "paused": bool(wf.paused),
        "nodes": nodes,
        "current": current,
        "summary": {
            "total": len(nodes),
            "success": done_count,
            "approved": approved,
            "rejected": rejected,
            "pending_review": sum(
                1 for n in nodes
                if n["state"] == "SUCCESS" and n["reviewable"]
                and n["review_status"] == ReviewStatus.NONE
            ),
        },
        "history": (wf.history or [])[-30:],
    }


# --------------------------------------------------------------------------- #
# 回退
# --------------------------------------------------------------------------- #
def _clear_stage_products(db: Session, project: Project, stage: str) -> dict[str, int]:
    """清空某节点的产物「引用」。文件与素材记录保留，便于对比与恢复。"""
    cleared: dict[str, int] = {}

    if stage == "script":
        scripts = list(db.execute(select(Script).where(Script.project_id == project.id)).scalars())
        for row in scripts:
            row.status = "STALE"
        cleared["script"] = len(scripts)

    if stage == "character":
        # 只清「本集自有」角色的引用。系列级角色的参考图是全系列共享的，
        # 若在某一集回退时一并清空，会连带把其它集的角色形象废掉。
        chars = characters_svc.own_characters(db, project.id)
        for char in chars:
            if char.reference_asset_id:
                char.reference_asset_id = None
                char.status = "PENDING"
        cleared["character"] = len(chars)

    if stage in SHOT_PRODUCT_STAGES:
        column = getattr(Shot, SHOT_PRODUCT_STAGES[stage])
        status_field = getattr(Shot, {
            "image": "image_status", "video": "video_status",
            "voice": "voice_status", "subtitle": "subtitle_status",
        }[stage])
        shots = list(db.execute(select(Shot).where(Shot.project_id == project.id)).scalars())
        for shot in shots:
            if getattr(shot, column.key) is not None:
                setattr(shot, column.key, None)
                setattr(shot, status_field.key, "PENDING")
        # 镜头总状态按剩余产物重算
        for shot in shots:
            if shot.video_asset_id:
                shot.status = "VIDEO_READY"
            elif shot.image_asset_id:
                shot.status = "IMAGE_READY"
            else:
                shot.status = "PENDING"
        cleared[stage] = len(shots)

    if stage in ("enhancement", "editing"):
        shots = list(db.execute(select(Shot).where(Shot.project_id == project.id)).scalars())
        for shot in shots:
            if shot.enhanced_video_asset_id:
                shot.enhanced_video_asset_id = None
        cleared[stage] = len(shots)

    return cleared


def rollback_to(
    db: Session, project: Project, step_key: str, *,
    actor: str = "user", reason: str = "", purge_products: bool = True,
    keep_review: bool = False, commit: bool = True,
) -> dict[str, Any]:
    """回退到某个节点：该节点与全部下游重置为待执行。

    * 下游产物引用被清空（文件保留，可在素材中心对比与复用）
    * 工作流状态回退到「上一个节点已完成」的状态
    * 审核结论一并失效（除非 keep_review）
    """
    if step_key not in STAGE_KEYS:
        raise KeyError(f"未知节点: {step_key}。可用：{', '.join(STAGE_KEYS)}")

    wf = workflow_svc.ensure_workflow(db, project)
    steps = {s.step_key: s for s in wf.steps}

    targets = (step_key, *STAGE_DOWNSTREAM.get(step_key, ()))
    reset_steps: list[str] = []
    for key in targets:
        step = steps.get(key)
        if step is None:
            continue
        step.state = "PENDING"
        step.started_at = None
        step.finished_at = None
        step.error = ""
        if not keep_review:
            step.review_status = ReviewStatus.NONE
            step.review_comment = ""
            step.reviewed_by = ""
            step.reviewed_at = None
        reset_steps.append(key)

    cleared: dict[str, int] = {}
    if purge_products:
        for key in targets:
            if key in SHOT_PRODUCT_STAGES or key in ("script", "character", "enhancement", "editing"):
                cleared.update(_clear_stage_products(db, project, key))

    # 工作流状态回退：回到「上一个节点已完成」
    index = STAGE_INDEX[step_key]
    target_state = WorkflowState.PROJECT_CREATED if index == 0 else STEP_DONE_STATE[STAGE_KEYS[index - 1]]

    step = steps.get(step_key)
    if step is not None:
        step.rolled_back_at = utcnow()
        step.rollback_count = (step.rollback_count or 0) + 1

    workflow_svc.transition(
        db, project, target_state, actor=actor, force=True,
        reason=reason or f"回退到「{STAGE_LABELS[step_key]}」",
        commit=False,
    )
    if project.status == "COMPLETED":
        project.status = "GENERATING" if index > 0 else "DRAFT"
    project.progress = projects_svc.compute_progress(db, project)

    agent_log.log_event(
        db, project_id=project.id, event="pipeline.rollback", actor=actor, level="WARN",
        message=(f"回退到节点「{STAGE_LABELS[step_key]}」，"
                 f"已重置 {len(reset_steps)} 个节点"
                 + (f"（{reason}）" if reason else "")),
        detail={"step_key": step_key, "reset": reset_steps, "cleared": cleared,
                "state": target_state},
    )
    if commit:
        db.commit()
    return {
        "rolled_back_to": step_key,
        "label": STAGE_LABELS[step_key],
        "reset_nodes": reset_steps,
        "cleared_products": cleared,
        "workflow_state": target_state,
        "progress": project.progress,
    }


# --------------------------------------------------------------------------- #
# 重新生成
# --------------------------------------------------------------------------- #
def _stage_tasks(
    db: Session, project: Project, step_key: str, *, reset: bool, actor: str,
    **params: Any,
) -> dict[str, Any]:
    """按节点语义提交任务，返回 {task_ids, notes}。"""
    pid = project.id
    task_ids: list[str] = []
    notes: list[str] = []

    def submit(type_: str, name: str, **kwargs: Any) -> str:
        task = tasks_svc.create_task(
            db, project_id=pid, type=type_, name=name, created_by=actor, commit=False, **kwargs,
        )
        task_ids.append(task.id)
        return task.id

    shots = list(db.execute(
        select(Shot).where(Shot.project_id == pid).order_by(Shot.sequence.asc())
    ).scalars())

    if step_key == "script":
        planned = planner.plan_script(
            requirement=project.requirement or project.name, style=project.style or "漫画教学风格",
            target_duration=float(project.target_duration or 300),
        )
        script = db.execute(
            select(Script).where(Script.project_id == pid).order_by(Script.created_at.desc())
        ).scalars().first()
        if script is None:
            script = Script(project_id=pid)
            db.add(script)
        script.title = planned["title"]
        script.content = planned["content"]
        script.outline = planned["outline"]
        script.style = project.style or ""
        script.status = "READY"
        script.provider = planned["provider"]
        script.model = planned["model"]
        script.parameters = planned["parameters"]
        db.flush()
        notes.append("脚本已由本地规划引擎重建")

    elif step_key == "storyboard":
        for row in list(db.execute(select(Shot).where(Shot.project_id == pid)).scalars()):
            db.delete(row)
        for row in list(db.execute(select(Scene).where(Scene.project_id == pid)).scalars()):
            db.delete(row)
        for row in list(db.execute(select(Storyboard).where(Storyboard.project_id == pid)).scalars()):
            db.delete(row)
        db.flush()

        chars = characters_svc.character_pool(db, project)
        planned = planner.plan_storyboard(
            requirement=project.requirement or project.name, style=project.style or "漫画教学风格",
            target_duration=float(project.target_duration or 300),
            shot_duration=float(params.get("shot_duration") or 5.0),
            characters=[{"name": c.name, "id": c.id} for c in chars] or None,
        )
        board = Storyboard(
            project_id=pid, title=planned["title"], synopsis=planned["synopsis"],
            visual_style=project.style or "", status="READY",
            provider=planned["provider"], model=planned["model"], parameters=planned["parameters"],
        )
        db.add(board)
        db.flush()
        shot_count = 0
        for s_idx, raw_scene in enumerate(planned["scenes"], start=1):
            scene = Scene(project_id=pid, storyboard_id=board.id, sequence=s_idx,
                          code=raw_scene.get("code", f"Scene {s_idx:02d}"),
                          title=raw_scene.get("title", ""), summary=raw_scene.get("summary", ""),
                          location=raw_scene.get("location", ""), mood=raw_scene.get("mood", ""))
            db.add(scene)
            db.flush()
            for shot_index, raw_shot in enumerate(raw_scene.get("shots") or [], start=1):
                seq = int(raw_shot.get("sequence") or shot_index)
                char_ids = characters_svc.resolve_character_ids(
                    db, project, raw_shot.get("character_ids") or []
                )
                db.add(Shot(
                    project_id=pid, scene_id=scene.id, sequence=seq,
                    code=raw_shot.get("code") or f"Shot {seq:03d}",
                    duration=float(raw_shot.get("duration") or 5.0),
                    description=raw_shot.get("description", ""), camera=raw_shot.get("camera", ""),
                    location=raw_shot.get("location", ""),
                    visual_style=raw_shot.get("visual_style", project.style or ""),
                    character_ids=char_ids, image_prompt=raw_shot.get("image_prompt", ""),
                    video_prompt=raw_shot.get("video_prompt", ""),
                    negative_prompt=raw_shot.get("negative_prompt", ""),
                    voice_script=raw_shot.get("voice_script", ""),
                    subtitle_text=raw_shot.get("subtitle_text") or raw_shot.get("voice_script", ""),
                    extra=raw_shot.get("extra") or {},
                ))
                shot_count += 1
        board.scene_count = len(planned["scenes"])
        board.shot_count = shot_count
        notes.append(f"分镜已重建：{board.scene_count} 场景 / {shot_count} 镜头")

    elif step_key == "character":
        pool = characters_svc.character_pool(db, project)
        if not pool:
            notes.append("项目还没有角色，请先创建角色")
        for char in pool:
            # 系列级角色的参考图跨集共享：重生成时不重置它，只在缺图时补齐，
            # 否则第 1 集的一次「重生成」会顺手废掉第 2、3 集的主角形象。
            shared = bool(char.series_id and not char.project_id)
            if char.reference_asset_id and (shared or not reset):
                continue
            submit(TaskType.GENERATE_CHARACTER_REFERENCE,
                   f"生成角色参考图：{char.name}", character_id=char.id)

    elif step_key in ("image", "video"):
        task_type = TaskType.GENERATE_IMAGE if step_key == "image" else TaskType.GENERATE_VIDEO
        column = Shot.image_asset_id if step_key == "image" else Shot.video_asset_id
        label = "生成关键帧" if step_key == "image" else "生成视频"
        todo = [s for s in shots if reset or getattr(s, column.key) is None]
        if not todo:
            notes.append("该节点产物已齐全，可先回退再重生成，或使用 force 全量重跑")
        for shot in todo:
            submit(task_type, f"{label} {shot.code}", shot_id=shot.id,
                   payload={"provider": params.get(f"{step_key}_provider"), "batch": "stage-regenerate"})

    elif step_key == "voice":
        submit(TaskType.GENERATE_VOICE, "批量生成配音",
               payload={"force": reset, "shot_ids": params.get("shot_ids")})

    elif step_key == "music":
        submit(TaskType.GENERATE_MUSIC, "生成背景音乐",
               payload={"duration": project.target_duration, "mood": params.get("mood") or "calm"})

    elif step_key == "subtitle":
        submit(TaskType.GENERATE_SUBTITLE, "生成全片字幕", payload={"format": "srt"})

    elif step_key == "enhancement":
        submit(TaskType.ENHANCE_VIDEO, "画质增强",
               payload={"operations": params.get("operations"),
                        "asset_id": params.get("asset_id")})

    elif step_key == "editing":
        submit(TaskType.MERGE_VIDEO, "拼接镜头")

    elif step_key == "composing":
        submit(TaskType.COMPOSE_VIDEO, "合成最终成片",
               payload={"with_music": params.get("with_music", True),
                        "with_subtitle": params.get("with_subtitle", True),
                        "auto_quality_check": params.get("auto_quality_check", True)})

    elif step_key == "quality_check":
        submit(TaskType.QUALITY_CHECK, "质量检查",
               payload={"auto_repair": params.get("auto_repair", False)})

    return {"task_ids": task_ids, "notes": notes}


def regenerate_stage(
    db: Session, project: Project, step_key: str, *,
    actor: str = "user", reset: bool = True, reason: str = "",
    auto_rollback: bool = True, commit: bool = True, **params: Any,
) -> dict[str, Any]:
    """重新生产某个节点。

    reset=True（默认）：先清空该节点产物引用，再全量重跑（点击「重新生成」的直觉行为）
    reset=False：只补缺，不动已有产物
    auto_rollback=True：重跑「画面」等上游节点时，自动把下游置为待执行，
    避免出现「画面换了、视频还是旧的」这种不一致状态。
    """
    if step_key not in STAGE_KEYS:
        raise KeyError(f"未知节点: {step_key}。可用：{', '.join(STAGE_KEYS)}")

    workflow_svc.ensure_workflow(db, project)
    rollback_info: dict[str, Any] | None = None

    if reset and auto_rollback:
        downstream = STAGE_DOWNSTREAM.get(step_key, ())
        if downstream:
            rollback_info = rollback_to(
                db, project, step_key, actor=actor,
                reason=reason or f"重生成「{STAGE_LABELS[step_key]}」导致下游失效",
                purge_products=True, keep_review=False, commit=False,
            )

    if reset:
        _clear_stage_products(db, project, step_key)

    wf = workflow_svc.ensure_workflow(db, project)
    step = workflow_svc.get_step(db, wf, step_key)
    if step is not None:
        step.state = "RUNNING"
        step.started_at = utcnow()
        step.finished_at = None
        step.error = ""
        step.attempts = (step.attempts or 0) + 1
        if step.review_status == ReviewStatus.REJECTED:
            step.review_status = ReviewStatus.NONE
            step.review_comment = ""

    result = _stage_tasks(db, project, step_key, reset=reset, actor=actor, **params)
    project.progress = projects_svc.compute_progress(db, project)
    if project.status in ("COMPLETED", "PAUSED"):
        project.status = "GENERATING"

    # 同步型节点（脚本/分镜）任务为空，直接标记完成
    task_ids = result["task_ids"]
    if step is not None and not task_ids:
        step.state = "SUCCESS"
        step.finished_at = utcnow()

    agent_log.log_event(
        db, project_id=project.id, event="pipeline.regenerate", actor=actor,
        message=(f"重新生成节点「{STAGE_LABELS[step_key]}」"
                 + (f"，提交 {len(task_ids)} 个任务" if task_ids else "")
                 + (f"（{reason}）" if reason else "")),
        detail={"step_key": step_key, "reset": reset, "task_ids": task_ids,
                "rollback": bool(rollback_info)},
    )
    if commit:
        db.commit()
    return {
        "step_key": step_key,
        "label": STAGE_LABELS[step_key],
        "reset": reset,
        "task_ids": task_ids,
        "submitted": len(task_ids),
        "notes": result["notes"],
        "rollback": rollback_info,
        "progress": project.progress,
        "message": ("任务已入队，请轮询 get_task_status"
                    if task_ids else "该节点已同步重建完成"),
    }


# --------------------------------------------------------------------------- #
# 审核
# --------------------------------------------------------------------------- #
def review_stage(
    db: Session, project: Project, step_key: str, *,
    decision: str, actor: str = "user", comment: str = "",
    auto_rollback: bool = False, regenerate: bool = False, commit: bool = True,
) -> dict[str, Any]:
    """对节点产出给出审核结论。

    APPROVED：认可，节点锁定为已审核
    REJECTED：驳回；可选 auto_rollback（重置该节点与下游）与 regenerate（立即重跑）
    """
    decision = (decision or "").upper()
    if decision not in (ReviewStatus.APPROVED, ReviewStatus.REJECTED):
        raise ValueError("decision 只能是 APPROVED 或 REJECTED")
    if step_key not in STAGE_KEYS:
        raise KeyError(f"未知节点: {step_key}")

    wf = workflow_svc.ensure_workflow(db, project)
    step = workflow_svc.get_step(db, wf, step_key)
    if step is None:
        raise KeyError(f"节点不存在: {step_key}")
    if step.state != "SUCCESS" and decision == ReviewStatus.APPROVED:
        raise ValueError(f"节点「{STAGE_LABELS[step_key]}」尚未生产完成，无法审核通过")

    step.review_status = decision
    step.reviewed_by = actor
    step.reviewed_at = utcnow()
    step.review_comment = comment or ""

    cleanup: dict[str, Any] | None = None
    if decision == ReviewStatus.REJECTED and auto_rollback:
        cleanup = rollback_to(
            db, project, step_key, actor=actor,
            reason=comment or f"审核驳回「{STAGE_LABELS[step_key]}」",
            purge_products=True, keep_review=True, commit=False,
        )
        # 驳回结论要在回退之后仍然保留
        step = workflow_svc.get_step(db, workflow_svc.ensure_workflow(db, project), step_key)
        if step is not None:
            step.review_status = ReviewStatus.REJECTED
            step.reviewed_by = actor
            step.reviewed_at = utcnow()
            step.review_comment = comment or ""

    regen: dict[str, Any] | None = None
    if decision == ReviewStatus.REJECTED and regenerate:
        regen = regenerate_stage(
            db, project, step_key, actor=actor, reset=True,
            reason=comment or "审核驳回后重生成", auto_rollback=False, commit=False,
        )

    agent_log.log_event(
        db, project_id=project.id, event=f"pipeline.review.{decision.lower()}",
        actor=actor, level="INFO" if decision == ReviewStatus.APPROVED else "WARN",
        message=(f"节点「{STAGE_LABELS[step_key]}」审核"
                 f"{'通过' if decision == ReviewStatus.APPROVED else '驳回'}"
                 + (f"：{comment}" if comment else "")),
        detail={"step_key": step_key, "decision": decision, "comment": comment},
    )
    if commit:
        db.commit()
    return {
        "step_key": step_key, "label": STAGE_LABELS[step_key],
        "review_status": decision, "reviewed_by": actor, "comment": comment,
        "rollback": cleanup, "regenerate": regen,
    }


# --------------------------------------------------------------------------- #
# 便捷：等待某节点任务（供 Agent / CLI 轮询）
# --------------------------------------------------------------------------- #
def stage_status(db: Session, project_id: str, step_key: str) -> dict[str, Any]:
    tasks = tasks_svc.list_tasks(db, project_id=project_id, limit=200)
    related = [t for t in tasks if (t.payload or {}).get("step_key") == step_key
               or step_key in (t.name or "")]
    running = [t for t in related if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING,
                                                   TaskStatus.RETRYING)]
    failed = [t for t in related if t.status == TaskStatus.FAILED]
    return {
        "step_key": step_key,
        "tasks": len(related),
        "running": [t.id for t in running],
        "failed": [t.id for t in failed],
        "done": len([t for t in related if t.status == TaskStatus.SUCCESS]),
    }
