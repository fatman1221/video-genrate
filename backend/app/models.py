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
    AssetStatus, AssetType, ProjectStatus, SceneStatus, ScriptSectionStatus, ShotStatus,
    TaskStatus, WorkflowState,
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
    script_sections: Mapped[list["ScriptSection"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
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


class ScriptSection(Base, TimestampMixin):
    """脚本分段（一幕 / 一章）。

    为什么单独建表：一集 5 分钟片的脚本上千字，一次性生成质量难控、改一处要整篇重来。
    按幕切开后既可以「只写这一幕」，续写时又能把前面已定稿的正文带上，上下文自然连贯。

    ``content`` 为空表示这一幕只有要点、正文待写（status=DRAFT）；写好后转 READY。
    ``summary`` / ``beat`` 是给生成用的意图说明，用户可直接改，改它们不必动正文。
    """
    __tablename__ = "script_sections"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sec"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    #: 归属脚本；允许为空 —— 可以先分段后建脚本，也可以只分段不建脚本
    script_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scripts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: 幕序，从 1 开始，决定阅读、生成与拼接顺序
    sequence: Mapped[int] = mapped_column(Integer, default=1, index=True)
    code: Mapped[str] = mapped_column(String(40), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    #: 这一幕要讲什么（剧情推进 / 信息点），生成正文的主要依据
    summary: Mapped[str] = mapped_column(Text, default="")
    #: 情绪节拍 / 关键转折，控制表演与镜头调性
    beat: Mapped[str] = mapped_column(Text, default="")
    #: 正文（旁白 / 对白）
    content: Mapped[str] = mapped_column(Text, default="")
    #: 这一幕的目标时长（秒）；0 = 未指定，按全片时长均分
    target_duration: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(
        String(32), default=ScriptSectionStatus.DRAFT, index=True
    )
    provider: Mapped[str] = mapped_column(String(60), default="agent")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    project: Mapped[Project] = relationship(back_populates="script_sections")

    __table_args__ = (Index("ix_script_sections_project_seq", "project_id", "sequence"),)


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

    # --- 视觉设定体系 / 分镜语法（Phase 3 迁移新增）-------------------------- #
    #: ⭐ 地点实体（替代字符串 `location`）；旧字符串列保留作单向兼容投影
    location_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    location_view_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("location_views.id", ondelete="SET NULL"), nullable=True
    )
    #: 视觉依据（引用 VB 条目 id + 结论）
    visual_basis: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: 绑定 [{kind, id, variant_id, role}] —— 取代 character_ids 成为真相
    asset_bindings: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    continuity_lock_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    continuity_delta_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    #: 起始 / 结束边界六分量（服化道 / 位置 / 姿态 / 情绪 / 光 / 向）
    start_boundary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    end_boundary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    primary_transition: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: {size, angle, camera_height, aspect_notes}
    framing: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: primary / intentional_repeat
    coverage_role: Mapped[str] = mapped_column(String(30), default="")

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

    # --- 视觉设定体系（Phase 3 迁移新增）------------------------------------- #
    #: 归属视觉设定总纲
    bible_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("visual_bibles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: 稳定代号（如 CHAR-LINYE），uq(project_id, code)
    code: Mapped[str] = mapped_column(String(60), default="")
    #: 持久识别锚点 —— 换一个就不再是同一个人
    identity_anchors: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    #: 明确不算身份的（单场雨水、瞬时表情…）
    not_identity: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    #: 持续表演事实
    persistent_performance_facts: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: 声音方向 {reference, criteria[], distinctness, pronunciation[], not_identity[]}
    voice_direction: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

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

    # --- 血缘与参考图（Phase 3 迁移新增）------------------------------------- #
    #: ⭐「这张图是哪个 Prompt 版本生成的」
    prompt_version_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    #: 出自哪个计划项（Preview/Confirm 链路的落地凭证）
    generation_plan_item_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    #: reference / reference_candidate / keyframe / final
    role: Mapped[str] = mapped_column(String(40), default="", index=True)
    #: 服务的实体类型 character / location / prop / shot
    subject_type: Mapped[str] = mapped_column(String(20), default="")
    subject_id: Mapped[str] = mapped_column(String(40), default="")
    #: 对应的 look / view / state
    variant_id: Mapped[str] = mapped_column(String(40), default="")
    #: 血缘快照（冗余但查询友好）
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

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

    # --- 规则分级（Phase 3 迁移新增）----------------------------------------- #
    #: structural_invariant / reviewed_invariant / craft_default / taste_option
    rule_tier: Mapped[str] = mapped_column(String(40), default="", index=True)
    #: 规则编号，如 CON-07
    rule_id: Mapped[str] = mapped_column(String(40), default="")


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


# =========================================================================== #
# 视觉设定体系（Visual Bible）—— 借鉴 drama-skills 的「身份 / 变体分离」
#
# 三对对称结构：
#   Character      → CharacterLook        （人物身份 → 造型变体）
#   Location       → LocationView         （地点身份 → 观看变体）
#   Prop           → PropState            （道具身份 → 状态变体）
# 判据：**身份**换一个就不再是同一个人/地/物；**变体**身份不变，但服装、伤势、
# 时段、天气、开合或持有状态改变。
#
# 约定（与现有实体一致）：
# - 全部挂 project_id，便于级联删除
# - 「指向运行时实体（prompt_versions / assets）」一律弱引用（String，不加 FK），
#   避免与 Asset/Prompt 形成循环依赖 —— 与 Shot.*_asset_id 的现有风格一致
# =========================================================================== #
class VisualBible(Base, TimestampMixin):
    """一个项目的视觉设定总纲（一项目一册）。

    `current_style_id` 是弱引用（不加 FK）：visual_styles.bible_id 指向本表，
    若此处再指回去会形成建表循环依赖。
    """

    __tablename__ = "visual_bibles"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("vb"))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, unique=True
    )
    series_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="")
    #: 视觉一句话（logline）
    visual_logline: Mapped[str] = mapped_column(Text, default="")
    #: 当前生效风格 —— 弱引用 visual_styles.id
    current_style_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    #: 时代锚点（跨全场不得漂移的形制、器物、字体…）
    era_anchors: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: 全局视觉规则 {lighting, palette, camera_language, composition, negative}
    global_rules: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: 全局文字政策 {readable_text_allowed, rules[]}
    text_policy: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class VisualStyle(Base, TimestampMixin):
    """视觉风格 / 形态。不是「风格名前缀」，而是规定各字段怎么写。"""

    __tablename__ = "visual_styles"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sty"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    bible_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("visual_bibles.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="")
    #: live_action / guoman_2d / dynamic_comic / chibi / stylized_3d / ink_wash
    form_card: Mapped[str] = mapped_column(String(40), default="", index=True)
    narrative_duty: Mapped[str] = mapped_column(Text, default="")
    identity_carrier: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    continuity_carriers: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    layer_split: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    rendering: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    lighting: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    palette: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    camera_language: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    composition_rules: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    motion_budget: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    negative_rules: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)


class CharacterLook(Base, TimestampMixin):
    """角色造型变体（换装 / 伤势 / 状态），身份不变。"""

    __tablename__ = "character_looks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("look"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    character_id: Mapped[str] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(60), default="")          # LOOK-LINYE-DEFAULT
    name: Mapped[str] = mapped_column(String(200), default="")
    #: 变体的基底造型（自引用）
    base_look_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("character_looks.id", ondelete="SET NULL"), nullable=True
    )
    #: {wardrobe_layers[], hair_styling[], makeup[], injury[], weathering[]}
    differences: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    cause_shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), nullable=True
    )
    valid_from: Mapped[str] = mapped_column(String(80), default="")
    valid_until: Mapped[str] = mapped_column(String(80), default="")
    reference_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")

    __table_args__ = (UniqueConstraint("character_id", "code", name="uq_look_character_code"),)


class Location(Base, TimestampMixin):
    """地点身份（换一个就不再是同一个地方）。"""

    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("loc"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    series_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bible_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("visual_bibles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    code: Mapped[str] = mapped_column(String(60), default="")          # LOC-FERRY-OFFICE
    name: Mapped[str] = mapped_column(String(200), default="")
    display_name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    #: {shape, zones[], entrances[{id, connects_to}], fixed_anchors[], materials[]}
    spatial_identity: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    era_form: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    not_identity: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    reference_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_location_project_code"),)


class LocationView(Base, TimestampMixin):
    """观看变体（同一地点的不同机位 / 时段 / 天气 / 陈设）。"""

    __tablename__ = "location_views"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("view"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    location_id: Mapped[str] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(60), default="")          # VIEW-...-NORTH-NIGHT
    name: Mapped[str] = mapped_column(String(200), default="")
    base_view_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("location_views.id", ondelete="SET NULL"), nullable=True
    )
    #: {from_zone, toward, visible_fixed_anchors[]}
    orientation: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: {dressing[], time, weather, light[]}
    state_differences: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    cause_shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), nullable=True
    )
    valid_from: Mapped[str] = mapped_column(String(80), default="")
    valid_until: Mapped[str] = mapped_column(String(80), default="")
    reference_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("location_id", "code", name="uq_view_location_code"),)


class Prop(Base, TimestampMixin):
    """道具身份（换一个就不再是同一件东西）。"""

    __tablename__ = "props"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("prop"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    series_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bible_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("visual_bibles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    code: Mapped[str] = mapped_column(String(60), default="")          # PROP-TIN-CASE
    name: Mapped[str] = mapped_column(String(200), default="")
    display_name: Mapped[str] = mapped_column(String(200), default="")
    #: {scale_and_form, materials[], function, permanent_marks[]}
    identity_anchors: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: {mode: exact_readable|graphic_only|no_readable_text|pending_creator_text, text, placement}
    text_policy: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    not_identity: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    reference_asset_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_prop_project_code"),)


class PropState(Base, TimestampMixin):
    """道具状态变体（开合 / 破损 / 持有者 / 内容物）。"""

    __tablename__ = "prop_states"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pst"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    prop_id: Mapped[str] = mapped_column(ForeignKey("props.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(60), default="")          # PSTATE-TIN-OPEN-EMPTY
    name: Mapped[str] = mapped_column(String(200), default="")
    base_state_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("prop_states.id", ondelete="SET NULL"), nullable=True
    )
    #: {open, damage, powered} 或 {summary}
    condition: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: {owner_id, holder_id, hand, location_id}
    custody: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: 内容物 prop id 列表
    contents: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    text_visibility: Mapped[str] = mapped_column(String(60), default="")
    cause_shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), nullable=True
    )
    valid_from: Mapped[str] = mapped_column(String(80), default="")
    valid_until: Mapped[str] = mapped_column(String(80), default="")
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("prop_id", "code", name="uq_pstate_prop_code"),)


# --------------------------------------------------------------------------- #
# 连续性锁 / 连续性增量 —— 两个**不同**的机制，分表建，不要合并
#
#   LOCK-  回答「什么永远不变」：缩成最小名词短语，逐字强制出现在所有 in-scope 提示词里
#   DELTA- 回答「什么变了、从什么变成什么、为什么」：before/after/cause/有效期/影响范围
# --------------------------------------------------------------------------- #
class ContinuityLock(Base, TimestampMixin):
    """连续性锁。

    `surface` 是灵魂：**必须逐字出现在所有 in-scope 的 Prompt 正文里**，
    校验规则 —— 大小写不敏感、换行按空格、整词匹配（不能粘在别的词上）、
    排除负面提示词里的命中。
    语法：「颜色 + 材质/形制 + 物体」的最小名词短语，不含标点/动作/状态/数量/剧情。
    """

    __tablename__ = "continuity_locks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("lock"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    bible_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("visual_bibles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    code: Mapped[str] = mapped_column(String(80), default="")          # LOCK-KNIT
    name: Mapped[str] = mapped_column(String(200), default="")         # 中文名
    surface: Mapped[str] = mapped_column(Text, default="")             # ⭐ 锁面
    prompt_language: Mapped[str] = mapped_column(String(10), default="en")
    subject_type: Mapped[str] = mapped_column(String(20), default="", index=True)  # character/location/prop/style
    #: ⚠️ 弱引用（指向具体实体：characters/locations/props 的 id）
    subject_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    #: 可空，指向 look/view/state
    variant_id: Mapped[str] = mapped_column(String(40), default="")
    #: ["all"] 或 [shot_id, ...]
    shot_scope: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    #: 图片提示词条目 code 列表
    prompt_scope: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_lock_project_code"),)


class ContinuityDelta(Base, TimestampMixin):
    """连续性增量（剧情导致的状态变化）。

    纪律：「未知」不等于「恢复默认」—— 上集带伤、本集没提，不能自动恢复为无伤，
    保留最后确认状态并建 `unresolved`。
    """

    __tablename__ = "continuity_deltas"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("delta"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(80), default="")          # DELTA-TIN-OPEN
    scene_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(20), default="")
    subject_id: Mapped[str] = mapped_column(String(40), default="")
    state_field: Mapped[str] = mapped_column(String(80), default="")   # condition.open / custody
    before: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    cause_shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), nullable=True
    )
    effective_from: Mapped[str] = mapped_column(String(80), default="")
    effective_until: Mapped[str] = mapped_column(String(80), default="")
    #: CON-01 边界核对（下一个关联镜头）
    next_linked_shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), nullable=True
    )
    reconciliation_status: Mapped[str] = mapped_column(String(30), default="must_match_or_revise")
    affected_refs: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")


# --------------------------------------------------------------------------- #
# Prompt 与版本 —— 「改设定不覆盖旧 Prompt，能反查这张图是哪个版本生成的」
# --------------------------------------------------------------------------- #
class Prompt(Base, TimestampMixin):
    """Prompt 逻辑单元（一个镜头一类产物一条）。

    `current_version_id` 是弱引用（不加 FK）：prompt_versions.prompt_id 指向本表。
    """

    __tablename__ = "prompts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("prm"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(20), default="image", index=True)  # image/video/voice/music
    code: Mapped[str] = mapped_column(String(80), default="")          # IMG-... / MOTION-...
    name: Mapped[str] = mapped_column(String(200), default="")
    #: 弱引用 prompt_versions.id —— 指向当前生效版本
    current_version_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_prompt_project_code"),)


class PromptVersion(Base, TimestampMixin):
    """Prompt 版本 —— **只 INSERT，永不 UPDATE 覆盖**。

    改动只产生新版本；`compiled_from` 记录编译输入快照（实体 id + version，**不用哈希**）。
    STALE 由 service 层动态判定，不写死在此表。
    """

    __tablename__ = "prompt_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pv"))
    prompt_id: Mapped[str] = mapped_column(ForeignKey("prompts.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    raw_prompt: Mapped[str] = mapped_column(Text, default="")
    compiled_prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    #: ⭐ 编译输入快照（实体 id + version，不是哈希）
    #: {bible:{id,version}, style:{...}, subjects:[{kind,id,version}],
    #:  locks:[{id,version,surface}], shot:{id,updated_at}, bindings:[...]}
    compiled_from: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: {renderer:'video-genrate-8seg', version:'1.0.0'}
    recipe: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    model: Mapped[str] = mapped_column(String(100), default="")
    provider: Mapped[str] = mapped_column(String(40), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="")
    resolution: Mapped[str] = mapped_column(String(20), default="")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: 参考图槽位绑定
    #: [{slot, order, asset_id|plan_locator, kind:REF|PLAN|IMG, role, label,
    #:   may_control[], must_not_control[], admission_status}]
    reference_assets: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    continuity_lock_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    stale_reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(40), default="agent")

    __table_args__ = (UniqueConstraint("prompt_id", "version", name="uq_pv_prompt_version"),)


# --------------------------------------------------------------------------- #
# 生成计划 —— Preview → Confirm → Produce（花钱前先落计划、看预览、显式确认）
# --------------------------------------------------------------------------- #
class GenerationPlan(Base, TimestampMixin):
    """一次批量生产的计划。DRAFT → PREVIEWED → CONFIRMED → RUNNING → DONE。

    `fingerprint` 对 (items + parameters + outputs) 做 canonical JSON + sha256；
    确认时记录，物化时校验；任一变化 → 旧确认失效。
    """

    __tablename__ = "generation_plans"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("plan"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    #: batch_image / batch_video / batch_voice / mixed
    plan_type: Mapped[str] = mapped_column(String(30), default="batch_image")
    #: 选中范围（shot codes / stage / 筛选条件）
    source_scope: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: ⭐ 预览汇总 {item_count, by_modality{}, by_provider{}, resolutions{},
    #:            est_seconds, est_cost_note, warnings[]}
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[str] = mapped_column(String(80), default="")
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 确认后创建的任务 id 列表
    task_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)


class GenerationPlanItem(Base, TimestampMixin):
    """计划项 —— 一个镜头一次产物的计划（PLAN 态：还没有 Asset 行）。"""

    __tablename__ = "generation_plan_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pli"))
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("generation_plans.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    prompt_version_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    modality: Mapped[str] = mapped_column(String(20), default="image")
    provider: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="")
    resolution: Mapped[str] = mapped_column(String(20), default="")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    reference_assets: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    predicted_cost: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: PENDING / TASK_CREATED / SKIPPED / FAILED
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    __table_args__ = (UniqueConstraint("plan_id", "ordinal", name="uq_plitem_plan_ordinal"),)
