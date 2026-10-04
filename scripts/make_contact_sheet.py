#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一批候选图拼成带标签的对比图（contact sheet），便于一眼挑选。

为什么不用图库工具？
    挑选角色定妆 / 关键帧候选时，需要的是「同一屏内、同样裁切、带方向标签」的
    横向比较。逐张点开看会严重失真（缩放比例不同、来回切换记忆负担大）。
    这个脚本统一裁切比例 + 固定标签栏，让差异只体现在画面本身。

用法：
    # 从 gen_character_variants.py 产出的清单拼
    python scripts/make_contact_sheet.py --manifest .tmp/林野_variants.json \
        --title "《归途信号》林野 · 定妆候选" --out .tmp/sheet.png --cols 4

    # 也可以直接给文件名
    python scripts/make_contact_sheet.py --files a.png b.png c.png \
        --labels 清瘦硬朗 沧桑疲惫 年轻利落 --out .tmp/sheet.png

    # 只拼指定序号（清单里的顺序），并放大单格
    python scripts/make_contact_sheet.py --manifest m.json --only 1,3,5 --cell 520x640
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path("C:/Windows/Fonts")
BG = (243, 244, 246)
INK = (20, 22, 28)
SUB = (120, 126, 138)
LINE = (205, 210, 218)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    for cand in (FONT_DIR / name, FONT_DIR / "simhei.ttf"):
        if cand.exists():
            return ImageFont.truetype(str(cand), size)
    return ImageFont.load_default(size)


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int, max_lines: int = 2) -> list[str]:
    """按像素宽度折行（中文逐字累加），最多 max_lines 行，末行加省略号。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        probe = cur + ch
        if font.getlength(probe) <= max_w:
            cur = probe
            continue
        lines.append(cur)
        cur = ch
        if len(lines) == max_lines:
            break
    if len(lines) < max_lines and cur:
        lines.append(cur)
    if len(lines) == max_lines:
        # 还有溢出则给末行收尾省略号
        rest_len = len(text) - sum(len(x) for x in lines)
        if rest_len > 0:
            last = lines[-1]
            while last and font.getlength(last + "…") > max_w:
                last = last[:-1]
            lines[-1] = last + "…"
    return lines


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return resized.crop((left, top, left + w, top + h))


def main() -> None:
    ap = argparse.ArgumentParser(description="候选图拼成带标签对比图")
    ap.add_argument("--manifest", default="", help="gen_character_variants.py 的清单 JSON")
    ap.add_argument("--files", nargs="*", default=[], help="直接给图片路径")
    ap.add_argument("--labels", nargs="*", default=[], help="与 --files 对应的标签")
    ap.add_argument("--notes", nargs="*", default=[], help="副标题（可选）")
    ap.add_argument("--only", default="", help="只保留这些序号（从 1 开始），逗号分隔")
    ap.add_argument("--title", default="", help="大标题")
    ap.add_argument("--subtitle", default="", help="副标题")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--cell", default="340x440", help="单格尺寸，如 340x440")
    args = ap.parse_args()

    items: list[dict] = []
    if args.manifest:
        man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        items = [{"path": c["file_path"], "label": c.get("label", ""),
                  "note": c.get("note", ""), "id": c.get("asset_id", "")}
                 for c in (man.get("candidates") or [])]
    if args.files:
        for i, f in enumerate(args.files):
            items.append({"path": f,
                          "label": args.labels[i] if i < len(args.labels) else Path(f).stem,
                          "note": args.notes[i] if i < len(args.notes) else "",
                          "id": ""})
    if not items:
        raise SystemExit("[!] 需要 --manifest 或 --files 提供图片")

    if args.only:
        keep = {int(x) for x in args.only.split(",") if x.strip().isdigit()}
        items = [it for i, it in enumerate(items, 1) if i in keep]
    if not items:
        raise SystemExit("[!] --only 过滤后没有剩余图片")

    cw, ch = (int(x) for x in args.cell.lower().split("x"))
    cols = max(1, min(args.cols, len(items)))
    rows = (len(items) + cols - 1) // cols
    pad = 18
    top = (124 if args.title else 28)
    label_h = 116 if any(it.get("note") for it in items) else 72
    img_h = ch - label_h
    W = pad + cols * (cw + pad)
    H = top + rows * (ch + pad) + 20

    f_title, f_sub = _font(44, True), _font(22)
    f_label, f_note, f_id = _font(32, True), _font(19), _font(16)

    sheet = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(sheet)
    if args.title:
        d.text((pad, 24), args.title, font=f_title, fill=INK)
    if args.subtitle:
        d.text((pad, 78), args.subtitle, font=f_sub, fill=SUB)

    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        x, y = pad + c * (cw + pad), top + r * (ch + pad)
        sheet.paste(_cover(Image.open(it["path"]).convert("RGB"), cw, img_h), (x, y))
        d.rectangle([x, y, x + cw - 1, y + img_h - 1], outline=LINE, width=1)
        d.rectangle([x, y + img_h, x + cw, y + ch], fill=(255, 255, 255))
        d.text((x + 12, y + img_h + 10), it["label"], font=f_label, fill=INK)
        if it["id"]:
            tag = it["id"][-10:]
            d.text((x + cw - 12 - f_id.getlength(tag), y + img_h + 20), tag, font=f_id,
                   fill=(158, 164, 176))
        note_lines = _wrap(it["note"], f_note, cw - 24, 2) if it["note"] else []
        for j, line in enumerate(note_lines):
            d.text((x + 12, y + img_h + 54 + j * 25), line, font=f_note, fill=SUB)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"[✓] {len(items)} 张 -> {out}  {sheet.size[0]}x{sheet.size[1]}")


if __name__ == "__main__":
    main()
