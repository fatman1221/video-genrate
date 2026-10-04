#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把逐镜头旁白导成一条按成片时间轴对齐的完整音轨。

用途：
    BGM 的旁白闪避（ducking）需要知道「哪几秒有人在说话」。
    合成时内部会临时拼一条，但那条拿不到 —— 所以在这里单独导出来，
    喂给 `scripts/prepare_bgm.py --duck <这条轨>`。

    顺带它也是校验音画同步的最好材料：这条轨的每一段起点，
    应该与成片里对应镜头的画面起点严格重合。

用法：
    python scripts/export_voice_timeline.py --project proj_xxx
    python scripts/export_voice_timeline.py --project proj_xxx --out some/path.mp3

时间轴口径（重要）：
    镜头的「槽位」取**视频素材的真实时长**，不是分镜表的计划时长 ——
    成片主视频是各镜头视频的拼接，用计划时长会让旁白在后半段逐渐提前于画面
    （实测 33 镜可累积约 1.1s）。这里直接复用 `handlers._shot_timeline`，
    保证与合成阶段**同一套口径**，避免两边各算一套。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


def main() -> None:
    ap = argparse.ArgumentParser(description="导出按成片时间轴对齐的旁白音轨")
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", default=None, help="输出路径，默认 storage/voices/<项目>/<项目>_voice_aligned.mp3")
    args = ap.parse_args()

    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    from app.database import session_scope  # noqa: E402
    from app.executors.handlers import _shot_timeline  # noqa: E402
    from app.models import Asset  # noqa: E402
    from app.providers import local_engine  # noqa: E402

    timeline: list[dict] = []
    cursor = 0.0
    with session_scope() as db:
        for shot, slot in _shot_timeline(db, args.project):
            if shot.voice_asset_id:
                asset = db.get(Asset, shot.voice_asset_id)
                if asset and Path(asset.file_path).exists():
                    timeline.append({"path": asset.file_path,
                                     "start": round(cursor, 3), "slot": slot})
            cursor += slot

    if not timeline:
        raise SystemExit(f"[!] 项目 {args.project} 没有任何可用配音")

    out = Path(args.out) if args.out else (
        ROOT / "backend/storage/voices" / args.project / f"{args.project}_voice_aligned.mp3")
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"[i] 对齐 {len(timeline)} 段旁白，成片总时长 {cursor:.2f}s")
    for seg in timeline:
        print(f"    @{seg['start']:>7.2f}s  槽位 {seg['slot']:>6.2f}s  "
              f"{Path(seg['path']).name}")

    path = local_engine.build_voice_timeline(
        segments=timeline, out_path=str(out), total_duration=round(cursor, 3))
    print(f"[✓] 已导出：{path}")
    print(f"    可直接用于闪避：python scripts/prepare_bgm.py build ... --duck \"{path}\"")


if __name__ == "__main__":
    main()
