"""ORM 数据模型。

设计要点（对应用户需求）：
- Shot 是一等核心实体，Agent 可以只重生某个 Shot
- Asset 记录完整生成元数据（prompt / model / provider / workflow / parameters）
- Task 支持异步、重试、取消、进度、错误信息
- Workflow / WorkflowStep 表达可跳转的状态机
- AgentLog 记录 Agent 执行过程
- 二进制文件一律不进数据库，只存 file_path / url
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .core.constants import (
    AssetStatus, AssetType, ProjectStatus, SceneStatus, ShotStatus, TaskStatus,
    WorkflowState,
)
from .database import Base, JSONType


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------- #
# Series（连续剧 / 剧集系列）
# --------------------------------------------------------------------------- #
class Series(Base, TimestampMixin):
    """一部连续剧（系列）。

    分层设计：**Series（整部剧）→ Project（每一集）→ 现有流水线**。
    每一集就是一个普通 Project，因此天然复用整套能力：独立工作流、独立节点
    回退 / 审核 / 重生成、独立成片。某集失败不会污染其它集。

    系列级角色库（``Character.series_id``）用于保证主角形象跨集一致。
    """
    __tablename__ = "series"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ser"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    requirement: Mapped[str] = mapped_column(Text, default="")   # 整部剧的总需求
    style: Mapped[str] = mapped_column(String(120), default="漫画教学风格")
    language: Mapped[str] = mapped_column(String(20), default="zh-CN")
    status: Mapped[str] = mapped_column(String(32), default=ProjectStatus.DRAFT, index=True)
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="16:9")
    width: Mapped[int] = mapped_column(Integer, default=1280)
    height: Mapped[int] = mapped_column(Integer, default=720)
    fps: Mapped[int] = mapped_column(Integer, default=24)
    #: 每一集的目标时长（秒）——新建集时作为默认值
    episode_duration: Mapped[float] = mapped_column(Float, default=300.0)
    #: 计划集数，0 表示未规划（边做边加）
    planned_episodes: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str] = mapped_column(String(120), default="workbuddy")
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    episodes: Mapped[list["Project"]] = relationship(
        back_populates="series", order_by="Project.episode_no"
    )
    characters: Mapped[list["Character"]] = relationship(back_populates="series")


# --------------------------------------------------------------------------- #
# Project（一部连续剧中的「一集」，或独立成片）
# --------------------------------------------------------------------------- #
class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("proj"))
    #: 所属连续剧；为空表示这是一个独立项目（非分集）
    series_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL"), index=True, nullable=True
    )
    #: 集号，从 1 开始；0 表示独立项目
    episode_no: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    requirement: Mapped[str] = mapped_column(Text, default="")  # 用户原始需求
    style: Mapped[str] = mapped_column(String(120), default="comic")  # 漫画教学风格等
    language: Mapped[str] = mapped_column(String(20), default="zh-CN")
    status: Mapped[str] = mapped_column(String(32), default=ProjectStatus.DRAFT, index=True)
    workflow_state: Mapped[str] = mapped_column(
        String(40), default=WorkflowState.PROJECT_CREATED, index=True
    )
    target_duration: Mapped[float] = mapped_column(Float, default=300.0)  # 秒
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="16:9")
    width: Mapped[int] = mapped_column(Integer, default=1280)
    height: Mapped[int] = mapped_column(Integer, default=720)
    fps: Mapped[int] = mapped_column(Integer, default=24)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str] = mapped_column(String(120), default="workbuddy")
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    series: Mapped[Optional["Series"]] = relationship(back_populates="episodes")
    scripts: Mapped[list["Script"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    storyboards: Mapped[list["Storyboard"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    scenes: Mapped[list["Scene"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    shots: Mapped[list["Shot"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    characters: Mapped[list["Character"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    assets: Mapped[list["Asset"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="project", cascade="all, delete-orphan")


# --------------------------------------------------------------------------- #
# Script
# --------------------------------------------------------------------------- #
class Script(Base, TimestampMixin):
    __tablename__ = "scripts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("scr"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    outline: Mapped[str] = mapped_column(Text, default="")
    style: Mapped[str] = mapped_column(String(120), default="")
    language: Mapped[str] = mapped_column(String(20), default="zh-CN")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    provider: Mapped[str] = mapped_column(String(60), default="agent")
    model: Mapped[str] = mapped_column(String(120), default="")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    project: Mapped[Project] = relationship(back_populates="scripts")


# --------------------------------------------------------------------------- #
# Storyboard / Scene / Shot
# --------------------------------------------------------------------------- #
class Storyboard(Base, TimestampMixin):
    __tablename__ = "storyboards"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sb"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    synopsis: Mapped[str] = mapped_column(Text, default="")
    visual_style: Mapped[str] = mapped_column(String(200), default="")
    scene_count: Mapped[int] = mapped_column(Integer, default=0)
    shot_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    provider: Mapped[str] = mapped_column(String(60), default="agent")
    model: Mapped[str] = mapped_column(String(120), default="")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    project: Mapped[Project] = relationship(back_populates="storyboards")
    scenes: Mapped[list["Scene"]] = relationship(
        back_populates="storyboard", cascade="all, delete-orphan", order_by="Scene.sequence"
    )


class Scene(Base, TimestampMixin):
    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("scn"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    storyboard_id: Mapped[str] = mapped_column(ForeignKey("storyboards.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    code: Mapped[str] = mapped_column(String(40), default="")          # Scene 01
    title: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    mood: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(32), default=SceneStatus.PENDING)

    project: Mapped[Project] = relationship(back_populates="scenes")
    storyboard: Mapped[Storyboard] = relationship(back_populates="scenes")
    shots: Mapped[list["Shot"]] = relationship(
        back_populates="scene", cascade="all, delete-orphan", order_by="Shot.sequence"
    )


class Shot(Base, TimestampMixin):
    """核心实体：单个镜头。失败重试的最小粒度。"""

    __tablename__ = "shots"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("shot"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    code: Mapped[str] = mapped_column(String(40), default="")          # Shot 001
    duration: Mapped[float] = mapped_column(Float, default=5.0)
    description: Mapped[str] = mapped_column(Text, default="")
    camera: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    visual_style: Mapped[str] = mapped_column(String(200), default="")
    character_ids: Mapped[list[str]] = mapped_column(JSONType, default=list)
    image_prompt: Mapped[str] = mapped_column(Text, default="")
    video_prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    voice_script: Mapped[str] = mapped_column(Text, default="")
    subtitle_text: Mapped[str] = mapped_column(Text, default="")
    #: 配音音色（Qwen3-TTS 的 speaker 名，如 sohee / vivian / uncle_fu）。
    #: 留空时由 provider 的默认音色兜底。
    voice_speaker: Mapped[str] = mapped_column(String(60), default="")
    #: 情感/语气指令（自然语言，如「像跟朋友聊天一样娓娓道来」）。
    #: Qwen3-TTS CustomVoice 用它控制情绪，是「配音有感情」的关键。
    voice_instruct: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(String(32), default=ShotStatus.PENDING, index=True)
    image_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    video_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    voice_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    subtitle_status: Mapped[str] = mapped_column(String(32), default="PENDING")

    image_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    video_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    voice_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    subtitle_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    enhanced_video_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    project: Mapped[Project] = relationship(back_populates="shots")
    scene: Mapped[Scene] = relationship(back_populates="shots")
    # 产物通过 *_asset_id 弱引用 Asset（不建 FK 约束，避免与 Asset 形成循环依赖）；
    # 需要完整元数据时用 db.get(Asset, shot.image_asset_id) 获取。

    __table_args__ = (UniqueConstraint("scene_id", "sequence", name="uq_shot_scene_seq"),)


# --------------------------------------------------------------------------- #
# Character
# --------------------------------------------------------------------------- #
class Character(Base, TimestampMixin):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("chr"))
    #: 所属项目（集）。系列级角色不挂在具体某集上，此列为空。
    project_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    #: 所属连续剧。非空表示这是系列级角色，系列下所有集都可以引用，
    #: 从而保证主角形象 / 音色跨集一致（连续剧的核心诉求）。
    series_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(120), default="supporting")   # protagonist / supporting
    description: Mapped[str] = mapped_column(Text, default="")
    appearance: Mapped[str] = mapped_column(Text, default="")
    personality: Mapped[str] = mapped_column(Text, default="")
    voice_style: Mapped[str] = mapped_column(String(200), default="")
    reference_prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    reference_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    provider: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    project: Mapped[Optional[Project]] = relationship(back_populates="characters")
    series: Mapped[Optional["Series"]] = relationship(back_populates="characters")


# --------------------------------------------------------------------------- #
# Asset Center
# --------------------------------------------------------------------------- #
class Asset(Base, TimestampMixin):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ast"))
    #: 可为空 —— 素材中心里独立生成/保存的素材（如直接合成的语音）不挂在任何项目下。
    #: 允许为空的语义与 characters.series_id 一致：先在建表/迁移层放开，业务层再决定归属。
    project_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scene_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    shot_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    character_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)

    type: Mapped[str] = mapped_column(String(32), default=AssetType.IMAGE, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    file_path: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    storage_backend: Mapped[str] = mapped_column(String(32), default="local")
    status: Mapped[str] = mapped_column(String(32), default=AssetStatus.READY, index=True)

    prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(160), default="")
    provider: Mapped[str] = mapped_column(String(60), default="")
    workflow: Mapped[str] = mapped_column(String(160), default="")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    fps: Mapped[float] = mapped_column(Float, default=0.0)
    format: Mapped[str] = mapped_column(String(20), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(80), default="")

    source: Mapped[str] = mapped_column(String(60), default="generated")  # generated / uploaded / external
    parent_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    project: Mapped[Optional[Project]] = relationship(back_populates="assets")

    __table_args__ = (Index("ix_assets_project_type", "project_id", "type"),)


# --------------------------------------------------------------------------- #
# Task / Workflow / Logs / QC
# --------------------------------------------------------------------------- #
class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("task"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    character_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    parent_task_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    type: Mapped[str] = mapped_column(String(48), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=5)

    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    provider: Mapped[str] = mapped_column(String(60), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    logs: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    worker: Mapped[str] = mapped_column(String(80), default="")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(60), default="agent")

    project: Mapped[Project] = relationship(back_populates="tasks")

    def append_log(self, message: str, level: str = "INFO") -> None:
        entries = list(self.logs or [])
        entries.append({"ts": utcnow().isoformat(), "level": level, "message": message})
        self.logs = entries[-200:]


class Workflow(Base, TimestampMixin):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("wf"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="default_pipeline")
    state: Mapped[str] = mapped_column(String(40), default=WorkflowState.PROJECT_CREATED)
    previous_state: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    context: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)

    steps: Mapped[list["WorkflowStep"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", order_by="WorkflowStep.order_index"
    )


class WorkflowStep(Base, TimestampMixin):
    """工作流中的一个节点。

    state 表示生产状态（PENDING/RUNNING/SUCCESS/FAILED/SKIPPED），
    review_status 表示人工/Agent 审核结论（NONE/PENDING/APPROVED/REJECTED），
    二者分离：一个节点可以已生产完成但尚未审核，也可以被审核驳回后重新生产。
    """

    __tablename__ = "workflow_steps"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("wfs"))
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # --- 审核 ---
    review_status: Mapped[str] = mapped_column(String(24), default="NONE")
    reviewed_by: Mapped[str] = mapped_column(String(80), default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")
    # --- 回退审计 ---
    rolled_back_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow: Mapped[Workflow] = relationship(back_populates="steps")

    __table_args__ = (UniqueConstraint("workflow_id", "step_key", name="uq_wf_step"),)


class AgentLog(Base, TimestampMixin):
    __tablename__ = "agent_logs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("log"))
    project_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    task_id: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    shot_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    actor: Mapped[str] = mapped_column(String(80), default="agent")
    event: Mapped[str] = mapped_column(String(80), default="")
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    message: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class QualityCheck(Base, TimestampMixin):
    __tablename__ = "quality_checks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("qc"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(40), default="project")  # project / shot / asset
    target_id: Mapped[str] = mapped_column(String(40), default="")
    check_key: Mapped[str] = mapped_column(String(60), default="")
    name: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(16), default="PASS")           # PASS / FAIL / WARN
    message: Mapped[str] = mapped_column(Text, default="")
    metric: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    run_id: Mapped[str] = mapped_column(String(40), default="")


class ProviderRecord(Base, TimestampMixin):
    """Provider 注册表（可运行时启停 / 设默认）。"""

    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)   # image / video / tts / music / sfx / subtitle / enhance / processing / browser
    name: Mapped[str] = mapped_column(String(60))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_api_key: Mapped[bool] = mapped_column(Boolean, default=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONType, default=list)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class BrowserTask(Base, TimestampMixin):
    """浏览器自动化任务（不在 Backend 写死流程，由 Agent 驱动）。"""

    __tablename__ = "browser_tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("bt"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    target_platform: Mapped[str] = mapped_column(String(120), default="")
    instruction: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    executor: Mapped[str] = mapped_column(String(60), default="agent")
    assigned_to: Mapped[str] = mapped_column(String(80), default="")
