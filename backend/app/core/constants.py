"""全局常量：状态枚举 + Workflow 状态机定义。

这里的状态机既定义「正常流水线顺序」，也允许 Agent 动态跳转
（例如 VIDEO_GENERATED -> QUALITY_CHECK -> FAILED -> ANALYZE -> REGENERATE -> QUALITY_CHECK）。
"""
from __future__ import annotations

from typing import Final


class ProjectStatus:
    DRAFT: Final = "DRAFT"
    PLANNING: Final = "PLANNING"
    GENERATING: Final = "GENERATING"
    COMPLETED: Final = "COMPLETED"
    FAILED: Final = "FAILED"
    PAUSED: Final = "PAUSED"

    ALL: Final = (DRAFT, PLANNING, GENERATING, COMPLETED, FAILED, PAUSED)


class TaskStatus:
    PENDING: Final = "PENDING"
    RUNNING: Final = "RUNNING"
    SUCCESS: Final = "SUCCESS"
    FAILED: Final = "FAILED"
    CANCELLED: Final = "CANCELLED"
    RETRYING: Final = "RETRYING"

    ALL: Final = (PENDING, RUNNING, SUCCESS, FAILED, CANCELLED, RETRYING)
    TERMINAL: Final = (SUCCESS, FAILED, CANCELLED)


class TaskType:
    GENERATE_IMAGE: Final = "GENERATE_IMAGE"
    GENERATE_VIDEO: Final = "GENERATE_VIDEO"
    GENERATE_CHARACTER_REFERENCE: Final = "GENERATE_CHARACTER_REFERENCE"
    GENERATE_VOICE: Final = "GENERATE_VOICE"
    GENERATE_MUSIC: Final = "GENERATE_MUSIC"
    GENERATE_SFX: Final = "GENERATE_SFX"
    GENERATE_SUBTITLE: Final = "GENERATE_SUBTITLE"
    ENHANCE_VIDEO: Final = "ENHANCE_VIDEO"
    UPSCALE_VIDEO: Final = "UPSCALE_VIDEO"
    INTERPOLATE_VIDEO: Final = "INTERPOLATE_VIDEO"
    EDIT_VIDEO: Final = "EDIT_VIDEO"
    MERGE_VIDEO: Final = "MERGE_VIDEO"
    ADD_VOICE: Final = "ADD_VOICE"
    ADD_MUSIC: Final = "ADD_MUSIC"
    ADD_SFX: Final = "ADD_SFX"
    ADD_SUBTITLE: Final = "ADD_SUBTITLE"
    COMPOSE_VIDEO: Final = "COMPOSE_VIDEO"
    QUALITY_CHECK: Final = "QUALITY_CHECK"
    BROWSER_TASK: Final = "BROWSER_TASK"
    CUSTOM: Final = "CUSTOM"

    ALL: Final = (
        GENERATE_IMAGE, GENERATE_VIDEO, GENERATE_CHARACTER_REFERENCE,
        GENERATE_VOICE, GENERATE_MUSIC, GENERATE_SFX, GENERATE_SUBTITLE,
        ENHANCE_VIDEO, UPSCALE_VIDEO, INTERPOLATE_VIDEO, EDIT_VIDEO, MERGE_VIDEO,
        ADD_VOICE, ADD_MUSIC, ADD_SFX, ADD_SUBTITLE, COMPOSE_VIDEO,
        QUALITY_CHECK, BROWSER_TASK, CUSTOM,
    )


class AssetType:
    CHARACTER: Final = "CHARACTER"
    IMAGE: Final = "IMAGE"
    SCENE: Final = "SCENE"
    VIDEO: Final = "VIDEO"
    VOICE: Final = "VOICE"
    MUSIC: Final = "MUSIC"
    SFX: Final = "SFX"
    SUBTITLE: Final = "SUBTITLE"
    PROJECT_OUTPUT: Final = "PROJECT_OUTPUT"
    TEMP: Final = "TEMP"

    ALL: Final = (CHARACTER, IMAGE, SCENE, VIDEO, VOICE, MUSIC, SFX, SUBTITLE, PROJECT_OUTPUT, TEMP)


class AssetStatus:
    PENDING: Final = "PENDING"
    READY: Final = "READY"
    FAILED: Final = "FAILED"
    ARCHIVED: Final = "ARCHIVED"


class ShotStatus:
    PENDING: Final = "PENDING"
    QUEUED: Final = "QUEUED"
    GENERATING: Final = "GENERATING"
    IMAGE_READY: Final = "IMAGE_READY"
    VIDEO_READY: Final = "VIDEO_READY"
    READY: Final = "READY"
    FAILED: Final = "FAILED"

    ALL: Final = (PENDING, QUEUED, GENERATING, IMAGE_READY, VIDEO_READY, READY, FAILED)


class SceneStatus:
    PENDING: Final = "PENDING"
    GENERATING: Final = "GENERATING"
    READY: Final = "READY"
    FAILED: Final = "FAILED"


class WorkflowState:
    """项目级工作流状态。"""

    PROJECT_CREATED: Final = "PROJECT_CREATED"
    SCRIPT_GENERATED: Final = "SCRIPT_GENERATED"
    STORYBOARD_GENERATED: Final = "STORYBOARD_GENERATED"
    CHARACTER_GENERATED: Final = "CHARACTER_GENERATED"
    IMAGE_GENERATED: Final = "IMAGE_GENERATED"
    VIDEO_GENERATED: Final = "VIDEO_GENERATED"
    VOICE_GENERATED: Final = "VOICE_GENERATED"
    MUSIC_GENERATED: Final = "MUSIC_GENERATED"
    SUBTITLE_GENERATED: Final = "SUBTITLE_GENERATED"
    ENHANCEMENT: Final = "ENHANCEMENT"
    EDITING: Final = "EDITING"
    COMPOSING: Final = "COMPOSING"
    QUALITY_CHECK: Final = "QUALITY_CHECK"
    ANALYZE: Final = "ANALYZE"
    REGENERATE: Final = "REGENERATE"
    COMPLETED: Final = "COMPLETED"
    FAILED: Final = "FAILED"

    #: 正常流水线顺序
    FLOW: Final = (
        PROJECT_CREATED,
        SCRIPT_GENERATED,
        STORYBOARD_GENERATED,
        CHARACTER_GENERATED,
        IMAGE_GENERATED,
        VIDEO_GENERATED,
        VOICE_GENERATED,
        MUSIC_GENERATED,
        SUBTITLE_GENERATED,
        ENHANCEMENT,
        EDITING,
        COMPOSING,
        QUALITY_CHECK,
        COMPLETED,
    )

    #: 任何状态都可以跳转到的状态（Agent 动态决策）
    ANY_TO: Final = (FAILED, ANALYZE, REGENERATE, QUALITY_CHECK, COMPLETED)

    #: 无序状态（用于进度计算）
    ORDER_INDEX: Final = {state: idx for idx, state in enumerate(FLOW)}


#: 工作流步骤定义：(step_key, 中文名, 产出说明)
WORKFLOW_STEPS: Final = (
    ("script", "脚本生成", "根据需求生成分镜脚本"),
    ("storyboard", "分镜拆解", "拆分为 Scene / Shot"),
    ("character", "人物设定", "生成角色设定与参考图"),
    ("image", "画面生成", "每个 Shot 生成关键帧"),
    ("video", "镜头视频", "每个 Shot 生成视频片段"),
    ("voice", "配音生成", "TTS 生成旁白"),
    ("music", "配乐生成", "生成背景音乐"),
    ("subtitle", "字幕生成", "生成 SRT 字幕"),
    ("enhancement", "画质增强", "超分 / 补帧 / 降噪"),
    ("editing", "剪辑", "裁剪 / 拼接 / 转场"),
    ("composing", "合成", "音画字幕混流"),
    ("quality_check", "质量检查", "完整性与一致性校验"),
)

#: 阶段 -> 完成后的工作流状态
STEP_DONE_STATE: Final = {
    "script": WorkflowState.SCRIPT_GENERATED,
    "storyboard": WorkflowState.STORYBOARD_GENERATED,
    "character": WorkflowState.CHARACTER_GENERATED,
    "image": WorkflowState.IMAGE_GENERATED,
    "video": WorkflowState.VIDEO_GENERATED,
    "voice": WorkflowState.VOICE_GENERATED,
    "music": WorkflowState.MUSIC_GENERATED,
    "subtitle": WorkflowState.SUBTITLE_GENERATED,
    "enhancement": WorkflowState.ENHANCEMENT,
    "editing": WorkflowState.EDITING,
    "composing": WorkflowState.COMPOSING,
    "quality_check": WorkflowState.QUALITY_CHECK,
}

#: 阶段 -> 项目进度权重（合计 100）
STEP_WEIGHTS: Final = {
    "script": 10,
    "storyboard": 12,
    "character": 8,
    "image": 12,
    "video": 20,
    "voice": 8,
    "music": 4,
    "subtitle": 4,
    "enhancement": 6,
    "editing": 6,
    "composing": 6,
    "quality_check": 4,
}

#: 质量检查项
QUALITY_CHECKS: Final = (
    ("video_integrity", "视频完整"),
    ("audio_integrity", "音频完整"),
    ("subtitle_integrity", "字幕完整"),
    ("resolution", "分辨率正确"),
    ("all_shots_done", "所有 Shot 已完成"),
    ("no_failed_tasks", "没有失败任务"),
    ("duration", "时长符合预期"),
    ("frame_rate", "帧率正确"),
)


def next_state(current: str) -> str | None:
    """返回正常流程的下一个状态。"""
    if current not in WorkflowState.FLOW:
        return WorkflowState.FLOW[0]
    idx = WorkflowState.FLOW.index(current)
    if idx + 1 >= len(WorkflowState.FLOW):
        return None
    return WorkflowState.FLOW[idx + 1]


def transition_allowed(current: str, target: str) -> bool:
    """是否允许从 current 跳到 target。"""
    if current == target:
        return True
    if target in WorkflowState.ANY_TO:
        return True
    if current not in WorkflowState.FLOW:
        return True
    return next_state(current) == target


def progress_for_state(state: str) -> int:
    """根据工作流状态粗算项目进度百分比。"""
    if state == WorkflowState.COMPLETED:
        return 100
    if state not in WorkflowState.ORDER_INDEX:
        return 0
    done = WorkflowState.ORDER_INDEX[state] + 1
    return int(round(done / len(WorkflowState.FLOW) * 100))


class ReviewStatus:
    """节点审核状态：与生产状态（WorkflowStep.state）解耦。"""

    NONE: Final = "NONE"          # 尚未审核
    APPROVED: Final = "APPROVED"  # 审核通过
    REJECTED: Final = "REJECTED"  # 审核驳回（需要重新生成 / 回退）

    ALL: Final = (NONE, APPROVED, REJECTED)


class StepState:
    """工作流步骤（节点）的生产状态。"""

    PENDING: Final = "PENDING"
    RUNNING: Final = "RUNNING"
    SUCCESS: Final = "SUCCESS"
    FAILED: Final = "FAILED"
    SKIPPED: Final = "SKIPPED"

    ALL: Final = (PENDING, RUNNING, SUCCESS, FAILED, SKIPPED)


#: 节点 -> 依赖它的下游节点。回退到某节点时，这些节点会被一并重置。
#: 设计原则：上游产物变化后，下游产物即视为失效，必须重新生产。
STAGE_DOWNSTREAM: Final = {
    "script": ("storyboard", "character", "image", "video", "voice", "music",
               "subtitle", "enhancement", "editing", "composing", "quality_check"),
    "storyboard": ("image", "video", "voice", "subtitle", "enhancement",
                   "editing", "composing", "quality_check"),
    "character": ("image", "video", "enhancement", "editing", "composing", "quality_check"),
    "image": ("video", "enhancement", "editing", "composing", "quality_check"),
    "video": ("enhancement", "editing", "composing", "quality_check"),
    "voice": ("composing", "quality_check"),
    "music": ("composing", "quality_check"),
    "subtitle": ("composing", "quality_check"),
    "enhancement": ("editing", "composing", "quality_check"),
    "editing": ("composing", "quality_check"),
    "composing": ("quality_check",),
    "quality_check": (),
}

#: 节点 -> 回退时需要清空的 Shot 产物字段（含状态字段）
STAGE_SHOT_FIELDS: Final = {
    "image": ("image_asset_id", "image_status"),
    "video": ("video_asset_id", "video_status"),
    "voice": ("voice_asset_id", "voice_status"),
    "subtitle": ("subtitle_asset_id", "subtitle_status"),
}

#: 节点 -> 重新生成时提交的任务（None 表示同步生成类能力，走 Skill 直接重建）
STAGE_TASK_TYPE: Final = {
    "character": "GENERATE_CHARACTER_REFERENCE",
    "image": "GENERATE_IMAGE",
    "video": "GENERATE_VIDEO",
    "voice": "GENERATE_VOICE",
    "music": "GENERATE_MUSIC",
    "subtitle": "GENERATE_SUBTITLE",
    "enhancement": "ENHANCE_VIDEO",
    "editing": "MERGE_VIDEO",
    "composing": "COMPOSE_VIDEO",
    "quality_check": "QUALITY_CHECK",
}

#: 节点 -> 中文名（用于日志与 UI 文案）
STAGE_LABELS: Final = {key: name for key, name, _ in WORKFLOW_STEPS}

#: 节点 -> 是否为「可审核」的关键交付节点（UI 上会显示审核徽章）
STAGE_REVIEWABLE: Final = (
    "script", "storyboard", "character", "image", "video",
    "voice", "music", "subtitle", "composing",
)


class ModelSettingKind:
    """系统设置里对用户可见的三类「模型」。

    每类映射到一个 Provider kind，用户在其中选择引擎（本地 / ComfyUI / 云端）。
    """

    IMAGE: Final = "image"
    VIDEO: Final = "video"
    TTS: Final = "tts"

    ALL: Final = (IMAGE, VIDEO, TTS)


#: 已保存凭证的回显掩码：设置页只暴露后 4 位，绝不把密钥原样返回浏览器
PUBLIC_MASK: Final = "•"


class AssetGroup:
    """素材中心的分类（用户视角），一个分类可对应多个 AssetType。"""

    CHARACTER: Final = "character"
    SCENE: Final = "scene"
    AUDIO: Final = "audio"
    IMAGE: Final = "image"
    VIDEO: Final = "video"

    ALL: Final = (CHARACTER, SCENE, AUDIO, IMAGE, VIDEO)
