"""分辨率档位（Phase 9）：单一事实来源 + 别名归一 + 能力闸门。

设计立场：
- 数值只在 ``PRESETS`` 里写一次，业务层不许自己算比例
- 能不能做由 **Provider 能力声明** 决定，模型名不写死在校验代码里
- 超过原生档位 **放行但告警** —— 不静默降级，也不粗暴阻断
"""
from __future__ import annotations

import pytest

from app.services import generation_plans as gp
from app.services import resolution as res


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("720p", "720p"), ("720", "720p"), ("HD", "720p"),
        ("1080p", "1080p"), ("FHD", "1080p"),
        ("2K", "2K"), ("1440p", "2K"), ("QHD", "2K"),
        ("4K", "4K"), ("2160p", "4K"), ("UHD", "4K"),
        ("", ""), (None, ""), ("auto", ""),
    ],
)
def test_normalize_resolution(raw, expected):
    assert res.normalize_resolution(raw) == expected


def test_unknown_resolution_rejected():
    with pytest.raises(res.ResolutionError, match="未知分辨率档位"):
        res.normalize_resolution("8K")


def test_required_resolution_cannot_be_empty():
    with pytest.raises(res.ResolutionError):
        res.normalize_resolution("", allow_empty=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("16:9", "16:9"), ("9:16", "9:16"), ("1:1", "1:1"),
     ("4:3", "4:3"), ("3:2", "3:2"), ("3/2", "3:2"), ("16x9", "16:9")],
)
def test_normalize_aspect(raw, expected):
    assert res.normalize_aspect(raw) == expected


def test_unknown_aspect_rejected():
    with pytest.raises(res.ResolutionError, match="未知画幅"):
        res.normalize_aspect("21:9")


def test_empty_resolution_keeps_fallback():
    """不传档位 = 维持现状（这是向后兼容的关键）。"""
    assert res.resolve_size("", None, fallback=(1280, 720)) == (1280, 720)
    assert res.resolve_size(None, None, fallback=(1920, 1080)) == (1920, 1080)


def test_native_2k_preset_matches_official_sizes():
    """2K 那一行必须与 Qwen-Image 2.1 官方推荐尺寸一致。"""
    assert res.PRESETS["2K"]["16:9"] == (2752, 1536)
    assert res.PRESETS["2K"]["1:1"] == (2048, 2048)
    assert res.PRESETS["2K"]["4:3"] == (2400, 1792)
    assert res.PRESETS["2K"]["3:2"] == (2528, 1696)


def test_describe_reports_above_native():
    info = res.describe("4K", "16:9")
    assert (info["width"], info["height"]) == (3840, 2160)
    assert info["above_native"] is True
    assert info["warnings"], "超原生必须给出可见告警"
    assert "超分" in info["warnings"][0] or "拉伸" in info["warnings"][0]


def test_describe_native_no_warning():
    info = res.describe("2K", "16:9")
    assert info["above_native"] is False
    assert info["warnings"] == []


def test_describe_without_tier_is_silent():
    info = res.describe(None, None, fallback=(1280, 720))
    assert info["resolution"] == ""
    assert info["warnings"] == []
    assert (info["width"], info["height"]) == (1280, 720)


def test_tier_rank_ordering():
    assert res.tier_rank("720p") < res.tier_rank("1080p") < res.tier_rank("2K") < res.tier_rank("4K")
    assert res.tier_rank("nope") == -1


def test_supported_of_parses_declaration():
    caps = ["text_to_image", "resolution:720p,1080p,2K,4K", "native_resolution:2K"]
    assert res.supported_of(caps) == ["720p", "1080p", "2K", "4K"]
    assert res.native_resolution_of(caps) == "2K"


def test_supported_of_empty_when_undeclared():
    assert res.supported_of(["text_to_image"]) == []
    assert res.native_resolution_of(["text_to_image"]) == ""


# --------------------------------------------------------------------------- #
# 与能力闸门联动
# --------------------------------------------------------------------------- #
def test_gate_rejects_unsupported_tier(db):
    ok, note = gp.check_provider_capability(db, "image", "comfyui", "8K")
    assert ok is False and "8K" in note


def test_gate_warns_above_native(db):
    ok, note = gp.check_provider_capability(db, "image", "comfyui", "4K")
    assert ok is True and note and "超采样" in note


def test_gate_accepts_native_tier(db):
    ok, note = gp.check_provider_capability(db, "image", "comfyui", "2K")
    assert ok is True and note == ""


def test_gate_ignores_provider_without_declaration(db):
    """查不到 provider → 跳过校验。**不要把「查不到」当成「不支持」**，否则会误伤。"""
    ok, _note = gp.check_provider_capability(db, "image", "missing-provider", "4K")
    assert ok is True
