#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键成片：从项目规格 JSON 驱动全链路。

用法：
    python scripts/build_project.py --file examples/paipai_project.json
    python scripts/build_project.py --file examples/paipai_project.json --from images

链路：
    建项目 → 固化出图配置 → 建角色 → 角色定妆图 → 建分镜
    → 全部关键帧 → 全部镜头视频 → 全部配音 → 配乐 → 字幕
    → 拼接粗剪 → 合成成片（自动质检）

断点续跑：状态写在 <规格文件>.state.json；重复执行会跳过已完成阶段。
批量阶段（关键帧/视频/配音）本身幂等（only_missing），重跑安全。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

API = "http://127.0.0.1:8077/api"

STAGES = [
    "project", "config", "characters", "char_refs", "storyboard",
    "images", "videos", "voices", "music", "subtitle", "merge", "compose",
]

# --------------------------------------------------------------------------- #
# HTTP 基础
# --------------------------------------------------------------------------- #

def call_skill(name: str, payload: dict[str, Any], *, timeout: float = 120) -> dict[str, Any]:
    """调用一个 Skill，返回 data 载荷（剥离 {ok,status,data} 信封）。

    Skill 层有 JSON Schema 校验，显式传 null 会被判类型错误 —— 先剔除 None 值。
    """
    clean = {k: v for k, v in payload.items() if v is not None}
    url = f"{API}/skills/{name}/invoke"
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url, json=clean)
        if r.status_code >= 400:
            raise SystemExit(f"[!] {name} HTTP {r.status_code}\n{r.text[:1500]}")
        env = r.json()
    if not env.get("ok", True):
        raise SystemExit(f"[!] {name} 返回失败：{json.dumps(env, ensure_ascii=False)[:1500]}")
    return env.get("data") or {}


def _task_state(task: dict[str, Any]) -> tuple[str, Any]:
    return task.get("status"), task.get("progress")


def get_task(task_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=60) as c:
        r = c.get(f"{API}/tasks/{task_id}")
        r.raise_for_status()
        env = r.json()
    data = env.get("data") or env
    return data.get("task") or data


def wait_task(task_id: str, *, timeout: float = 3600, label: str = "") -> dict[str, Any]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        task = get_task(task_id)
        state, progress = _task_state(task)
        line = f"    [{state}] {progress}% {label or task.get('name', '')}"
        if line != last:
            print(line, flush=True)
            last = line
        if state == "SUCCESS":
            return task
        if state in ("FAILED", "CANCELLED"):
            raise SystemExit(f"[!] 任务失败 {task_id}（{label}）：{task.get('error') or '无错误信息'}")
        time.sleep(3)
    raise SystemExit(f"[!] 任务超时 {task_id}（{label}）")


def wait_batch(task_ids: list[str], *, label: str = "") -> None:
    """等一组任务全部到终态；任何一个失败立即中止。"""
    pending = dict.fromkeys(task_ids)
    done: dict[str, str] = {}
    while pending:
        for tid in list(pending):
            task = get_task(tid)
            state = task.get("status")
            if state == "SUCCESS":
                done[tid] = "ok"
                del pending[tid]
                print(f"    [ok] {task.get('name') or tid}", flush=True)
            elif state in ("FAILED", "CANCELLED"):
                raise SystemExit(
                    f"[!] 批量任务中有失败 {tid}（{task.get('name')}）："
                    f"{task.get('error') or '无错误信息'}\n"
                    f"    已成功的可保留，修复后重跑本脚本即可（断点续跑）。"
                )
        if pending:
            print(f"    ... {label} 进行中：剩 {len(pending)} / 共 {len(task_ids)}", flush=True)
            time.sleep(5)
    print(f"    [✓] {label} 全部完成（{len(done)} 个）", flush=True)


def resolve_asset_url(payload: Any) -> str | None:
    node = payload
    for _ in range(8):
        if not isinstance(node, dict):
            break
        for key in ("url", "file_path", "path"):
            if node.get(key):
                return node[key]
        nxt = (node.get("result") or node.get("asset") or node.get("task")
               or node.get("data") or node.get("output"))
        if nxt is None:
            break
        node = nxt
    return None


# --------------------------------------------------------------------------- #
# 状态管理（断点续跑）
# --------------------------------------------------------------------------- #

class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"stages": {}, "ids": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def done(self, stage: str) -> bool:
        return bool(self.data["stages"].get(stage))

    def mark(self, stage: str) -> None:
        self.data["stages"][stage] = True
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        return self.data["ids"].get(key, default)

    def put(self, key: str, value: Any) -> None:
        self.data["ids"][key] = value
        self.save()


# --------------------------------------------------------------------------- #
# 各阶段
# --------------------------------------------------------------------------- #

def stage_project(spec: dict, st: State) -> str:
    pid = st.get("project_id")
    if pid and st.done("project"):
        print(f"[=] 项目已存在：{pid}")
        return pid
    res = call_skill("create_project", {
        "name": spec["name"],
        "requirement": spec.get("requirement") or spec["name"],
        "style": spec.get("style", ""),
        "target_duration": spec.get("target_duration", 300),
        "language": spec.get("language", "zh"),
        "width": spec.get("width", 1280),
        "height": spec.get("height", 720),
        "fps": spec.get("fps", 24),
    })
    pid = res.get("project_id") or res.get("project", {}).get("id")
    if not pid:
        raise SystemExit(f"[!] create_project 未返回 project_id：{json.dumps(res, ensure_ascii=False)[:400]}")
    st.put("project_id", pid)
    st.mark("project")
    print(f"[+] 项目已创建：{pid}")
    return pid


def stage_config(spec: dict, st: State, pid: str) -> None:
    if st.done("config"):
        print("[=] 出图配置已固化，跳过")
        return
    call_skill("set_image_provider", {
        "project_id": pid,
        "image_provider": spec.get("image_provider", "comfyui"),
        "scene_workflow_name": spec.get("scene_workflow_name"),
        "character_workflow_name": spec.get("character_workflow_name"),
        "scene_prompt_prefix": spec.get("scene_prompt_prefix"),
        "character_prompt_prefix": spec.get("character_prompt_prefix"),
        "negative_prompt": spec.get("negative_prompt"),
        "video_provider": spec.get("video_provider"),
        "video_workflow_name": spec.get("video_workflow_name"),
    })
    st.mark("config")
    print("[+] 出图配置已固化到项目")


def stage_characters(spec: dict, st: State, pid: str) -> dict[str, str]:
    """建角色，返回 {角色名: character_id}。"""
    mapping: dict[str, str] = st.get("characters") or {}
    for ch in spec.get("characters", []):
        if ch["name"] in mapping:
            print(f"[=] 角色已存在：{ch['name']} -> {mapping[ch['name']]}")
            continue
        res = call_skill("create_character", {
            "project_id": pid,
            "name": ch["name"],
            "role": ch.get("role", "supporting"),
            "appearance": ch.get("appearance", ""),
            "personality": ch.get("personality", ""),
            "voice_style": ch.get("voice_style", ""),
            "reference_prompt": ch.get("reference_prompt", ""),
            "auto_reference": False,
        })
        cid = res.get("character_id") or res.get("character", {}).get("id")
        mapping[ch["name"]] = cid
        st.put("characters", mapping)
        print(f"[+] 角色已创建：{ch['name']} -> {cid}")
    st.mark("characters")
    return mapping


def stage_char_refs(spec: dict, st: State, mapping: dict[str, str]) -> None:
    if st.done("char_refs"):
        print("[=] 角色定妆图已完成，跳过")
        return
    tids = st.get("char_ref_tasks") or []
    if not tids:
        for name, cid in mapping.items():
            res = call_skill("generate_character_reference", {
                "character_id": cid, "provider": spec.get("image_provider", "comfyui"),
            })
            tid = res.get("task_id") or res.get("id")
            tids.append(tid)
            print(f"[+] 定妆图任务：{name} -> {tid}")
        st.put("char_ref_tasks", tids)
    wait_batch(tids, label="角色定妆图")
    st.mark("char_refs")


def stage_storyboard(spec: dict, st: State, pid: str) -> list[dict[str, Any]]:
    """建分镜，返回按 sequence 排序的镜头列表 [{id, code, sequence, ...}]。"""
    if not st.done("storyboard"):
        call_skill("create_storyboard", {
            "project_id": pid,
            "title": f"{spec['name']} · 分镜表",
            "synopsis": spec.get("requirement", ""),
            "visual_style": spec.get("style", ""),
            "scenes": spec["scenes"],
        }, timeout=180)
        st.mark("storyboard")
        print(f"[+] 分镜已创建：{len(spec['scenes'])} 场景")
    else:
        print("[=] 分镜已创建，跳过")

    res = call_skill("list_shots", {"project_id": pid})
    shots = res.get("shots") or res.get("items") or []
    if not shots:
        raise SystemExit("[!] list_shots 为空，分镜可能未创建成功")
    shots.sort(key=lambda s: s.get("sequence", 0))
    st.put("shots", [{"id": s["id"], "code": s.get("code"), "sequence": s.get("sequence")}
                     for s in shots])
    print(f"[=] 共 {len(shots)} 个镜头")
    return shots


def stage_images(spec: dict, st: State, pid: str) -> None:
    if st.done("images"):
        print("[=] 关键帧已全部生成，跳过")
        return
    tids = st.get("image_tasks")
    if not tids:
        res = call_skill("generate_all_images", {
            "project_id": pid, "provider": spec.get("image_provider", "comfyui"),
            "only_missing": True,
        })
        tids = res.get("task_ids") or []
        if not tids:
            print("[=] 所有镜头都已有关键帧")
            st.mark("images")
            return
        st.put("image_tasks", tids)
        print(f"[+] 已提交 {len(tids)} 个关键帧任务")
    wait_batch(tids, label="关键帧生成")
    st.mark("images")


def stage_videos(spec: dict, st: State, pid: str) -> None:
    if st.done("videos"):
        print("[=] 镜头视频已全部生成，跳过")
        return
    tids = st.get("video_tasks")
    if not tids:
        res = call_skill("generate_all_videos", {
            "project_id": pid, "provider": spec.get("video_provider", "local"),
            "only_missing": True,
        })
        tids = res.get("task_ids") or []
        if not tids:
            print("[=] 所有镜头都已有视频")
            st.mark("videos")
            return
        st.put("video_tasks", tids)
        print(f"[+] 已提交 {len(tids)} 个镜头视频任务")
    wait_batch(tids, label="镜头视频生成")
    st.mark("videos")


def stage_voices(spec: dict, st: State, pid: str) -> None:
    if st.done("voices"):
        print("[=] 配音已全部生成，跳过")
        return
    if (spec.get("voice_engine") or "").lower() == "qwen3tts":
        # 情感配音：Qwen3-TTS CustomVoice（每镜头 voice_instruct 控制语气）
        print("[*] 使用 Qwen3-TTS 情感配音（voice_instruct 驱动）")
        import subprocess
        script = Path(__file__).resolve().parent / "rebuild_voices.py"
        spec_path = str(st.path).removesuffix(".state.json")
        proc = subprocess.run(
            [sys.executable, str(script), "--spec", spec_path, "--project", pid],
        )
        if proc.returncode != 0:
            raise RuntimeError("Qwen3-TTS 配音失败，详见上方输出")
        st.mark("voices")
        return
    tid = st.get("voice_task")
    if not tid:
        res = call_skill("generate_voice", {
            "project_id": pid,
            "voice": spec.get("voice"),
            "rate": spec.get("voice_rate"),
        })
        tid = res.get("task_id") or res.get("id")
        st.put("voice_task", tid)
        print(f"[+] 批量配音任务：{tid}")
    wait_task(tid, label="批量配音")
    st.mark("voices")


def stage_music(spec: dict, st: State, pid: str) -> None:
    if st.done("music"):
        print("[=] 配乐已生成，跳过")
        return
    tid = st.get("music_task")
    if not tid:
        music = spec.get("music") or {}
        res = call_skill("generate_music", {
            "project_id": pid,
            "prompt": music.get("prompt"),
            "mood": music.get("mood", "calm"),
            "duration": spec.get("target_duration"),
        })
        tid = res.get("task_id") or res.get("id")
        st.put("music_task", tid)
        print(f"[+] 配乐任务：{tid}")
    wait_task(tid, label="配乐生成")
    st.mark("music")


def stage_subtitle(spec: dict, st: State, pid: str) -> None:
    if st.done("subtitle"):
        print("[=] 字幕已生成，跳过")
        return
    tid = st.get("subtitle_task")
    if not tid:
        res = call_skill("generate_subtitle", {"project_id": pid, "format": "srt"})
        tid = res.get("task_id") or res.get("id")
        st.put("subtitle_task", tid)
        print(f"[+] 字幕任务：{tid}")
    wait_task(tid, label="字幕生成")
    st.mark("subtitle")


def stage_merge(spec: dict, st: State, pid: str) -> None:
    if st.done("merge"):
        print("[=] 粗剪已拼接，跳过")
        return
    tid = st.get("merge_task")
    if not tid:
        res = call_skill("merge_video", {"project_id": pid})
        tid = res.get("task_id") or res.get("id")
        st.put("merge_task", tid)
        print(f"[+] 拼接任务：{tid}")
    task = wait_task(tid, label="拼接粗剪")
    print(f"    粗剪产物：{resolve_asset_url(task)}")
    st.mark("merge")


def stage_compose(spec: dict, st: State, pid: str) -> None:
    tid = st.get("compose_task")
    if not tid:
        res = call_skill("compose_video", {
            "project_id": pid,
            "with_music": True, "with_subtitle": True,
            "music_volume": 0.16, "voice_volume": 1.0,
            "font_size": 22, "auto_quality_check": True,
        })
        tid = res.get("task_id") or res.get("id")
        st.put("compose_task", tid)
        print(f"[+] 成片合成任务：{tid}")
    task = wait_task(tid, timeout=7200, label="合成最终成片")
    final = resolve_asset_url(task)
    st.put("final_video", final)
    st.mark("compose")
    print(f"[✓] 成片完成：{final}")


# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="从项目规格 JSON 一键成片（支持断点续跑）")
    ap.add_argument("--file", required=True, help="项目规格 JSON 路径")
    ap.add_argument("--from", dest="from_stage", default=None,
                    choices=STAGES, help="从指定阶段开始重跑（会清除该阶段及之后的状态）")
    args = ap.parse_args()

    spec_path = Path(args.file)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    st = State(spec_path.with_suffix(spec_path.suffix + ".state.json"))

    if args.from_stage:
        idx = STAGES.index(args.from_stage)
        for s in STAGES[idx:]:
            st.data["stages"].pop(s, None)
        st.save()
        print(f"[*] 将从阶段 {args.from_stage} 开始重跑")

    print(f"===== 《{spec['name']}》一键成片 =====")
    print(f"    目标时长 {spec.get('target_duration')}s | "
          f"{spec.get('width')}x{spec.get('height')}@{spec.get('fps')}fps | "
          f"{sum(len(sc['shots']) for sc in spec['scenes'])} 镜头")

    pid = stage_project(spec, st)
    stage_config(spec, st, pid)
    mapping = stage_characters(spec, st, pid)
    stage_char_refs(spec, st, mapping)
    stage_storyboard(spec, st, pid)
    stage_images(spec, st, pid)
    stage_videos(spec, st, pid)
    stage_voices(spec, st, pid)
    stage_music(spec, st, pid)
    stage_subtitle(spec, st, pid)
    stage_merge(spec, st, pid)
    stage_compose(spec, st, pid)

    print("\n===== 全部完成 =====")
    print(f"    项目 ID : {pid}")
    print(f"    成片    : {st.get('final_video')}")
    print(f"    状态文件: {st.path}")


if __name__ == "__main__":
    main()
