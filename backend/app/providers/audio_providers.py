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
from . import runtime


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


class Qwen3TTSProvider(TTSProvider):
    """Qwen3-TTS CustomVoice：本地 1.7B，支持音色切换与自然语言情感指令。

    torch 依赖重（约 3.5G 权重 + cu126），与后端进程隔离，一律子进程调用
    ``tools/qwen3-tts/infer.py``。参数经 ``parameters`` 透传：

    - ``speaker``：音色名（sohee / vivian / uncle_fu ...），见 ``engine.QWEN3_SPEAKERS``
    - ``instruct``：情感指令，如「像跟朋友聊天一样娓娓道来，声音明亮清晰」
    - ``device``：auto / cuda:0 / cpu
    """

    name = "qwen3tts"
    display_name = "Qwen3-TTS 情感配音（本地）"
    capabilities = ("tts", "offline", "emotion", "multi_voice")
    doc = "阿里开源 Qwen3-TTS-12Hz-1.7B-CustomVoice，情感指令驱动语气，9 种音色可选。"

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return engine.qwen3tts_available()

    def synthesize(self, *, text: str, voice: str = "", rate: int = 0,
                   parameters: dict[str, Any] | None = None) -> GenerationResult:
        started = time.time()
        params = dict(parameters or {})
        if not text.strip():
            raise ProviderError("TTS 文本为空", retryable=False)
        if not engine.qwen3tts_available():
            raise ProviderError(
                "Qwen3-TTS 不可用（检查 tools/qwen3-tts/ 与 qwen3tts_enabled 配置）",
                retryable=False,
            )
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="tts_"))
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"voice_{int(time.time() * 1000)}.mp3"

        speaker = voice or params.get("speaker") or settings.qwen3tts_speaker
        instruct = params.get("instruct") or ""
        engine.qwen3_tts_synthesize(
            text=text, out_path=str(out), speaker=speaker, instruct=instruct,
            device=str(params.get("device") or settings.qwen3tts_device),
        )
        info = engine.ffprobe(out)
        return GenerationResult(
            file_path=str(out), provider=self.name, model=settings.qwen3tts_model,
            workflow="qwen3_tts_custom_voice",
            parameters={**params, "speaker": speaker, "instruct": instruct},
            duration=info["duration"], format="mp3", size_bytes=info["size_bytes"],
            prompt=text, extra={"sample_rate": info["sample_rate"], "speaker": speaker,
                                "instruct": instruct},
            elapsed_ms=int((time.time() - started) * 1000),
        )


class CloudTTSProvider(TTSProvider):
    name = "cloud"
    display_name = "云端 TTS API（通用适配器）"
    requires_api_key = True
    capabilities = ("tts", "multi_voice", "emotion", "high_quality")
    doc = "POST {CLOUD_TTS_BASE_URL} 提交 {text,voice,rate}，可直接返回音频字节或 {url}。"

    @property
    def base_url(self) -> str:
        return runtime.resolve("tts", "cloud", "base_url", settings.cloud_tts_base_url)

    @property
    def api_key(self) -> str:
        return runtime.resolve("tts", "cloud", "api_key", settings.cloud_tts_api_key)

    @property
    def default_model(self) -> str:
        return runtime.model_of("tts", "cloud")

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return bool(self.base_url and self.api_key)

    def synthesize(self, *, text: str, voice: str = "", rate: int = 0,
                   parameters: dict[str, Any] | None = None) -> GenerationResult:
        if not self.functional:
            raise ProviderError(
                "云端 TTS Provider 未配置（请在『系统设置』里填写 Base URL 与 API Key，"
                "或设置 CLOUD_TTS_BASE_URL / CLOUD_TTS_API_KEY 环境变量）",
                retryable=False,
            )
        started = time.time()
        params = dict(parameters or {})
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="cloud_tts_"))
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "cloud_voice.mp3"
        model = params.get("model") or self.default_model
        with httpx.Client(timeout=float(params.get("timeout", 300))) as client:
            resp = client.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"text": text, "voice": voice or params.get("voice", ""),
                      "rate": rate or params.get("rate", 0),
                      **({"model": model} if model else {}),
                      **params.get("extra", {})},
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
            file_path=str(out), provider=self.name,
            model=params.get("model") or self.default_model or "cloud-tts",
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
        style = str(params.get("style") or "pop")
        if engine.bgm_synth_available():
            # 真实乐器合成（和弦分解 + 鼓组/贝斯/拨弦），比 lavfi 正弦垫音好得多
            engine.synth_bgm(
                out_path=str(out), duration=duration, style=style,
                peak_db=float(params.get("peak_db") or -9.0),
            )
            model, workflow = f"bgm-synth-{style}", "bgm_synth"
        else:
            engine.synth_music(out_path=str(out), duration=duration, mood=mood, prompt=prompt)
            model, workflow = "ffmpeg-synth-pad", "local_music_synthesis"
        info = engine.ffprobe(out)
        return GenerationResult(
            file_path=str(out), provider=self.name, model=model,
            workflow=workflow,
            parameters={**params, "mood": mood, "style": style, "duration": duration},
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
        # 修正：原实现引用了 cloud_video_* 配置（与音乐无关），且该 provider 尚未接线
        return bool(runtime.resolve("music", "cloud", "base_url", ""))

    def generate_music(self, *, prompt: str = "", duration: float = 30.0,
                       mood: str = "calm",
                       parameters: dict[str, Any] | None = None) -> GenerationResult:
        raise ProviderError(
            "云端音乐 Provider 尚未接线：请在 audio_providers.CloudMusicProvider 中补齐 HTTP 调用",
            retryable=False,
        )


def available_voices() -> list[str]:
    """本机可用的全部音色名（含 Qwen3-TTS 的 speaker），供下拉框使用。"""
    voices = list(engine.list_voices())
    for sp in engine.QWEN3_SPEAKERS:
        if sp["name"] not in voices:
            voices.append(sp["name"])
    return voices


def tts_engines() -> list[dict[str, Any]]:
    """给 Web UI 的 TTS 引擎清单：引擎 → 可用音色 → 默认值。

    前端据此渲染「配音调音台」，不必硬编码任何音色名。
    """
    engines: list[dict[str, Any]] = []
    if engine.qwen3tts_available():
        engines.append({
            "name": "qwen3tts",
            "label": "Qwen3-TTS 情感配音（本地）",
            "desc": "支持情感指令与 9 种音色，配音有情绪起伏；单条约几秒到十几秒",
            "supports_instruct": True,
            "default_speaker": settings.qwen3tts_speaker,
            "speakers": [dict(sp) for sp in engine.QWEN3_SPEAKERS],
        })
    engines.append({
        "name": "local",
        "label": "系统 / edge-tts",
        "desc": "轻量快速，无情感指令；音色来自系统与 edge-tts",
        "supports_instruct": False,
        "default_speaker": settings.tts_voice,
        "speakers": [{"name": v, "label": v, "desc": ""} for v in engine.list_voices()],
    })
    return engines


def music_styles() -> list[dict[str, Any]]:
    """BGM 可选的曲风（对应 scripts/gen_bgm.py 的 --style）。"""
    return [
        {"name": "pop", "label": "流行律动（108BPM）",
         "desc": "鼓组 + 贝斯 + 明亮拨弦，经典 4536 走向，适合短视频/vlog"},
        {"name": "warm", "label": "温暖钢琴（76BPM）",
         "desc": "钢琴和弦分解 + 弦乐垫，安静治愈，适合叙事"},
    ]


def register() -> None:
    registry.register(LocalTTSProvider(), default=True)
    registry.register(Qwen3TTSProvider())
    registry.register(CloudTTSProvider())
    registry.register(LocalMusicProvider(), default=True)
    registry.register(CloudMusicProvider())
    registry.register(LocalSFXProvider(), default=True)
