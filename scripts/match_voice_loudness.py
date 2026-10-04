#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配音电平一致性匹配：把过轻的旁白补到可听，但不压掉刻意的情感动态。

用法：
    # 先看（只测量，不改任何文件）
    python scripts/match_voice_loudness.py --project proj_xxx --report

    # 实际匹配（默认目标 -26 LUFS，单条最多补 +10 dB）
    python scripts/match_voice_loudness.py --project proj_xxx --apply

    python scripts/match_voice_loudness.py --project proj_xxx --apply --target -25 --max-boost 12

为什么是「只补不提」：
    TTS 会按 voice_instruct 真的把某些台词念得很轻（「极低的气声」「轻声哽咽」），
    这种偏轻是**设计内的表演**。如果无差别地把所有旁白归一化到同一响度，
    刻意的气声与哽咽会被拉平，情感动态就没了。
    真正需要修的是**失手**的过轻 —— 比如同一批里比中位数低 10 dB 以上，
    那种在混音里会被 BGM 直接盖住、观众根本听不见。

    所以策略是：低于目标就补，最多补 max_boost 分贝；高于目标一律不动。
    补太多会破坏表演，补不回来的是记录在案的缺陷，交由人工决定是否重录。

判定与产出：
    - 用 ffmpeg ebur128 测「整合响度 I（LUFS）」，这是电影/播客混音的标准口径，
      比峰值和 RMS 都更贴近人耳感受。
    - 补过电平的条目会**新建一条 VOICE 资产**（保留原始文件，可回溯），
      并把 shot.voice_asset_id 指向新资产；合成时按 shot 取配音，不会取错。

依赖：ffmpeg（.env 里的 FFMPEG_BIN，或 WorkBuddy 托管版）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FFMPEG = os.environ.get("FFMPEG_BIN") or str(
    Path.home() / ".workbuddy/binaries/ffmpeg/bin/ffmpeg.exe"
)


def db_path() -> Path:
    return BACKEND / "video_agent_studio.db"


def measure_integrated(path: str) -> tuple[float | None, float | None]:
    """返回 (整合响度 LUFS, 真峰 dBFS)。"""
    proc = subprocess.run(
        [FFMPEG, "-v", "info", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True,
    )
    out = proc.stderr.decode("utf-8", "replace")
    lufs = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", out)
    peak = re.findall(r"Peak:\s*(-?[\d.]+)\s*dBFS", out)
    return (float(lufs[-1]) if lufs else None,
            float(peak[-1]) if peak else None)


def main() -> None:
    ap = argparse.ArgumentParser(description="配音电平一致性匹配（只补不提，保护情感动态）")
    ap.add_argument("--project", required=True)
    ap.add_argument("--target", type=float, default=-26.0,
                    help="目标整合响度 LUFS（默认 -26，适合有旁白的影片混音）")
    ap.add_argument("--max-boost", type=float, default=10.0,
                    help="单条最大补正分贝（默认 +10，防止过度放大）")
    ap.add_argument("--min-gain", type=float, default=0.5,
                    help="低于该增益（dB）的条目跳过，避免无谓重编码")
    ap.add_argument("--report", action="store_true", help="只测量并打印，不改动")
    ap.add_argument("--apply", action="store_true", help="真正执行电平匹配")
    args = ap.parse_args()

    if not (args.report or args.apply):
        ap.error("请指定 --report（只看）或 --apply（执行）")

    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "select id, name, file_path, shot_id, scene_id from assets "
        "where project_id=? and type='VOICE' order by name", (args.project,)))
    if not rows:
        raise SystemExit(f"[!] 项目 {args.project} 没有 VOICE 资产")
    print(f"[i] {len(rows)} 条 VOICE 资产")

    items = []
    for r in rows:
        if not Path(r["file_path"]).exists():
            print(f"[!] 文件缺失，跳过：{r['file_path']}")
            continue
        lufs, peak = measure_integrated(r["file_path"])
        items.append({"row": r, "lufs": lufs, "peak": peak})

    vals = [it["lufs"] for it in items if it["lufs"] is not None]
    if not vals:
        raise SystemExit("[!] 没有解析到响度数据，检查 ffmpeg 输出")
    med = statistics.median(vals)
    print(f"整组：中位 {med:.1f} LUFS  范围 {min(vals):+.1f} ~ {max(vals):+.1f}"
          f"（极差 {max(vals) - min(vals):.1f} dB）\n")

    plan = []
    for it in sorted(items, key=lambda x: x["lufs"] if x["lufs"] is not None else 0):
        code = (it["row"]["name"] or "").split()[0]
        gain = 0.0
        if it["lufs"] is not None:
            gain = min(max(args.target - it["lufs"], 0.0), args.max_boost)
        need = gain >= args.min_gain
        if need:
            plan.append((it, gain))
        flag = f"→ 补 {gain:+.1f} dB" if need else ("（低于目标但已到上限）" if gain > 0 else "保持")
        print(f"  {code:<4} {it['lufs']:>7.1f} LUFS  峰 {it['peak']:>7.1f} dBFS  "
              f"偏离中位 {it['lufs'] - med:+5.1f}  {flag}")

    print(f"\n[i] 计划调整 {len(plan)} 条（目标 {args.target:.1f} LUFS，"
          f"单条上限 +{args.max_boost:.0f} dB）")
    if args.report or not plan:
        return

    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    from app.database import session_scope  # noqa: E402
    from app.models import Asset, Shot  # noqa: E402
    from app.providers.base import GenerationResult  # noqa: E402
    from app.services import assets as assets_svc  # noqa: E402

    for it, gain in plan:
        src = Path(it["row"]["file_path"])
        dst = src.with_name(f"{src.stem}_g{int(round(gain))}.mp3")
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-i", str(src),
             "-af", f"volume={gain:.2f}dB", "-c:a", "libmp3lame", "-b:a", "192k",
             "-ar", "44100", "-ac", "2", str(dst)],
            check=True)
        new_lufs, new_peak = measure_integrated(str(dst))
        dur = subprocess.run(
            [FFMPEG, "-i", str(dst), "-f", "null", "-"], capture_output=True
        ).stderr.decode("utf-8", "replace")
        seconds = 0.0
        for ln in dur.splitlines():
            if "time=" in ln:
                part = ln.split("time=")[1].split(" ")[0]
                try:
                    h, m, s = part.split(":")
                    seconds = int(h) * 3600 + int(m) * 60 + float(s)
                except ValueError:
                    pass

        with session_scope() as db:
            shot = db.get(Shot, it["row"]["shot_id"])
            res = GenerationResult(
                file_path=str(dst), provider="loudness-match", model="ffmpeg-volume",
                workflow="volume_gain", duration=seconds, format="mp3",
                size_bytes=dst.stat().st_size,
                parameters={"gain_db": gain, "from": str(src), "lufs_before": it["lufs"]},
                prompt="", elapsed_ms=0,
            )
            asset = assets_svc.ingest_result(
                db, project_id=args.project, result=res, asset_type="VOICE",
                name=it["row"]["name"], scene_id=it["row"]["scene_id"],
                shot_id=it["row"]["shot_id"],
                extra={"role": "shot_voice", "gain_db": gain,
                       "lufs_before": it["lufs"], "lufs_after": new_lufs,
                       "source_asset_id": it["row"]["id"]},
            )
            if shot:
                shot.voice_asset_id = asset.id
            db.commit()
        print(f"[✓] {(it['row']['name'] or '').split()[0]}  "
              f"{it['lufs']:.1f} → {new_lufs:.1f} LUFS  (+{gain:.1f} dB)")


if __name__ == "__main__":
    main()
