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
from . import runtime


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

    # 连接信息动态读取：设置页改完即时生效，不需要重启进程
    @property
    def base_url(self) -> str:
        return runtime.resolve("image", "comfyui", "base_url", settings.comfyui_base_url).rstrip("/")

    @property
    def api_key(self) -> str:
        return runtime.resolve("image", "comfyui", "api_key", settings.comfyui_api_key)

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return bool(self.base_url) and _http_alive(f"{self.base_url}/system_stats")

    def _upload_input_image(self, client: "httpx.Client", image_path: str,
                            headers: dict[str, str]) -> str:
        """把本地参考图上传到 ComfyUI input 目录，返回 LoadImage 可用的相对文件名。"""
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
                f"参考图上传失败: {resp.status_code}", detail=resp.text[:500])
        info = resp.json()
        name = info.get("name") or p.name
        sub = info.get("subfolder") or ""
        return f"{sub}/{name}" if sub else name

    def generate(self, *, prompt: str, negative_prompt: str = "", width: int = 1280,
                 height: int = 720, seed: int | None = None,
                 reference_image: str | None = None,
                 parameters: dict[str, Any] | None = None) -> GenerationResult:
        params = dict(parameters or {})
        workflow = _resolve_workflow(params, prompt=prompt, negative_prompt=negative_prompt,
                                     width=width, height=height, seed=seed)
        started = time.time()
        # {{reference_image}} 在上传后才能替换（需要 input 目录的相对文件名），
        # 若最终仍未替换则清空占位符，避免 JSON 里留下非法字符串
        resolved = _substitute(workflow, {
            "prompt": prompt, "negative_prompt": negative_prompt,
            "width": width, "height": height, "seed": seed or int(time.time()) % 2**31,
        })
        client_id = f"video-agent-{int(time.time())}"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with httpx.Client(timeout=30) as client:
            if reference_image and Path(reference_image).exists() and "{{reference_image}}" in json.dumps(resolved):
                ref_name = self._upload_input_image(client, reference_image, headers)
                resolved = _substitute(resolved, {"reference_image": ref_name})
            else:
                resolved = _substitute(resolved, {"reference_image": ""})
            resp = client.post(
                f"{self.base_url}/prompt",
                json={"prompt": resolved, "client_id": client_id},
                headers=headers,
            )
            if resp.status_code >= 400:
                raise ProviderError(
                    f"ComfyUI 提交失败: {resp.status_code}",
                    detail=_humanize_comfy_error(resp.text)[:1500],
                )
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
            file_path=str(out), provider=self.name,
            model=params.get("ckpt_name") or runtime.model_of("image", "comfyui") or "comfyui",
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
    def base_url(self) -> str:
        return runtime.resolve("image", "cloud", "base_url", settings.cloud_image_base_url)

    @property
    def api_key(self) -> str:
        return runtime.resolve("image", "cloud", "api_key", settings.cloud_image_api_key)

    @property
    def default_model(self) -> str:
        return runtime.model_of("image", "cloud")

    @property
    def functional(self) -> bool:  # type: ignore[override]
        return bool(self.base_url and self.api_key)

    def generate(self, *, prompt: str, negative_prompt: str = "", width: int = 1280,
                 height: int = 720, seed: int | None = None,
                 reference_image: str | None = None,
                 parameters: dict[str, Any] | None = None) -> GenerationResult:
        if not self.functional:
            raise ProviderError(
                "云端图像 Provider 未配置（请在『系统设置』里填写 Base URL 与 API Key，"
                "或设置 CLOUD_IMAGE_BASE_URL / CLOUD_IMAGE_API_KEY 环境变量）",
                retryable=False,
            )
        started = time.time()
        params = dict(parameters or {})
        body: dict[str, Any] = {
            "prompt": prompt, "negative_prompt": negative_prompt,
            "width": width, "height": height, "seed": seed, **params.get("extra", {}),
        }
        model = params.get("model") or self.default_model
        if model:
            body["model"] = model
        with httpx.Client(timeout=float(params.get("timeout", 600))) as client:
            resp = client.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
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
            file_path=str(out), provider=self.name,
            model=payload.get("model") or model or "cloud-image",
            workflow="cloud_text_to_image", parameters=params,
            width=width, height=height, format="png", size_bytes=out.stat().st_size,
            prompt=prompt, negative_prompt=negative_prompt, seed=seed,
            elapsed_ms=int((time.time() - started) * 1000),
        )


_ALIVE_CACHE: dict[str, tuple[float, bool]] = {}


def _resolve_workflow(params: dict[str, Any], *, prompt: str, negative_prompt: str,
                      width: int, height: int, seed: int | None) -> dict[str, Any]:
    """拿到本次要提交的工作流。

    优先级：
    1. `parameters.workflow_json` —— 调用方直接给的完整工作流（已渲染或含占位符）
    2. `parameters.workflow_name` —— 模板名，走模板管理器渲染
       （Skill 层只需说 `workflow_name="qwen_image_character"`）

    两者都没有时抛出可读错误，并列出当前可用模板 —— 这是最常见的接入卡点，
    错误信息必须直接告诉人怎么办，而不是只丢一句"缺少参数"。
    """
    inline = params.get("workflow_json")
    if inline:
        return inline

    name = (params.get("workflow_name") or "").strip()
    if not name:
        from ..workflows import list_templates

        available = ", ".join(t.key for t in list_templates()) or "<无内置模板>"
        raise ProviderError(
            "ComfyUI 出图需要 workflow_json 或 workflow_name 之一",
            retryable=False,
            detail=(
                f"可用模板：{available}。"
                "用法：调用 generate_image 时传 workflow_name=\"qwen_image_character\"，"
                "或传 workflow_json=<API 格式工作流 dict>。"
            ),
        )

    from ..workflows import WorkflowTemplateError, get_template

    try:
        template = get_template(name)
    except WorkflowTemplateError as exc:
        raise ProviderError(str(exc), retryable=False) from exc

    return template.render(
        prompt=prompt, negative_prompt=negative_prompt,
        width=width, height=height, seed=seed or int(time.time()) % 2**31,
    )


def _humanize_comfy_error(body: str) -> str:
    """把 ComfyUI 的 node_errors 原文压成一句能直接照做的提示。"""
    try:
        payload = json.loads(body)
    except Exception:
        return body
    parts: list[str] = []
    err = payload.get("error")
    if isinstance(err, dict):
        parts.append(f"{err.get('type', 'error')}: {err.get('message', '')}")
    elif err:
        parts.append(str(err))
    node_errors = payload.get("node_errors") or {}
    for node_id, info in node_errors.items():
        if not isinstance(info, dict):
            continue
        cls = info.get("class_type", "?")
        for e in info.get("errors") or []:
            msg = e.get("message") or e.get("details") or str(e)
            parts.append(f"节点 {node_id}({cls}): {msg}")
    if not parts:
        return body
    return " | ".join(parts)


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
