#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成单个镜头的视频并等待完成 —— 用于「先试跑一条，确认耗时与画质」。

为什么单独做：
    MiniMax H3 图生视频是本机最慢的一环（20GB 权重 > 16GB 显存，走流式采样，
    单条约 9~17 分钟）。全量 33 条要 5~9 小时，所以必须先拿一条实测，
    再决定是全量跑还是只挑关键镜头。

用法：
    python scripts/gen_one_video.py --project proj_xxx --code 09
    python scripts/gen_one_video.py --shot-id shot_xxx --duration 7 --provider comfyui
    python scripts/gen_one_video.py --project proj_xxx --code 09 --force   # 已存在也重跑

环境变量：
    STUDIO  后端地址，默认 http://127.0.0.1:8077
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

import httpx

STUDIO = os.environ.get("STUDIO", "http://127.0.0.1:8077").rstrip("/")
API = f"{STUDIO}/api"


def call_skill(name: str, payload: dict[str, Any], *, timeout: float = 120) -> dict[str, Any]:
    clean = {k: v for k, v in payload.items() if v is not None}
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{API}/skills/{name}/invoke", json=clean)
        if r.status_code >= 400:
            raise SystemExit(f"[!] {name} HTTP {r.status_code}\n{r.text[:1200]}")
        env = r.json()
    if not env.get("ok", True):
        raise SystemExit(f"[!] {name} 失败：{json.dumps(env, ensure_ascii=False)[:1200]}")
    return env.get("data") or {}


def get_task(task_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=60) as c:
        r = c.get(f"{API}/tasks/{task_id}")
        r.raise_for_status()
        env = r.json()
    data = env.get("data") or env
    return data.get("task") or data


def find_shot(project_id: str, code: str) -> dict[str, Any]:
    listed = call_skill("list_shots", {"project_id": project_id, "limit": 500})
    for s in listed.get("shots") or []:
        if s.get("code") == code:
            return s
    raise SystemExit(f"[!] 项目 {project_id} 里找不到镜号 {code}")


def main() -> None:
    ap = argparse.ArgumentParser(description="生成单个镜头视频并等待完成")
    ap.add_argument("--project", help="项目 ID（配合 --code 定位镜头）")
    ap.add_argument("--code", help="镜号，如 09")
    ap.add_argument("--shot-id", help="直接指定 shot_id（与 --project/--code 二选一）")
    ap.add_argument("--provider", default="comfyui", help="comfyui / local / cloud")
    ap.add_argument("--duration", type=float, default=None, help="秒；默认取镜头的 duration")
    ap.add_argument("--force", action="store_true", help="已存在视频也重跑（走 regenerate_video）")
    ap.add_argument("--poll", type=float, default=30.0, help="轮询间隔（秒）")
    args = ap.parse_args()

    shot: dict[str, Any] = {}
    if args.shot_id:
        listed = call_skill("list_shots", {"project_id": args.project, "limit": 500}) if args.project else {}
        for s in listed.get("shots") or []:
            if s.get("shot_id") == args.shot_id:
                shot = s
                break
        if not shot:
            shot = {"shot_id": args.shot_id, "code": args.shot_id, "duration": args.duration or 5}
    else:
        if not (args.project and args.code):
            raise SystemExit("[!] 需要 --shot-id，或 --project + --code")
        shot = find_shot(args.project, args.code)

    sid = shot["shot_id"]
    duration = args.duration if args.duration is not None else shot.get("duration")
    existing = (shot.get("assets") or {}).get("video") or {}

    print(f"[i] 镜头 {shot.get('code')}  shot_id={sid}")
    print(f"[i] 时长 {duration}s | provider={args.provider} | 关键帧 "
          f"{'有' if (shot.get('assets') or {}).get('image') else '无（会自动先生成）'}")
    if existing and not args.force:
        print(f"[=] 该镜头已有视频：{existing.get('file_path')}")
        print("    如需重跑请加 --force")
        return

    skill_name = "regenerate_video" if (existing and args.force) else "generate_video"
    payload: dict[str, Any] = {"shot_id": sid, "provider": args.provider, "duration": duration}
    if skill_name == "regenerate_video":
        payload["reason"] = "人工试跑重生成"
    res = call_skill(skill_name, payload)
    tid = res.get("task_id") or res.get("id")
    print(f"[+] 已提交 {skill_name} → task={tid}\n")

    t0 = time.time()
    last = ""
    while True:
        task = get_task(tid)
        state = task.get("status")
        el = time.time() - t0
        stage = task.get("stage") or task.get("message") or ""
        line = f"    [{el:7.1f}s] {state:<9} {int(task.get('progress') or 0):>3}%  {str(stage)[:60]}"
        if line != last:
            print(line, flush=True)
            last = line
        if state in ("SUCCESS", "FAILED", "CANCELLED"):
            break
        time.sleep(args.poll)

    cost = time.time() - t0
    task = get_task(tid)
    if task.get("status") != "SUCCESS":
        print(f"\n[x] 失败（{cost:.1f}s）：{task.get('error') or task.get('status')}")
        raise SystemExit(1)

    print(f"\n[✓] 完成，用时 {cost / 60:.1f} 分钟（{cost:.0f}s）")
    if args.project:
        after = find_shot(args.project, shot.get("code"))
        v = (after.get("assets") or {}).get("video") or {}
        print(f"    文件: {v.get('file_path')}")
        print(f"    规格: {v.get('width')}x{v.get('height')} | 时长 {v.get('duration')}s "
              f"| provider={v.get('provider')}")
        print(f"    素材ID: {v.get('asset_id')}")


if __name__ == "__main__":
    main()
