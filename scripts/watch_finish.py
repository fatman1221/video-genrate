# -*- coding: utf-8 -*-
"""无人值守守候：等 22 镜头视频批量完成 → 等 BGM → 自动跑 music/subtitle/merge/compose 出片。

用法: python scripts/watch_finish.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.build_project import State, resolve_asset_url, wait_task  # noqa: E402

VIDEO_BATCH = "task_89c5e97cb8b9"
MUSIC_TASK = "task_7f6d18731a01"
SPEC = "examples/paipai_project.json"


def batch_stats() -> tuple[int, int]:
    import sqlite3
    db = sqlite3.connect(str(ROOT / "backend" / "video_agent_studio.db"))
    succ = db.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_task_id=? AND status='SUCCESS'",
        (VIDEO_BATCH,)).fetchone()[0]
    fail = db.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_task_id=? AND status IN ('FAILED','CANCELLED')",
        (VIDEO_BATCH,)).fetchone()[0]
    db.close()
    return succ, fail


def main() -> None:
    t0 = time.time()
    # 1) 等视频批量
    while True:
        succ, fail = batch_stats()
        print(f"[watch] 视频 成功{succ}/21 失败{fail} ({(time.time()-t0)/60:.0f}min)", flush=True)
        if succ + fail >= 21:
            break
        time.sleep(120)
    if succ < 21:
        print(f"[watch] 有 {21-succ} 条视频失败，停止自动合成，请人工检查", flush=True)
        sys.exit(1)
    print("[watch] 视频全部完成", flush=True)

    # 2) 等 BGM 任务（早就在队列排队）
    wait_task(MUSIC_TASK, timeout=7200, label="背景音乐")
    print("[watch] BGM 完成", flush=True)

    # 3) 写入 state 并跑后续阶段
    st = State(ROOT / SPEC.replace("/", "\\").replace(".json", ".state.json")
               if False else ROOT / "examples" / "paipai_project.json.state.json")
    st.put("music_task", MUSIC_TASK)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_project.py"),
         "--file", SPEC, "--from", "music"],
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        print("[watch] build_project --from music 失败", flush=True)
        sys.exit(1)

    # 4) 报告成片
    final = st.get("final_video")
    print(f"[watch] ✅ 成片完成: {final}", flush=True)


if __name__ == "__main__":
    main()
