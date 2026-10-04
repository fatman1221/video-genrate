"""分辨率档位解析。

设计要点
--------
1. **数值只写一次**：所有 (档位, 画幅) → (width, height) 都在这张表里，
   业务层只调用 ``resolve_size``，不再各自算比例（历史上 1280x720 就是被
   ``.env`` 和项目记录各存一份，改一处另一处不动）。
2. **模型不写死**：这里只回答"用户要哪个档位"，能不能做由
   **Provider 能力声明**（``capabilities`` 里的 ``resolution:`` / ``native_resolution:``）
   决定，见 ``generation_plans.check_provider_capability``。
3. **原生档位要留痕**：Qwen-Image 2.1 原生 2K，把它当 4K 用是「超采样」而不是
   「更高画质」—— 所以超过原生档位时返回 warning，让调用方看得见，而不是静默降级。
   （本项目反复踩过「任务 SUCCESS 但产物是假的」这类静默降级。）
"""
from __future__ import annotations

from typing import Any

#: 档位 → 该档位的**长边**像素（按 16:9 基准）。仅用于排序与展示。
RESOLUTION_TIERS: dict[str, int] = {
    "720p": 1280,
    "1080p": 1920,
    "2K": 2752,
    "4K": 3840,
}

#: 档位顺序（从小到大），用于比较"是否超过原生档位"
TIER_ORDER: tuple[str, ...] = ("720p", "1080p", "2K", "4K")

#: 支持的画幅。数值取自 Qwen-Image 2.1 官方推荐尺寸（2K 一行为官方值），
#: 其余档位由官方比例等比推出，并尽量落在 8/16 的整数倍上。
PRESETS: dict[str, dict[str, tuple[int, int]]] = {
    "720p": {
        "16:9": (1280, 720), "9:16": (720, 1280), "1:1": (960, 960),
        "4:3": (960, 720), "3:2": (1080, 720),
    },
    "1080p": {
        "16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1440, 1440),
        "4:3": (1440, 1080), "3:2": (1620, 1080),
    },
    "2K": {
        "16:9": (2752, 1536), "9:16": (1536, 2752), "1:1": (2048, 2048),
        "4:3": (2400, 1792), "3:2": (2528, 1696),
    },
    "4K": {
        "16:9": (3840, 2160), "9:16": (2160, 3840), "1:1": (2880, 2880),
        "4:3": (2880, 2160), "3:2": (3240, 2160),
    },
}

SUPPORTED_ASPECTS: tuple[str, ...] = ("16:9", "9:16", "1:1", "4:3", "3:2")
DEFAULT_ASPECT = "16:9"

#: 当前主力图像模型的原生档位。超过它只发 warning，不阻断
#: —— 用户可能就是要 4K 出图，我们只负责把代价说清楚。
MODEL_NATIVE_TIER = "2K"

_ALIASES = {
    "": "", "none": "", "auto": "", "default": "",
    "720": "720p", "720P": "720p", "HD": "720p",
    "1080": "1080p", "1080P": "1080p", "FHD": "1080p",
    "2K": "2K", "1440P": "2K", "QHD": "2K",
    "4K": "4K", "2160P": "4K", "UHD": "4K",
}


class ResolutionError(ValueError):
    """档位 / 画幅不合法。"""


def normalize_resolution(value: Any, *, allow_empty: bool = True) -> str:
    """把外部传入的档位归一化成 ``720p/1080p/2K/4K``（空值返回 ``""``）。"""
    if value is None:
        if allow_empty:
            return ""
        raise ResolutionError("必须指定 resolution")
    raw = str(value).strip()
    if not raw:
        if allow_empty:
            return ""
        raise ResolutionError("必须指定 resolution")
    key = raw.upper() if raw.upper() in _ALIASES else raw
    if key in _ALIASES:
        tier = _ALIASES[key]
        if tier:
            return tier
        if allow_empty:
            return ""
        raise ResolutionError("必须指定 resolution")
    raise ResolutionError(
        f"未知分辨率档位「{raw}」，可选：{', '.join(TIER_ORDER)}"
    )


def normalize_aspect(value: Any, *, allow_empty: bool = True) -> str:
    """把外部传入的画幅归一化成 ``16:9`` 这类标准写法。"""
    if value is None:
        if allow_empty:
            return DEFAULT_ASPECT
        raise ResolutionError("必须指定 aspect_ratio")
    raw = str(value).strip().replace("：", ":").replace("/", ":").replace("x", ":")
    if not raw:
        if allow_empty:
            return DEFAULT_ASPECT
        raise ResolutionError("必须指定 aspect_ratio")
    parts = raw.split(":")
    if len(parts) == 2:
        try:
            left, right = float(parts[0]), float(parts[1])
        except ValueError:
            left = right = 0.0
        if left > 0 and right > 0:
            ratio = left / right
            for candidate in SUPPORTED_ASPECTS:
                a, b = (float(x) for x in candidate.split(":"))
                if abs(a / b - ratio) < 0.02:
                    return candidate
    raise ResolutionError(
        f"未知画幅「{value}」，可选：{', '.join(SUPPORTED_ASPECTS)}"
    )


def tier_rank(tier: str) -> int:
    """档位序号（未知档位返回 -1）。"""
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return -1


def resolve_size(resolution: Any, aspect_ratio: Any = None,
                 *, fallback: tuple[int, int] = (1280, 720)) -> tuple[int, int]:
    """把（档位, 画幅）解析成具体像素。

    档位为空时返回 ``fallback``（项目默认尺寸），保证"不传就维持现状"。
    """
    tier = normalize_resolution(resolution)
    if not tier:
        return int(fallback[0]), int(fallback[1])
    aspect = normalize_aspect(aspect_ratio)
    preset = PRESETS.get(tier, {}).get(aspect)
    if preset is None:
        raise ResolutionError(f"档位 {tier} 不支持画幅 {aspect}")
    return preset


def describe(resolution: Any, aspect_ratio: Any = None,
             *, fallback: tuple[int, int] = (1280, 720)) -> dict[str, Any]:
    """解析 + 生成给调用方看的口径说明（含"超过原生档位"的 warning）。"""
    tier = normalize_resolution(resolution)
    aspect = normalize_aspect(aspect_ratio)
    width, height = resolve_size(tier, aspect, fallback=fallback)
    warnings: list[str] = []
    if tier and tier_rank(tier) > tier_rank(MODEL_NATIVE_TIER):
        warnings.append(
            f"请求档位 {tier} 高于图像模型原生档位 {MODEL_NATIVE_TIER}"
            f"（原生上限约 {PRESETS[MODEL_NATIVE_TIER]['16:9'][0]}×"
            f"{PRESETS[MODEL_NATIVE_TIER]['16:9'][1]}）。"
            "超出原生分辨率不等于更高画质，可能只是被拉伸；"
            "追求成片可用建议原生出图后用超分链（enhance/upscale）放大。"
        )
    return {
        "resolution": tier,
        "aspect_ratio": aspect,
        "width": width,
        "height": height,
        "megapixels": round(width * height / 1_000_000, 2),
        "native_tier": MODEL_NATIVE_TIER,
        "above_native": bool(tier) and tier_rank(tier) > tier_rank(MODEL_NATIVE_TIER),
        "warnings": warnings,
    }


def native_resolution_of(capabilities: list[str] | tuple[str, ...] | None) -> str:
    """从 Provider 的 ``capabilities`` 里读它声明的原生档位（没声明返回 ``""``）。"""
    for cap in capabilities or []:
        text = str(cap)
        if text.lower().startswith("native_resolution:"):
            raw = text.split(":", 1)[1]
            try:
                return normalize_resolution(raw)
            except ResolutionError:
                return ""
    return ""


def supported_of(capabilities: list[str] | tuple[str, ...] | None) -> list[str]:
    """从 ``capabilities`` 里读它声明支持的档位列表（没声明返回空列表 = 不校验）。"""
    found: set[str] = set()
    for cap in capabilities or []:
        text = str(cap)
        if text.lower().startswith("resolution:"):
            for part in text.split(":", 1)[1].split(","):
                try:
                    tier = normalize_resolution(part.strip())
                except ResolutionError:
                    continue
                if tier:
                    found.add(tier)
    return [t for t in TIER_ORDER if t in found]
