"""角色池解析。

**一条重要边界：读用「池」，写只动「本集」。**

- *可用角色池*（``character_pool``）= 本集自有角色 + 所属系列的系列级角色。
  分镜引用、planner 提示、界面展示都应该看到完整的池子，否则连续剧的主角
  在每一集里都会「查无此人」。
- *本集自有角色*（``own_characters``）= 仅 ``project_id`` 指向本集的角色。
  重置、重新生成这类写操作只能作用于它们 —— 系列级角色的参考图是全系列共享的，
  若被某一集重置，其它集也会跟着遭殃。
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Character, Project


def character_pool(db: Session, project: Project) -> list[Character]:
    """某一集可用的全部角色：本集角色 + 所属系列的系列级角色（按创建时间排序）。"""
    conditions = [Character.project_id == project.id]
    if project.series_id:
        conditions.append(Character.series_id == project.series_id)
    stmt = select(Character).where(or_(*conditions)).order_by(Character.created_at.asc())
    return list(db.execute(stmt).scalars())


def own_characters(db: Session, project_id: str) -> list[Character]:
    """仅属于该集的角色（不含系列级角色）—— 写操作的安全边界。"""
    stmt = (
        select(Character)
        .where(Character.project_id == project_id)
        .order_by(Character.created_at.asc())
    )
    return list(db.execute(stmt).scalars())


def pool_size(db: Session, project: Project) -> int:
    """可用角色数量，供进度统计使用。"""
    return len(character_pool(db, project))


def resolve_character_ids(db: Session, project: Project, values: list) -> list[str]:
    """把分镜里写的角色（**角色 id 或中文名都可以**）统一解析成角色 id。

    planner 产出的是 id，人手写/Agent 写的往往是名字，两种都必须支持，
    否则会出现「提示词里角色名是对的、引用却丢了」这种半对半错的状态。
    """
    if not values:
        return []
    pool = character_pool(db, project)
    by_id = {c.id: c.id for c in pool}
    by_name = {c.name: c.id for c in pool}
    out: list[str] = []
    for value in values:
        if not value:
            continue
        key = str(value)
        resolved = by_id.get(key) or by_name.get(key)
        if resolved:
            out.append(resolved)
    return out
