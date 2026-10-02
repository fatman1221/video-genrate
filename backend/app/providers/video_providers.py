"""视频生成 Provider。

- LocalVideoProvider：由关键帧 + ffmpeg 运镜生成真实 MP4（离线可跑）
- ComfyUIVideoProvider：ComfyUI 视频工作流适配器（AnimateDiff / SVD / Wan 等）
- CloudVideoProvider：通用云端视频 API 适配器（提交任务 + 轮询）
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import settings
from .base import GenerationResult, ProviderError, VideoProvider, registry
from . import local_engine as engine
from . import runtime
from .image_providers import (
    LocalImageProvider,
    _http_alive,
    _humanize_comfy_error,
    _resolve_workflow,
    _substitute,
)

_MOTION_KEYWORDS = (
    (("推近", "zoom in", "特写", "close-up"), "zoom_in"),
    (("拉远", "zoom out", "远景", "establishing"), "zoom_out"),
    (("摇镜", "pan left", "左移"), "pan_left"),
    (("右移", "pan right"), "pan_right"),
)


class LocalVideoProvider(VideoProvider):
    name = "local"
    display_name = "本地渲染引擎（关键帧 + ffmpeg 运镜）"
    capabilities = ("image_to_video", "ken_burns", "offline", "fast")
    doc = "内置引擎：用关键帧与 ffmpeg 运镜生成真实 MP4，用于打通完整链路。"

    def generate(self, *, prompt: str, image_path: str | None = None,
                 width: int = 1280, height: int = 720, duration: float = 5.0,
                 fps: int = 24, seed: int | None = None,
                 parameters: dict[str, Any] | None = None,
                 progress_cb: Callable[[int, str], None] | None = None) -> GenerationResult:
        started = time.time()
        params = dict(parameters or {})
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="vidgen_"))
        workdir.mkdir(parents=True, exist_ok=True)
        fps = int(fps or settings.default_fps)
        duration = float(duration or settings.default_shot_duration)

        if progress_cb:
            progress_cb(5, "准备关键帧")

        frame = image_path
        frame_generated = False
        if not frame or not Path(frame).exists():
            img_result = LocalImageProvider().generate(
                prompt=prompt, width=width, height=height, seed=seed,
                parameters={
                    "title": params.get("title") or prompt,
                    "body": params.get("subtitle") or "",
                    "badge": params.get("badge") or "",
                    "style_tag": params.get("style_tag") or "",
                    "shot_code": params.get("shot_code") or "",
                    "workdir": str(workdir),
                },
            )
            frame = img_result.file_path
            frame_generated = True

        motion = params.get("motion") or "auto"
        if motion == "auto":
            lowered = f"{prompt} {params.get('camera', '')}".lower()
            for keywords, name in _MOTION_KEYWORDS:
                if any(k in lowered for k in keywords):
                    motion = name
                    break

        if progress_cb:
            progress_cb(15, f"运镜模式 {motion}，开始渲染 {duration:.1f}s")
        out = workdir / f"shot_{int(time.time() * 1000)}.mp4"
        engine.image_to_video(
            image_path=frame, out_path=str(out), duration=duration, fps=fps,
            width=width, height=height, motion=motion,
        )
        info = engine.ffprobe(out)
        if progress_cb:
            progress_cb(97, "视频渲染完成")
        return GenerationResult(
            file_path=str(out), provider=self.name, model="ffmpeg-kenburns",
            workflow="local_image_to_video",
            parameters={**params, "motion": motion, "duration": duration, "fps": fps},
            width=info["width"] or width, height=info["height"] or height,
            duration=info["duration"] or duration, fps=info["fps"] or fps,
            format="mp4", size_bytes=info["size_bytes"],
            prompt=prompt, seed=seed,
            extra={"source_frame": frame, "frame_generated": frame_generated},
            elapsed_ms=int((time.time() - started) * 1000),
        )


class ComfyUIVideoProvider(VideoProvider):
    name = "comfyui"
    display_name = "ComfyUI 视频工作流"
    capabilities = ("image_to_video", "text_to_video", "animatediff", "svd", "wan")
    doc = "提交 ComfyUI 工作流并下载视频输出。需配置 COMFYUI_BASE_URL。"

    # 连接信息动态读取：设置页改完即时生效，不需要重启进程
    @property
    def base_url(self) -> str:
        return runtime.resolve("video", "comfyui", "base_url", settings.comfyui_base_url).rstrip("/")

    @property
    def api_key(self) -> str:
        return runtime.resolve("video", "comfyui", "api_key", settings.comfyui_api_key)

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return bool(self.base_url) and _http_alive(f"{self.base_url}/system_stats")

    def _upload_input_image(self, client: "httpx.Client", image_path: str,
                            headers: dict[str, str]) -> str:
        """把本地关键帧上传到 ComfyUI input 目录，返回 LoadImage 可用的文件名。

        ComfyUI 的 LoadImage 只认 input 目录里的相对文件名，绝对路径会直接报错。
        """
        p = Path(image_path)
        # multipart 上传不能带 JSON Content-Type，否则请求体被污染 → 400
        upload_headers = {k: v for k, v in headers.items()
                          if k.lower() != "content-type"}
        resp = client.post(
            f"{self.base_url}/upload/image",
            files={"image": (p.name, p.read_bytes(), "image/png")},
            data={"overwrite": "true", "type": "input"},
            headers=upload_headers,
        )
        if resp.status_code >= 400:
            raise ProviderError(
                f"关键帧上传失败: {resp.status_code}", detail=resp.text[:500])
        info = resp.json()
        name = info.get("name") or p.name
        sub = info.get("subfolder") or ""
        return f"{sub}/{name}" if sub else name

    def _conform_video(self, src: Path, *, width: int, height: int, fps: int,
                       duration: float, workdir: Path) -> Path:
        """把 ComfyUI 产物校准到镜头要求的尺寸与时长。

        - 帧网格对齐（17k+5）会让片段比目标略长 → 截断；
        - 帧数被 max_frames 钳制后片段偏短 → setpts 减速补齐（运镜更慢，但内容完整）；
        - 模板用原生分辨率（1376x768）→ 缩放到项目尺寸；
        - 顺手去掉源音轨（成片的配音/配乐由 compose 阶段统一混入）。
        """
        info = engine.ffprobe(src)
        actual = float(info.get("duration") or 0)
        w, h = int(info.get("width") or 0), int(info.get("height") or 0)
        need_scale = (w, h) != (width, height) and w and h
        need_time = actual and abs(actual - duration) > 0.25

        if not need_scale and not need_time:
            return src

        out = workdir / f"conform_{src.stem}.mp4"
        vf = f"scale={width}:{height}" if need_scale else None
        cmd: list[str] = ["-y", "-i", str(src)]
        if need_time and actual < duration:
            # 偏短：整体减速补齐，再以目标时长兜底截断
            factor = duration / actual
            cmd += ["-vf", f"{vf},setpts=PTS*{factor:.6f}" if vf else f"setpts=PTS*{factor:.6f}"]
        elif vf:
            cmd += ["-vf", vf]
        cmd += ["-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-r", str(int(fps) if fps else 24)]
        if need_time:
            cmd += ["-t", f"{duration:.3f}"]
        cmd += [str(out)]
        engine.run_ffmpeg(cmd, label="校准镜头视频")
        return out

    def generate(self, *, prompt: str, image_path: str | None = None,
                 width: int = 1280, height: int = 720, duration: float = 5.0,
                 fps: int = 24, seed: int | None = None,
                 parameters: dict[str, Any] | None = None,
                 progress_cb: Callable[[int, str], None] | None = None) -> GenerationResult:
        params = dict(parameters or {})
        frames = int(duration * fps) if fps else 124

        # 模板可声明 max_frames（如 MiniMax H3 训练范围 124~362）：
        # 超长镜头先钳制生成，再由 _conform_video 减速补齐时长
        max_frames = None
        name = (params.get("workflow_name") or "").strip()
        if name:
            from ..workflows import get_template
            try:
                max_frames = get_template(name).max_frames
            except Exception:  # noqa: BLE001 —— 模板缺失时让 _resolve_workflow 去报错
                max_frames = None
        if max_frames and frames > max_frames:
            frames = int(max_frames)

        started = time.time()
        resolved = _resolve_workflow(params, prompt=prompt, negative_prompt="",
                                     width=width, height=height, seed=seed)
        # 注意：{{image}} 此处不能替换 —— 上传完成后用 input 目录里的相对文件名替换
        resolved = _substitute(resolved, {
            "prompt": prompt, "width": width, "height": height,
            "frames": frames, "seed": seed or int(time.time()) % 2**31,
        })
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="comfy_vid_"))
        workdir.mkdir(parents=True, exist_ok=True)

        with httpx.Client(timeout=30) as client:
            image_ref = ""
            if image_path and Path(image_path).exists():
                if progress_cb:
                    progress_cb(8, "上传关键帧到 ComfyUI")
                image_ref = self._upload_input_image(client, image_path, headers)
                resolved = _substitute(resolved, {"image": image_ref})

            resp = client.post(f"{self.base_url}/prompt", json={"prompt": resolved}, headers=headers)
            if resp.status_code >= 400:
                raise ProviderError(
                    f"ComfyUI 提交失败: {resp.status_code}",
                    detail=_humanize_comfy_error(resp.text)[:1500],
                )
            prompt_id = resp.json().get("prompt_id")
            deadline = time.time() + float(params.get("timeout", 1800))
            outputs: dict[str, Any] = {}
            run_error = ""
            while time.time() < deadline:
                hist = client.get(f"{self.base_url}/history/{prompt_id}", headers=headers)
                if hist.status_code == 200:
                    entry = hist.json().get(prompt_id) or {}
                    outputs = entry.get("outputs") or {}
                    if any(v.get("gifs") or v.get("videos") or v.get("animated")
                           for v in outputs.values()):
                        break
                    st = entry.get("status") or {}
                    if st.get("status_str") == "error":
                        run_error = str(st.get("messages") or "")[:1200]
                        break
                if progress_cb:
                    progress_cb(50, f"等待 ComfyUI 视频任务（目标 {frames} 帧）")
                time.sleep(3)
            candidates: list[dict[str, Any]] = []
            video_exts = {".mp4", ".webm", ".mov", ".mkv", ".gif"}
            for node in outputs.values():
                candidates.extend(node.get("gifs") or [])
                candidates.extend(node.get("videos") or [])
                candidates.extend(node.get("animated") or [])
                # SaveVideo（ComfyUI 原生）把 mp4 列在 images 键下，
                # animated 只是 [true] 标志 —— 按扩展名甄别真正的视频文件
                for item in node.get("images") or []:
                    if Path(item.get("filename", "")).suffix.lower() in video_exts:
                        candidates.append(item)
            candidates = [c for c in candidates if isinstance(c, dict) and c.get("filename")]
            if not candidates:
                raise ProviderError(
                    "ComfyUI 视频任务超时或无输出" + ("（执行出错）" if run_error else ""),
                    retryable=True,
                    detail=_humanize_comfy_error(run_error or str(outputs))[:900],
                )
            first = candidates[0]
            resp2 = client.get(f"{self.base_url}/view", params={
                "filename": first.get("filename", ""),
                "subfolder": first.get("subfolder", ""),
                "type": first.get("type", "output"),
            }, headers=headers)
            resp2.raise_for_status()
            ext = Path(first.get("filename", "out.mp4")).suffix or ".mp4"
            out = workdir / f"comfy_{prompt_id}{ext}"
            out.write_bytes(resp2.content)

        out = self._conform_video(out, width=width, height=height, fps=int(fps or 24),
                                  duration=float(duration), workdir=workdir)
        info = engine.ffprobe(out)
        return GenerationResult(
            file_path=str(out), provider=self.name, model=params.get("ckpt_name", "comfyui-video"),
            workflow=params.get("workflow_name", "comfyui_video"), parameters=params,
            width=info["width"] or width, height=info["height"] or height,
            duration=info["duration"] or duration, fps=info["fps"] or fps,
            format=out.suffix.lstrip("."), size_bytes=info["size_bytes"],
            prompt=prompt, seed=seed, extra={"prompt_id": prompt_id, "source_image": image_ref},
            elapsed_ms=int((time.time() - started) * 1000),
        )


class CloudVideoProvider(VideoProvider):
    name = "cloud"
    display_name = "云端视频 API（提交 + 轮询）"
    requires_api_key = True
    capabilities = ("text_to_video", "image_to_video", "high_quality", "long_duration")
    doc = "通用云端适配器：POST 提交任务拿到 job_id，GET 轮询状态并下载视频。"

    @property
    def base_url(self) -> str:
        return runtime.resolve("video", "cloud", "base_url", settings.cloud_video_base_url).rstrip("/")

    @property
    def api_key(self) -> str:
        return runtime.resolve("video", "cloud", "api_key", settings.cloud_video_api_key)

    @property
    def default_model(self) -> str:
        return runtime.model_of("video", "cloud")

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return bool(self.base_url and self.api_key)

    def generate(self, *, prompt: str, image_path: str | None = None,
                 width: int = 1280, height: int = 720, duration: float = 5.0,
                 fps: int = 24, seed: int | None = None,
                 parameters: dict[str, Any] | None = None,
                 progress_cb: Callable[[int, str], None] | None = None) -> GenerationResult:
        if not self.functional:
            raise ProviderError(
                "云端视频 Provider 未配置（请在『系统设置』里填写 Base URL 与 API Key，"
                "或设置 CLOUD_VIDEO_BASE_URL / CLOUD_VIDEO_API_KEY 环境变量）",
                retryable=False,
            )
        started = time.time()
        params = dict(parameters or {})
        base = self.base_url
        headers = {"Authorization": f"Bearer {self.api_key}"}
        submit_path = params.get("submit_path", "/generate")
        status_path = params.get("status_path", "/status/{job_id}")
        model = params.get("model") or self.default_model

        with httpx.Client(timeout=60) as client:
            resp = client.post(base + submit_path, headers=headers, json={
                "prompt": prompt, "image": image_path, "width": width,
                "height": height, "duration": duration, "fps": fps, "seed": seed,
                **({"model": model} if model else {}),
                **params.get("extra", {}),
            })
            resp.raise_for_status()
            job = resp.json()
            job_id = job.get("job_id") or job.get("id") or job.get("task_id")
            if not job_id:
                raise ProviderError("云端视频 API 未返回任务 ID", detail=str(job)[:400])

            deadline = time.time() + float(params.get("timeout", 3600))
            video_url = None
            while time.time() < deadline:
                st = client.get(base + status_path.format(job_id=job_id), headers=headers)
                st.raise_for_status()
                data = st.json()
                state = str(data.get("status", "")).upper()
                if progress_cb:
                    progress_cb(int(data.get("progress") or 50), f"云端状态 {state}")
                if state in ("SUCCESS", "SUCCEEDED", "DONE", "COMPLETED"):
                    video_url = data.get("url") or data.get("video_url") or (data.get("output") or {}).get("url")
                    break
                if state in ("FAILED", "ERROR", "CANCELLED"):
                    raise ProviderError(f"云端视频任务失败: {data.get('error') or state}",
                                        retryable=True, detail=str(data)[:600])
                time.sleep(float(params.get("poll_interval", 8)))
            if not video_url:
                raise ProviderError("云端视频任务超时", retryable=True)

            workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="cloud_vid_"))
            workdir.mkdir(parents=True, exist_ok=True)
            out = workdir / f"cloud_{job_id}.mp4"
            out.write_bytes(client.get(video_url).content)

        info = engine.ffprobe(out)
        return GenerationResult(
            file_path=str(out), provider=self.name,
            model=params.get("model") or self.default_model or "cloud-video",
            workflow="cloud_video_generation",
            parameters=params, width=info["width"] or width, height=info["height"] or height,
            duration=info["duration"] or duration, fps=info["fps"] or fps,
            format="mp4", size_bytes=info["size_bytes"], prompt=prompt, seed=seed,
            extra={"job_id": job_id, "video_url": video_url},
            elapsed_ms=int((time.time() - started) * 1000),
        )


def register() -> None:
    registry.register(LocalVideoProvider(), default=True)
    registry.register(ComfyUIVideoProvider())
    registry.register(CloudVideoProvider())
