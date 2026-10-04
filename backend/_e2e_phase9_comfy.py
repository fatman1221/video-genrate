"""Phase 9 真实出图验证：经 ComfyUI 走通「resolution → 模板 → 产物尺寸」全链路。

跑法（在 backend/ 下，需后端已在**隔离库**上启动）：
    unset PYTHONPATH && ../.venv/Scripts/python.exe _e2e_phase9_comfy.py [2K|4K|720p]

⚠️ 会真实占用 GPU。先用 2K（模型原生档）验证通路，再考虑 4K。
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("REGRESS_BASE", "http://127.0.0.1:8077")
TIER = (sys.argv[1] if len(sys.argv) > 1 else "2K").upper()


def invoke(name: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    r = httpx.post(f"{BASE}/api/skills/{name}/invoke", json=payload or {}, timeout=timeout)
    r.raise_for_status()
    out = r.json()
    if not out.get("ok"):
        raise RuntimeError(f"[{name}] 调用失败：{out.get('error')}")
    return out.get("data") or {}


def wait_task(task_id: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    tick = 0
    while time.time() < deadline:
        last = invoke("get_task_status", {"task_id": task_id, "include_logs": False})
        status = str(last.get("status") or "")
        tick += 1
        if tick % 6 == 0:
            print(f"    … {int(time.time() - (deadline - timeout))}s status={status} "
                  f"progress={last.get('progress')}", flush=True)
        if status in ("SUCCESS", "FAILED", "CANCELLED"):
            return last
        time.sleep(5)
    return {"status": "TIMEOUT", **last}


def main() -> None:
    print(f"\n=== 真实 ComfyUI 出图 · 目标档位 {TIER} ===", flush=True)
    boot = invoke("bootstrap_project", {
        "name": f"Phase9 真实出图 {TIER}", "requirement": "深夜办公室，一个人趴在桌上",
        "target_duration": 30, "shot_duration": 5,
        "create_characters": False, "generate_references": False,
    })
    pid = boot["project_id"]
    shots = invoke("list_shots", {"project_id": pid}).get("shots") or []
    shot_id = shots[0]["shot_id"]
    print(f"  project={pid} shot={shot_id}", flush=True)

    invoke("set_image_provider", {
        "project_id": pid, "image_provider": "comfyui",
        "scene_workflow_name": "qwen_image_scene",
        "negative_prompt": "text, watermark, lowres, blurry, extra fingers",
    })

    comp = invoke("compile_image_prompt", {"shot_id": shot_id, "resolution": TIER,
                                          "aspect_ratio": "16:9"})
    print(f"  compiled {comp.get('code')} v{comp.get('version')} "
          f"({len(str(comp.get('compiled_prompt')))} 字)", flush=True)

    task = invoke("generate_image", {"shot_id": shot_id, "provider": "comfyui",
                                     "resolution": TIER, "aspect_ratio": "16:9"})
    print(f"  task={task.get('task_id')} size={task.get('size')}", flush=True)
    for w in task.get("warnings") or []:
        print(f"  [warn] {w}", flush=True)

    t0 = time.time()
    final = wait_task(task["task_id"], timeout=2400)
    elapsed = int(time.time() - t0)
    print(f"  任务状态 {final.get('status')}（{elapsed}s）", flush=True)
    if final.get("status") != "SUCCESS":
        print(f"  [ERROR] {str(final.get('error'))[:400]}", flush=True)
        sys.exit(2)

    shots = invoke("list_shots", {"project_id": pid}).get("shots") or []
    row = next(s for s in shots if s["shot_id"] == shot_id)
    asset = invoke("get_asset", {"asset_id": row["image_asset_id"]}).get("asset") or {}
    print(f"  产物 {asset.get('width')}x{asset.get('height')}  "
          f"{(asset.get('size_bytes') or 0) / 1e6:.2f} MB  provider={asset.get('provider')}  "
          f"model={asset.get('model')}", flush=True)
    print(f"  file={asset.get('file_path')}", flush=True)
    print(f"  prompt_version_id={asset.get('prompt_version_id')}", flush=True)
    print(f"OK {'RESULT=' + str(asset.get('width')) + 'x' + str(asset.get('height'))}", flush=True)


if __name__ == "__main__":
    main()
