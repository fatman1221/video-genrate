"""端到端验证：采样参数可配 + seed 落库 + 产物可复现。

**需要真实后端（8077）+ 真实 ComfyUI（8188）在跑**，全程不 mock。
单元测试（`backend/tests/test_sampler_params.py`）用打桩覆盖逻辑，这个脚本负责证明
「真的提交了什么、真的落库了什么、同参同 seed 真的能逐字节复现」。

三个用例：
    ① 显式覆盖 steps=28 / cfg=4.0 / seed=20261006 → 落库应记实际值
    ② 与 ① 同 seed 同 prompt → 产物 md5 应与 ① **完全相同**（可复现性）
    ③ 不传 steps/cfg → 应回落到项目级 `image_steps` / `image_cfg`

用法（后端与 ComfyUI 都起着）：
    cd backend && unset PYTHONPATH && ../.venv/Scripts/python.exe ../scripts/verify_sampler_params.py

产物按 md5 比对，不写额外文件；退出码 0 = 全部通过。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

API = os.environ.get("STUDIO_API", "http://127.0.0.1:8077/api")
DB_PATH = Path(os.environ.get(
    "STUDIO_DB",
    Path(__file__).resolve().parent.parent / "backend" / "video_agent_studio.db",
))
PROMPT = "写实电影感科幻剧照，一名男性宇航员站在空间站舷窗前望向外面的星海，神态安静克制"
SEED = 20261006


def skill(c, name: str, payload: dict) -> dict:
    r = c.post(f"{API}/skills/{name}/invoke", json=payload, timeout=120)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise SystemExit(f"{name} 失败: {body.get('error')}")
    return body.get("data") or {}


def wait_task(c, task_id: str, timeout: float = 900) -> str:
    end = time.time() + timeout
    while time.time() < end:
        d = skill(c, "get_task_status", {"task_id": task_id})
        st = str(d.get("status") or "")
        if st in ("SUCCESS", "FAILED", "CANCELLED"):
            if st != "SUCCESS":
                print("   任务详情:", json.dumps(d, ensure_ascii=False)[:400])
            return st
        time.sleep(3)
    raise SystemExit("任务超时")


def md5(p: str) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    with httpx.Client(timeout=120) as c:
        proj = skill(c, "bootstrap_project", {
            "name": f"参数可配验证-{int(time.time())}", "requirement": "宇航员在空间站舷窗前",
            "target_duration": 30, "shot_duration": 5,
            "create_characters": False, "generate_references": False,
        })
        pid = proj["project_id"]
        print("项目:", pid)

        skill(c, "set_image_provider", {
            "project_id": pid, "image_provider": "comfyui",
            "scene_workflow_name": "qwen_image_scene",
            "scene_prompt_prefix": "", "negative_prompt": "",
            "image_steps": 28, "image_cfg": 4.0,
        })
        print("已配置 comfyui + qwen_image_scene + 项目级 image_steps=28 / image_cfg=4.0")

        shots = skill(c, "list_shots", {"project_id": pid})["shots"]
        print(f"镜头数: {len(shots)}\n")

        cases = [
            ("① 显式覆盖 steps/cfg", {"steps": 28, "cfg": 4.0, "seed": SEED}),
            ("② 同参数同 seed（验可复现）", {"steps": 28, "cfg": 4.0, "seed": SEED}),
            ("③ 不传（走项目级配置）", {"seed": 11}),
        ]
        results = []
        for (label, extra), shot in zip(cases, shots):
            payload = {"shot_id": shot["shot_id"], "provider": "comfyui",
                       "prompt": PROMPT, "width": 1280, "height": 720, **extra}
            t = skill(c, "generate_image", payload)
            st = wait_task(c, t["task_id"])
            print(f"{label} → {st}")
            if st != "SUCCESS":
                raise SystemExit("生成失败，中止")
            results.append((label, shot["shot_id"]))

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    print("\n=== 落库参数（Asset.parameters）===")
    rows = []
    for label, sid in results:
        r = db.execute(
            "select id,name,file_path,width,height,provider,workflow,parameters "
            "from assets where shot_id=? and type='IMAGE' order by created_at desc limit 1",
            (sid,)).fetchone()
        p = json.loads(r["parameters"] or "{}")
        rows.append((label, r, p))
        print(f"\n{label}")
        print(f"  尺寸 {r['width']}x{r['height']} | workflow {r['workflow']}")
        print(f"  steps={p.get('steps')}  cfg={p.get('cfg')}  seed={p.get('seed')}")
        print(f"  final_prompt[:60]={str(p.get('final_prompt'))[:60]}")
        print(f"  prefix_injected={p.get('prefix_injected')}")

    print("\n=== 校验 ===")
    checks = []
    checks.append(("① 显式 steps=28 生效", rows[0][2].get("steps") == 28))
    checks.append(("① 显式 cfg=4.0 生效", rows[0][2].get("cfg") == 4.0))
    checks.append(("① seed 落库 == 20261006", rows[0][2].get("seed") == SEED))
    checks.append(("② seed 落库一致", rows[1][2].get("seed") == SEED))
    same = md5(rows[0][1]["file_path"]) == md5(rows[1][1]["file_path"])
    checks.append(("② 同参同 seed 产物逐字节相同（可复现）", same))
    checks.append(("③ 未传时走项目级 28/4.0", (rows[2][2].get("steps"), rows[2][2].get("cfg")) == (28, 4.0)))
    checks.append(("seed 全部为 int（非 None）",
                   all(isinstance(r[2].get("seed"), int) for r in rows)))

    ok = True
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    print("\n结果:", "全部通过" if ok else "有未通过项")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
