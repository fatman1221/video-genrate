"""画质增强 Provider。

架构上支持：Upscale / Frame Interpolation / Denoise / Sharpen / Face Enhancement /
Color Enhancement / Audio Enhancement。
第一阶段实现基于 ffmpeg 的基础能力；Face Enhancement 需要专用模型，
在此设计为「可插拔 op」，未接线时明确返回 skipped 而不是假装成功。
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from .base import EnhanceProvider, GenerationResult, ProviderError, registry
from . import local_engine as engine


class FFmpegEnhanceProvider(EnhanceProvider):
    name = "ffmpeg"
    display_name = "FFmpeg 增强链"
    capabilities = ("upscale", "interpolate", "denoise", "sharpen", "color", "audio")
    doc = "把多个增强算子串成单次 ffmpeg 滤镜链，避免多次重编码造成画质损失。"

    def enhance(self, *, video_path: str, operations: list[dict[str, Any]],
                width: int = 0, height: int = 0, fps: int = 0,
                progress_cb: Callable[[int, str], None] | None = None) -> GenerationResult:
        started = time.time()
        src = Path(video_path)
        if not src.exists():
            raise ProviderError(f"源视频不存在: {video_path}", retryable=False)
        info = engine.ffprobe(src)
        target_w = width or info["width"] or settings.default_video_width
        target_h = height or info["height"] or settings.default_video_height

        vf: list[str] = []
        af: list[str] = []
        applied: list[str] = []
        skipped: list[str] = []

        for op in operations or []:
            name = str(op.get("op") or "").lower()
            if name == "upscale":
                scale = float(op.get("scale") or 0)
                if scale:
                    target_w = int(target_w * scale)
                    target_h = int(target_h * scale)
                vf.append(f"scale={target_w}:{target_h}:flags=lanczos")
                applied.append(f"upscale:{target_w}x{target_h}")
            elif name == "interpolate":
                target_fps = int(op.get("fps") or 48)
                vf.append(f"minterpolate=fps={target_fps}:mi_mode=mci:mc_mode=aobmc:vsbmc=1")
                applied.append(f"interpolate:{target_fps}fps")
            elif name == "denoise":
                strength = float(op.get("strength") or 1.5)
                vf.append(f"hqdn3d={strength}:{strength}:{strength * 4}:{strength * 4}")
                applied.append(f"denoise:{strength}")
            elif name == "sharpen":
                amount = float(op.get("amount") or 0.9)
                vf.append(f"unsharp=5:5:{amount}:5:5:0.0")
                applied.append(f"sharpen:{amount}")
            elif name == "color":
                contrast = float(op.get("contrast") or 1.06)
                saturation = float(op.get("saturation") or 1.12)
                brightness = float(op.get("brightness") or 0.0)
                gamma = float(op.get("gamma") or 1.0)
                vf.append(f"eq=contrast={contrast}:saturation={saturation}:brightness={brightness}:gamma={gamma}")
                applied.append("color")
            elif name == "audio":
                if op.get("denoise", True):
                    af.append("afftdn=nf=-25")
                if op.get("normalize"):
                    af.append("loudnorm=I=-16:TP=-1.5:LRA=11")
                if op.get("volume") is not None:
                    af.append(f"volume={float(op['volume']):.3f}")
                applied.append("audio")
            elif name == "face":
                skipped.append(
                    "face: 需要专用模型（GFPGAN / CodeFormer）。请在 enhance provider 中接入对应 runner。"
                )
            else:
                skipped.append(f"{name}: 未知算子")

        if not vf and not af:
            raise ProviderError("没有任何可执行的增强算子", retryable=False)

        if vf:
            vf.append("format=yuv420p")
        workdir = Path(tempfile.mkdtemp(prefix="enhance_"))
        out = workdir / f"enhanced_{int(time.time() * 1000)}.mp4"

        args: list[str] = ["-i", str(src)]
        if vf:
            args += ["-vf", ",".join(vf), "-c:v", "libx264", "-preset", "medium", "-crf", "18"]
        else:
            args += ["-c:v", "copy"]
        if af:
            args += ["-af", ",".join(af), "-c:a", "aac", "-b:a", "192k"]
        else:
            args += ["-c:a", "copy"]
        args += ["-movflags", "+faststart", str(out)]

        engine.run_ffmpeg(
            args, timeout=3600, progress_cb=progress_cb,
            total_duration=info["duration"], label="画质增强",
        )
        new_info = engine.ffprobe(out)
        return GenerationResult(
            file_path=str(out), provider=self.name, model="ffmpeg-enhance-chain",
            workflow="ffmpeg_enhance", parameters={"operations": operations},
            width=new_info["width"], height=new_info["height"],
            duration=new_info["duration"], fps=new_info["fps"],
            format="mp4", size_bytes=new_info["size_bytes"],
            extra={"applied": applied, "skipped": skipped},
            elapsed_ms=int((time.time() - started) * 1000),
        )


def register() -> None:
    registry.register(FFmpegEnhanceProvider(), default=True)
