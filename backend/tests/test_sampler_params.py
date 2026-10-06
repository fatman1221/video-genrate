"""采样参数可配 + 真实 seed 落库 —— 回归用例。

背景（2026-10-06 实测）：
    在 ComfyUI 里调好的那套参数（28 步 / cfg 4.0 / 固定 seed）在 Studio 里**复刻不出来**，
    因为 ① steps、cfg 写死在模板 JSON 里；② ComfyUI 通道提交用的随机 seed 从不落库。
    结果是「出了好图也复现不了」，而且 studio 出图与直连出图对不上却说不出差在哪。

这组用例锁死三件事：
    1. 模板占位符：默认值兜底、显式入参覆盖、字符串可转数字、None 不算覆盖；
    2. 落库留痕：`Asset.parameters` 里的 steps/cfg/seed 是**实际提交值**，不是入参回声；
    3. seed=0 这种合法种子不会被 `or` 吃掉换成随机值。
"""
from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from app.executors.handlers import _flag, _sampler_overrides
from app.providers.image_providers import (
    ComfyUIImageProvider,
    _extract_sampler_params,
    _resolve_workflow,
)
from app.workflows import get_template


# --------------------------------------------------------------------------- #
# ① 模板层：占位符与默认值
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key,default_steps,default_cfg", [
    ("qwen_image_scene", 20, 2.5),
    ("qwen_image_character", 20, 2.5),
    ("qwen_edit_scene", 30, 2.5),
])
def test_template_declares_sampler_placeholders(key, default_steps, default_cfg):
    tpl = get_template(key)
    assert "steps" in tpl.placeholders, f"{key} 未把 steps 做成占位符"
    assert "cfg" in tpl.placeholders, f"{key} 未把 cfg 做成占位符"
    assert tpl.defaults["steps"] == default_steps
    assert tpl.defaults["cfg"] == default_cfg


def _ksampler(workflow: dict) -> dict:
    for node in workflow.values():
        if isinstance(node, dict) and node.get("class_type") == "KSampler":
            return node["inputs"]
    raise AssertionError("工作流里没有 KSampler 节点")


def test_template_defaults_apply_when_not_specified():
    """不传 steps/cfg 时回落到模板默认值，行为与改动前完全一致。"""
    wf = get_template("qwen_image_scene").render(
        prompt="P", negative_prompt="N", width=1280, height=720, seed=1)
    inputs = _ksampler(wf)
    assert inputs["steps"] == 20
    assert inputs["cfg"] == 2.5


def test_template_caller_overrides_defaults():
    wf = get_template("qwen_image_scene").render(
        prompt="P", negative_prompt="N", steps=28, cfg=4.0)
    inputs = _ksampler(wf)
    assert inputs["steps"] == 28
    assert inputs["cfg"] == 4.0
    # 覆盖 steps/cfg 不应破坏其它占位符
    assert inputs["sampler_name"] == "euler"
    assert inputs["scheduler"] == "simple"


def test_template_coerces_string_numbers_and_keeps_int_type():
    """从 .env / 项目 extra / HTTP 入参来的可能是字符串，必须转成数字。

    ComfyUI 对 steps/cfg 做数值校验，字符串会直接报错；而且 int 不能被转成 "28"。
    """
    wf = get_template("qwen_image_scene").render(
        prompt="P", negative_prompt="N", steps="28", cfg="4.0", width="1280")
    inputs = _ksampler(wf)
    assert inputs["steps"] == 28 and isinstance(inputs["steps"], int)
    assert inputs["cfg"] == 4.0 and isinstance(inputs["cfg"], float)
    assert wf["6"]["inputs"]["width"] == 1280


def test_template_treats_none_as_unspecified():
    """显式传 None = 「没指定」，不能把 None 写进 JSON（会让 ComfyUI 报类型错）。"""
    wf = get_template("qwen_image_scene").render(
        prompt="P", negative_prompt="N", steps=None, cfg=None)
    inputs = _ksampler(wf)
    assert inputs["steps"] == 20
    assert inputs["cfg"] == 2.5


def test_other_templates_unaffected():
    """没有声明占位符的模板保持字面量 —— 改动不能波及到它。"""
    tpl = get_template("sdxl_ipadapter_scene")
    assert "steps" not in tpl.placeholders
    inputs = _ksampler(tpl.render(prompt="P", negative_prompt="N", steps=99, cfg=9.0))
    assert inputs["steps"] == 30 and inputs["cfg"] == 5.5


# --------------------------------------------------------------------------- #
# ② 参数解析与抄回
# --------------------------------------------------------------------------- #
def test_sampler_overrides_priority():
    """单次任务 > 项目固化；都没给则返回 None，交给模板默认值。"""
    assert _sampler_overrides({"steps": 28, "cfg": 4.0},
                              {"image_steps": 12, "image_cfg": 1.5}) == (28, 4.0)
    assert _sampler_overrides({}, {"image_steps": 12, "image_cfg": 1.5}) == (12, 1.5)
    assert _sampler_overrides({}, {}) == (None, None)


@pytest.mark.parametrize("value,expected", [
    (None, True),          # 未设置 → 默认开
    (True, True),
    (False, False),
    ("false", False),      # 手写配置常写成字符串，不能静默取反
    ("False", False),
    ("0", False),
    ("off", False),
    ("关闭", False),
    ("true", True),
])
def test_flag_parses_common_truthy_falsy_spellings(value, expected):
    assert _flag(value, True) is expected


def test_extract_sampler_params_records_effective_values():
    """留痕取的是**实际提交值**，不是入参回声 —— 模板默认值也算数。"""
    resolved = get_template("qwen_image_scene").render(
        prompt="P", negative_prompt="N", width=1280, height=720, seed=7)
    picked = _extract_sampler_params(resolved)
    assert picked["steps"] == 20 and picked["cfg"] == 2.5
    assert picked["sampler_name"] == "euler"
    assert picked["seed"] == 7
    # model/positive 这类 list 引用必须被跳过，不能污染落库参数
    assert all(not isinstance(v, (list, dict)) for v in picked.values())


def test_resolve_workflow_forwards_sampler_overrides():
    wf = _resolve_workflow({"workflow_name": "qwen_image_scene", "steps": 28, "cfg": 4.0},
                           prompt="P", negative_prompt="N",
                           width=1280, height=720, seed=3)
    inputs = _ksampler(wf)
    assert (inputs["steps"], inputs["cfg"], inputs["seed"]) == (28, 4.0, 3)


# --------------------------------------------------------------------------- #
# ③ Provider 端到端（打桩 HTTP，不碰 GPU/ComfyUI）
# --------------------------------------------------------------------------- #
def _png_bytes(w: int = 1280, h: int = 720) -> bytes:
    """造一张和真实出图同尺寸的 PNG。

    刻意不用 1x1：落库的 width/height 取的是**实际解码尺寸**而不是入参，
    尺寸太小会让「落库尺寸正确」这条断言失去意义。
    """
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 40, 60)).save(buf, "PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, payload=None, content: bytes = b"", status_code: int = 200):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


class _FakeComfyClient:
    """伪造 ComfyUI 的 /prompt -> /history -> /view 三跳，并留下提交原文。"""

    def __init__(self, **_kw):
        self.submitted: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def post(self, url, json=None, **_kw):  # noqa: A002 —— 对齐 httpx 签名
        assert url.endswith("/prompt"), url
        self.submitted = json["prompt"]
        return _FakeResponse(payload={"prompt_id": "pid-test"})

    def get(self, url, **_kw):
        if "/history/" in url:
            return _FakeResponse(payload={"pid-test": {"outputs": {
                "9": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]},
            }}})
        if "/view" in url:
            return _FakeResponse(content=_png_bytes())
        raise AssertionError(f"未预期的 GET {url}")


@pytest.fixture()
def comfy(monkeypatch, tmp_path):
    """打桩 httpx.Client，返回 (provider, 可读取提交内容的 fake client)。"""
    fake = _FakeComfyClient()
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: fake)
    return ComfyUIImageProvider(), fake, tmp_path


def _generate(provider, fake, tmp_path, *, seed=None, steps=None, cfg=None):
    params = {"workflow_name": "qwen_image_scene", "workdir": str(tmp_path)}
    if steps is not None:
        params["steps"] = steps
    if cfg is not None:
        params["cfg"] = cfg
    return provider.generate(prompt="测试提示词", negative_prompt="负向",
                             width=1280, height=720, seed=seed, parameters=params)


def test_actual_seed_is_persisted(comfy):
    """核心修复：提交用的随机 seed 必须落库，否则产物不可复现。"""
    provider, fake, tmp_path = comfy
    result = _generate(provider, fake, tmp_path, seed=None)

    submitted_seed = fake.submitted["7"]["inputs"]["seed"]
    assert isinstance(submitted_seed, int)
    # 三处必须一致：返回结构、落库参数、真正提交给 ComfyUI 的值
    assert result.seed == submitted_seed
    assert result.parameters["seed"] == submitted_seed


def test_explicit_seed_is_honoured(comfy):
    provider, fake, tmp_path = comfy
    result = _generate(provider, fake, tmp_path, seed=20261006)
    assert fake.submitted["7"]["inputs"]["seed"] == 20261006
    assert result.seed == 20261006
    assert result.parameters["seed"] == 20261006


def test_zero_seed_is_not_treated_as_missing(comfy):
    """seed=0 是合法种子：历史写法 `seed or random` 会把它悄悄换成随机值。"""
    provider, fake, tmp_path = comfy
    result = _generate(provider, fake, tmp_path, seed=0)
    assert fake.submitted["7"]["inputs"]["seed"] == 0
    assert result.seed == 0
    assert result.parameters["seed"] == 0


def test_overrides_reach_comfyui_and_are_recorded(comfy):
    """打桩之外，还要确认覆盖值真的进了提交的工作流、并落库留痕。"""
    provider, fake, tmp_path = comfy
    result = _generate(provider, fake, tmp_path, steps=28, cfg=4.0)

    inputs = fake.submitted["7"]["inputs"]
    assert (inputs["steps"], inputs["cfg"]) == (28, 4.0)
    assert (result.parameters["steps"], result.parameters["cfg"]) == (28, 4.0)


def test_defaults_are_recorded_when_no_override(comfy):
    """没传覆盖值时，落库记的是模板默认值 —— 复盘时不至于「不知道跑了几步」。"""
    provider, fake, tmp_path = comfy
    result = _generate(provider, fake, tmp_path)
    assert (result.parameters["steps"], result.parameters["cfg"]) == (20, 2.5)
    assert result.parameters["width"] == 1280
    assert result.parameters["height"] == 720
