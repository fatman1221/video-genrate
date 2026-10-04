"""生成计划 —— Preview → Confirm → Produce 四步硬闸门。

**为什么必须要有**

花钱之前得能看清要花什么。现状是 ``generate_all_videos`` 直接建任务开跑，
以 33 镜 × 6 分钟计，一次误触发就是 3.3 小时的 GPU 时间。drama-skills 的做法是
把「计划」本身变成可持久化实体，四步顺序不可合并：

1. **建计划**（``DRAFT``）：一种模态、明确数量、完整 Prompt、参考文件、参数、输出
2. **预览**（``PREVIEWED``）：校验 + 算指纹 + 出汇总，**不消耗任何生成资源**
3. **确认**（``CONFIRMED``）：只有创作者**看到预览之后**明确同意**这一项当前任务**才算
   —— 「继续」「都做完」「之前确认过另一版」都不算
4. **物化**（``RUNNING``）：创建真实 Task。**这一步才花钱**

**三条不可违背的纪律**

- **指纹绑定**：``fingerprint = sha256(canonical(items + parameters))``；
  计划任一字段变化 → 旧确认立即失效，必须重新预览
- **一次性消费**：确认只能用于一次物化；重复物化被拒
- **预估不承诺**：``summary.est_cost_note`` **只描述口径、不报具体金额**
  —— 我们没有可靠的计价数据，报数字就是撒谎

**中断成本保护**（照搬 drama-skills 的教训）

> 视频任务在**提交那一刻**就已经计费，不是在拿到结果时。不要在 audit
> 报 orphaned 时直接重投 —— 那是为同一个镜头付第二次钱。

因此物化的任务把 provider 侧 job id 尽早写进 ``Task.result``（由 handler 负责），
重投前应先查该 id 能否免费取回。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.constants import AssetStatus, TaskType
from ..models import (
    Asset, GenerationPlan, GenerationPlanItem, Project, PromptVersion, ProviderRecord, Shot, new_id,
)
from . import agent_log
from . import resolution as resolution_svc
from .tasks import create_task

PLAN_STATUS = ("DRAFT", "PREVIEWED", "CONFIRMED", "RUNNING", "DONE", "CANCELLED", "EXPIRED")
ITEM_STATUS = ("PENDING", "TASK_CREATED", "SKIPPED", "FAILED")

#: 模态 → Provider kind / Task 类型
MODALITY_KIND = {"image": "image", "video": "video", "voice": "tts", "music": "music"}
MODALITY_TASK = {
    "image": TaskType.GENERATE_IMAGE,
    "video": TaskType.GENERATE_VIDEO,
    "voice": TaskType.GENERATE_VOICE,
    "music": TaskType.GENERATE_MUSIC,
}

#: 单个产物的粗估耗时（秒）—— 只用于给创作者一个「大概多大工程」的手感，
#: **不是承诺**。视频按本机实测（流式采样 20GB 权重）取 360s。
EST_SECONDS = {"image": 60, "video": 360, "voice": 20, "music": 60}

#: 确认有效期
DEFAULT_TTL_MINUTES = 30

class PlanError(ValueError):
    """计划流程违例（指纹不符、状态不对、引用 PLAN 态资产等）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 建计划
# --------------------------------------------------------------------------- #
def create_plan(
    db: Session, project: Project, *, name: str = "", plan_type: str = "batch_image",
    items: list[dict[str, Any]], parameters: dict[str, Any] | None = None,
    source_scope: dict[str, Any] | None = None, actor: str = "agent",
) -> GenerationPlan:
    """建计划（**不花钱**）。``items`` 每项至少要有 ``shot_id`` + ``modality``。"""
    if not items:
        raise PlanError("计划不能为空")
    plan = GenerationPlan(
        id=new_id("plan"),
        project_id=project.id,
        name=name or f"{plan_type} · {len(items)} 项",
        plan_type=plan_type,
        source_scope=source_scope or {},
        parameters=dict(parameters or {}),
        summary={},
        status="DRAFT",
    )
    db.add(plan)
    db.flush()

    for ordinal, spec in enumerate(items, start=1):
        modality = (spec.get("modality") or "image").strip()
        if modality not in MODALITY_KIND:
            raise PlanError(f"未知模态「{modality}」，可选：{', '.join(MODALITY_KIND)}")
        shot_id = spec.get("shot_id")
        if shot_id and db.get(Shot, shot_id) is None:
            raise PlanError(f"计划项 {ordinal} 引用了不存在的镜头：{shot_id}")
        item = GenerationPlanItem(
            id=new_id("pli"),
            plan_id=plan.id,
            ordinal=ordinal,
            shot_id=shot_id,
            prompt_version_id=spec.get("prompt_version_id"),
            modality=modality,
            provider=str(spec.get("provider") or ""),
            model=str(spec.get("model") or ""),
            width=int(spec.get("width") or 0),
            height=int(spec.get("height") or 0),
            aspect_ratio=str(spec.get("aspect_ratio") or ""),
            resolution=str(spec.get("resolution") or ""),
            parameters=dict(spec.get("parameters") or {}),
            reference_assets=list(spec.get("reference_assets") or []),
            predicted_cost=dict(spec.get("predicted_cost") or {}),
            status="PENDING",
        )
        db.add(item)
    db.flush()
    agent_log.log_event(
        db, project_id=project.id, event="generation_plan.created", actor=actor,
        message=f"建立生成计划「{plan.name}」（{len(items)} 项，尚未消耗资源）",
        detail={"plan_id": plan.id, "plan_type": plan_type},
    )
    return plan


# --------------------------------------------------------------------------- #
# 预览（不花钱）
# --------------------------------------------------------------------------- #
def preview_plan(db: Session, plan: GenerationPlan, *, actor: str = "agent") -> GenerationPlan:
    """校验 + 算指纹 + 出汇总。**绝不创建 Task、绝不调用 Provider。**

    校验不通过（引用了 PLAN 态资产 / 不存在的素材等）→ 抛 :class:`PlanError`。
    软问题（prompt 版本被取代、provider 未声明分辨率支持）→ 收集到 ``warnings``，
    不阻断，交由 Agent 判断。
    """
    if plan.status in ("CONFIRMED", "RUNNING"):
        raise PlanError(f"计划已进入 {plan.status}，不能重新预览；如需变更请新建计划")
    items = list_plan_items(db, plan.id)
    if not items:
        raise PlanError("计划里没有任何条目")

    failures: list[str] = []
    warnings: list[str] = []
    by_modality: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    resolutions: dict[str, int] = {}
    est_seconds = 0

    for item in items:
        by_modality[item.modality] = by_modality.get(item.modality, 0) + 1
        by_provider[item.provider or "(默认)"] = by_provider.get(item.provider or "(默认)", 0) + 1
        if item.resolution:
            resolutions[item.resolution] = resolutions.get(item.resolution, 0) + 1
        est_seconds += EST_SECONDS.get(item.modality, 60)

        # --- Prompt 版本 ---
        if item.prompt_version_id:
            version = db.get(PromptVersion, item.prompt_version_id)
            if version is None:
                failures.append(f"第 {item.ordinal} 项引用了不存在的 Prompt 版本")
            elif version.status in ("SUPERSEDED",):
                warnings.append(f"第 {item.ordinal} 项的 Prompt 版本已被取代（{version.status}）")
            elif version.status not in ("READY", "APPROVED"):
                warnings.append(f"第 {item.ordinal} 项的 Prompt 版本状态为 {version.status}")
        else:
            warnings.append(f"第 {item.ordinal} 项没有绑定 Prompt 版本，将依赖运行时现场生成")

        # --- 参考资产：PLAN 态**不可作生产输入** ---
        for slot in item.reference_assets or []:
            kind = (slot.get("kind") or "REF").upper()
            if kind == "PLAN":
                failures.append(
                    f"第 {item.ordinal} 项的参考图「{slot.get('label') or slot.get('slot')}」"
                    "是 PLAN 态（创作者自备、项目内不存在），不能作生产输入"
                )
                continue
            if kind == "IMG":
                continue
            asset_id = slot.get("asset_id")
            if not asset_id:
                warnings.append(f"第 {item.ordinal} 项有参考槽位但没绑资产：{slot.get('slot')}")
                continue
            asset = db.get(Asset, asset_id)
            if asset is None:
                failures.append(f"第 {item.ordinal} 项的参考资产不存在：{asset_id}")
            elif asset.status != AssetStatus.READY:
                warnings.append(f"第 {item.ordinal} 项的参考资产状态为 {asset.status}")

        # --- Provider 能力 ---
        ok, note = check_provider_capability(db, item.modality, item.provider, item.resolution)
        if not ok:
            failures.append(f"第 {item.ordinal} 项：{note}")
        elif note:
            warnings.append(f"第 {item.ordinal} 项：{note}")

    if failures:
        raise PlanError("预览未通过：" + "；".join(failures))

    plan.summary = {
        "item_count": len(items),
        "by_modality": by_modality,
        "by_provider": by_provider,
        "resolutions": resolutions,
        "est_seconds": est_seconds,
        "est_seconds_human": _human_seconds(est_seconds),
        "est_cost_note": "本机自建管线（ComfyUI / ffmpeg / edge-tts），无按次计费；耗时按本机实测粗估，非承诺",
        "warnings": warnings,
    }
    plan.fingerprint = compute_fingerprint(db, plan)
    plan.status = "PREVIEWED"
    db.flush()
    agent_log.log_event(
        db, project_id=plan.project_id, event="generation_plan.previewed", actor=actor,
        message=f"预览计划「{plan.name}」：{len(items)} 项，预计约 {_human_seconds(est_seconds)}（未消耗资源）",
        detail={"plan_id": plan.id, "fingerprint": plan.fingerprint[:12], "warnings": warnings},
    )
    return plan


def _human_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def check_provider_capability(
    db: Session, modality: str, provider: str, resolution: str
) -> tuple[bool, str]:
    """校验 Provider 是否支持该模态（以及它自己声明的分辨率）。

    分辨率是**可选声明**：能力里写了 ``resolution:4K,2K`` 才校验，没写就跳过
    —— 免得把「没声明」误判成「不支持」。模型不写死在这里。
    """
    expected_kind = MODALITY_KIND.get(modality, "")
    if not provider:
        return True, ""
    record = db.execute(
        select(ProviderRecord).where(
            (ProviderRecord.id == provider) | (ProviderRecord.name == provider)
        )
    ).scalars().first()
    if record is None:
        return True, f"Provider「{provider}」未在注册表中，跳过能力校验（运行时可能回退到默认）"
    if expected_kind and record.kind != expected_kind:
        return False, f"Provider「{provider}」的类别是 {record.kind}，不适用于 {modality}"
    if not resolution:
        return True, ""
    # 档位写法统一由 resolution 模块归一化（``2160p`` → ``4K``），
    # 避免同一件事在能力声明、入参、错误信息里出现三种拼法
    try:
        wanted = resolution_svc.normalize_resolution(resolution)
    except resolution_svc.ResolutionError as exc:
        return False, str(exc)
    supported = resolution_svc.supported_of(record.capabilities)
    if not supported:
        # 没声明分辨率能力 = 不做这项校验（免得把"没声明"误判成"不支持"）
        return True, ""
    if wanted not in supported:
        return False, (
            f"Provider「{provider}」不支持分辨率 {wanted}，"
            f"它声明支持：{', '.join(supported)}"
        )
    native = resolution_svc.native_resolution_of(record.capabilities)
    if native and resolution_svc.tier_rank(wanted) > resolution_svc.tier_rank(native):
        return True, (
            f"Provider「{provider}」原生档位为 {native}，{wanted} 属于超采样"
            f"（不等于更高画质，建议原生出图后用超分链放大）"
        )
    return True, ""


# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #
def _canonical_items(db: Session, plan: GenerationPlan) -> list[dict[str, Any]]:
    items = list_plan_items(db, plan.id)
    return [
        {
            "ordinal": item.ordinal,
            "shot_id": item.shot_id,
            "prompt_version_id": item.prompt_version_id,
            "modality": item.modality,
            "provider": item.provider,
            "model": item.model,
            "width": item.width,
            "height": item.height,
            "aspect_ratio": item.aspect_ratio,
            "resolution": item.resolution,
            "parameters": item.parameters or {},
            "reference_assets": [
                {"slot": s.get("slot"), "kind": s.get("kind"), "asset_id": s.get("asset_id")}
                for s in (item.reference_assets or [])
            ],
        }
        for item in items
    ]


def compute_fingerprint(db: Session, plan: GenerationPlan) -> str:
    """``sha256(canonical(items + parameters))``。"""
    payload = {
        "plan_type": plan.plan_type,
        "parameters": plan.parameters or {},
        "items": _canonical_items(db, plan),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 确认
# --------------------------------------------------------------------------- #
def confirm_plan(
    db: Session, plan: GenerationPlan, fingerprint: str, *, actor: str = "agent",
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> GenerationPlan:
    """确认（**不花钱**）。指纹不符 → 拒绝，要求重新预览。

    确认短语约定 ``CONFIRM <plan_id> <fingerprint[:12]>`` —— 便于 Agent 把
    「看到预览之后明确同意这一项当前任务」这件事落成可核验的动作。
    """
    if plan.status == "DRAFT":
        raise PlanError("计划尚未预览，不能确认")
    if plan.status != "PREVIEWED":
        raise PlanError(f"计划当前状态为 {plan.status}，不能确认（只允许从 PREVIEWED 确认）")

    current = compute_fingerprint(db, plan)
    expected = (fingerprint or "").strip().lower()
    if not expected:
        raise PlanError(
            f"确认必须带上指纹，格式：CONFIRM {plan.id} {current[:12]}"
        )
    if expected not in (current.lower(), current[:12].lower()):
        raise PlanError(
            "指纹不符 —— 计划内容在预览之后发生变化，旧确认已失效，请重新预览后再确认"
        )

    plan.status = "CONFIRMED"
    plan.fingerprint = current
    plan.confirmed_at = _now()
    plan.confirmed_by = actor
    plan.expires_at = _now() + timedelta(minutes=ttl_minutes)
    db.flush()
    agent_log.log_event(
        db, project_id=plan.project_id, event="generation_plan.confirmed", actor=actor,
        message=f"确认生成计划「{plan.name}」（指纹 {current[:12]}，{ttl_minutes} 分钟内有效）",
        detail={"plan_id": plan.id, "fingerprint": current},
    )
    return plan


# --------------------------------------------------------------------------- #
# 物化（**这一步才花钱**）
# --------------------------------------------------------------------------- #
def materialize_plan(db: Session, plan: GenerationPlan, *, actor: str = "agent") -> list[str]:
    """把已确认的计划物化成真实 Task。返回新建的 task id 列表。

    一次性消费：只有 ``CONFIRMED`` 且未过期、且指纹仍一致才允许，重复调用直接拒绝。
    """
    if plan.status != "CONFIRMED":
        raise PlanError(
            f"计划当前状态为 {plan.status}，不能物化"
            "（确认只能消费一次；需要再跑请重新预览并确认）"
        )
    if plan.expires_at and plan.expires_at < _now():
        plan.status = "EXPIRED"
        db.flush()
        raise PlanError("确认已过期，请重新预览并确认")

    current = compute_fingerprint(db, plan)
    if current != plan.fingerprint:
        raise PlanError("指纹不符 —— 计划内容已变化，旧确认失效，请重新预览并确认")

    items = list_plan_items(db, plan.id)
    pending = [item for item in items if item.status == "PENDING"]
    if not pending:
        raise PlanError("计划里没有待生成的条目")

    project = db.get(Project, plan.project_id)
    task_ids: list[str] = []
    for item in pending:
        task = create_task(
            db,
            project_id=plan.project_id,
            type=MODALITY_TASK[item.modality],
            name=f"{plan.name} #{item.ordinal}",
            payload=_item_payload(db, item, project),
            shot_id=item.shot_id,
            provider=item.provider,
            created_by=actor,
            commit=False,
        )
        item.status = "TASK_CREATED"
        item.task_id = task.id
        task_ids.append(task.id)

    plan.task_ids = list(plan.task_ids or []) + task_ids
    plan.status = "RUNNING"
    db.flush()
    agent_log.log_event(
        db, project_id=plan.project_id, event="generation_plan.materialized", actor=actor,
        message=f"物化计划「{plan.name}」：创建 {len(task_ids)} 个任务",
        detail={"plan_id": plan.id, "task_ids": task_ids},
    )
    return task_ids


def _item_payload(db: Session, item: GenerationPlanItem, project: Project | None) -> dict[str, Any]:
    """组装任务负载：把编译好的正文与参考图一并带上，handler 不必再回查。"""
    version = db.get(PromptVersion, item.prompt_version_id) if item.prompt_version_id else None
    reference_paths = [
        s.get("file_path") for s in (item.reference_assets or [])
        if (s.get("kind") or "REF").upper() == "REF" and s.get("file_path")
    ]
    payload: dict[str, Any] = {
        "shot_id": item.shot_id,
        "prompt_version_id": item.prompt_version_id,
        "prompt": (version.compiled_prompt if version else "") or "",
        "negative_prompt": (version.negative_prompt if version else "") or "",
        "width": item.width or (project.width if project else 0),
        "height": item.height or (project.height if project else 0),
        "aspect_ratio": item.aspect_ratio or (project.aspect_ratio if project else ""),
        "resolution": item.resolution,
        "provider": item.provider,
        "model": item.model,
        "reference_images": [p for p in reference_paths if p],
        "parameters": item.parameters or {},
        "generation_plan_item_id": item.id,
    }
    return {k: v for k, v in payload.items() if v not in (None, "", [], {})}


# --------------------------------------------------------------------------- #
# 取消与查询
# --------------------------------------------------------------------------- #
def cancel_plan(db: Session, plan: GenerationPlan, *, actor: str = "agent", reason: str = "") -> GenerationPlan:
    if plan.status in ("DONE", "CANCELLED"):
        return plan
    plan.status = "CANCELLED"
    db.flush()
    agent_log.log_event(
        db, project_id=plan.project_id, event="generation_plan.cancelled", actor=actor,
        message=f"取消生成计划「{plan.name}」{f'：{reason}' if reason else ''}",
        detail={"plan_id": plan.id},
    )
    return plan


def get_plan(db: Session, plan_id: str) -> GenerationPlan | None:
    return db.get(GenerationPlan, plan_id)


def list_plans(db: Session, project_id: str) -> list[GenerationPlan]:
    return list(db.execute(
        select(GenerationPlan).where(GenerationPlan.project_id == project_id)
        .order_by(GenerationPlan.created_at.desc())
    ).scalars().all())


def list_plan_items(db: Session, plan_id: str) -> list[GenerationPlanItem]:
    return list(db.execute(
        select(GenerationPlanItem).where(GenerationPlanItem.plan_id == plan_id)
        .order_by(GenerationPlanItem.ordinal.asc())
    ).scalars().all())


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def plan_brief(plan: GenerationPlan, *, items: list[GenerationPlanItem] | None = None) -> dict[str, Any]:
    data = {
        "id": plan.id,
        "project_id": plan.project_id,
        "name": plan.name,
        "plan_type": plan.plan_type,
        "source_scope": plan.source_scope or {},
        "parameters": plan.parameters or {},
        "summary": plan.summary or {},
        "fingerprint": plan.fingerprint,
        "confirm_phrase": f"CONFIRM {plan.id} {(plan.fingerprint or '')[:12]}" if plan.fingerprint else "",
        "status": plan.status,
        "confirmed_at": plan.confirmed_at.isoformat() if plan.confirmed_at else None,
        "confirmed_by": plan.confirmed_by,
        "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
        "task_ids": plan.task_ids or [],
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
    }
    if items is not None:
        data["items"] = [item_brief(item) for item in items]
    return data


def item_brief(item: GenerationPlanItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "plan_id": item.plan_id,
        "ordinal": item.ordinal,
        "shot_id": item.shot_id,
        "prompt_version_id": item.prompt_version_id,
        "modality": item.modality,
        "provider": item.provider,
        "model": item.model,
        "width": item.width,
        "height": item.height,
        "aspect_ratio": item.aspect_ratio,
        "resolution": item.resolution,
        "parameters": item.parameters or {},
        "reference_assets": item.reference_assets or [],
        "predicted_cost": item.predicted_cost or {},
        "status": item.status,
        "task_id": item.task_id,
    }
