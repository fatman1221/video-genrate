"""数据库连接与会话管理。"""
from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

# 跨方言 JSON 类型：PostgreSQL 用 JSONB，其它用通用 JSON
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _build_engine():
    kwargs: dict = {"echo": settings.db_echo, "future": True, "pool_pre_ping": True}
    if settings.is_postgres:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_size * 2,
            pool_recycle=1800,
        )
    else:
        kwargs.update(connect_args={"check_same_thread": False})
    return create_engine(settings.database_url, **kwargs)


engine = _build_engine()

if not settings.is_postgres:

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_connection, _record):  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # 多 worker 线程会并发写同一张 tasks 表，没有 busy_timeout 时
        # 后到的写会直接抛 "database is locked"，表现为任务莫名失败。
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本 / 后台线程用，自动提交与回滚。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  确保模型注册到 metadata

    # 顺序有讲究：
    #   1. create_all 建新表（含其索引/约束）
    #   2. ensure_columns 给老表补列（必须带 DEFAULT，老行才会被填充）
    #   3. backfill 回填业务数据（如给老角色补 code）
    #   4. ensure_indexes 建索引 —— 必须在 backfill 之后，
    #      否则 characters(project_id, code) 唯一索引会撞上「全部 code=''」
    #   5. ensure_nullable / ensure_foreign_keys
    Base.metadata.create_all(bind=engine)
    ensure_columns()
    ensure_nullable()
    backfill()
    ensure_indexes()
    ensure_foreign_keys()


#: 轻量迁移：create_all 不会给已存在的表补列，启动时对齐一次。
#: 只做「加列」，不做删改，保证已有数据安全。
#: ⚠️ JSON 类列的 DDL 必须以 ``JSON`` 开头 —— Postgres 下会被改写成 JSONB。
#: ⚠️ 所有列都带 DEFAULT：SQLite / Postgres 的 ADD COLUMN 都会用默认值填充**已有行**，
#:    否则老行是 NULL，ORM 侧的 ``default=[]`` 只在 INSERT 时生效，读出来会炸。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("workflow_steps", "review_status", "VARCHAR(24) DEFAULT 'NONE'"),
    ("workflow_steps", "reviewed_by", "VARCHAR(80) DEFAULT ''"),
    ("workflow_steps", "reviewed_at", "TIMESTAMPTZ NULL"),
    ("workflow_steps", "review_comment", "TEXT DEFAULT ''"),
    ("workflow_steps", "rolled_back_at", "TIMESTAMPTZ NULL"),
    ("workflow_steps", "rollback_count", "INTEGER DEFAULT 0"),
    # 连续剧分层：Project 归属 Series，并用 episode_no 标识第几集
    ("projects", "series_id", "VARCHAR(40) NULL"),
    ("projects", "episode_no", "INTEGER DEFAULT 0"),
    # 系列级角色（跨集复用）
    ("characters", "series_id", "VARCHAR(40) NULL"),
    # 镜头级配音参数（音色 + 情感指令），供 Web UI 调音台编辑
    ("shots", "voice_speaker", "VARCHAR(60) DEFAULT ''"),
    ("shots", "voice_instruct", "TEXT DEFAULT ''"),
    # --- Phase 3：drama-skills 能力迁移 ------------------------------------- #
    # characters：身份锚点（视觉设定体系）
    ("characters", "bible_id", "VARCHAR(40) NULL"),
    ("characters", "code", "VARCHAR(60) DEFAULT ''"),
    ("characters", "identity_anchors", "JSON DEFAULT '[]'"),
    ("characters", "not_identity", "JSON DEFAULT '[]'"),
    ("characters", "persistent_performance_facts", "JSON DEFAULT '{}'"),
    ("characters", "voice_direction", "JSON DEFAULT '{}'"),
    # shots：地点实体 + 绑定 + 连续性 + 边界/转场/景别（旧列保留作单向兼容投影）
    ("shots", "location_id", "VARCHAR(40) NULL"),
    ("shots", "location_view_id", "VARCHAR(40) NULL"),
    ("shots", "visual_basis", "JSON DEFAULT '{}'"),
    ("shots", "asset_bindings", "JSON DEFAULT '[]'"),
    ("shots", "continuity_lock_ids", "JSON DEFAULT '[]'"),
    ("shots", "continuity_delta_ids", "JSON DEFAULT '[]'"),
    ("shots", "start_boundary", "JSON DEFAULT '{}'"),
    ("shots", "end_boundary", "JSON DEFAULT '{}'"),
    ("shots", "primary_transition", "JSON DEFAULT '{}'"),
    ("shots", "framing", "JSON DEFAULT '{}'"),
    ("shots", "coverage_role", "VARCHAR(30) DEFAULT ''"),
    # assets：血缘 + 参考图角色
    ("assets", "prompt_version_id", "VARCHAR(40) NULL"),
    ("assets", "generation_plan_item_id", "VARCHAR(40) NULL"),
    ("assets", "role", "VARCHAR(40) DEFAULT ''"),
    ("assets", "subject_type", "VARCHAR(20) DEFAULT ''"),
    ("assets", "subject_id", "VARCHAR(40) DEFAULT ''"),
    ("assets", "variant_id", "VARCHAR(40) DEFAULT ''"),
    ("assets", "provenance", "JSON DEFAULT '{}'"),
    # quality_checks：规则分级
    ("quality_checks", "rule_tier", "VARCHAR(40) DEFAULT ''"),
    ("quality_checks", "rule_id", "VARCHAR(40) DEFAULT ''"),
)

#: 需要解除 NOT NULL 的列。
#: - 系列级角色不挂在具体某一集上，characters.project_id 必须可空。
#: - 素材中心里独立生成/保存的素材（如直接合成的语音）不属于任何项目，
#:   assets.project_id 同样必须可空。
_DROP_NOT_NULL: tuple[tuple[str, str], ...] = (
    ("characters", "project_id"),
    ("assets", "project_id"),
)

#: 已存在表需要补齐的外键（表, 约束名, 列, 引用表, 引用列, 级联动作）
_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("projects", "fk_projects_series_id", "series_id", "series", "id", "SET NULL"),
    ("characters", "fk_characters_series_id", "series_id", "series", "id", "CASCADE"),
)

#: 已存在表的新列需要补建的索引（表, 索引名, 列表达式, 是否唯一）。
#: ``create_all`` 只建新表，老表靠 ALTER 补的列不会自动带索引，这里显式补齐。
#: 必须在 backfill 之后执行（见 init_db 注释）。
_ADDED_INDEXES: tuple[tuple[str, str, str, bool], ...] = (
    ("characters", "uq_characters_project_code", "project_id, code", True),
    ("characters", "ix_characters_bible_id", "bible_id", False),
    ("shots", "ix_shots_location_id", "location_id", False),
    ("assets", "ix_assets_prompt_version_id", "prompt_version_id", False),
    ("assets", "ix_assets_role", "role", False),
    ("quality_checks", "ix_quality_checks_rule_tier", "rule_tier", False),
)


def ensure_columns() -> list[str]:
    """对已有表补齐缺失列，返回实际执行的语句（用于启动日志）。"""
    from sqlalchemy import inspect, text

    executed: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, column, ddl in _ADDED_COLUMNS:
        if table not in existing_tables:
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if column in columns:
            continue
        stmt = f"ALTER TABLE {table} ADD COLUMN {column} {_dialect_ddl(ddl)}"
        if settings.is_postgres:
            stmt = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {_dialect_ddl(ddl)}"
        with engine.begin() as conn:
            conn.execute(text(stmt))
        executed.append(f"{table}.{column}")
    return executed


def _dialect_ddl(ddl: str) -> str:
    """把跨方言的 ``JSON`` 前缀改写为 Postgres 的 ``JSONB``。"""
    if settings.is_postgres and ddl.startswith("JSON"):
        return "JSONB" + ddl[len("JSON"):]
    return ddl


def ensure_indexes() -> list[str]:
    """为老表补建索引。单条失败只跳过并记录，不阻断启动。"""
    from sqlalchemy import inspect, text

    executed: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, name, columns, unique in _ADDED_INDEXES:
        if table not in existing_tables:
            continue
        try:
            existing = {ix["name"] for ix in inspector.get_indexes(table)}
            if name in existing:
                continue
            kind = "UNIQUE INDEX" if unique else "INDEX"
            stmt = f"CREATE {kind} IF NOT EXISTS {name} ON {table} ({columns})"
            with engine.begin() as conn:
                conn.execute(text(stmt))
            executed.append(f"{name}({table}.{columns})")
        except Exception as exc:  # pragma: no cover - 迁移容错
            executed.append(f"{name} SKIPPED: {exc}")
    return executed


def backfill() -> dict[str, int]:
    """幂等数据回填（可重复执行）。

    - 为每个项目建一条空 ``visual_bibles``（若不存在）
    - 给老 ``characters`` 补稳定 ``code``（新字段为真相，旧行不能留空）
    - 给已有 ``Shot.image_prompt`` 建 ``prompts`` + v1（``compiled_from`` 标 legacy_backfill）

    ⚠️ 历史 ``assets.prompt_version_id`` **不回填** —— 老 Asset 的 Prompt 已丢失，
    NULL 即代表「迁移前产物、血缘不可考」。
    """
    return {
        "visual_bibles": _backfill_visual_bibles(),
        "character_codes": _backfill_character_codes(),
        "legacy_prompts": _backfill_legacy_prompts(),
    }


def _backfill_visual_bibles() -> int:
    from sqlalchemy import select

    from . import models as m

    created = 0
    with session_scope() as db:
        rows = (
            db.execute(
                select(m.Project.id).outerjoin(
                    m.VisualBible, m.VisualBible.project_id == m.Project.id
                ).where(m.VisualBible.id.is_(None))
            )
            .scalars()
            .all()
        )
        for project_id in rows:
            db.add(
                m.VisualBible(
                    id=m.new_id("vb"), project_id=project_id, title="", status="DRAFT", version=1
                )
            )
            created += 1
    return created


def _code_suffix(name: str) -> str:
    """由名称生成稳定代号后缀：ASCII 直接取，其它用确定性摘要（不用随机数）。"""
    import hashlib
    import re

    cleaned = re.sub(r"[^0-9A-Za-z]+", "", name or "")
    if cleaned:
        return cleaned[:24].upper()
    return hashlib.md5((name or "").encode("utf-8")).hexdigest()[:6].upper()


def _backfill_character_codes() -> int:
    from sqlalchemy import or_, select

    from . import models as m

    filled = 0
    with session_scope() as db:
        chars = (
            db.execute(
                select(m.Character).where(
                    or_(m.Character.code == "", m.Character.code.is_(None))
                )
            )
            .scalars()
            .all()
        )
        if not chars:
            return 0
        # 按 project_id 分组去重（与 uq_characters_project_code 的口径一致）
        used: dict[str, set[str]] = {}
        for pid, code in db.execute(
            select(m.Character.project_id, m.Character.code).where(m.Character.code != "")
        ).all():
            used.setdefault(pid or "", set()).add(code)

        for ch in chars:
            scope = ch.project_id or ""
            bucket = used.setdefault(scope, set())
            base, n = _code_suffix(ch.name), 2
            candidate = base
            while candidate in bucket:
                candidate = f"{base}-{n}"
                n += 1
            bucket.add(candidate)
            ch.code = f"CHAR-{candidate}"
            filled += 1
    return filled


def _backfill_legacy_prompts() -> int:
    """给已有 ``Shot.image_prompt`` 建 ``prompts`` + v1。

    ⚠️ ``Shot.code`` 在项目内**并不唯一**（唯一约束是 ``uq(scene_id, sequence)``，
    同一项目里多个镜头可能都叫 ``Shot 001``）→ 代号必须做项目内确定性去重，
    否则会撞 ``uq(prompt_project_code)``。
    """
    from sqlalchemy import select

    from . import models as m

    created = 0
    with session_scope() as db:
        shots = db.execute(select(m.Shot).where(m.Shot.image_prompt != "")).scalars().all()
        if not shots:
            return 0
        # 已有 Prompt 的镜头直接跳过 —— 这是幂等的关键守卫
        done_shots = {
            sid
            for (sid,) in db.execute(
                select(m.Prompt.shot_id).where(
                    m.Prompt.shot_id.is_not(None), m.Prompt.type == "image"
                )
            ).all()
        }
        # 预载各项目已存在的 Prompt code，避免逐条查库
        used: dict[str, set[str]] = {}
        for pid, code in db.execute(select(m.Prompt.project_id, m.Prompt.code)).all():
            used.setdefault(pid, set()).add(code)

        for shot in shots:
            if shot.id in done_shots:
                continue
            base = f"IMG-{shot.code or f'{shot.sequence:03d}'}"
            bucket = used.setdefault(shot.project_id, set())
            code, n = base, 2
            while code in bucket:
                code = f"{base}-{n}"
                n += 1
            bucket.add(code)

            prompt = m.Prompt(
                id=m.new_id("prm"),
                project_id=shot.project_id,
                shot_id=shot.id,
                type="image",
                code=code,
                name=f"镜头 {shot.code or shot.sequence} 关键帧",
                latest_version=1,
            )
            version = m.PromptVersion(
                id=m.new_id("pv"),
                prompt_id=prompt.id,
                version=1,
                raw_prompt=shot.image_prompt or "",
                compiled_prompt=shot.image_prompt or "",
                negative_prompt=shot.negative_prompt or "",
                status="READY",
                recipe={"renderer": "legacy", "version": "0"},
                compiled_from={"legacy_backfill": True, "shot": {"id": shot.id}},
                width=shot.project.width if shot.project else 0,
                height=shot.project.height if shot.project else 0,
            )
            prompt.current_version_id = version.id
            db.add_all([prompt, version])
            created += 1
    return created


def ensure_nullable() -> list[str]:
    """解除历史遗留的 NOT NULL 约束（只针对确有必要放开可空的列）。"""
    from sqlalchemy import inspect, text

    executed: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, column in _DROP_NOT_NULL:
        if table not in existing_tables:
            continue
        info = {c["name"]: c for c in inspector.get_columns(table)}.get(column)
        if info is None or info.get("nullable", True):
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL"))
        executed.append(f"{table}.{column} DROP NOT NULL")
    return executed


def ensure_foreign_keys() -> list[str]:
    """为已存在的表补外键约束。

    ``create_all`` 只在建新表时带约束；老库补了列却没有外键，级联行为会不一致，
    因此这里显式补齐。SQLite 无法用 DDL 动态加约束，跳过（其表由 create_all 建）。
    """
    if not settings.is_postgres:
        return []
    from sqlalchemy import inspect, text

    executed: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, name, column, ref_table, ref_column, on_delete in _FOREIGN_KEYS:
        if table not in existing_tables or ref_table not in existing_tables:
            continue
        with engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}
            ).scalar()
            if exists:
                continue
            conn.execute(text(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                f"FOREIGN KEY ({column}) REFERENCES {ref_table}({ref_column}) "
                f"ON DELETE {on_delete}"
            ))
        executed.append(f"{table}.{column} → {ref_table}.{ref_column}")
    return executed
