"""图像生成 Provider。

- LocalImageProvider：内置可运行的漫画分镜渲染引擎（PIL），离线可用
- ComfyUIImageProvider：真实 ComfyUI HTTP API 适配器（/prompt -> /history -> /view）
- CloudImageProvider：通用云端文生图 HTTP 适配器

业务层只调用 ImageProvider.generate，不感知底层实现。
"""
from __future__ import annotations

import json
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from .base import GenerationResult, ImageProvider, ProviderError, registry
from . import local_engine as engine


class LocalImageProvider(ImageProvider):
    name = "local"
    display_name = "本地渲染引擎（PIL 漫画分镜）"
    capabilities = ("text_to_image", "storyboard_frame", "offline", "fast")
    doc = "内置引擎，无需任何外部依赖，生成漫画教学风格分镜关键帧。"

    def generate(self, *, prompt: str, negative_prompt: str = "", width: int = 1280,
                 height: int = 720, seed: int | None = None,
                 reference_image: str | None = None,
                 parameters: dict[str, Any] | None = None) -> GenerationResult:
        started = time.time()
        params = dict(parameters or {})
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="imggen_"))
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"frame_{int(time.time() * 1000)}.png"

        engine.make_storyboard_image(
            out_path=str(out),
            width=int(width or settings.default_image_width),
            height=int(height or settings.default_image_height),
            title=params.get("title") or prompt,
            body=params.get("body") or params.get("subtitle") or "",
            badge=params.get("badge") or "",
            seed_key=f"{prompt}|{seed}|{params.get('shot_code', '')}",
            style_tag=params.get("style_tag") or "",
        )
        with engine.Image.open(out) as im:
            w, h = im.size
        return GenerationResult(
            file_path=str(out),
            provider=self.name,
            model="pil-storyboard-v1",
            workflow="local_storyboard_render",
            parameters={**params, "width": w, "height": h, "seed": seed},
            width=w, height=h, format="png", size_bytes=out.stat().st_size,
            prompt=prompt, negative_prompt=negative_prompt, seed=seed,
            extra={"reference_image": reference_image},
            elapsed_ms=int((time.time() - started) * 1000),
        )


class ComfyUIImageProvider(ImageProvider):
    name = "comfyui"
    display_name = "ComfyUI（本地图形工作流）"
    requires_api_key = False
    capabilities = ("text_to_image", "image_to_image", "workflow_json", "lora", "controlnet")
    doc = "通过 ComfyUI HTTP API 提交工作流。需在设置中配置 COMFYUI_BASE_URL 并保持服务运行。"

    def __init__(self) -> None:
        self.base_url = settings.comfyui_base_url.rstrip("/")
        self.api_key = settings.comfyui_api_key

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return bool(self.base_url) and _http_alive(f"{self.base_url}/system_stats")

    def generate(self, *, prompt: str, negative_prompt: str = "", width: int = 1280,
                 height: int = 720, seed: int | None = None,
                 reference_image: str | None = None,
                 parameters: dict[str, Any] | None = None) -> GenerationResult:
        params = dict(parameters or {})
        workflow = params.get("workflow_json")
        if not workflow:
            raise ProviderError(
                "ComfyUIProvider 需要 parameters.workflow_json（导出的 API 格式工作流）",
                retryable=False,
                detail="可在 ComfyUI 中使用『Save (API Format)』导出，并把占位符写成 {{prompt}} / {{width}} / {{height}} / {{seed}}。",
            )
        started = time.time()
        resolved = _substitute(workflow, {
            "prompt": prompt, "negative_prompt": negative_prompt,
            "width": width, "height": height, "seed": seed or int(time.time()) % 2**31,
        })
        client_id = f"video-agent-{int(time.time())}"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{self.base_url}/prompt",
                json={"prompt": resolved, "client_id": client_id},
                headers=headers,
            )
            if resp.status_code >= 400:
                raise ProviderError(f"ComfyUI 提交失败: {resp.status_code}", detail=resp.text[:800])
            prompt_id = resp.json().get("prompt_id")
            if not prompt_id:
                raise ProviderError("ComfyUI 未返回 prompt_id", detail=resp.text[:400])

            deadline = time.time() + float(params.get("timeout", 900))
            images: list[dict[str, Any]] = []
            while time.time() < deadline:
                hist = client.get(f"{self.base_url}/history/{prompt_id}", headers=headers)
                if hist.status_code == 200:
                    data = hist.json().get(prompt_id) or {}
                    outputs = data.get("outputs") or {}
                    for node in outputs.values():
                        images.extend(node.get("images") or [])
                    if images:
                        break
                time.sleep(2)
            if not images:
                raise ProviderError("ComfyUI 生成超时或无输出", retryable=True)

            workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="comfy_"))
            workdir.mkdir(parents=True, exist_ok=True)
            first = images[0]
            query = urllib.parse.urlencode({
                "filename": first.get("filename", ""),
                "subfolder": first.get("subfolder", ""),
                "type": first.get("type", "output"),
            })
            img_resp = client.get(f"{self.base_url}/view?{query}", headers=headers)
            if img_resp.status_code >= 400:
                raise ProviderError(f"ComfyUI 下载结果失败: {img_resp.status_code}")
            out = workdir / f"comfy_{prompt_id}.png"
            out.write_bytes(img_resp.content)

        w, h = 0, 0
        try:
            with engine.Image.open(out) as im:
                w, h = im.size
        except Exception:  # pragma: no cover
            pass
        return GenerationResult(
            file_path=str(out), provider=self.name, model=params.get("ckpt_name", "comfyui"),
            workflow=params.get("workflow_name", "comfyui_default"), parameters=params,
            width=w, height=h, format=out.suffix.lstrip("."), size_bytes=out.stat().st_size,
            prompt=prompt, negative_prompt=negative_prompt, seed=seed,
            extra={"prompt_id": prompt_id, "images": images},
            elapsed_ms=int((time.time() - started) * 1000),
        )


class CloudImageProvider(ImageProvider):
    name = "cloud"
    display_name = "云端图像 API（通用 HTTP 适配器）"
    requires_api_key = True
    capabilities = ("text_to_image", "image_to_image", "high_quality")
    doc = "通用云端适配器：POST {CLOUD_IMAGE_BASE_URL} 提交 {prompt,width,height,seed}，返回 {url|b64_json}。"

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return bool(settings.cloud_image_base_url and settings.cloud_image_api_key)

    def generate(self, *, prompt: str, negative_prompt: str = "", width: int = 1280,
                 height: int = 720, seed: int | None = None,
                 reference_image: str | None = None,
                 parameters: dict[str, Any] | None = None) -> GenerationResult:
        if not self.functional:
            raise ProviderError(
                "云端图像 Provider 未配置（需要 CLOUD_IMAGE_BASE_URL / CLOUD_IMAGE_API_KEY）",
                retryable=False,
            )
        started = time.time()
        params = dict(parameters or {})
        with httpx.Client(timeout=float(params.get("timeout", 600))) as client:
            resp = client.post(
                settings.cloud_image_base_url,
                headers={"Authorization": f"Bearer {settings.cloud_image_api_key}"},
                json={"prompt": prompt, "negative_prompt": negative_prompt,
                      "width": width, "height": height, "seed": seed, **params.get("extra", {})},
            )
            resp.raise_for_status()
            payload = resp.json()
        url = payload.get("url") or (payload.get("data") or [{}])[0].get("url")
        if not url:
            raise ProviderError("云端图像 API 未返回可下载 URL", detail=json.dumps(payload)[:500])
        workdir = Path(params.get("workdir") or tempfile.mkdtemp(prefix="cloud_img_"))
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "cloud_image.png"
        with httpx.Client(timeout=300) as client:
            out.write_bytes(client.get(url).content)
        return GenerationResult(
            file_path=str(out), provider=self.name, model=payload.get("model", "cloud-image"),
            workflow="cloud_text_to_image", parameters=params,
            width=width, height=height, format="png", size_bytes=out.stat().st_size,
            prompt=prompt, negative_prompt=negative_prompt, seed=seed,
            elapsed_ms=int((time.time() - started) * 1000),
        )


_ALIVE_CACHE: dict[str, tuple[float, bool]] = {}


def _http_alive(url: str, timeout: float = 1.5, ttl: float = 60.0) -> bool:
    """带 TTL 缓存的可用性探测，避免 /api/providers 每次都探网。"""
    now = time.time()
    cached = _ALIVE_CACHE.get(url)
    if cached and now - cached[0] < ttl:
        return cached[1]
    try:
        with httpx.Client(timeout=timeout) as client:
            alive = client.get(url).status_code < 500
    except Exception:
        alive = False
    _ALIVE_CACHE[url] = (now, alive)
    return alive


def _substitute(node: Any, mapping: dict[str, Any]) -> Any:
    """递归替换工作流 JSON 中的 {{placeholder}} 占位符。"""
    if isinstance(node, dict):
        return {k: _substitute(v, mapping) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, mapping) for v in node]
    if isinstance(node, str):
        out: Any = node
        if node.startswith("{{") and node.endswith("}}"):
            key = node[2:-2].strip()
            return mapping.get(key, node)
        for key, value in mapping.items():
            out = str(out).replace(f"{{{{{key}}}}}", str(value))
        return out
    return node


def register() -> None:
    registry.register(LocalImageProvider(), default=True)
    registry.register(ComfyUIImageProvider())
    registry.register(CloudImageProvider())
