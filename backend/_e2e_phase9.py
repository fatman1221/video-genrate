"""Phase 9 端到端验证：分辨率档位 + 编译器产物灌入既有出图链路。

跑法（在 backend/ 下，需后端已在**隔离库**上启动）：
    unset PYTHONPATH && ../.venv/Scripts/python.exe _e2e_phase9.py

不触发 GPU：出图用 `local` provider（PIL 渲染），只验证"参数是否真的落到位"。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, ".")

import httpx

BASE = os.environ.get("REGRESS_BASE", "http://127.0.0.1:8077")
DB_PATH = os.environ.get(
    "REGRESS_DB",
    r"C:/Users/Administrator/WorkBuddy/video-generate/video-genrate/backend/_tmp_phase9/phase9.db",
)
OK: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{('  → ' + extra) if extra else ''}")
    if not cond:
        raise AssertionError(label + (" " + extra if extra else ""))
    OK.append(label)


def invoke(name: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    r = httpx.post(f"{BASE}/api/skills/{name}/invoke", json=payload or {}, timeout=timeout)
    r.raise_for_status()
    out = r.json()
    if not out.get("ok"):
        raise RuntimeError(f"[{name}] 调用失败：{out.get('error')}")
    return out.get("data") or {}


def try_invoke(name: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    r = httpx.post(f"{BASE}/api/skills/{name}/invoke", json=payload or {}, timeout=timeout)
    return r.json()


def wait_task(task_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = invoke("get_task_status", {"task_id": task_id})
        status = str(last.get("status") or "")
        if status in ("SUCCESS", "FAILED", "CANCELLED"):
            return last
        time.sleep(2)
    raise AssertionError(f"任务超时未结束：{task_id}（最后状态 {last.get('status')}）")


def main() -> None:
    print("\n=== A. 分辨率解析（纯函数口径）===")
    from app.services import resolution as res  # noqa: PLC0415

    check("4K 16:9 = 3840x2160", res.resolve_size("4K", "16:9") == (3840, 2160))
    check("2K 16:9 = 2752x1536（对齐官方推荐）", res.resolve_size("2K", "16:9") == (2752, 1536))
    check("2K 9:16 自动换轴", res.resolve_size("2K", "9:16") == (1536, 2752))
    check("不传档位 = 维持项目默认", res.resolve_size("", "") == (1280, 720))
    check("导出 4K 会带「高于原生档位」warning", res.describe("4K", "16:9")["above_native"] is True)
    check("导出 2K 不带 warning（就是原生档）", res.describe("2K", "16:9")["warnings"] == [])
    try:
        res.normalize_resolution("8K")
        check("未知档位应报错", False)
    except res.ResolutionError as exc:
        check("未知档位报错清楚", "720p" in str(exc), str(exc)[:60])

    print("\n=== B. 准备项目与镜头 ===")
    boot = invoke("bootstrap_project", {
        "name": "Phase9 端到端", "requirement": "验证分辨率与编译产物灌入出图链路",
        "target_duration": 30, "shot_duration": 5,
        "create_characters": True, "generate_references": False,
    })
    pid = boot["project_id"]
    shots = invoke("list_shots", {"project_id": pid}).get("shots") or []
    check("项目与镜头就绪", len(shots) > 0, f"{len(shots)} 镜")
    shot_id = shots[0]["shot_id"]
    # 出图走 local provider，避免占 GPU
    invoke("set_image_provider", {"project_id": pid, "image_provider": "local"})

    print("\n=== C. Provider 能力闸门（模型不写死）===")
    from sqlalchemy import create_engine, select as sa_select  # noqa: PLC0415
    from sqlalchemy.orm import Session as SaSession  # noqa: PLC0415

    import app.models as models  # noqa: PLC0415

    eng = create_engine(f"sqlite:///{DB_PATH}")
    original: list[str] = []
    with SaSession(eng) as db:
        rec = db.execute(sa_select(models.ProviderRecord).where(
            models.ProviderRecord.name == "comfyui",
            models.ProviderRecord.kind == "image")).scalars().first()
        check("Provider 注册表里有 comfyui(image)", rec is not None)
        original = list(rec.capabilities or [])
        check("comfyui 声明了可被要求的分辨率档位",
              any(str(c).lower().startswith("resolution:") for c in original), str(original)[:90])
        check("comfyui 声明了原生档位 2K",
              any("native_resolution:2K" == str(c) for c in original))
        # 临时收窄为"只支持 720p/1080p"，验证 4K 会被挡下
        rec.capabilities = ["text_to_image", "resolution:720p,1080p"]
        db.commit()

    rejected = try_invoke("generate_image", {"shot_id": shot_id, "provider": "comfyui",
                                             "resolution": "4K", "aspect_ratio": "16:9"})
    check("超出 Provider 声明的档位被拒绝", rejected.get("ok") is False,
          str(rejected.get("error"))[:100])
    check("拒绝原因列出了它支持的档位", "720p" in str(rejected.get("error")),
          str(rejected.get("error"))[:100])
    check("被拒时不产生任务（无副作用）",
          httpx.get(f"{BASE}/api/projects/{pid}/tasks", timeout=30).json().get("total", 0) == 0)

    with SaSession(eng) as db:
        rec = db.execute(sa_select(models.ProviderRecord).where(
            models.ProviderRecord.name == "comfyui",
            models.ProviderRecord.kind == "image")).scalars().first()
        rec.capabilities = original
        db.commit()
    eng.dispose()
    print("  [info] 已恢复 comfyui 能力声明")

    print("\n=== D. 编译产物灌入（新层优先，老字段兜底）===")
    invoke("create_continuity_lock", {
        "project_id": pid, "name": "战术手电",
        "surface": "磨砂铝合金外壳的战术手电筒"})
    comp = invoke("compile_image_prompt", {"shot_id": shot_id, "resolution": "4K",
                                           "aspect_ratio": "16:9"})
    check("编译成功", bool(comp.get("prompt_id")), f"{comp.get('code')} v{comp.get('version')}")
    compiled_text = str(comp.get("compiled_prompt") or "")

    # 往老字段里塞一句"垃圾"，用来证明新层确实压过老字段
    poison = "LEGACY-FIELD-POISON-SHOULD-NOT-BE-USED"
    invoke("update_shot", {"shot_id": shot_id, "image_prompt": poison})

    task = invoke("generate_image", {"shot_id": shot_id, "provider": "local",
                                     "resolution": "4K", "aspect_ratio": "16:9"})
    check("generate_image 仍返回 task_id（契约未破）", bool(task.get("task_id")),
          str(task.get("task_id")))
    check("信封里同时带回了 taskId 别名", bool(task.get("taskId")))
    size = task.get("size") or {}
    check("返回解析后的尺寸 3840x2160",
          size.get("width") == 3840 and size.get("height") == 2160, str(size))
    check("4K 超原生带 warning", bool(task.get("warnings")), str(task.get("warnings"))[:70])

    final = wait_task(task["task_id"])
    check("出图任务成功", final.get("status") == "SUCCESS",
          f"{final.get('status')} / {str(final.get('error'))[:80]}")

    shots = invoke("list_shots", {"project_id": pid}).get("shots") or []
    shot = next(s for s in shots if s["shot_id"] == shot_id)
    asset_id = shot.get("image_asset_id")
    check("关键帧已回写", bool(asset_id), str(asset_id))
    asset = invoke("get_asset", {"asset_id": asset_id}).get("asset") or {}
    check("产物尺寸 = 3840x2160（4K 真的落到位）",
          int(asset.get("width") or 0) == 3840 and int(asset.get("height") or 0) == 2160,
          f"{asset.get('width')}x{asset.get('height')}")
    check("产物记录挂到了编译出的 PromptVersion（血缘闭环）",
          bool(asset.get("prompt_version_id")), str(asset.get("prompt_version_id")))

    print("\n=== E. 血缘与「新层压过老字段」的实证 ===")
    prov = httpx.get(f"{BASE}/api/assets/{asset_id}/provenance", timeout=30).json()
    chain = prov.get("chain") or {}
    check("血缘链完整（无 missing）", prov.get("complete") is True, str(prov.get("missing")))
    for key in ("project", "prompt", "prompt_version", "shot", "scene", "storyboard", "task"):
        check(f"血缘链含 {key}", bool(chain.get(key)), str(chain.get(key))[:40])
    pv = chain.get("prompt_version") or {}
    # 不给兜底默认值：拿不到正文就应该 FAIL，否则这条断言是空转的
    check("血缘链里带得出当时用的正文",
          bool(pv.get("compiled_prompt")), str(pv.get("compiled_prompt"))[:50])
    check("血缘链里的正文 = 编译正文（不是老字段）",
          compiled_text[:40] in str(pv.get("compiled_prompt") or ""),
          str(pv.get("compiled_prompt"))[:60])
    check("血缘链带编译输入快照（可判当时依据哪一版设定）",
          bool(pv.get("compiled_from")), str(list((pv.get("compiled_from") or {}).keys())))
    check("血缘链带锁与参考图槽位",
          "continuity_lock_ids" in pv and "reference_assets" in pv)
    check("产出的 asset.prompt 用的不是被投毒的老字段", poison not in str(asset.get("prompt") or ""),
          str(asset.get("prompt"))[:70])
    check("产出的 asset.prompt 含编译正文片段",
          compiled_text[:30] in str(asset.get("prompt") or ""),
          str(asset.get("prompt"))[:70])

    print("\n=== F. 无编译产物时回落到老字段（向后兼容）===")
    shot2 = shots[1]["shot_id"]
    invoke("update_shot", {"shot_id": shot2, "image_prompt": "老链路提示词：锈蚀舱室，蓝色应急灯"})
    task2 = invoke("generate_image", {"shot_id": shot2, "provider": "local"})
    check("不传 resolution 时尺寸回落到项目默认 1280x720",
          (task2.get("size") or {}).get("width") == 1280
          and (task2.get("size") or {}).get("height") == 720, str(task2.get("size")))
    final2 = wait_task(task2["task_id"])
    check("老链路出图成功", final2.get("status") == "SUCCESS", str(final2.get("error"))[:80])
    shots = invoke("list_shots", {"project_id": pid}).get("shots") or []
    shot2_row = next(s for s in shots if s["shot_id"] == shot2)
    asset2 = invoke("get_asset", {"asset_id": shot2_row["image_asset_id"]}).get("asset") or {}
    check("老链路产物用了 Shot.image_prompt",
          "老链路提示词" in str(asset2.get("prompt") or ""), str(asset2.get("prompt"))[:60])
    check("老链路产物尺寸 1280x720",
          int(asset2.get("width") or 0) == 1280 and int(asset2.get("height") or 0) == 720,
          f"{asset2.get('width')}x{asset2.get('height')}")

    print(f"\n全部 {len(OK)} 项断言通过。project_id={pid}")


if __name__ == "__main__":
    main()
