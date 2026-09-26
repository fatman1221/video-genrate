"""视频处理 Provider（统一入口）。

上层只调用 ProcessingProvider.run(operation=...)，不关心底层是 FFmpeg、
ComfyUI 还是第三方 API。当前 ffmpeg 实现覆盖：

merge_video / trim_video / scale_video / add_voice / add_music / add_sfx /
mix_audio / add_subtitle / compose_video / extract_audio / make_thumbnail
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .base import GenerationResult, ProcessingProvider, ProviderError, registry
from . import local_engine as engine


class FFmpegProcessingProvider(ProcessingProvider):
    name = "ffmpeg"
    display_name = "FFmpeg 视频处理"
    capabilities = (
        "merge_video", "trim_video", "scale_video", "add_voice", "add_music",
        "add_sfx", "mix_audio", "add_subtitle", "compose_video",
        "concat_audio", "build_voice_timeline", "extract_audio", "make_thumbnail",
    )
    doc = "本地 ffmpeg 处理链，无外部依赖，覆盖剪辑 / 混音 / 烧字幕 / 合成。"

    def run(self, *, operation: str, inputs: list[str], output_name: str,
            params: dict[str, Any] | None = None,
            progress_cb: Callable[[int, str], None] | None = None) -> GenerationResult:
        started = time.time()
        params = dict(params or {})
        op = (operation or "").strip().lower()
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="proc_"))
        workdir.mkdir(parents=True, exist_ok=True)
        ext = params.get("ext") or ("mp3" if op == "extract_audio" else
                                    "jpg" if op == "make_thumbnail" else
                                    params.get("subtitle_ext", "mp4") if op == "add_subtitle" else "mp4")
        out = workdir / f"{output_name or op}_{int(time.time() * 1000)}.{ext}"

        for path in inputs:
            if path and not Path(path).exists():
                raise ProviderError(f"输入文件不存在: {path}", retryable=False)

        handler = {
            "merge_video": self._merge,
            "concat_video": self._merge,
            "trim_video": self._trim,
            "scale_video": self._scale,
            "add_voice": self._add_voice,
            "add_music": self._add_music,
            "add_sfx": self._add_sfx,
            "mix_audio": self._mix,
            "add_subtitle": self._add_subtitle,
            "compose_video": self._compose,
            "concat_audio": self._concat_audio,
            "build_voice_timeline": self._build_voice_timeline,
            "extract_audio": self._extract_audio,
            "make_thumbnail": self._thumbnail,
        }.get(op)
        if handler is None:
            raise ProviderError(
                f"不支持的视频处理操作: {operation}（可用: merge_video, trim_video, "
                "scale_video, add_voice, add_music, add_sfx, mix_audio, add_subtitle, "
                "compose_video, extract_audio, make_thumbnail）",
                retryable=False,
            )

        extra = handler(inputs=inputs, out_path=str(out), params=params, progress_cb=progress_cb)
        info = engine.ffprobe(out) if out.exists() else {}
        return GenerationResult(
            file_path=str(out), provider=self.name, model="ffmpeg",
            workflow=f"ffmpeg_{op}", parameters=params,
            width=info.get("width", 0), height=info.get("height", 0),
            duration=info.get("duration", 0.0), fps=info.get("fps", 0.0),
            format=ext, size_bytes=info.get("size_bytes", 0) or (out.stat().st_size if out.exists() else 0),
            extra=extra or {},
            elapsed_ms=int((time.time() - started) * 1000),
        )

    # ---- 具体算子 ----
    def _merge(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        if len(inputs) < 1:
            raise ProviderError("merge_video 需要至少 1 个输入", retryable=False)
        engine.concat_videos(video_paths=inputs, out_path=out_path, progress_cb=progress_cb)
        return {"merged_count": len(inputs)}

    def _trim(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        start = float(params.get("start") or 0.0)
        end = float(params.get("end") or 0.0)
        if end <= start:
            end = engine.audio_duration(inputs[0])
        engine.trim_video(video_path=inputs[0], out_path=out_path, start=start, end=end)
        return {"start": start, "end": end}

    def _scale(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        engine.scale_video(
            video_path=inputs[0], out_path=out_path,
            width=int(params.get("width") or 1920), height=int(params.get("height") or 1080),
            sharpen=bool(params.get("sharpen", True)), progress_cb=progress_cb,
        )
        return {}

    def _add_voice(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        if len(inputs) < 2:
            raise ProviderError("add_voice 需要 [video, voice]", retryable=False)
        engine.mux_audio(video_path=inputs[0], audio_path=inputs[1], out_path=out_path,
                         volume=float(params.get("volume") or 1.0), progress_cb=progress_cb)
        return {}

    def _add_music(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        if len(inputs) < 2:
            raise ProviderError("add_music 需要 [video, music]", retryable=False)
        tracks = [{"path": inputs[1], "volume": float(params.get("volume") or 0.18), "loop": True}]
        engine.mix_audio_tracks(video_path=inputs[0], tracks=tracks, out_path=out_path,
                                keep_original=bool(params.get("keep_original", True)),
                                progress_cb=progress_cb)
        return {}

    def _add_sfx(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        if len(inputs) < 2:
            raise ProviderError("add_sfx 需要 [video, sfx]", retryable=False)
        tracks = [{"path": inputs[1], "volume": float(params.get("volume") or 0.7),
                   "loop": False, "delay": float(params.get("delay") or 0.0)}]
        engine.mix_audio_tracks(video_path=inputs[0], tracks=tracks, out_path=out_path,
                                keep_original=bool(params.get("keep_original", True)),
                                progress_cb=progress_cb)
        return {}

    def _mix(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        if len(inputs) < 2:
            raise ProviderError("mix_audio 需要 [video, ...tracks]", retryable=False)
        tracks = []
        for path in inputs[1:]:
            tracks.append({
                "path": path, "volume": float(params.get("volume") or 0.3),
                "loop": bool(params.get("loop", True)),
            })
        engine.mix_audio_tracks(video_path=inputs[0], tracks=tracks, out_path=out_path,
                                keep_original=bool(params.get("keep_original", True)),
                                progress_cb=progress_cb)
        return {}

    def _add_subtitle(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        if len(inputs) < 2:
            raise ProviderError("add_subtitle 需要 [video, subtitle]", retryable=False)
        mode = str(params.get("mode") or "burn")
        if mode == "soft":
            engine.embed_soft_subtitles(video_path=inputs[0], subtitle_path=inputs[1], out_path=out_path)
            return {"mode": "soft"}
        engine.burn_subtitles(
            video_path=inputs[0], subtitle_path=inputs[1], out_path=out_path,
            font_size=int(params.get("font_size") or 22), progress_cb=progress_cb,
        )
        return {"mode": "burn"}

    def _compose(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        """一步合成：主视频 + 配音 + 配乐 + 音效 + 字幕。"""
        if not inputs:
            raise ProviderError("compose_video 需要 [video, (voice), (music), (sfx)]", retryable=False)
        video = inputs[0]
        voice = inputs[1] if len(inputs) > 1 else None
        music = inputs[2] if len(inputs) > 2 else None
        sfx = inputs[3] if len(inputs) > 3 else None
        subtitle = params.get("subtitle_path")

        current = video
        tmpdir = Path(tempfile.mkdtemp(prefix="compose_"))
        step = 0

        if voice or music or sfx:
            if progress_cb:
                progress_cb(10, "混音：配音 + 配乐 + 音效")
            tracks = []
            if voice:
                tracks.append({"path": voice, "volume": float(params.get("voice_volume") or 1.0), "loop": False})
            if music:
                tracks.append({"path": music, "volume": float(params.get("music_volume") or 0.16), "loop": True})
            if sfx:
                tracks.append({"path": sfx, "volume": float(params.get("sfx_volume") or 0.5),
                               "loop": False, "delay": float(params.get("sfx_delay") or 0.0)})
            step += 1
            mixed = tmpdir / f"mixed_{step}.mp4"
            engine.mix_audio_tracks(
                video_path=current, tracks=tracks, out_path=str(mixed),
                keep_original=bool(params.get("keep_original_audio", True)),
                progress_cb=(lambda p, m: progress_cb(10 + int(p * 0.5), m)) if progress_cb else None,
            )
            current = str(mixed)

        if subtitle and Path(subtitle).exists():
            if progress_cb:
                progress_cb(65, "烧录字幕")
            step += 1
            burned = tmpdir / f"sub_{step}.mp4"
            try:
                engine.burn_subtitles(
                    video_path=current, subtitle_path=subtitle, out_path=str(burned),
                    font_size=int(params.get("font_size") or 22),
                    progress_cb=(lambda p, m: progress_cb(65 + int(p * 0.3), m)) if progress_cb else None,
                )
                current = str(burned)
            except engine.EngineError as exc:  # 烧录失败降级为软字幕
                if params.get("soft_fallback", True):
                    engine.embed_soft_subtitles(video_path=current, subtitle_path=subtitle, out_path=str(burned))
                    current = str(burned)
                else:
                    raise exc

        if Path(current).resolve() != Path(out_path).resolve():
            engine.run_ffmpeg(["-i", current, "-c", "copy", "-movflags", "+faststart", out_path],
                              label="输出封装")
        if progress_cb:
            progress_cb(98, "合成完成")
        return {}

    def _concat_audio(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        """把多段音频按顺序拼成一条完整音轨（用于逐镜头配音汇总）。"""
        if not inputs:
            raise ProviderError("concat_audio 需要至少 1 个输入", retryable=False)
        valid = [p for p in inputs if p and Path(p).exists()]
        if not valid:
            raise ProviderError("concat_audio 输入文件都不存在", retryable=False)
        if len(valid) == 1:
            engine.run_ffmpeg(["-i", valid[0], "-c:a", "libmp3lame", "-b:a", "192k", out_path],
                              label="音轨转码")
            return {"segments": 1}
        work = Path(params.get("workdir") or tempfile.mkdtemp(prefix="cat_audio_"))
        work.mkdir(parents=True, exist_ok=True)
        list_file = work / "audio_list.txt"
        list_file.write_text(
            "".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in valid), encoding="utf-8",
        )
        total = sum(engine.audio_duration(p) for p in valid)
        engine.run_ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", out_path],
            timeout=1800, progress_cb=progress_cb, total_duration=total, label="拼接音轨",
        )
        return {"segments": len(valid)}

    def _build_voice_timeline(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        """按镜头时间轴对齐逐条旁白，生成一条完整配音音轨（音画同步）。"""
        timeline = params.get("timeline") or []
        if not timeline:
            raise ProviderError(
                "build_voice_timeline 需要 params.timeline=[{path,start,slot}, ...]",
                retryable=False,
            )
        valid = [s for s in timeline if s.get("path") and Path(s["path"]).exists()]
        if not valid:
            raise ProviderError("build_voice_timeline 没有可用输入", retryable=False)
        engine.build_voice_timeline(
            segments=valid, out_path=out_path,
            total_duration=float(params.get("total_duration") or 0.0),
            progress_cb=progress_cb,
        )
        return {"segments": len(valid)}

    def _extract_audio(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        engine.run_ffmpeg(
            ["-i", inputs[0], "-vn", "-c:a", "libmp3lame", "-b:a", "192k", out_path],
            label="提取音轨",
        )
        return {}

    def _thumbnail(self, *, inputs, out_path, params, progress_cb) -> dict[str, Any]:
        at = float(params.get("at") or 1.0)
        engine.run_ffmpeg(
            ["-ss", f"{at:.2f}", "-i", inputs[0], "-frames:v", "1", "-q:v", "2", out_path],
            label="生成封面",
        )
        return {}


def register() -> None:
    registry.register(FFmpegProcessingProvider(), default=True)
