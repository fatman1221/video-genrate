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

    Base.metadata.create_all(bind=engine)
    ensure_columns()
    ensure_nullable()
    ensure_foreign_keys()


#: 轻量迁移：create_all 不会给已存在的表补列，启动时对齐一次。
#: 只做「加列」，不做删改，保证已有数据安全。
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
)

#: 需要解除 NOT NULL 的列：系列级角色不挂在具体某一集上，project_id 必须可空。
_DROP_NOT_NULL: tuple[tuple[str, str], ...] = (
    ("characters", "project_id"),
)

#: 已存在表需要补齐的外键（表, 约束名, 列, 引用表, 引用列, 级联动作）
_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("projects", "fk_projects_series_id", "series_id", "series", "id", "SET NULL"),
    ("characters", "fk_characters_series_id", "series_id", "series", "id", "CASCADE"),
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
        stmt = f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
        if settings.is_postgres:
            stmt = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"
        with engine.begin() as conn:
            conn.execute(text(stmt))
        executed.append(f"{table}.{column}")
    return executed


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
