"""音频 Provider：TTS / 音乐 / 音效。

- LocalTTSProvider：macOS `say` 本地合成（真实可跑）
- CloudTTSProvider：通用云端 TTS HTTP 适配器
- LocalMusicProvider / LocalSFXProvider：ffmpeg lavfi 合成
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from .base import (
    GenerationResult, MusicProvider, ProviderError, SFXProvider, TTSProvider, registry,
)
from . import local_engine as engine


class LocalTTSProvider(TTSProvider):
    name = "local"
    display_name = "本地 TTS（macOS say）"
    capabilities = ("tts", "offline", "multi_voice", "fast")
    doc = "调用系统 say 合成中文/英文旁白，输出 mp3。可用 VOICE 名称列表切换音色。"

    def synthesize(self, *, text: str, voice: str = "", rate: int = 0,
                   parameters: dict[str, Any] | None = None) -> GenerationResult:
        started = time.time()
        params = dict(parameters or {})
        if not text.strip():
            raise ProviderError("TTS 文本为空", retryable=False)
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="tts_"))
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"voice_{int(time.time() * 1000)}.mp3"
        used_voice = voice or params.get("voice") or settings.tts_voice
        engine.say_tts(text=text, out_path=str(out), voice=used_voice,
                       rate=int(rate or params.get("rate") or settings.tts_rate))
        info = engine.ffprobe(out)
        return GenerationResult(
            file_path=str(out), provider=self.name, model=f"say/{used_voice}",
            workflow="local_tts", parameters={**params, "voice": used_voice,
                                              "rate": int(rate or settings.tts_rate)},
            duration=info["duration"], format="mp3", size_bytes=info["size_bytes"],
            prompt=text, extra={"sample_rate": info["sample_rate"]},
            elapsed_ms=int((time.time() - started) * 1000),
        )


class CloudTTSProvider(TTSProvider):
    name = "cloud"
    display_name = "云端 TTS API（通用适配器）"
    requires_api_key = True
    capabilities = ("tts", "multi_voice", "emotion", "high_quality")
    doc = "POST {CLOUD_TTS_BASE_URL} 提交 {text,voice,rate}，可直接返回音频字节或 {url}。"

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return bool(settings.cloud_tts_base_url and settings.cloud_tts_api_key)

    def synthesize(self, *, text: str, voice: str = "", rate: int = 0,
                   parameters: dict[str, Any] | None = None) -> GenerationResult:
        if not self.functional:
            raise ProviderError(
                "云端 TTS Provider 未配置（需要 CLOUD_TTS_BASE_URL / CLOUD_TTS_API_KEY）",
                retryable=False,
            )
        started = time.time()
        params = dict(parameters or {})
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="cloud_tts_"))
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "cloud_voice.mp3"
        with httpx.Client(timeout=float(params.get("timeout", 300))) as client:
            resp = client.post(
                settings.cloud_tts_base_url,
                headers={"Authorization": f"Bearer {settings.cloud_tts_api_key}"},
                json={"text": text, "voice": voice or params.get("voice", ""),
                      "rate": rate or params.get("rate", 0), **params.get("extra", {})},
            )
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                payload = resp.json()
                url = payload.get("url")
                if not url:
                    raise ProviderError("云端 TTS 未返回音频", detail=str(payload)[:400])
                out.write_bytes(client.get(url).content)
            else:
                out.write_bytes(resp.content)
        info = engine.ffprobe(out)
        return GenerationResult(
            file_path=str(out), provider=self.name, model=params.get("model", "cloud-tts"),
            workflow="cloud_tts", parameters=params, duration=info["duration"],
            format="mp3", size_bytes=info["size_bytes"], prompt=text,
            elapsed_ms=int((time.time() - started) * 1000),
        )


class LocalMusicProvider(MusicProvider):
    name = "local"
    display_name = "本地配乐合成（ffmpeg 和弦垫）"
    capabilities = ("music", "offline", "mood_based")
    doc = "按 mood（calm/warm/happy/tense/epic/tech）合成环境配乐，时长可指定。"

    def generate_music(self, *, prompt: str = "", duration: float = 30.0,
                       mood: str = "calm",
                       parameters: dict[str, Any] | None = None) -> GenerationResult:
        started = time.time()
        params = dict(parameters or {})
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="music_"))
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"music_{int(time.time() * 1000)}.mp3"
        engine.synth_music(out_path=str(out), duration=duration, mood=mood, prompt=prompt)
        info = engine.ffprobe(out)
        return GenerationResult(
            file_path=str(out), provider=self.name, model="ffmpeg-synth-pad",
            workflow="local_music_synthesis",
            parameters={**params, "mood": mood, "duration": duration},
            duration=info["duration"], format="mp3", size_bytes=info["size_bytes"],
            prompt=prompt or mood,
            elapsed_ms=int((time.time() - started) * 1000),
        )


class LocalSFXProvider(SFXProvider):
    name = "local"
    display_name = "本地音效合成（ffmpeg lavfi）"
    capabilities = ("sfx", "offline", "whoosh", "ding", "click")
    doc = "根据关键词合成转场/提示/点击等音效。"

    def generate_sfx(self, *, prompt: str = "", duration: float = 1.5,
                     parameters: dict[str, Any] | None = None) -> GenerationResult:
        started = time.time()
        params = dict(parameters or {})
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="sfx_"))
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"sfx_{int(time.time() * 1000)}.mp3"
        engine.synth_sfx(out_path=str(out), duration=duration, prompt=prompt)
        info = engine.ffprobe(out)
        return GenerationResult(
            file_path=str(out), provider=self.name, model="ffmpeg-sfx",
            workflow="local_sfx_synthesis", parameters={**params, "duration": duration},
            duration=info["duration"], format="mp3", size_bytes=info["size_bytes"],
            prompt=prompt,
            elapsed_ms=int((time.time() - started) * 1000),
        )


class CloudMusicProvider(MusicProvider):
    name = "cloud"
    display_name = "云端音乐 API（通用适配器）"
    requires_api_key = True
    capabilities = ("music", "high_quality", "style_control")

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return bool(settings.cloud_video_base_url and settings.cloud_video_api_key)

    def generate_music(self, *, prompt: str = "", duration: float = 30.0,
                       mood: str = "calm",
                       parameters: dict[str, Any] | None = None) -> GenerationResult:
        raise ProviderError(
            "云端音乐 Provider 尚未接线：请在 audio_providers.CloudMusicProvider 中补齐 HTTP 调用",
            retryable=False,
        )


def available_voices() -> list[str]:
    return engine.list_voices()


def register() -> None:
    registry.register(LocalTTSProvider(), default=True)
    registry.register(CloudTTSProvider())
    registry.register(LocalMusicProvider(), default=True)
    registry.register(CloudMusicProvider())
    registry.register(LocalSFXProvider(), default=True)
