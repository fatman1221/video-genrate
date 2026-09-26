"""字幕 Provider。"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from ..config import settings
from .base import GenerationResult, ProviderError, SubtitleProvider, registry
from . import local_engine as engine


class LocalSubtitleProvider(SubtitleProvider):
    name = "local"
    display_name = "本地字幕生成（SRT / ASS / VTT）"
    capabilities = ("srt", "ass", "vtt", "offline")
    doc = "根据镜头时间轴与旁白文本生成字幕文件，支持简繁与样式参数。"

    def generate(self, *, segments: list[dict[str, Any]], language: str = "zh-CN",
                 fmt: str = "srt",
                 parameters: dict[str, Any] | None = None) -> GenerationResult:
        started = time.time()
        params = dict(parameters or {})
        if not segments:
            raise ProviderError("字幕片段为空", retryable=False)
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="sub_"))
        workdir.mkdir(parents=True, exist_ok=True)
        fmt = (fmt or "srt").lower()
        out = workdir / f"subtitle_{int(time.time() * 1000)}.{fmt}"
        cleaned = [s for s in segments if str(s.get("text") or "").strip()]
        if not cleaned:
            raise ProviderError("字幕片段全部为空文本", retryable=False)

        if fmt == "ass":
            engine.write_ass(
                out_path=str(out), segments=cleaned,
                width=int(params.get("width") or 1280),
                height=int(params.get("height") or 720),
                font_size=int(params.get("font_size") or 24),
            )
        elif fmt == "vtt":
            lines = ["WEBVTT", ""]
            for i, seg in enumerate(cleaned, start=1):
                start = engine._srt_timestamp(float(seg.get("start") or 0)).replace(",", ".")
                end = engine._srt_timestamp(float(seg.get("end") or 0)).replace(",", ".")
                lines += [str(i), f"{start} --> {end}", str(seg["text"]).strip(), ""]
            out.write_text("\n".join(lines), encoding="utf-8")
        else:
            engine.write_srt(out_path=str(out), segments=cleaned)

        total_end = max(float(s.get("end") or 0) for s in cleaned)
        return GenerationResult(
            file_path=str(out), provider=self.name, model="local-subtitle-writer",
            workflow="local_subtitle", parameters={**params, "language": language, "format": fmt},
            duration=total_end, format=fmt, size_bytes=out.stat().st_size,
            prompt="", extra={"segment_count": len(cleaned), "language": language},
            elapsed_ms=int((time.time() - started) * 1000),
        )


class CloudASRSubtitleProvider(SubtitleProvider):
    """云端语音识别转字幕（未接线占位）。用于给已有音轨生成字幕。"""

    name = "cloud_asr"
    display_name = "云端 ASR 字幕（待接线）"
    requires_api_key = True
    capabilities = ("asr", "auto_timing", "multi_language")

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return False

    def generate(self, *, segments: list[dict[str, Any]], language: str = "zh-CN",
                 fmt: str = "srt",
                 parameters: dict[str, Any] | None = None) -> GenerationResult:
        raise ProviderError(
            "ASR 字幕 Provider 尚未接线。可接入 Whisper（本地 faster-whisper 或云 API）。",
            retryable=False,
        )


def register() -> None:
    registry.register(LocalSubtitleProvider(), default=True)
    registry.register(CloudASRSubtitleProvider())
