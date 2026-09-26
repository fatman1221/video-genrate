#!/usr/bin/env python3
"""Agent CLI —— 供 WorkBuddy / Codex 等 Agent 直接调用的命令行入口。

设计意图：Agent 不需要理解本项目的内部实现，只要：
    1) 看清单     : agent_cli.py skills
    2) 看契约     : agent_cli.py describe generate_video
    3) 调用       : agent_cli.py call generate_video '{"shot_id":"shot_xxx"}'
    4) 轮询       : agent_cli.py status task_xxx
    5) 等全部跑完 : agent_cli.py watch <project_id>

用法示例：
    export STUDIO=http://127.0.0.1:8077
    python agent_cli.py skills --category video
    python agent_cli.py call bootstrap_project '{"name":"AI Agent 教学视频","requirement":"...","target_duration":300}'
    python agent_cli.py pipeline <project_id>
    python agent_cli.py watch <project_id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    print("需要 httpx：pip install httpx", file=sys.stderr)
    raise SystemExit(2)

BASE = os.environ.get("STUDIO", "http://127.0.0.1:8077").rstrip("/")
ACTIVE = {"PENDING", "RUNNING", "RETRYING"}


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=120)


def cmd_skills(args: argparse.Namespace) -> int:
    with _client() as c:
        resp = c.get("/api/skills", params={"category": args.category, "keyword": args.keyword,
                                            "detail": "true" if args.detail else "false"})
        resp.raise_for_status()
        data = resp.json()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"共 {data['total']} 个 Skill，分类：")
    for cat, count in data["categories"].items():
        print(f"  {cat:<16}{count}")
    print()
    for skill in data["skills"]:
        flag = "async" if skill["async"] else "sync "
        print(f"  [{flag}] {skill['name']:<34}{skill['description'][:64]}")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    with _client() as c:
        resp = c.get(f"/api/skills/{args.name}")
        if resp.status_code == 404:
            print(f"未找到 Skill: {args.name}", file=sys.stderr)
            return 1
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {}
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as exc:
            print(f"JSON 解析失败：{exc}", file=sys.stderr)
            return 2
    if args.arg:
        for item in args.arg:
            key, _, value = item.partition("=")
            if value.lower() in ("true", "false"):
                payload[key] = value.lower() == "true"
            elif value.replace(".", "", 1).isdigit():
                payload[key] = float(value) if "." in value else int(value)
            else:
                payload[key] = value
    with _client() as c:
        resp = c.post(f"/api/skills/{args.name}/invoke", json=payload)
        data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if not data.get("ok"):
        return 1
    if args.wait and data.get("task_id"):
        return _wait_task(data["task_id"], timeout=args.timeout)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with _client() as c:
        resp = c.get(f"/api/tasks/{args.task_id}")
        if resp.status_code == 404:
            print(f"任务不存在: {args.task_id}", file=sys.stderr)
            return 1
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    return 0


def _wait_task(task_id: str, *, timeout: float = 1800, poll: float = 2.0) -> int:
    started = time.time()
    while time.time() - started < timeout:
        with _client() as c:
            task = c.get(f"/api/tasks/{task_id}").json()["task"]
        print(f"\r  {task['status']:<10} {task['progress']:>3}%  {task['name'][:48]}", end="")
        if task["status"] not in ACTIVE:
            print()
            print(json.dumps({"task_id": task_id, "status": task["status"],
                              "result": task.get("result"), "error": task.get("error")},
                             ensure_ascii=False, indent=2))
            return 0 if task["status"] == "SUCCESS" else 1
        time.sleep(poll)
    print("\n超时", file=sys.stderr)
    return 1


def cmd_watch(args: argparse.Namespace) -> int:
    started = time.time()
    last = ""
    while time.time() - started < args.timeout:
        with _client() as c:
            tasks = c.get("/api/tasks", params={"project_id": args.project_id, "limit": 500}).json()
            overview = c.get(f"/api/projects/{args.project_id}/overview").json()
        active = [t for t in tasks["items"] if t["status"] in ACTIVE]
        stats = tasks.get("stats", {})
        line = (f"  进度 {overview['status']['progress']:>3}% | 工作流 {overview['status']['workflow_state']:<22}"
                f"| 运行中 {len(active):>2} | 成功 {stats.get('SUCCESS', 0):>3} | 失败 {stats.get('FAILED', 0):>2}")
        if line != last:
            print(line, flush=True)
            last = line
        if not active:
            final = overview.get("final_output")
            quality = overview.get("quality")
            print("=" * 60)
            print(f"工作流状态：{overview['status']['workflow_state']}")
            if final:
                print(f"最终成片：{final['url']}  ({final['duration']:.1f}s, "
                      f"{final['width']}x{final['height']})")
            if quality:
                print(f"质检：{quality['status']} 得分 {quality['score']}")
            return 0
        time.sleep(args.poll)
    print("超时", file=sys.stderr)
    return 1


def cmd_pipeline(args: argparse.Namespace) -> int:
    with _client() as c:
        resp = c.post("/api/skills/run_pipeline/invoke", json={"project_id": args.project_id})
        data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if not data.get("ok"):
        return 1
    if args.watch:
        return cmd_watch(argparse.Namespace(project_id=args.project_id, timeout=args.timeout, poll=3.0))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    with _client() as c:
        print(json.dumps(c.get("/api/health").json(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Video Agent Studio · Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("skills", help="列出全部 Skill")
    p.add_argument("--category")
    p.add_argument("--keyword")
    p.add_argument("--detail", action="store_true", help="输出完整 JSON Schema")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("describe", help="查看单个 Skill 的契约")
    p.add_argument("name")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("call", help="调用 Skill")
    p.add_argument("name")
    p.add_argument("payload", nargs="?", help="JSON 字符串")
    p.add_argument("-a", "--arg", action="append", help="k=v 形式参数，可重复")
    p.add_argument("--wait", action="store_true", help="异步任务等待完成")
    p.add_argument("--timeout", type=float, default=1800)
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("status", help="查询任务状态")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="持续观察项目直到没有活跃任务")
    p.add_argument("project_id")
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--poll", type=float, default=3.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("pipeline", help="推进项目流水线")
    p.add_argument("project_id")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--timeout", type=float, default=3600)
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("health", help="引擎健康检查")
    p.set_defaults(func=cmd_health)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
