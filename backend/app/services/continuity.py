"""连续性 —— 锁（LOCK-）与增量（DELTA-）。

**这是最容易被搞混的地方，drama-skills 把它们明确分成两个机制：**

============  ==========================  ====================================
              ``LOCK-`` 连续性锁          ``DELTA-`` 连续性增量
============  ==========================  ====================================
回答什么      「什么永远不变」            「什么变了、从什么变成什么、为什么」
粒度          最小名词短语                before / after / cause / 有效期 / 影响范围
什么时候用    跨镜一致                    剧情导致的状态变化
============  ==========================  ====================================

两者**分表建，不合并**（``continuity_locks`` / ``continuity_deltas``）。

## 锁面（surface）的三条硬规则

1. **只保留最小可辨识名词短语**：颜色 + 材质/形制 + 物体
2. **不含**标点、动作、状态、数量、镜头信息、剧情
3. **不写会随镜头变化的词**（``in her hands`` / ``half-finished`` / ``on the sofa``）

## 校验器必须抓的两种「假命中」

1. **粘在词上的匹配不算**：``chipped white enamel mug`` **不被**
   ``unchipped white enamel mug`` 满足（词边界要卡住前后）
2. **负面提示词里的匹配不算**：写在 ``no`` / ``not`` / ``without`` 之后的
   不构成「在场证据」→ 因此本模块只认**正向正文**里的命中

## 数量纪律（craft_default，不阻断）

一集里的锁通常是个位数。把每条识别锚点都升级成锁，提示词会被同一串名词短语撑满，
反而挤掉本镜真正要执行的动作。超过 :data:`LOCK_COUNT_HINT` 会记一条警告日志。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ContinuityDelta, ContinuityLock, Project, Shot, new_id
from . import agent_log

#: 锁面长度上限（拼音/英文名词短语不该超过这个量级）
SURFACE_MAX_CHARS = 120
#: 锁面词数上限
SURFACE_MAX_WORDS = 8
#: 单项目锁数量的软上限（craft_default：只提示，不阻断）
LOCK_COUNT_HINT = 12

SUBJECT_TYPES = ("character", "location", "prop", "style")
PROMPT_LANGUAGES = ("en", "zh")

#: 标点 —— 锁面里不允许出现
_PUNCT_RE = re.compile(r"[，。、；：！？,.;:!?\"'“”‘’()（）\[\]{}<>《》—…~`|/\\*#@$%^&=+]")
#: 镜头 / 分镜信息 —— 属于瞬态，不该上锁
_PLOT_RE = re.compile(r"(shot|scene|frame|镜头|场景|画面|特写|近景|远景|中景)", re.IGNORECASE)
#: 状态 / 动作 / 位置 —— 会随剧情变，不该上锁
_STATE_RE = re.compile(
    r"(in (his|her|their|its) hands?|holding|wearing|wet|drying|damaged|healed|broken"
    r"|folded|unfolded|open|closed|half-?finished"
    r"|手里|拿着|穿着|湿|晾干|破损|愈合|打开|合上|半成品|桌上|地上|沙发)",
    re.IGNORECASE,
)
#: 数量词 —— 锁面不写数量
_QUANTITY_RE = re.compile(r"^\s*\d+\s|\b(one|two|three|a pair of|several)\b|^\s*[一两二三四五六七八九十]\s*", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


# --------------------------------------------------------------------------- #
# 锁面卫生
# --------------------------------------------------------------------------- #
def validate_surface(surface: str) -> list[str]:
    """检查锁面是否符合「最小名词短语」规范，返回问题列表（空 = 通过）。

    这些是 ``structural_invariant`` 级规则：编译期**阻断**，不是警告。
    """
    problems: list[str] = []
    body = (surface or "").strip()
    if not body:
        problems.append("锁面不能为空")
        return problems
    if len(body) > SURFACE_MAX_CHARS:
        problems.append(f"锁面过长（{len(body)} > {SURFACE_MAX_CHARS} 字符），只保留最小名词短语")
    if _PUNCT_RE.search(body):
        problems.append("锁面不能含标点，只保留名词短语")
    if _PLOT_RE.search(body):
        problems.append("锁面不能含镜头 / 分镜信息（会随镜头变，属瞬态）")
    if _STATE_RE.search(body):
        problems.append("锁面不能含会随剧情变化的状态 / 动作 / 位置词")
    if _QUANTITY_RE.search(body):
        problems.append("锁面不能写数量")
    if len(body.split()) > SURFACE_MAX_WORDS:
        problems.append(f"锁面词数过多（{len(body.split())} > {SURFACE_MAX_WORDS}），不像最小名词短语")
    return problems


def _normalize(surface: str) -> str:
    return re.sub(r"\s+", " ", (surface or "").strip())


def surface_hits(text: str, surface: str) -> bool:
    """锁面是否**真的在场**（整词匹配，不粘在别的词上）。

    - 全 ASCII 锁面 → 用词边界正则（``unchipped`` 不会命中 ``chipped``）
    - 含中文的锁面 → 中文不分词，退化为子串匹配（否则会大量误报为缺失）
    """
    body = _normalize(surface)
    if not body or not text:
        return False
    haystack = re.sub(r"\s+", " ", text)
    if _CJK_RE.search(body):
        return body.lower() in haystack.lower()
    pattern = re.compile(
        r"(?<![0-9A-Za-z])"
        + r"\s+".join(re.escape(word) for word in body.split(" "))
        + r"(?![0-9A-Za-z])",
        re.IGNORECASE,
    )
    return bool(pattern.search(haystack))


def verify_surfaces(
    positive: str, negative: str, locks: Iterable[ContinuityLock]
) -> list[dict[str, Any]]:
    """校验每条 in-scope 锁是否逐字出现在**正向正文**里。

    返回问题列表；空列表 = 全部通过。负面提示词里的命中**不算在场证据**，
    因此这里只看 ``positive``（但会把 ``in_negative`` 一并报出来便于排查）。
    """
    problems: list[dict[str, Any]] = []
    for lock in locks:
        hit = surface_hits(positive, lock.surface)
        if hit:
            continue
        problems.append({
            "lock_id": lock.id,
            "code": lock.code,
            "surface": lock.surface,
            "reason": "missing_in_positive",
            "in_negative": surface_hits(negative, lock.surface),
            "message": f"连续性锁 {lock.code}「{lock.surface}」未出现在正向提示词正文中",
        })
    return problems


# --------------------------------------------------------------------------- #
# 锁
# --------------------------------------------------------------------------- #
def create_lock(
    db: Session, project: Project, *, name: str, surface: str, code: str = "",
    subject_type: str = "", subject_id: str = "", variant_id: str = "",
    shot_scope: list[Any] | None = None, prompt_scope: list[Any] | None = None,
    bible_id: str | None = None, prompt_language: str = "en",
    actor: str = "agent",
) -> ContinuityLock:
    """建锁。锁面不合规 → **直接报错**（不静默放过）。"""
    problems = validate_surface(surface)
    if problems:
        raise ValueError("锁面不合规：" + "；".join(problems))
    if subject_type and subject_type not in SUBJECT_TYPES:
        raise ValueError(f"未知主体类型「{subject_type}」，可选：{', '.join(SUBJECT_TYPES)}")
    if prompt_language not in PROMPT_LANGUAGES:
        raise ValueError(f"未知提示词语言「{prompt_language}」，可选：{', '.join(PROMPT_LANGUAGES)}")

    normalized = _normalize(surface)
    existing = db.execute(
        select(ContinuityLock).where(
            ContinuityLock.project_id == project.id, ContinuityLock.surface == normalized
        )
    ).scalars().first()
    if existing is not None:
        raise ValueError(f"锁面已存在（{existing.code}）：{normalized}")

    lock_code = code or f"LOCK-{_code_tail(name, project.id, normalized)}"
    lock = ContinuityLock(
        id=new_id("lock"),
        project_id=project.id,
        bible_id=bible_id,
        code=lock_code,
        name=name or normalized,
        surface=normalized,
        prompt_language=prompt_language,
        subject_type=subject_type,
        subject_id=subject_id,
        variant_id=variant_id,
        shot_scope=shot_scope if shot_scope is not None else ["all"],
        prompt_scope=prompt_scope or [],
        status="ACTIVE",
        version=1,
    )
    db.add(lock)
    db.flush()

    total = len(list_project_locks(db, project.id))
    if total > LOCK_COUNT_HINT:
        agent_log.log_event(
            db, project_id=project.id, event="continuity.lock.over_locked", level="WARN", actor=actor,
            message=f"本项目已有 {total} 条连续性锁（建议个位数）—— 锁太多会挤掉本镜真正要执行的动作",
            detail={"lock_count": total},
        )
    agent_log.log_event(
        db, project_id=project.id, event="continuity.lock.created", actor=actor,
        message=f"建立连续性锁 {lock.code}「{lock.name}」：{lock.surface}",
        detail={"lock_id": lock.id, "surface": lock.surface, "shot_scope": lock.shot_scope},
    )
    return lock


def _code_tail(name: str, *seeds: str) -> str:
    import hashlib

    cleaned = re.sub(r"[^0-9A-Za-z]+", "-", name or "").strip("-").upper()
    if cleaned:
        return cleaned[:40]
    digest = hashlib.md5("|".join(seeds).encode("utf-8")).hexdigest()[:6].upper()
    return f"X{digest}"


def list_project_locks(db: Session, project_id: str, *, only_active: bool = True) -> list[ContinuityLock]:
    query = select(ContinuityLock).where(ContinuityLock.project_id == project_id)
    if only_active:
        query = query.where(ContinuityLock.status == "ACTIVE")
    return list(db.execute(query.order_by(ContinuityLock.created_at.asc())).scalars().all())


def lock_applies_to_shot(lock: ContinuityLock, shot: Shot) -> bool:
    """锁是否作用于该镜头。空 scope 一律按「全集」处理。"""
    scope = lock.shot_scope or []
    if not scope or "all" in scope:
        return True
    if shot.id in scope:
        return True
    return bool(shot.code and shot.code in scope)


def locks_for_shot(db: Session, shot: Shot) -> list[ContinuityLock]:
    """取该镜头生效的全部锁（供 Prompt Compiler 强制注入）。"""
    locks = list_project_locks(db, shot.project_id, only_active=True)
    return [lock for lock in locks if lock_applies_to_shot(lock, shot)]


# --------------------------------------------------------------------------- #
# 增量
# --------------------------------------------------------------------------- #
def create_delta(
    db: Session, project: Project, *, code: str = "", subject_type: str, subject_id: str,
    state_field: str, before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
    scene_id: str | None = None, shot_id: str | None = None, cause_shot_id: str | None = None,
    effective_from: str = "", effective_until: str = "",
    next_linked_shot_id: str | None = None,
    reconciliation_status: str = "must_match_or_revise",
    affected_refs: list[Any] | None = None, actor: str = "agent",
) -> ContinuityDelta:
    """记录一次连续性变化。

    纪律：「未知」不等于「恢复默认」—— 上集带伤、本集没提，**不能**自动视为无伤。
    保留最后确认状态，并把 ``reconciliation_status`` 置为 ``unresolved``
    交由后续边界核对解决。
    """
    if not subject_type or not subject_id:
        raise ValueError("连续性增量必须指定 subject_type 与 subject_id")
    if not state_field:
        raise ValueError("连续性增量必须指定 state_field（如 condition.open / custody）")
    resolved_code = code or f"DELTA-{_code_tail(state_field, subject_id or '', project.id)}"
    delta = ContinuityDelta(
        id=new_id("delta"),
        project_id=project.id,
        code=resolved_code,
        scene_id=scene_id,
        shot_id=shot_id,
        subject_type=subject_type,
        subject_id=subject_id,
        state_field=state_field,
        before=before or {},
        after=after or {},
        cause_shot_id=cause_shot_id,
        effective_from=effective_from,
        effective_until=effective_until,
        next_linked_shot_id=next_linked_shot_id,
        reconciliation_status=reconciliation_status,
        affected_refs=affected_refs or [],
        status="ACTIVE",
    )
    db.add(delta)
    db.flush()
    agent_log.log_event(
        db, project_id=project.id, event="continuity.delta.created", actor=actor,
        message=f"记录连续性增量 {delta.code}：{state_field}",
        detail={"delta_id": delta.id, "before": delta.before, "after": delta.after},
    )
    return delta


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def lock_brief(lock: ContinuityLock) -> dict[str, Any]:
    return {
        "id": lock.id,
        "project_id": lock.project_id,
        "bible_id": lock.bible_id,
        "code": lock.code,
        "name": lock.name,
        "surface": lock.surface,
        "prompt_language": lock.prompt_language,
        "subject_type": lock.subject_type,
        "subject_id": lock.subject_id,
        "variant_id": lock.variant_id,
        "shot_scope": lock.shot_scope or [],
        "prompt_scope": lock.prompt_scope or [],
        "status": lock.status,
        "version": lock.version,
    }


def delta_brief(delta: ContinuityDelta) -> dict[str, Any]:
    return {
        "id": delta.id,
        "project_id": delta.project_id,
        "code": delta.code,
        "scene_id": delta.scene_id,
        "shot_id": delta.shot_id,
        "subject_type": delta.subject_type,
        "subject_id": delta.subject_id,
        "state_field": delta.state_field,
        "before": delta.before or {},
        "after": delta.after or {},
        "cause_shot_id": delta.cause_shot_id,
        "effective_from": delta.effective_from,
        "effective_until": delta.effective_until,
        "next_linked_shot_id": delta.next_linked_shot_id,
        "reconciliation_status": delta.reconciliation_status,
        "affected_refs": delta.affected_refs or [],
        "status": delta.status,
    }
