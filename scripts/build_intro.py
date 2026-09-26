#!/usr/bin/env python3
"""把截图裁切、压缩并以内嵌 base64 注入 intro.html（让介绍页单文件自包含）。

可重复执行：先按 <img alt> 把已内嵌的 data URI 还原成占位符，再重新内嵌。

## 为什么要裁

原始截图宽高比差异很大（0.95 ~ 2.02），直接放进等高卡片会高矮不齐。
两步处理：
  1. `box`  —— 可选，先按相对坐标裁出「真正想展示的那一块」
     （例如成片截图只保留播放器与规格行，避开旧版紫色顶栏）；
  2. `ratio`/`anchor` —— 再统一裁到目标宽高比，anchor 决定保留哪一边。

用法：
    env -u PYTHONPATH python scripts/build_intro.py
"""
from __future__ import annotations

import base64
import io
import pathlib
import re
import sys

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "intro.html"

# anchor：0 = 贴左（上）边，0.5 = 居中，1 = 贴右（下）边
SLOTS: tuple[dict, ...] = (
    {
        "token": "__IMG_PIPELINE__", "alt": "项目详情：节点式生产流水线",
        "src": "docs/screenshots/11_detail_new.png",
        "width": 1400, "box": None, "ratio": 1.6, "anchor": (0.5, 0.0), "quality": 80,
    },
    {
        # 抽屉在右侧，锚右侧才留得住面板本体
        "token": "__IMG_STAGE__", "alt": "节点操作面板",
        "src": "docs/screenshots/13_stage_actions.png",
        "width": 1400, "box": None, "ratio": 1.6, "anchor": (1.0, 0.5), "quality": 80,
    },
    {
        # 卡片在左侧，锚左侧
        "token": "__IMG_SERIES_LIST__", "alt": "连续剧列表",
        "src": "docs/screenshots/20_series_list.png",
        "width": 1400, "box": None, "ratio": 1.6, "anchor": (0.0, 0.5), "quality": 80,
    },
    {
        "token": "__IMG_SERIES_DETAIL__", "alt": "连续剧详情",
        "src": "docs/screenshots/21_series_detail.png",
        "width": 1400, "box": None, "ratio": 1.6, "anchor": (0.5, 0.0), "quality": 80,
    },
    {
        # 这张是旧版 UI（紫色顶栏 / 13 个 Tab），只留「成片播放器 + 规格行」
        "token": "__IMG_FINAL__", "alt": "成片验证截图",
        "src": "docs/screenshots/04_final.png",
        "width": 1100, "box": (0.015, 0.46, 0.578, 0.83), "ratio": 1.45,
        "anchor": (0.5, 0.5), "quality": 82,
    },
)


def restore_placeholders(html: str) -> str:
    """把已经内嵌的 data URI 还原为占位符，让脚本可重复执行。"""
    for slot in SLOTS:
        pattern = re.compile(
            r'<img src="data:image/jpeg;base64,[^"]*" alt="' + re.escape(slot["alt"]) + r'"'
        )
        html, n = pattern.subn(f'<img src="{slot["token"]}" alt="{slot["alt"]}"', html)
        if n:
            print(f"  · 还原 {slot['alt']}")
    return html


def crop_box(im: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    """按相对坐标裁出目标区域。"""
    w, h = im.size
    x0, y0, x1, y1 = box
    return im.crop((round(w * x0), round(h * y0), round(w * x1), round(h * y1)))


def crop_to_ratio(im: Image.Image, ratio: float, ax: float, ay: float) -> Image.Image:
    """按目标宽高比裁掉多余部分，保留以 anchor 定位的那一块。"""
    w, h = im.size
    if abs(w / h - ratio) < 0.01:
        return im
    if w / h > ratio:  # 太宽 -> 裁左右
        nw, nh = round(h * ratio), h
    else:  # 太高 -> 裁上下
        nw, nh = w, round(w / ratio)
    left = round((w - nw) * ax)
    top = round((h - nh) * ay)
    return im.crop((left, top, left + nw, top + nh))


def encode(slot: dict) -> tuple[str, int, int]:
    path = ROOT / slot["src"]
    if not path.exists():
        raise SystemExit(f"缺少截图: {path}")
    im = Image.open(path).convert("RGB")
    if slot["box"]:
        im = crop_box(im, slot["box"])
    if slot["ratio"]:
        im = crop_to_ratio(im, slot["ratio"], *slot["anchor"])
    if im.size[0] > slot["width"]:
        im = im.resize((slot["width"], round(im.size[1] * slot["width"] / im.size[0])),
                       Image.LANCZOS)
    buf = io.BytesIO()
    # optimize=True 会显著变慢，且历史上踩过 PNG 优化导致下游解码器卡死的坑，保持关闭
    im.save(buf, format="JPEG", quality=slot["quality"], optimize=False, progressive=True)
    raw = buf.getvalue()
    return base64.b64encode(raw).decode("ascii"), len(raw), im.size[0]


def main() -> int:
    if not HTML.exists():
        raise SystemExit(f"找不到 {HTML}")
    html = HTML.read_text(encoding="utf-8")
    if re.search(r'src="data:image/jpeg;base64,', html):
        html = restore_placeholders(html)

    total = 0
    for slot in SLOTS:
        if slot["token"] not in html:
            print(f"  ! 占位符缺失（跳过）: {slot['token']}")
            continue
        b64, nbytes, out_w = encode(slot)
        html = html.replace(slot["token"], f"data:image/jpeg;base64,{b64}")
        total += nbytes
        print(f"  ✓ {slot['src']:<38} → {out_w}px  {nbytes/1024:6.1f} KB")

    if "__IMG_" in html:
        print("  ! 仍有未替换的占位符，请检查 token 是否与 HTML 一致")

    HTML.write_text(html, encoding="utf-8")
    print(f"\n内嵌图片合计 {total/1024:.0f} KB / 输出 {HTML.relative_to(ROOT)} = "
          f"{HTML.stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
