#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为角色一次生成多张「定妆候选图」，供人工挑选后再确定为基准参考图。

为什么需要这个脚本？
    一角色的定妆图会锁定全片所有出镜镜头的脸（本项目用「定妆图做底图 + 参考图编辑」
    保证人物一致性）。所以「一次只出一张、出完即定案」是错的 —— 形象没得挑，
    不满意只能推翻重来，而且第一张会被立刻写进 character.reference_asset_id。

    正确的做法是：先出一批**互不覆盖**的候选（role=reference_candidate），
    人工挑中后用 set_character_reference 提升为基准，其余自动降级留档。

内置形象方向（--presets 可挑选子集）：
    A 清瘦硬朗    B 沧桑疲惫    C 年轻利落    D 温和内敛
    E 硬朗粗粝    F 清冷疏离    G 沉稳厚重    H 清秀未褪

用法：
    # 出全部 8 个方向
    python scripts/gen_character_variants.py --project proj_xxx --character 林野

    # 只出其中几个方向
    python scripts/gen_character_variants.py --project proj_xxx --character chr_xxx --presets A,C,E

    # 同一方向出 4 张（同词不同种子）
    python scripts/gen_character_variants.py --project proj_xxx --character 林野 --presets A --count 4

    # 自定义方向（JSON 数组：[{label, prompt, note, seed}]）
    python scripts/gen_character_variants.py --project proj_xxx --character 林野 --file my_directions.json

    # 出完自动把某张提升为基准（asset_id 由 --pick 指定，或 --pick latest）
    python scripts/gen_character_variants.py --project proj_xxx --character 林野 --pick latest

环境变量：
    STUDIO  后端地址，默认 http://127.0.0.1:8077
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

STUDIO = os.environ.get("STUDIO", "http://127.0.0.1:8077").rstrip("/")
API = f"{STUDIO}/api"

# 公共基线：与角色 reference_prompt 同源，保证候选之间只在「形象方向」上分叉
BASE = (
    "角色设定图，写实电影感：35 岁中国男性，{face}；{hair}，{build}，神情{temperament}，"
    "穿深灰色哑光航天工装内衬高领，正面半身，柔和自然光，浅灰纯净背景，干净构图"
)

# 8 个形象方向：只改「面部 / 发型 / 身形 / 气质」四个维度，其余全部锁死，
# 保证挑出来的是「同一部片里的同一个角色」，而不是八个不同的人。
PRESETS: dict[str, dict[str, str]] = {
    "A": {
        "label": "清瘦硬朗",
        "note": "原设定：轮廓清瘦硬朗，眉骨深，下颌线清晰，沉静内敛",
        "prompt": BASE.format(
            face="轮廓清瘦硬朗，眉骨深，鼻梁挺直，下颌线清晰，眼下有浅青黑眼圈",
            hair="短寸头略带杂乱", build="身形偏瘦但挺拔", temperament="沉静内敛，眼神清澈专注"),
    },
    "B": {
        "label": "沧桑疲惫",
        "note": "加深岁月感：胡渣、眼窝更深、脸更削，多年值守的痕迹写在脸上",
        "prompt": BASE.format(
            face="面部削瘦，颧骨与眉骨突出，眼窝深陷，眼下青黑明显，下巴有淡淡胡渣，脸颊有一道旧疤",
            hair="短寸头略显灰白，发际线略退", build="身形清瘦，锁骨明显",
            temperament="疲惫而坚忍，眼神里有化不开的孤独"),
    },
    "C": {
        "label": "年轻利落",
        "note": "更年轻：皮肤状态好、发型利落干净，眼神锐利",
        "prompt": BASE.format(
            face="面部线条利落干净，眉形清晰，眼睛明亮有神，皮肤状态好，无疲态",
            hair="短寸头修剪利落整齐", build="身形挺拔匀称",
            temperament="沉静专注，锐利而克制"),
    },
    "D": {
        "label": "温和内敛",
        "note": "更柔和：眉眼温润，书卷气，攻击性低",
        "prompt": BASE.format(
            face="面部线条柔和温润，眉形舒展，眼型偏长，眼角微垂，鼻梁挺直，嘴唇略薄",
            hair="略长的短发自然垂落，发丝柔软", build="身形偏瘦，肩背放松",
            temperament="温和内敛，眼底有暖意"),
    },
    "E": {
        "label": "硬朗粗粝",
        "note": "更男人：骨相更重，粗粝有故事感",
        "prompt": BASE.format(
            face="骨相厚重，眉骨与颧骨明显，鼻梁高挺，下颌方正有力，面部皮肤有细微纹理，下巴青胡茬",
            hair="短寸头干脆利落", build="肩背厚实，身形健壮",
            temperament="沉稳果决，眼神直接而坚定"),
    },
    "F": {
        "label": "清冷疏离",
        "note": "更冷：面部更瘦长，眼神更远，冷调疏离",
        "prompt": BASE.format(
            face="面部瘦长，轮廓分明，眼型偏细长，瞳孔深黑，嘴唇平直，肤色偏冷",
            hair="短寸头利落，发色深", build="身形清瘦，脖颈线条清晰",
            temperament="清冷疏离，目光越过镜头看向远方"),
    },
    "G": {
        "label": "沉稳厚重",
        "note": "更可靠：体量感强，厚重可信",
        "prompt": BASE.format(
            face="面部轮廓厚重有力，眉骨深，鼻梁挺直，下颌线清晰，面部线条成熟",
            hair="短寸头整齐", build="身形结实，肩宽背厚，坐姿挺拔",
            temperament="沉稳厚重，眼神坚定有担当"),
    },
    "H": {
        "label": "清秀未褪",
        "note": "少年感：更清秀、更亮，反差感强",
        "prompt": BASE.format(
            face="面部清秀，皮肤细腻，眉眼疏朗，鼻梁挺直，下颌线清爽，眼神明亮",
            hair="短寸头柔软，额前有少许碎发", build="身形清瘦挺拔",
            temperament="安静克制，眼神干净清亮"),
    },
}


def call_skill(name: str, payload: dict[str, Any], *, timeout: float = 120) -> dict[str, Any]:
    clean = {k: v for k, v in payload.items() if v is not None}
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{API}/skills/{name}/invoke", json=clean)
        if r.status_code >= 400:
            raise SystemExit(f"[!] {name} HTTP {r.status_code}\n{r.text[:1500]}")
        env = r.json()
    if not env.get("ok", True):
        raise SystemExit(f"[!] {name} 失败：{json.dumps(env, ensure_ascii=False)[:1500]}")
    return env.get("data") or {}


def get_task(task_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=60) as c:
        r = c.get(f"{API}/tasks/{task_id}")
        r.raise_for_status()
        env = r.json()
    data = env.get("data") or env
    return data.get("task") or data


def resolve_character(project_id: str, who: str) -> dict[str, Any]:
    """--character 支持角色 ID 或名字。"""
    data = call_skill("get_character", {"project_id": project_id})
    chars = data.get("characters") or []
    for ch in chars:
        if ch.get("character_id") == who or ch.get("name") == who:
            return ch
    names = "、".join(f"{c.get('name')}({c.get('character_id')})" for c in chars) or "（无）"
    raise SystemExit(f"[!] 项目 {project_id} 里找不到角色「{who}」。可用：{names}")


def main() -> None:
    ap = argparse.ArgumentParser(description="为角色批量生成定妆候选图")
    ap.add_argument("--project", required=True)
    ap.add_argument("--character", required=True, help="角色 ID 或名字")
    ap.add_argument("--presets", default="A,B,C,D,E,F,G,H",
                    help="内置形象方向，逗号分隔，如 A,C,E")
    ap.add_argument("--count", type=int, default=1,
                    help="每个方向出几张（>1 时同词不同种子）")
    ap.add_argument("--file", default="", help="自定义方向 JSON 文件，给了就以它为准")
    ap.add_argument("--provider", default="comfyui")
    ap.add_argument("--workflow", default="qwen_image_character")
    ap.add_argument("--seed", type=int, default=None, help="基准种子，逐个候选 +1")
    ap.add_argument("--pick", default="", help="出完后提升为基准：asset_id 或 latest")
    ap.add_argument("--no-wait", action="store_true", help="只提交不等待")
    ap.add_argument("--out", default="", help="候选清单落盘路径（默认 .tmp/<角色>_variants.json）")
    args = ap.parse_args()

    char = resolve_character(args.project, args.character)
    char_id, char_name = char["character_id"], char.get("name") or args.character

    if args.file:
        specs = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if not isinstance(specs, list) or not specs:
            raise SystemExit("[!] --file 需要是一个非空的 JSON 数组")
    else:
        keys = [k.strip().upper() for k in args.presets.split(",") if k.strip()]
        unknown = [k for k in keys if k not in PRESETS]
        if unknown:
            raise SystemExit(f"[!] 未知方向 {unknown}，可选：{sorted(PRESETS)}")
        specs = [dict(PRESETS[k]) for k in keys]

    variants: list[dict[str, Any]] = []
    for spec in specs:
        for i in range(max(1, args.count)):
            item = {"label": spec.get("label") or "候选", "prompt": spec.get("prompt") or "",
                    "note": spec.get("note") or ""}
            if args.count > 1:
                item["label"] = f"{item['label']} {i + 1}"
            if spec.get("seed") is not None:
                item["seed"] = int(spec["seed"]) + i
            variants.append(item)

    print(f"[i] 角色：{char_name}（{char_id}）")
    print(f"[i] 当前基准图：{char.get('reference_asset_id')}")
    print(f"[i] 待出候选 {len(variants)} 张，方向：" +
          "、".join(v["label"] for v in variants))
    print(f"[i] 工作流：{args.workflow} @ {args.provider}（候选不会覆盖当前基准图）\n")

    res = call_skill("generate_character_reference", {
        "character_id": char_id, "provider": args.provider,
        "workflow_name": args.workflow, "variants": variants, "seed": args.seed,
    })
    tid = res.get("task_id") or res.get("id")
    print(f"[+] 任务已提交：{tid}")
    if args.no_wait:
        print("    （--no-wait：不等待，任务在后台执行）")
        return

    t0 = time.time()
    last = ""
    while True:
        task = get_task(tid)
        state = task.get("status")
        line = f"  [{state}] {task.get('progress')}%  {task.get('name') or ''}"
        if line != last:
            print(line, flush=True)
            last = line
        if state in ("SUCCESS", "SUCCEEDED", "COMPLETED", "DONE"):
            break
        if state in ("FAILED", "CANCELLED"):
            raise SystemExit(f"[!] 任务失败：{task.get('error') or state}")
        time.sleep(5)

    result = task.get("result") or {}
    payload = result.get("result") if isinstance(result.get("result"), dict) else result
    candidates = payload.get("candidates") or []
    failed = payload.get("failed") or []
    used = time.time() - t0

    print(f"\n[✓] 候选完成 {len(candidates)} 张，用时 {used / 60:.1f} 分钟")
    for c in candidates:
        print(f"  · {c['label']:<12} {c['asset_id']}  seed={c.get('seed')}\n      {c.get('file_path')}")
    for f in failed:
        print(f"  [x] {f['label']} 失败：{f['error']}")

    out = Path(args.out) if args.out else Path(".tmp") / f"{char_name}_variants.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "project_id": args.project, "character_id": char_id, "character_name": char_name,
        "task_id": tid, "workflow": args.workflow, "provider": args.provider,
        "reference_asset_id": payload.get("reference_asset_id"),
        "candidates": candidates, "failed": failed,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[i] 候选清单已写入 {out}")

    if args.pick:
        target = candidates[-1]["asset_id"] if args.pick == "latest" else args.pick
        picked = call_skill("set_character_reference",
                            {"character_id": char_id, "asset_id": target})
        print(f"[✓] 已把 {target} 设为基准参考图（旧基准 "
              f"{picked.get('previous_asset_id')} 已降级为候选）")


if __name__ == "__main__":
    main()
