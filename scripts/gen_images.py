"""批量出图脚本 —— 用本地 ComfyUI + Qwen-Image 生成「人物图」与「场景图」。

这是一个「薄客户端」：它不直接调 ComfyUI，而是走 Studio 的 Skill API，
这样每次出图的 prompt / model / 工作流 / 参数都会被记进 assets 表，
可追溯、可重跑、可回退 —— 这正是这套工程存在的意义。

用法（先确保后端已启动、ComfyUI 已启动）：

    # 0) 看一眼 ComfyUI 与工作流模板是否就绪
    python scripts/gen_images.py doctor

    # 1) 建项目并固化出图配置（人物图 / 场景图各用哪个模板）
    python scripts/gen_images.py init --name "我的短剧"

    # 2) 出人物图（角色设定图）
    python scripts/gen_images.py character --name "小林" \
        --appearance "20岁短发女生，米色卫衣，温和神情"

    # 3) 出场景图
    python scripts/gen_images.py scene --prompt "清晨的大学图书馆，阳光从高窗斜射，木质长桌"

    # 4) 一次出多张（从 json 文件读，见 --file 说明）
    python scripts/gen_images.py batch --file shots.json

环境变量：
    STUDIO   后端地址，默认 http://127.0.0.1:8077
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

STUDIO = os.environ.get("STUDIO", "http://127.0.0.1:8077").rstrip("/")
API = f"{STUDIO}/api"


# --------------------------------------------------------------------------- #
# Skill 调用封装
# --------------------------------------------------------------------------- #
def call_skill(name: str, payload: dict[str, Any], *, timeout: float = 120) -> dict[str, Any]:
    """调用一个 Skill，返回其 `data` 载荷（已剥离 {ok,status,data} 信封）。

    所有生成类 Skill 都是异步的，这里只负责提交。
    注意：Skill 层有 JSON Schema 校验，显式传 null 会被判为类型错误，
    因此这里统一剔除取值 None 的键，让服务端使用默认值。
    """
    payload = {k: v for k, v in payload.items() if v is not None}
    url = f"{API}/skills/{name}/invoke"
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url, json=payload)
        if r.status_code >= 400:
            raise SystemExit(f"[!] {name} 调用失败 HTTP {r.status_code}\n{r.text[:1200]}")
        env = r.json()
    if not env.get("ok", True):
        raise SystemExit(f"[!] {name} 返回失败：{json.dumps(env, ensure_ascii=False)[:1200]}")
    return env.get("data") or {}


def wait_task(task_id: str, *, timeout: float = 2400) -> dict[str, Any]:
    """轮询任务直到终态。出图是长任务，提交不阻塞，但这里要等结果。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        with httpx.Client(timeout=60) as c:
            r = c.get(f"{API}/tasks/{task_id}")
            r.raise_for_status()
            env = r.json()
        task = env.get("task") or env.get("data", {}).get("task") or env
        state = task.get("status")
        progress = task.get("progress")
        line = f"  [{state}] {progress}%"
        if line != last:
            print(line, flush=True)
            last = line
        if state in ("SUCCEEDED", "SUCCESS", "COMPLETED", "DONE"):
            return task
        if state in ("FAILED", "CANCELLED"):
            raise SystemExit(f"[!] 任务失败 {task_id}：{task.get('error') or ''}")
        time.sleep(3)
    raise SystemExit(f"[!] 任务超时 {task_id}")


def resolve_asset_url(payload: dict[str, Any]) -> str | None:
    """从任务结果里挖出产物 URL / 本地路径。不同 Skill 返回结构略有差异。"""
    node: Any = payload
    for _ in range(8):
        if not isinstance(node, dict):
            break
        if node.get("url"):
            return node["url"]
        if node.get("file_path"):
            return node["file_path"]
        nxt = (node.get("result") or node.get("data") or node.get("asset")
               or node.get("output") or node.get("task"))
        if nxt is None:
            break
        node = nxt
    return None



# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_doctor() -> None:
    print("== Studio 后端 ==")
    try:
        with httpx.Client(timeout=15) as c:
            h = c.get(f"{API}/health").json()
        print(f"  status={h.get('status')}  workers={h.get('workers', {}).get('count')}  "
              f"providers={h.get('providers')}")
        db = h.get("database", {})
        print(f"  DB: {db.get('dialect')} ok={db.get('ok')}")
    except Exception as exc:
        raise SystemExit(f"  [x] 后端连接失败：{exc}\n  请先启动后端")

    print("== ComfyUI ==")
    try:
        with httpx.Client(timeout=15) as c:
            s = c.get("http://127.0.0.1:8188/system_stats").json().get("system", {})
        print(f"  OK  ComfyUI {s.get('comfyui_version')}  "
              f"RAM free {round(s.get('ram_free', 0) / 1e9, 1)} GB")
    except Exception as exc:
        print(f"  [x] ComfyUI 连不上：{exc}")

    print("== 工作流模板 ==")
    for t in call_skill("list_image_workflows", {}).get("templates", []):
        print(f"  - {t['key']:26s} {t['description'][:46]}")
        for m in t.get("models", []):
            print(f"      model: {m}")

    print("== 图像 Provider ==")
    with httpx.Client(timeout=15) as c:
        provs = c.get(f"{API}/providers").json()
    if isinstance(provs, dict):
        provs = provs.get("providers") or provs.get("data") or []
    for p in provs:
        if p.get("kind") == "image":
            print(f"  - {p['name']:10s} default={p.get('is_default')} "
                  f"functional={p.get('functional')}")


def cmd_init(args: argparse.Namespace) -> None:
    res = call_skill("create_project", {
        "name": args.name,
        "requirement": args.requirement or f"{args.name} 的人物图与场景图素材",
        "style": args.style,
        "target_duration": 60,
        "width": args.width, "height": args.height,
    })
    pid = res.get("project_id")
    print(f"[+] 项目已创建：{pid}")

    cfg = call_skill("set_image_provider", {
        k: v for k, v in {
            "project_id": pid,
            "image_provider": "comfyui",
            "scene_workflow_name": args.scene_workflow,
            "character_workflow_name": args.character_workflow,
            "scene_prompt_prefix": args.scene_prefix,
            "character_prompt_prefix": args.character_prefix,
            "negative_prompt": args.negative,
        }.items() if v is not None  # Skill 有 JSON Schema 校验，null 会被拒
    })
    print("[+] 出图配置已固化：")
    print(json.dumps(cfg.get("image_config", {}), ensure_ascii=False, indent=2))
    print(f"\n项目 ID = {pid}   （后续命令用 --project {pid}）")


def cmd_character(args: argparse.Namespace) -> None:
    res = call_skill("create_character", {
        "project_id": args.project,
        "name": args.name,
        "role": args.role,
        "appearance": args.appearance,
        "personality": args.personality,
        "auto_reference": False,
    })
    cid = res.get("character_id")
    print(f"[+] 角色已创建：{args.name} -> {cid}")

    t = call_skill("generate_character_reference", {
        "character_id": cid,
        "provider": "comfyui",
        "workflow_name": args.workflow,
        "seed": args.seed,
    })
    task_id = t.get("task_id")
    print(f"[+] 已提交人物图任务：{task_id}")
    outcome = wait_task(task_id)
    print(f"[✓] 完成：{resolve_asset_url(outcome)}")


def cmd_scene(args: argparse.Namespace) -> None:
    """场景图：建一个场景 + 一个镜头来承载这张图（复用镜头的关键帧能力）。

    Studio 没有独立的 create_scene —— 场景是分镜表里的结构，
    所以这里用 create_storyboard 建「一个只含一个场景、一个镜头」的分镜，
    再对该镜头调 generate_image。这样产出的图片会正常挂在 scene/shot 上，
    后续要接视频时可以直接沿用它。
    """
    board = call_skill("create_storyboard", {
        "project_id": args.project,
        "title": args.title,
        "synopsis": args.prompt[:200],
        "scenes": [{
            "title": args.title,
            "summary": args.prompt[:200],
            "location": args.location or args.title,
            "mood": args.mood,
            "shots": [{
                "sequence": 1,
                "description": args.prompt,
                "image_prompt": args.prompt,
                "duration": 5,
            }],
        }],
    })
    # create_storyboard 不返回 shot_id，用 list_shots 回查本项目最新的镜头
    listed = call_skill("list_shots", {"project_id": args.project})
    shots = listed.get("shots") or []
    if not shots:
        raise SystemExit(f"[!] 分镜创建了但没有镜头，返回：{json.dumps(board, ensure_ascii=False)[:400]}")
    shot_id = shots[-1]["shot_id"]
    print(f"[+] 场景「{args.title}」已创建，镜头：{shot_id}")

    t = call_skill("generate_image", {
        "shot_id": shot_id, "provider": "comfyui",
        "workflow_name": args.workflow, "seed": args.seed,
    })
    task_id = t.get("task_id")
    print(f"[+] 已提交场景图任务：{task_id}")
    outcome = wait_task(task_id)
    print(f"[✓] 完成：{resolve_asset_url(outcome)}")


def _collect_shots(board: dict[str, Any]) -> list[str]:
    """从嵌套结构里挖出所有 shot_id（create_storyboard 的返回结构可能变化时兜底）。"""
    ids: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("shot_id"), str):
                ids.append(node["shot_id"])
            elif str(node.get("id", "")).startswith("shot_"):
                ids.append(node["id"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(board)
    return list(dict.fromkeys(ids))  # 去重且保持顺序




def cmd_batch(args: argparse.Namespace) -> None:
    """从 json 批量出图。

    json 格式（人物与场景可混排）：
    {
      "characters": [
        {"name":"小林","appearance":"20岁短发女生，米色卫衣"},
        {"name":"导师","appearance":"45岁男性，深色西装，戴眼镜"}
      ],
      "scenes": [
        {"title":"图书馆清晨","prompt":"清晨的大学图书馆，阳光从高窗斜射"},
        {"title":"雨夜街头","prompt":"雨夜的城市街道，霓虹倒影，湿滑路面"}
      ]
    }
    """
    spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
    chars = spec.get("characters") or []
    scenes = spec.get("scenes") or []
    print(f"[i] 待生成：人物 {len(chars)} 个，场景 {len(scenes)} 个")
    print("[i] 本地 ComfyUI 串行出图，请耐心等待\n")

    for spec_c in chars:
        ns = argparse.Namespace(
            project=args.project, name=spec_c["name"], role=spec_c.get("role", "supporting"),
            appearance=spec_c.get("appearance", ""), personality=spec_c.get("personality", ""),
            workflow=spec_c.get("workflow_name") or args.character_workflow,
            seed=spec_c.get("seed"),
        )
        print(f"--- 人物：{ns.name} ---")
        cmd_character(ns)
        print()

    for spec_s in scenes:
        ns = argparse.Namespace(
            project=args.project, title=spec_s.get("title", "场景"),
            prompt=spec_s["prompt"], location=spec_s.get("location", ""),
            mood=spec_s.get("mood", ""),
            workflow=spec_s.get("workflow_name") or args.scene_workflow,
            seed=spec_s.get("seed"),
        )
        print(f"--- 场景：{ns.title} ---")
        cmd_scene(ns)
        print()

    print("[✓] 全部完成")


# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(description="用本地 ComfyUI + Qwen-Image 批量出人物图与场景图")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="自检：后端 / ComfyUI / 模板 / Provider").set_defaults(func=lambda a: cmd_doctor())

    pi = sub.add_parser("init", help="建项目并固化出图配置")
    pi.add_argument("--name", required=True)
    pi.add_argument("--requirement", default="")
    pi.add_argument("--style", default="写实电影感")
    pi.add_argument("--width", type=int, default=1280)
    pi.add_argument("--height", type=int, default=720)
    pi.add_argument("--scene-workflow", default="qwen_image_scene")
    pi.add_argument("--character-workflow", default="qwen_image_character")
    pi.add_argument("--scene-prefix", default="")
    pi.add_argument("--character-prefix", default="")
    pi.add_argument("--negative", default="低质量, 模糊, 变形, 多余手指, 文字水印")
    pi.set_defaults(func=cmd_init)

    pc = sub.add_parser("character", help="生成人物图 / 角色设定图")
    pc.add_argument("--project", required=True)
    pc.add_argument("--name", required=True)
    pc.add_argument("--appearance", default="")
    pc.add_argument("--personality", default="")
    pc.add_argument("--role", default="supporting")
    pc.add_argument("--workflow", default=None)
    pc.add_argument("--seed", type=int, default=None)
    pc.set_defaults(func=cmd_character)

    ps = sub.add_parser("scene", help="生成场景图")
    ps.add_argument("--project", required=True)
    ps.add_argument("--prompt", required=True)
    ps.add_argument("--title", default="场景")
    ps.add_argument("--location", default="")
    ps.add_argument("--mood", default="")
    ps.add_argument("--workflow", default=None)
    ps.add_argument("--seed", type=int, default=None)
    ps.set_defaults(func=cmd_scene)

    pb = sub.add_parser("batch", help="从 json 批量出图")
    pb.add_argument("--project", required=True)
    pb.add_argument("--file", required=True, help="json 清单路径")
    pb.add_argument("--character-workflow", default="qwen_image_character")
    pb.add_argument("--scene-workflow", default="qwen_image_scene")
    pb.set_defaults(func=cmd_batch)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
