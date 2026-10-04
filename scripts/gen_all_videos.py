#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量生成全片镜头视频（AI 真图生视频），并轮询到全部终结 —— 可断点续跑。

为什么单独做：
    `generate_all_videos` 只负责「提交」任务就返回，之后就没人盯进度了。
    33 条 × 约 6 分钟 = 3+ 小时，需要一条能在后台挂着、能报进度、
    中断后重跑不会重复烧 GPU 的批处理命令。

用法：
    python scripts/gen_all_videos.py --project proj_xxx
    python scripts/gen_all_videos.py --project proj_xxx --codes 05,09,20   # 只跑指定镜号
    python scripts/gen_all_videos.py --project proj_xxx --force            # 已有视频也重跑
    python scripts/gen_all_videos.py --project proj_xxx --poll 60 --timeout 14400

断点续跑：
    默认 only_missing —— 已有视频的镜头会被自动跳过，直接重跑本命令即可续做。
    （本平台的任务领取已加幂等闸门，重跑不会重复产出。）

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
TERMINAL = ("SUCCESS", "FAILED", "CANCELLED")


def call_skill(name: str, payload: dict[str, Any], *, timeout: float = 180) -> dict[str, Any]:
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


def list_shots(project_id: str) -> list[dict[str, Any]]:
    return call_skill("list_shots", {"project_id": project_id, "limit": 500}).get("shots") or []


def main() -> None:
    ap = argparse.ArgumentParser(description="批量生成镜头视频并等待全部完成")
    ap.add_argument("--project", required=True, help="项目 ID")
    ap.add_argument("--provider", default="comfyui", help="comfyui / local / cloud")
    ap.add_argument("--codes", default=None,
                    help="逗号分隔的镜号白名单，如 05,09,20；省略=全部缺视频的镜头")
    ap.add_argument("--force", action="store_true", help="已有视频的镜头也重跑（走 regenerate_video）")
    ap.add_argument("--poll", type=float, default=45.0, help="轮询间隔（秒）")
    ap.add_argument("--timeout", type=float, default=8 * 3600, help="总超时（秒），默认 8 小时")
    args = ap.parse_args()

    shots = list_shots(args.project)
    by_code = {s.get("code"): s for s in shots}

    if args.codes:
        want = [c.strip() for c in args.codes.split(",") if c.strip()]
        missing = [c for c in want if c not in by_code]
        if missing:
            raise SystemExit(f"[!] 找不到镜号：{missing}")
        targets = [by_code[c] for c in want]
    else:
        targets = [s for s in shots if not (s.get("assets") or {}).get("video")]

    if not args.force:
        targets = [s for s in targets if not (s.get("assets") or {}).get("video")]

    if not targets:
        print("[=] 所有目标镜头都已有视频，无需处理（如需重跑加 --force）")
        return

    have_img = sum(1 for s in targets if (s.get("assets") or {}).get("image"))
    print(f"[i] 目标镜头 {len(targets)} 个（关键帧就绪 {have_img}/{len(targets)}）")
    print(f"    provider={args.provider}  镜号：{','.join(s['code'] for s in targets)}\n")

    # 逐个提交，记录 task -> code 映射，便于报进度
    tid_to_code: dict[str, str] = {}
    submitted, failed_submit = [], []
    for s in targets:
        try:
            if (s.get("assets") or {}).get("video") and args.force:
                res = call_skill("regenerate_video", {
                    "shot_id": s["shot_id"], "provider": args.provider,
                    "duration": s.get("duration"), "reason": "批量重生成",
                })
            else:
                res = call_skill("generate_video", {
                    "shot_id": s["shot_id"], "provider": args.provider,
                    "duration": s.get("duration"),
                })
            tid = res.get("task_id") or res.get("id")
            if tid:
                tid_to_code[tid] = s["code"]
                submitted.append((s["code"], tid))
                print(f"  [+] {s['code']} → {tid}", flush=True)
            else:
                failed_submit.append((s["code"], "无 task_id"))
                print(f"  [!] {s['code']} 未返回 task_id", flush=True)
        except SystemExit as e:
            failed_submit.append((s["code"], str(e)[:160]))
            print(f"  [x] {s['code']} 提交失败：{str(e)[:160]}", flush=True)

    print(f"\n[i] 已提交 {len(submitted)} 个，提交失败 {len(failed_submit)} 个")
    if not submitted:
        return

    # 轮询到全部终结
    t0 = time.time()
    done: dict[str, str] = {}
    last_line = ""
    while True:
        states: dict[str, str] = {}
        for tid in list(tid_to_code):
            if tid in done:
                continue
            try:
                t = get_task(tid)
            except Exception as e:  # 网络抖动不致命
                states[tid] = f"POLL_ERR({str(e)[:20]})"
                continue
            st = t.get("status") or "?"
            states[tid] = st
            if st in TERMINAL:
                done[tid] = st

        n_ok = sum(1 for v in done.values() if v == "SUCCESS")
        n_fail = sum(1 for v in done.values() if v != "SUCCESS")
        el = time.time() - t0
        run = [tid_to_code[t] for t, st in states.items() if st not in TERMINAL and t not in done]
        line = (f"    [{el/60:6.1f}min] 完成 {n_ok}/{len(submitted)}  失败 {n_fail}  "
                f"进行中 {len(run)}: {','.join(run[:6])}")
        if line != last_line:
            print(line, flush=True)
            last_line = line

        if len(done) >= len(tid_to_code):
            break
        if el > args.timeout:
            print(f"\n[!] 总超时 {args.timeout}s，仍有 {len(tid_to_code)-len(done)} 个未完成")
            break
        time.sleep(args.poll)

    total = time.time() - t0
    ok = [(code, t) for t, code in tid_to_code.items() if done.get(t) == "SUCCESS"]
    bad = [(code, t, done.get(t)) for t, code in tid_to_code.items() if done.get(t) != "SUCCESS"]
    print(f"\n===== 批量视频结束 =====")
    print(f"    成功 {len(ok)}/{len(submitted)}   失败 {len(bad)}   总耗时 {total/60:.1f} 分钟")
    if bad:
        print("    失败清单：")
        for code, tid, st in bad:
            print(f"      - {code}  {tid}  {st}")
    if ok:
        avg = total / max(1, len(ok))
        print(f"    平均 {avg/60:.1f} 分钟/条")


if __name__ == "__main__":
    main()
