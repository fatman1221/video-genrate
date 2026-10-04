#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按镜头类型自动分派工作流，批量生成镜头关键帧。

为什么需要这个脚本？
    ComfyUI 的「参考图编辑」类工作流（qwen_edit_scene / sdxl_ipadapter_scene）
    依赖 {{reference_image}} 占位符。**如果镜头没有绑定角色（没有定妆图），
    参考图会被替换成空串，LoadImage 直接报错** —— 空间站、屏幕、空镜这类
    镜头用它们必定失败。

    而这个项目想要「一张脸贯穿全片」：有林野出镜的镜头必须走参考图编辑，
    没有他的镜头必须走纯文生图。所以出图阶段必须按镜头分派两套工作流 ——
    generate_all_images 只能传一个 workflow_name，做不到这件事。

规则：
    shot.character_ids 非空（且角色已定妆）→ --reference-workflow（默认 qwen_edit_scene）
    shot.character_ids 为空                  → --plain-workflow（默认 qwen_image_scene）

用法：
    # 全部缺图镜头
    python scripts/gen_shot_images.py --project proj_xxx

    # 只出指定的几个镜号（做人工确认的首批关键帧）
    python scripts/gen_shot_images.py --project proj_xxx --codes 03,04,05,20,27,31

    # 强制重出（例如换了人物一致性工作流后全量重跑）
    python scripts/gen_shot_images.py --project proj_xxx --force

    # 只提交不等待
    python scripts/gen_shot_images.py --project proj_xxx --no-wait

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


def wait_all(tids: list[str], *, label: str = "") -> dict[str, str]:
    """等一组任务全部结束，返回 {task_id: ok|failed}。单个失败不中止其余。"""
    pending = dict.fromkeys(tids)
    outcome: dict[str, str] = {}
    while pending:
        for tid in list(pending):
            task = get_task(tid)
            state = task.get("status")
            if state == "SUCCESS":
                outcome[tid] = "ok"
                del pending[tid]
                print(f"    [ok] {task.get('name') or tid}", flush=True)
            elif state in ("FAILED", "CANCELLED"):
                outcome[tid] = "failed"
                del pending[tid]
                print(f"    [x ] {task.get('name') or tid}: {task.get('error') or state}", flush=True)
        if pending:
            print(f"    ... {label} 剩 {len(pending)} / 共 {len(tids)}", flush=True)
            time.sleep(5)
    return outcome


def main() -> None:
    ap = argparse.ArgumentParser(description="按镜头类型分派工作流，批量出关键帧")
    ap.add_argument("--project", required=True)
    ap.add_argument("--provider", default="comfyui")
    ap.add_argument("--reference-workflow", default="qwen_edit_scene",
                    help="有角色出镜的镜头用（需支持 {{reference_image}}）")
    ap.add_argument("--plain-workflow", default="qwen_image_scene",
                    help="无角色出镜的镜头用（纯文生图）")
    ap.add_argument("--codes", default="",
                    help="只处理这些镜号，逗号分隔，如 03,04,05；留空=全部缺图镜头")
    ap.add_argument("--force", action="store_true",
                    help="忽略已有关键帧，强制重出")
    ap.add_argument("--no-wait", action="store_true", help="只提交不等待")
    ap.add_argument("--concurrency-hint", type=int, default=1)
    args = ap.parse_args()

    listed = call_skill("list_shots", {"project_id": args.project, "limit": 500})
    shots = listed.get("shots") or []
    if not shots:
        raise SystemExit("[!] 项目里没有镜头")

    want = {c.strip() for c in args.codes.split(",") if c.strip()}
    targets: list[tuple[dict, str]] = []
    skipped_done = 0
    for shot in sorted(shots, key=lambda s: s.get("sequence", 0)):
        if want and shot.get("code") not in want:
            continue
        has_image = bool(shot.get("image_asset_id"))
        if has_image and not args.force:
            skipped_done += 1
            continue
        wf = args.reference_workflow if (shot.get("character_ids") or []) else args.plain_workflow
        targets.append((shot, wf))

    if not targets:
        print(f"[=] 没有要出的镜头（已有关键帧 {skipped_done} 个）。")
        return

    print(f"[i] 待出 {len(targets)} 个镜头，已跳过 {skipped_done} 个（已有关键帧）")
    ref_n = sum(1 for _, w in targets if w == args.reference_workflow)
    print(f"[i] 参考图工作流 {ref_n} 个（{args.reference_workflow}） | "
          f"文生图工作流 {len(targets) - ref_n} 个（{args.plain_workflow}）\n")

    tids: list[str] = []
    for shot, wf in targets:
        kind = "参考图" if wf == args.reference_workflow else "文生图"
        if args.force:
            # 重生成：先清空旧关键帧再入队，否则处理器会直接复用已有产物
            res = call_skill("regenerate_image", {
                "shot_id": shot["shot_id"], "provider": args.provider,
                "workflow_name": wf, "keep_old": True,
            })
        else:
            res = call_skill("generate_image", {
                "shot_id": shot["shot_id"], "provider": args.provider, "workflow_name": wf,
            })
        tid = res.get("task_id") or res.get("id")
        tids.append(tid)
        print(f"  [{shot.get('code')}] {kind} -> {wf}  task={tid}", flush=True)

    print(f"\n[+] 已提交 {len(tids)} 个关键帧任务")
    if args.no_wait:
        print("    （--no-wait：不等待，任务在后台执行）")
        return

    outcome = wait_all(tids, label="关键帧")
    ok = sum(1 for v in outcome.values() if v == "ok")
    bad = [k for k, v in outcome.items() if v != "ok"]
    print(f"\n[✓] 完成 {ok} / {len(tids)}")
    if bad:
        print(f"[!] 失败 {len(bad)} 个任务，可用 list_shots --missing image 复查后重跑本脚本")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
