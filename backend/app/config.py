"""应用配置：支持通过环境变量 / .env 切换数据库与存储后端。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 应用 ----
    app_name: str = "AI Video Agent Studio"
    api_prefix: str = "/api"
    host: str = "127.0.0.1"
    port: int = 8077
    debug: bool = True

    # ---- 数据库 ----
    # 默认 PostgreSQL；如需零依赖运行可设为 sqlite:///./video_agent.db
    database_url: str = (
        "postgresql+psycopg://trade_app:change-me@127.0.0.1:5432/video_agent_studio"
    )
    db_echo: bool = False
    db_pool_size: int = 10

    # ---- 存储 ----
    # local | s3 | oss | minio  —— 第一阶段为 local，接口已抽象
    storage_backend: str = "local"
    storage_root: str = str(BACKEND_DIR / "storage")
    public_base_url: str = "http://127.0.0.1:8077"
    s3_bucket: str = ""
    s3_endpoint: str = ""
    s3_region: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""

    # ---- 执行引擎 ----
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    task_workers: int = 4
    default_provider_image: str = "local"
    default_provider_video: str = "local"
    default_provider_tts: str = "local"
    default_provider_music: str = "local"
    default_provider_sfx: str = "local"
    default_provider_subtitle: str = "local"
    default_provider_enhance: str = "ffmpeg"

    # ---- 外部引擎（占位，接入时填） ----
    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_api_key: str = ""
    #: 外部工作流模板目录（可选）。放本机专属、不便入库的工作流 JSON。
    #: 同名模板会覆盖 backend/app/workflows/templates/ 下的内置模板。
    comfyui_workflow_dir: str = ""
    #: 人物图 / 场景图默认使用的 ComfyUI 工作流模板（见 app/workflows/templates/）
    comfyui_workflow_character: str = "qwen_image_character"
    comfyui_workflow_scene: str = "qwen_image_scene"
    #: 单张图生成超时（秒）。本地 Qwen-Image 首张含加载模型，给足时间。
    comfyui_timeout: int = 1800
    cloud_video_base_url: str = ""
    cloud_video_api_key: str = ""
    cloud_image_base_url: str = ""
    cloud_image_api_key: str = ""
    cloud_tts_base_url: str = ""
    cloud_tts_api_key: str = ""
    runpod_base_url: str = ""
    runpod_api_key: str = ""

    # ---- 生成默认参数 ----
    default_image_width: int = 1280
    default_image_height: int = 720
    default_video_width: int = 1280
    default_video_height: int = 720
    default_fps: int = 24
    default_shot_duration: float = 5.0
    tts_voice: str = "Tingting"
    tts_rate: int = 180

    # ---- TTS 引擎（多级降级，见 providers/local_engine.py: say_tts） ----
    # auto | cosyvoice | edge | say | sapi
    # auto：CosyVoice（阿里开源）→ edge-tts → 系统内置；注册见 backend/.env
    tts_engine: str = "auto"
    # edge-tts 音色（微软神经网络，中文女声）
    tts_voice_edge: str = "zh-CN-XiaoxiaoNeural"
    # SAPI 音色（Windows 内置，留空用系统默认）
    tts_voice_sapi: str = ""
    # CosyVoice 独立推理进程的命令模板；{text_file}/{out_path}/{voice} 会被替换。
    # 留空则探测 tools/cosyvoice/ 下的默认入口
    cosyvoice_tts_cmd: str = ""

    # ---- 行为开关 ----
    auto_advance_workflow: bool = True
    simulate_latency: bool = False

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_root).resolve()

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# ffmpeg 允许通过环境变量覆盖，便于本机静态二进制场景
os.environ.setdefault("PATH", os.environ.get("PATH", ""))
