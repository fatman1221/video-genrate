"""Provider 抽象层。

原则：
- 业务层只依赖抽象接口，不关心底层是 ComfyUI、云端 API 还是本地 ffmpeg。
- 每种能力（image / video / tts / music / sfx / subtitle / enhance / processing / browser）
  各自维护一个 Provider 注册表，可运行时切换、可多个并存、可日后扩展。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


class ProviderError(RuntimeError):
    """Provider 执行失败。可携带 retryable 标记，供任务系统决定是否重试。"""

    def __init__(self, message: str, *, retryable: bool = True, detail: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.detail = detail


@dataclass
class GenerationResult:
    """统一的生成结果。所有 provider 必须返回该结构，保证可追踪。"""

    file_path: str                      # 本地绝对路径
    provider: str = ""
    model: str = ""
    workflow: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    format: str = ""
    size_bytes: int = 0
    prompt: str = ""
    negative_prompt: str = ""
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    def to_asset_fields(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "provider": self.provider,
            "model": self.model,
            "workflow": self.workflow,
            "parameters": self.parameters,
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
            "fps": self.fps,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
        }


class Provider(ABC):
    """所有 Provider 的基类。"""

    kind: str = "base"
    name: str = "base"
    display_name: str = ""
    requires_api_key: bool = False
    capabilities: tuple[str, ...] = ()
    #: 是否为真实执行引擎（False 表示占位/未接线适配器）
    functional: bool = True
    doc: str = ""

    def health(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "display_name": self.display_name or self.name,
            "functional": self.functional,
            "requires_api_key": self.requires_api_key,
            "capabilities": list(self.capabilities),
            "status": "READY" if self.functional else "NOT_CONFIGURED",
            "checked_at": time.time(),
        }


class ProviderRegistry:
    """按 kind 管理的 provider 注册表。"""

    def __init__(self) -> None:
        self._providers: dict[str, dict[str, Provider]] = {}
        self._defaults: dict[str, str] = {}

    def register(self, provider: Provider, *, default: bool = False) -> Provider:
        self._providers.setdefault(provider.kind, {})[provider.name] = provider
        if default or provider.kind not in self._defaults:
            self._defaults.setdefault(provider.kind, provider.name)
        if default:
            self._defaults[provider.kind] = provider.name
        return provider

    def get(self, kind: str, name: str | None = None) -> Provider:
        bucket = self._providers.get(kind, {})
        if not bucket:
            raise ProviderError(f"未注册任何 {kind} Provider", retryable=False)
        key = name or self._defaults.get(kind) or next(iter(bucket))
        if key not in bucket:
            raise ProviderError(
                f"未找到 {kind} Provider: {key}（可选：{', '.join(bucket)}）", retryable=False
            )
        return bucket[key]

    def set_default(self, kind: str, name: str) -> None:
        self.get(kind, name)
        self._defaults[kind] = name

    def default_name(self, kind: str) -> str:
        return self._defaults.get(kind, "")

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        kinds = [kind] if kind else list(self._providers)
        for k in kinds:
            for name, provider in self._providers.get(k, {}).items():
                info = provider.health()
                info["is_default"] = name == self._defaults.get(k)
                info["doc"] = provider.doc
                out.append(info)
        return out

    def has(self, kind: str, name: str | None = None) -> bool:
        try:
            self.get(kind, name)
            return True
        except ProviderError:
            return False


registry = ProviderRegistry()


# --------------------------------------------------------------------------- #
# 各能力接口
# --------------------------------------------------------------------------- #
class ImageProvider(Provider):
    kind = "image"

    @abstractmethod
    def generate(
        self, *, prompt: str, negative_prompt: str = "",
        width: int = 1280, height: int = 720,
        seed: int | None = None, reference_image: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> GenerationResult: ...


class VideoProvider(Provider):
    kind = "video"

    @abstractmethod
    def generate(
        self, *, prompt: str, image_path: str | None = None,
        width: int = 1280, height: int = 720, duration: float = 5.0,
        fps: int = 24, seed: int | None = None, parameters: dict[str, Any] | None = None,
        progress_cb: Callable[[int, str], None] | None = None,
    ) -> GenerationResult: ...


class TTSProvider(Provider):
    kind = "tts"

    @abstractmethod
    def synthesize(
        self, *, text: str, voice: str = "", rate: int = 0,
        parameters: dict[str, Any] | None = None,
    ) -> GenerationResult: ...


class MusicProvider(Provider):
    kind = "music"

    @abstractmethod
    def generate_music(
        self, *, prompt: str = "", duration: float = 30.0,
        mood: str = "calm", parameters: dict[str, Any] | None = None,
    ) -> GenerationResult: ...


class SFXProvider(Provider):
    kind = "sfx"

    @abstractmethod
    def generate_sfx(
        self, *, prompt: str = "", duration: float = 1.5,
        parameters: dict[str, Any] | None = None,
    ) -> GenerationResult: ...


class SubtitleProvider(Provider):
    kind = "subtitle"

    @abstractmethod
    def generate(
        self, *, segments: list[dict[str, Any]], language: str = "zh-CN",
        fmt: str = "srt", parameters: dict[str, Any] | None = None,
    ) -> GenerationResult: ...


class EnhanceProvider(Provider):
    kind = "enhance"

    @abstractmethod
    def enhance(
        self, *, video_path: str, operations: list[dict[str, Any]],
        width: int = 0, height: int = 0, fps: int = 0,
        progress_cb: Callable[[int, str], None] | None = None,
    ) -> GenerationResult: ...


class ProcessingProvider(Provider):
    kind = "processing"

    @abstractmethod
    def run(
        self, *, operation: str, inputs: list[str], output_name: str,
        params: dict[str, Any] | None = None,
        progress_cb: Callable[[int, str], None] | None = None,
    ) -> GenerationResult: ...


class BrowserProvider(Provider):
    """浏览器自动化不是唯一执行方式，且不写死在 Backend 业务里。

    Backend 只负责任务登记与状态回写，真正的操作由 Agent 通过
    浏览器 Skill 执行（API → CLI → 浏览器，优先级依次下降）。
    """

    kind = "browser"

    @abstractmethod
    def execute(self, *, task_id: str, instruction: str, steps: list[dict[str, Any]],
                parameters: dict[str, Any] | None = None) -> dict[str, Any]: ...
