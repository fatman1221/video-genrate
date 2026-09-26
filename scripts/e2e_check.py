"""端到端链路验证脚本。

通过 Skill API 驱动一次完整生产：
建项目 → 脚本 → 分镜 → 人物 → 关键帧 → 镜头视频 → 配音 → 字幕 → 配乐 → 合成 → 质检

用法：
    env -u PYTHONPATH python scripts/e2e_check.py [目标时长秒数] [单镜头时长]
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

import httpx

BASE = "http://127.0.0.1:8077"
SKILL = f"{BASE}/api/skills"

PENDING_STATES = {"PENDING", "RUNNING", "RETRYING"}


def invoke(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(f"{SKILL}/{name}/invoke", json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"[{name}] 调用失败：{data.get('error')}")
    return data.get("data") or {}


def wait_tasks(project_id: str, label: str, timeout: float = 1800, poll: float = 2.0) -> dict:
    started = time.time()
    last_line = ""
    while time.time() - started < timeout:
        resp = httpx.get(f"{BASE}/api/projects/{project_id}/tasks", params={"limit": 500}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        items = data["items"]
        stats = data.get("stats") or {}
        active = [t for t in items if t["status"] in PENDING_STATES]
        running = [t for t in items if t["status"] == "RUNNING"]
        line = (f"  [{label}] 运行中 {len(running)} / 待处理 {len(active) - len(running)} "
                f"| 成功 {stats.get('SUCCESS', 0)} 失败 {stats.get('FAILED', 0)}")
        if line != last_line:
            print(line, flush=True)
            last_line = line
        if not active:
            return stats
        time.sleep(poll)
    raise TimeoutError(f"{label} 超时")


def main() -> int:
    target = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    shot_duration = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

    print("=" * 72)
    print(f"端到端验证：目标时长 {target}s / 单镜头 {shot_duration}s")
    print("=" * 72)

    health = httpx.get(f"{BASE}/api/health", timeout=30).json()
    print(f"环境：DB={health['database']['dialect']} ffmpeg={health['engines']['ffmpeg']['available']} "
          f"say={health['engines']['say']['available']} workers={health['workers']['count']}")

    # 1) 建项目 + 脚本 + 分镜 + 人物
    boot = invoke("bootstrap_project", {
        "name": f"AI Agent 教学视频（验证 {int(target)}s）",
        "requirement": "制作一个 AI Agent 教学视频，漫画教学风格",
        "style": "漫画教学风格",
        "target_duration": target,
        "shot_duration": shot_duration,
        "create_characters": True,
        "generate_references": True,
    })
    project_id = boot["project_id"]
    print(f"✓ 项目已建：{project_id}")
    print(f"  脚本 {boot['script_id']} | 分镜 {boot['storyboard_id']} "
          f"| {boot['scene_count']} 场景 / {boot['shot_count']} 镜头 "
          f"| 角色 {len(boot['character_ids'])} 个")

    # 2) 关键帧
    imgs = invoke("generate_all_images", {"project_id": project_id})
    print(f"→ 提交关键帧任务 {imgs['count']} 个")
    wait_tasks(project_id, "关键帧")

    # 3) 镜头视频
    vids = invoke("generate_all_videos", {"project_id": project_id})
    print(f"→ 提交视频任务 {vids['count']} 个")
    wait_tasks(project_id, "镜头视频", timeout=3600)

    # 4) 配音（批量）+ 配乐 + 字幕
    invoke("generate_voice", {"project_id": project_id})
    invoke("generate_music", {"project_id": project_id, "mood": "warm", "duration": target})
    wait_tasks(project_id, "配音与配乐", timeout=1800)

    # 5) 字幕
    invoke("generate_subtitle", {"project_id": project_id, "format": "srt"})
    wait_tasks(project_id, "字幕")

    # 6) 画质增强（对整个项目最新视频做一层基础增强，验证增强链）
    enhance = invoke("enhance_video", {
        "project_id": project_id,
        "operations": [{"op": "color", "contrast": 1.06, "saturation": 1.12},
                       {"op": "sharpen", "amount": 0.8},
                       {"op": "audio", "normalize": True}],
    })
    wait_tasks(project_id, "画质增强", timeout=1200)

    # 7) 合成
    compose = invoke("compose_video", {"project_id": project_id, "with_music": True,
                                       "with_subtitle": True, "auto_quality_check": True})
    wait_tasks(project_id, "合成与质检", timeout=1800)

    # 8) 结果
    overview = httpx.get(f"{BASE}/api/projects/{project_id}/overview", timeout=60).json()
    final = overview.get("final_output")
    quality = overview.get("quality")
    status = overview["status"]

    print("-" * 72)
    print(f"项目进度：{status['progress']}% | 工作流状态：{status['workflow_state']}")
    print(f"镜头：{status['counts']['shots_done']}/{status['counts']['shots']} 完成，"
          f"失败 {status['counts']['shots_failed']}")
    print(f"素材：{status['counts']['assets']} 个 | 任务：{status['counts']['tasks']} 个"
          f"（失败 {status['counts']['tasks_failed']}）")
    if final:
        print(f"最终成片：{final['url']}")
        print(f"  时长 {final['duration']:.2f}s | {final['width']}x{final['height']} "
              f"@ {final['fps']}fps | {final['size_bytes'] / 1024 / 1024:.2f} MB")
    else:
        print("!! 未产出最终成片")
    if quality:
        print(f"质检：{quality['status']} | 得分 {quality['score']} | "
              f"通过 {quality['passed']}/{quality['total']}")
        for item in quality["items"]:
            mark = {"PASS": "✓", "FAIL": "✗", "WARN": "!"}.get(item["status"], "?")
            line = f"  {mark} {item['name']}"
            if item["message"]:
                line += f" — {item['message']}"
            print(line)

    print("-" * 72)
    print(json.dumps({"project_id": project_id,
                      "final_url": final["url"] if final else None,
                      "progress": status["progress"],
                      "quality": quality["status"] if quality else None}, ensure_ascii=False))
    return 0 if final else 1


if __name__ == "__main__":
    raise SystemExit(main())
