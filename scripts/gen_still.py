#!/usr/bin/env python
"""高清静帧出图 —— 直接走 ComfyUI API，绕过项目的 1280x720 默认配置。

用途：出 2K 定妆图 / 海报 / 角色九视图等**不进视频链路**的静态素材。
（成片链路的关键帧仍应用 gen_shot_images.py，那里必须 1280x720 以免全片重跑。）

两条链路自动分派：
  - 无 --reference → qwen_image_character.json（Qwen-Image 2.1 纯文生图）
  - 有 --reference → qwen_edit_scene.json（Qwen-Image-Edit 2511，锁人物一致性）

示例：
  # 2K 定妆图（Qwen-Image 2.1 原生 16:9）
  python scripts/gen_still.py --prompt "角色设定图，..." --width 2752 --height 1536 \
      --out backend/storage/temp/stills/linye_2k.png

  # 带参考图（锁脸）
  python scripts/gen_still.py --prompt "同一个角色的正面全身..." --reference <png> \
      --width 1024 --height 1024 --out out.png
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "backend" / "app" / "workflows" / "templates"

NEGATIVE_DEFAULT = (
    "低质量，模糊，变形，多手多指，肢体错乱，文字水印，logo，签名，杂乱背景，"
    "过曝，噪点，卡通感，3D 渲染感，塑料感"
)


def _req(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 120):
    r = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read()


def _get_json(base: str, path: str):
    return json.loads(_req(base + path, timeout=60).decode("utf-8"))


def upload_image(base: str, img_path: Path) -> str:
    """把参考图上传到 ComfyUI 的 input 目录，返回可用于 LoadImage 的文件名。"""
    boundary = "----wb" + uuid.uuid4().hex
    name = f"wb_ref_{uuid.uuid4().hex[:8]}{img_path.suffix}"
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += img_path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    url = base + "/upload/image"
    r = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(r, timeout=180) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    # ComfyUI 返回 {"name":..., "subfolder":..., "type":"input"}
    sub = d.get("subfolder") or ""
    return f"{sub}/{d['name']}" if sub else d["name"]


def prep_reference(ref: Path, width: int, height: int, mode: str = "crop") -> Path:
    """把参考图处理成与目标同比例 —— 否则工作流里 ImageScale(crop=disabled)
    会用 lanczos 把参考图**非等比拉伸**（如 640x720 → 1024x1280 纵向拉长 11%），
    脸型跟着变形，身份对齐随之漂移。

    mode:
      stretch —— 不处理（原行为，会被拉伸）
      crop    —— 居中裁剪到目标比例（默认，最保真）
      pad     —— 等比缩放后四周补灰（不裁内容）
    """
    if mode == "stretch":
        return ref
    try:
        from PIL import Image
    except Exception:
        return ref
    im = Image.open(ref).convert("RGB")
    sw, sh = im.size
    tgt, src = width / height, sw / sh
    if abs(src - tgt) < 0.01:
        return ref
    if mode == "crop":
        if src > tgt:                       # 太宽 → 裁两侧
            nw = max(1, int(round(sh * tgt)))
            x0 = (sw - nw) // 2
            im = im.crop((x0, 0, x0 + nw, sh))
        else:                               # 太高 → 裁底部（人像脸在上方，先保头）
            nh = max(1, int(round(sw / tgt)))
            im = im.crop((0, 0, sw, min(nh, sh)))
    else:                                   # pad
        scale = min(width / sw, height / sh)
        nw, nh = max(1, int(round(sw * scale))), max(1, int(round(sh * scale)))
        small = im.resize((nw, nh), Image.LANCZOS)
        im = Image.new("RGB", (width, height), (128, 128, 128))
        im.paste(small, ((width - nw) // 2, (height - nh) // 2))
    out_dir = ROOT / "backend" / "storage" / "temp" / "refs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{ref.stem}_fit{width}x{height}.png"
    im.save(out)
    return out


def apply_overrides(wf: dict, steps: int = 0, cfg: float = 0.0,
                    sampler: str = "", scheduler: str = "") -> None:
    """覆盖工作流里 KSampler 的采样参数（模板里 cfg/sampler 是硬编码的）。"""
    for node in wf.values():
        if node.get("class_type") == "KSampler":
            ins = node["inputs"]
            if steps > 0:
                ins["steps"] = steps
            if cfg > 0:
                ins["cfg"] = cfg
            if sampler:
                ins["sampler_name"] = sampler
            if scheduler:
                ins["scheduler"] = scheduler


def build_workflow(template: str, mapping: dict) -> dict:
    raw = json.loads((TEMPLATES / template).read_text(encoding="utf-8"))
    raw.pop("__meta__", None)

    def sub(v):
        if isinstance(v, str) and v in mapping:
            return mapping[v]          # 直接返回原值 → 保住 int 类型
        if isinstance(v, str):
            for k, val in mapping.items():
                if k in v:
                    v = v.replace(k, str(val))
            return v
        if isinstance(v, dict):
            return {kk: sub(vv) for kk, vv in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v

    return {nid: sub(node) for nid, node in raw.items()}


def submit(base: str, wf: dict, client_id: str) -> str:
    payload = json.dumps({"prompt": wf, "client_id": client_id}).encode("utf-8")
    out = _req(base + "/prompt", data=payload,
               headers={"Content-Type": "application/json"}, timeout=120)
    d = json.loads(out.decode("utf-8"))
    if "prompt_id" not in d:
        raise RuntimeError(f"提交失败: {json.dumps(d, ensure_ascii=False)[:800]}")
    return d["prompt_id"]


def wait(base: str, pid: str, timeout: int, label: str) -> list[dict]:
    t0 = time.time()
    last = -1
    while True:
        if time.time() - t0 > timeout:
            raise TimeoutError(f"{label} 超时（{timeout}s）")
        try:
            hist = _get_json(base, f"/history/{pid}")
        except Exception:
            hist = {}
        if pid in hist:
            h = hist[pid]
            status = h.get("status", {})
            if status.get("status_str") == "error" or not status.get("completed", True):
                msgs = status.get("messages", [])
                raise RuntimeError(f"{label} 执行出错: {json.dumps(msgs, ensure_ascii=False)[:900]}")
            outs = []
            for _, node_out in (h.get("outputs") or {}).items():
                for img in node_out.get("images", []) or []:
                    outs.append(img)
            if outs:
                return outs
        # 进度提示
        try:
            q = _get_json(base, "/queue")
            run = q.get("queue_running") or []
            if run:
                pct = (run[0][-1] if isinstance(run[0], list) else {}) or {}
                if isinstance(pct, dict):
                    val, mx = pct.get("value"), pct.get("max")
                    if val is not None and val != last:
                        last = val
                        print(f"    [{label}] step {val}/{mx}", flush=True)
        except Exception:
            pass
        time.sleep(3)


def download(base: str, img: dict, out: Path) -> Path:
    q = urllib.parse.urlencode({
        "filename": img.get("filename", ""),
        "subfolder": img.get("subfolder", ""),
        "type": img.get("type", "output"),
    })
    data = _req(base + "/view?" + q, timeout=300)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="ComfyUI 高清静帧出图")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--negative", default=NEGATIVE_DEFAULT)
    ap.add_argument("--out", required=True, help="输出图片路径（.png）")
    ap.add_argument("--width", type=int, default=2752)
    ap.add_argument("--height", type=int, default=1536)
    ap.add_argument("--steps", type=int, default=0, help="0 = 用模板默认步数 (30)")
    ap.add_argument("--cfg", type=float, default=0.0, help="0 = 用模板默认值 (2.5)")
    ap.add_argument("--sampler", default="", help="采样器，如 dpmpp_2m；空 = 模板默认 euler")
    ap.add_argument("--scheduler", default="", help="调度器，如 karras；空 = 模板默认 simple")
    ap.add_argument("--ref-fit", default="crop", choices=["crop", "pad", "stretch"],
                    help="参考图适配目标比例的方式（默认 crop，防脸被拉伸）")
    ap.add_argument("--seed", type=int, default=-1, help="-1 = 随机")
    ap.add_argument("--reference", default="", help="参考图路径 → 走 Qwen-Image-Edit 锁一致性")
    ap.add_argument("--prefix", default="still", help="ComfyUI 文件名前缀")
    ap.add_argument("--host", default="http://127.0.0.1:8188")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    base = args.host.rstrip("/")
    seed = args.seed if args.seed >= 0 else random.randint(1, 2**31 - 1)
    out = Path(args.out)
    if not out.is_absolute():
        out = (ROOT / out).resolve()

    use_edit = bool(args.reference)
    template = "qwen_edit_scene.json" if use_edit else "qwen_image_character.json"

    mapping = {
        "{{prompt}}": args.prompt,
        "{{negative_prompt}}": args.negative,
        "{{width}}": args.width,
        "{{height}}": args.height,
        "{{seed}}": seed,
    }

    print(f"[链路] {'Qwen-Image-Edit 2511（带参考图）' if use_edit else 'Qwen-Image 2.1（文生图）'}")
    print(f"[尺寸] {args.width}x{args.height}  ({args.width*args.height/1e6:.2f} MP)")
    print(f"[随机种子] {seed}")

    if use_edit:
        ref = Path(args.reference)
        if not ref.is_absolute():
            ref = (ROOT / ref).resolve()
        if not ref.exists():
            print(f"参考图不存在: {ref}", file=sys.stderr)
            return 2
        ref_use = prep_reference(ref, args.width, args.height, args.ref_fit)
        if ref_use != ref:
            print(f"[参考图适配] {args.ref_fit} -> {ref_use.name}", flush=True)
        print(f"[上传参考图] {ref_use.name} ...", flush=True)
        mapping["{{reference_image}}"] = upload_image(base, ref_use)
        print(f"    -> {mapping['{{reference_image}}']}", flush=True)

    wf = build_workflow(template, mapping)
    apply_overrides(wf, steps=args.steps, cfg=args.cfg,
                    sampler=args.sampler, scheduler=args.scheduler)
    # 统一文件名前缀，便于取回
    for node in wf.values():
        if node.get("class_type") == "SaveImage":
            node["inputs"]["filename_prefix"] = args.prefix

    client_id = uuid.uuid4().hex
    print("[提交] ...", flush=True)
    pid = submit(base, wf, client_id)
    print(f"[任务] {pid}  等待生成（2K 可能需数分钟）...", flush=True)

    imgs = wait(base, pid, args.timeout, out.name)
    saved = download(base, imgs[0], out)
    print(f"[完成] {saved}  ({saved.stat().st_size/1024:.0f} KB)")
    print(f"[种子] {seed}  ← 同种子可复现")
    return 0


if __name__ == "__main__":
    sys.exit(main())
