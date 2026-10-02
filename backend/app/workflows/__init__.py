"""ComfyUI 工作流模板管理。

为什么需要这一层：
    `ComfyUIImageProvider` 要求调用方传入 `parameters.workflow_json`，但 Skill 层
    原本没有任何入参能把它透传下来 —— ComfyUI 通道因此在 API 上不可达。

    这里把「工作流 JSON」从「每次调用都要手写的一大坨参数」变成「一个有名字的模板」，
    Skill 只需要说 `workflow_name="qwen_image_character"`，剩下的由模板管理器负责：
    读取 → 校验 → 替换占位符 → 交给 Provider。

占位符约定（与 providers/image_providers.py 的 `_substitute` 保持一致）：
    {{prompt}}  {{negative_prompt}}  {{width}}  {{height}}  {{seed}}

    - 整个字符串就是 `{{key}}` 时：原样替换为对应的**非字符串**值（保留 int 类型，
      ComfyUI 的 width/height/seed 必须是数字，不能是 "512" 这种字符串）。
    - 字符串中内嵌 `{{key}}` 时：做文本替换，结果是字符串。

模板存放位置：
    backend/app/workflows/templates/*.json      （随源码走，随仓库分发）
    也支持从 `backend/.env` 的 COMFYUI_WORKFLOW_DIR 指定的外部目录读取，
    便于放「本机专属、不便入库」的大工作流。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

BUILTIN_DIR = Path(__file__).resolve().parent / "templates"

#: 模板里会被替换的占位符；用于校验与提示
PLACEHOLDER_KEYS = ("prompt", "negative_prompt", "width", "height", "seed")


class WorkflowTemplateError(RuntimeError):
    """模板不存在 / 格式非法。"""


@dataclass
class WorkflowTemplate:
    """一个可复用的 ComfyUI 工作流模板。"""

    key: str                      # 模板标识，如 qwen_image_character
    path: Path
    raw: dict[str, Any]
    description: str = ""
    kind: str = "image"           # image / video
    #: 模板实际用到的占位符，自动扫描得出
    placeholders: tuple[str, ...] = field(default_factory=tuple)
    #: 模板里的模型文件名（供排查"模型名对不上"这类问题）
    models: tuple[str, ...] = field(default_factory=tuple)
    #: 视频模板可声明帧数上限（如 MiniMax H3 训练范围 124~362 帧）；
    #: 超长镜头由 Provider 生成到上限后用 ffmpeg 减速补齐时长
    max_frames: int | None = None

    def render(self, *, prompt: str = "", negative_prompt: str = "",
               width: int | None = None, height: int | None = None,
               seed: int | None = None, **extra: Any) -> dict[str, Any]:
        """把占位符替换成真实值，返回可直接 POST /prompt 的 workflow dict。

        返回的是深拷贝，调用方改动不会污染模板缓存。
        """
        mapping: dict[str, Any] = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
        }
        if width is not None:
            mapping["width"] = int(width)
        if height is not None:
            mapping["height"] = int(height)
        if seed is not None:
            mapping["seed"] = int(seed)
        mapping.update(extra)
        return _substitute(this := copy.deepcopy(self.raw), mapping) or this

    def to_meta(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "description": self.description,
            "placeholders": list(self.placeholders),
            "models": list(self.models),
            "path": str(self.path),
        }


def _substitute(node: Any, mapping: dict[str, Any]) -> Any:
    """递归替换 {{placeholder}}。

    与 providers.image_providers._substitute 语义一致，但额外支持
    「整个字符串即占位符」时保留原始类型（int 仍是 int）。
    """
    if isinstance(node, dict):
        return {k: _substitute(v, mapping) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, mapping) for v in node]
    if isinstance(node, str):
        stripped = node.strip()
        if stripped.startswith("{{") and stripped.endswith("}}"):
            key = stripped[2:-2].strip()
            if key in mapping:
                return mapping[key]
            return node
        out = node
        for key, value in mapping.items():
            out = out.replace(f"{{{{{key}}}}}", str(value))
        return out
    return node


def _scan_placeholders(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for v in node.values():
            _scan_placeholders(v, found)
    elif isinstance(node, list):
        for v in node:
            _scan_placeholders(v, found)
    elif isinstance(node, str):
        for key in PLACEHOLDER_KEYS:
            if f"{{{{{key}}}}}" in node:
                found.add(key)


def _scan_models(node: Any, found: set[str]) -> None:
    """粗略提取模型文件名，方便排查"模型不存在"类报错。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("ckpt_name", "unet_name", "model_name", "lora_name", "vae_name",
                     "clip_name", "clip_name1", "clip_name2", "gguf_name") and isinstance(v, str):
                found.add(v)
            else:
                _scan_models(v, found)
    elif isinstance(node, list):
        for v in node:
            _scan_models(v, found)


def _load_one(path: Path) -> WorkflowTemplate:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowTemplateError(f"工作流模板不是合法 JSON：{path} —— {exc}") from exc
    meta = raw.pop("__meta__", {}) if isinstance(raw, dict) else {}
    found: set[str] = set()
    _scan_placeholders(raw, found)
    models: set[str] = set()
    _scan_models(raw, models)
    return WorkflowTemplate(
        key=meta.get("key") or path.stem,
        path=path,
        raw=raw,
        description=meta.get("description", ""),
        kind=meta.get("kind", "image"),
        placeholders=tuple(sorted(found)),
        models=tuple(sorted(models)),
        max_frames=meta.get("max_frames"),
    )


@lru_cache(maxsize=1)
def _discover() -> tuple[tuple[str, WorkflowTemplate], ...]:
    """扫描内置目录 + COMFYUI_WORKFLOW_DIR，后者同名时覆盖前者。"""
    from ..config import settings

    dirs: list[Path] = [BUILTIN_DIR]
    extra = (settings.comfyui_workflow_dir or "").strip()
    if extra:
        dirs.append(Path(extra).expanduser())
    elif (backend_env_dir := BUILTIN_DIR.parent.parent / "workflows").is_dir():
        # backend/workflows/ —— 便于放本机专属工作流而不动源码目录
        dirs.append(backend_env_dir)

    found: dict[str, WorkflowTemplate] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            tpl = _load_one(f)
            found[tpl.key] = tpl
    return tuple(found.items())


def refresh() -> None:
    """清缓存，热更新模板（新增/修改 json 后调用）。"""
    _discover.cache_clear()


def list_templates(kind: str | None = None) -> list[WorkflowTemplate]:
    items = [t for _, t in _discover()]
    if kind:
        items = [t for t in items if t.kind == kind]
    return items


def get_template(key: str) -> WorkflowTemplate:
    for k, t in _discover():
        if k == key:
            return t
    available = ", ".join(k for k, _ in _discover()) or "<无>"
    raise WorkflowTemplateError(f"工作流模板不存在：{key}（可用：{available}）")


def has_template(key: str) -> bool:
    return any(k == key for k, _ in _discover())
