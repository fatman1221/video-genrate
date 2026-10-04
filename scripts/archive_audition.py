#!/usr/bin/env python
"""归档角色的形象候选图 —— 把「试镜」阶段的候选连图带索引另存一份，供以后复用。

为什么需要它：
    `backend/storage/**` 在 .gitignore 里（原则是「仓库只存源码」），而候选图是
    有价值的创作资产 —— 换形象、复用角色、回顾决策都要用到。本脚本把它们
    复制到工程内的 `archive/` 下，附带结构化索引与人读清单，且不移动原文件
    （DB 里的 file_path 仍然有效，「挑选 / 切换基准」流程照常可用）。

用法：
    python scripts/archive_audition.py --character-id chr_xxx --out archive/character_auditions/linye_v1
    python scripts/archive_audition.py --character-id chr_xxx --out ... --project-id proj_xxx
    # 指定候选方向顺序（决定 A/B/C… 前缀）
    python scripts/archive_audition.py --character-id chr_xxx --out ... \
        --presets "清瘦硬朗,沧桑疲惫,年轻利落,温和内敛,硬朗粗粝,清冷疏离,沉稳厚重,清秀未褪"
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "backend" / "video_agent_studio.db"

# 候选方向 → 字母前缀（与 gen_character_variants.py 的 PRESETS 顺序保持一致）
DEFAULT_PRESETS = ["清瘦硬朗", "沧桑疲惫", "年轻利落", "温和内敛",
                   "硬朗粗粝", "清冷疏离", "沉稳厚重", "清秀未褪"]
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _safe(name: str) -> str:
    """把标签转成安全的文件名片段（剔除路径非法字符与空白）。"""
    bad = '<>:"/\\|?*'
    out = "".join("_" if (ch in bad or ch.isspace()) else ch for ch in str(name)).strip("_")
    return out or "unnamed"


def load_assets(character_id: str) -> tuple[dict, list[dict]]:
    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在：{DB_PATH}")
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    char = db.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
    if char is None:
        raise SystemExit(f"角色不存在：{character_id}")
    rows = db.execute(
        "SELECT * FROM assets WHERE character_id=? AND type='CHARACTER' ORDER BY created_at ASC",
        (character_id,),
    ).fetchall()
    db.close()
    return dict(char), [dict(r) for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser(description="归档角色形象候选图（图 + 索引 + 清单）")
    ap.add_argument("--character-id", required=True)
    ap.add_argument("--out", required=True, help="归档目录（相对工程根或绝对路径）")
    ap.add_argument("--presets", default="", help="候选方向顺序，逗号分隔；决定 A/B/C… 前缀")
    ap.add_argument("--label", default="", help="本批候选的版本标注，如 v1")
    args = ap.parse_args()

    order = [s.strip() for s in args.presets.split(",") if s.strip()] or DEFAULT_PRESETS
    char, assets = load_assets(args.character_id)

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out

    archived: list[dict] = []
    kept = [a for a in assets if (a.get("file_path") or "").strip()]
    letter_of = {name: LETTERS[i] for i, name in enumerate(order) if i < len(LETTERS)}

    used_names: set[str] = set()
    for a in kept:
        extra = a.get("extra") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except json.JSONDecodeError:
                extra = {}
        label = str(extra.get("label") or "").strip()
        role = str(extra.get("role") or "").strip()
        src = Path(a["file_path"])
        if not src.exists():
            print(f"  [跳过] 文件缺失：{src}")
            continue

        if label:
            stem = f"{letter_of.get(label, 'X')}_{_safe(label)}"
        elif role == "reference_candidate":
            # 无方向标签的历史基准（最早那张定妆图）
            stem = f"Z_历史基准_{_safe(a.get('name') or '')}"
        else:
            stem = f"_初始基准_{_safe(a.get('name') or '')}"
        # 同名去重
        base_stem, n = stem, 2
        while stem in used_names:
            stem = f"{base_stem}_{n}"
            n += 1
        used_names.add(stem)

        suffix = src.suffix or ".png"
        dst = out / f"{stem}{suffix}"
        archived.append({
            "seq": len(archived) + 1,
            "letter": letter_of.get(label, ""),
            "label": label,
            "note": extra.get("note") or "",
            "role": role or "reference_candidate",
            "is_reference": a["id"] == char.get("reference_asset_id"),
            "asset_id": a["id"],
            "name": a.get("name") or "",
            "archived_file": dst.name,
            "source_file": str(src),
            "width": a.get("width"),
            "height": a.get("height"),
            "provider": a.get("provider"),
            "model": a.get("model"),
            "prompt": a.get("prompt") or "",
            "seed": (extra.get("seed") if isinstance(extra, dict) else None)
                    or ((a.get("parameters") or {}).get("seed")
                        if isinstance(a.get("parameters"), dict) else None),
            "created_at": str(a.get("created_at") or ""),
        })

    out.mkdir(parents=True, exist_ok=True)
    for a in archived:
        shutil.copy2(a["source_file"], out / a["archived_file"])

    index = {
        "kind": "character_audition",
        "batch_label": args.label or "",
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "project_id": char.get("project_id"),
        "character_id": char["id"],
        "character_name": char.get("name"),
        "character_appearance": char.get("appearance"),
        "reference_asset_id": char.get("reference_asset_id"),
        "reference_letter": next((a["letter"] for a in archived if a["is_reference"]), ""),
        "count": len(archived),
        "items": archived,
    }
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人读清单
    ref = next((a for a in archived if a["is_reference"]), None)
    lines = [
        f"# {char.get('name')} · 形象候选归档" + (f"（{args.label}）" if args.label else ""),
        "",
        f"- 角色 ID：`{char['id']}`　项目：`{char.get('project_id')}`",
        f"- 归档时间：{index['archived_at']}　共 {len(archived)} 张",
        f"- **当前基准**：{(ref['letter'] + ' · ' + ref['label']) if ref and ref['label'] else (ref['archived_file'] if ref else '（未知）')}"
        f"　`{char.get('reference_asset_id')}`",
        "",
        "> 本目录是候选图的副本；`backend/storage/` 下的原文件与数据库记录均未改动，",
        "> 系统内的「列出候选 / 切换基准」流程照常可用。",
        "",
        "| 版本 | 方向 | 说明 | 标记 | 素材 ID | 文件 |",
        "|---|---|---|---|---|---|",
    ]
    for a in archived:
        mark = "**当前基准**" if a["is_reference"] else ("历史基准" if not a["letter"] else "")
        lines.append(
            f"| {a['letter'] or '-'} | {a['label'] or a['name'] or '-'} | {a['note'] or '-'} "
            f"| {mark} | `{a['asset_id']}` | `{a['archived_file']}` |"
        )
    lines += [
        "",
        "## 如何把某一版重新设为基准",
        "",
        "```bash",
        f'curl -s --noproxy \'*\' "http://127.0.0.1:8077/api/skills/set_character_reference/invoke" \\',
        '  -X POST -H \'Content-Type: application/json\' \\',
        f'  -d \'{{"character_id":"{char["id"]}","asset_id":"<素材 ID>"}}\'',
        "```",
        "",
        "或在 Swagger / 前端角色面板里选择对应候选图。素材 ID 见上表。",
        "",
    ]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"归档完成 → {out}")
    print(f"  图片 {len(archived)} 张 + index.json + README.md")
    for a in archived:
        flag = "  <= 当前基准" if a["is_reference"] else ""
        print(f"  {a['letter'] or '-':>2} {a['label'] or a['name']:<8} {a['archived_file']}{flag}")


if __name__ == "__main__":
    main()
