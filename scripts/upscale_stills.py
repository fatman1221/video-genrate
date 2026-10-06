#!/usr/bin/env python
"""批量图片超分（本地 Real-ESRGAN ncnn-vulkan）—— 供「低分辨率生成 + 本地增强」链路收尾。

为什么用 ncnn-vulkan 单文件版而不是 pip 的 realesrgan：
  免装 torch/CUDA 依赖、单文件 exe，且支持 `-s` 指定倍率。

为什么默认「x4 出母版再降采样」而不是直接 x2：
  超分模型在 4x 下重建的细节最完整，再从 4x 母版降采样到目标尺寸，
  观感严格一致且比直接 x2 更锐利（一次超分、派生多档）。

模型选择：
  realesrgan-x4plus        —— 写实 / 3D CG / 照片向（人物资产默认用这个）
  realesrgan-x4plus-anime  —— 二次元插画向
  realesr-animevideov3     —— 视频向，快（x2/x3/x4），细节弱于 x4plus

用法：
  # 把 raw/ 里的低分辨率母版超分到 2K，成品放上一级目录
  python scripts/upscale_stills.py \
      --in-dir backend/storage/temp/stills/daji_assets/raw \
      --out-dir backend/storage/temp/stills/daji_assets \
      --width 1536 --height 2048

  # 只跑某几张 / 强制重跑 / 同时保留 4x 母版
  python scripts/upscale_stills.py --in-dir raw --out-dir . --only 01,02 --force --keep-master
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = Path.home() / ".workbuddy" / "tools" / "realesrgan-ncnn-vulkan" / "realesrgan-ncnn-vulkan.exe"


def sr_one(exe: Path, src: Path, model: str, scale: int, tile: int) -> Path:
    """跑一次超分，返回 ncnn-vulkan 写出的文件（在临时目录里）。

    ⚠️ 该 exe 要求 `-i` 与 `-o` **同为文件或同为目录**，混用会直接报
    `inputpath and outputpath must be either file or directory at the same time`，
    所以这里显式给出输出文件路径。
    """
    tmp = Path(tempfile.mkdtemp(prefix="sr_"))
    out = tmp / src.name
    cmd = [str(exe), "-i", str(src), "-o", str(out), "-n", model, "-s", str(scale)]
    if tile > 0:
        cmd += ["-t", str(tile)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"超分失败 rc={p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    if not out.exists():                      # 少数版本会改扩展名
        cands = list(tmp.glob(src.stem + ".*"))
        if not cands:
            raise RuntimeError(f"超分无输出，tmp={tmp} 内容={[f.name for f in tmp.iterdir()]}")
        out = cands[0]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="批量图片超分（Real-ESRGAN ncnn-vulkan）")
    ap.add_argument("--in-dir", required=True, help="待超分图片目录")
    ap.add_argument("--out-dir", required=True, help="成品输出目录")
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    # 默认走 anime 变体：3D CG / 国漫角色上它保留五官、头发有缕感，
    # 而照片向的 x4plus 会把皮肤和发丝处理成塑料 + 蜡质感。
    ap.add_argument("--model", default="realesrgan-x4plus-anime")
    ap.add_argument("--scale", type=int, default=4, help="超分倍率（x4plus 只支持 4）")
    ap.add_argument("--width", type=int, default=0, help="成品宽（0=不缩放，直接留超分结果）")
    ap.add_argument("--height", type=int, default=0, help="成品高")
    ap.add_argument("--tile", type=int, default=0, help="显存不够时设 256/512（0=不分块）")
    ap.add_argument("--only", default="", help="只处理指定前缀，逗号分隔，如 01,02")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的成品")
    ap.add_argument("--keep-master", action="store_true", help="同时把 4x 母版存到 <out-dir>/master/")
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.exists():
        print(f"超分可执行文件不存在: {exe}", file=sys.stderr)
        return 2
    in_dir = Path(args.in_dir)
    if not in_dir.is_absolute():
        in_dir = (ROOT / in_dir).resolve()
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    if not in_dir.is_dir():
        print(f"输入目录不存在: {in_dir}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    only = tuple(s.strip() for s in args.only.split(",") if s.strip())
    files = sorted(p for p in in_dir.glob("*.png") if not p.name.startswith("_"))
    if only:
        files = [p for p in files if p.name.startswith(only)]

    from PIL import Image
    print(f"[超分] {len(files)} 张 · {args.model} x{args.scale} · "
          f"{in_dir.name} -> {out_dir.name}")
    if args.width and args.height:
        print(f"[成品] {args.width}x{args.height}（从 {args.scale}x 母版降采样）\n")
    else:
        print("[成品] 保留超分原始尺寸\n")

    ok, skip, fail = 0, 0, []
    for i, src in enumerate(files, 1):
        dst = out_dir / src.name
        if dst.exists() and dst.stat().st_size > 1024 and not args.force:
            print(f"[{i}/{len(files)}] {src.name} — 已存在，跳过")
            skip += 1
            continue
        t0 = time.time()
        try:
            master = sr_one(exe, src, args.model, args.scale, args.tile)
            im = Image.open(master).convert("RGB")
            ms = im.size
            if args.keep_master:
                md = out_dir / "master"
                md.mkdir(parents=True, exist_ok=True)
                im.save(md / src.name)
            if args.width and args.height:
                im = im.resize((args.width, args.height), Image.LANCZOS)
            im.save(dst)
            print(f"[{i}/{len(files)}] {src.name}  "
                  f"{ms[0]}x{ms[1]} -> {im.size[0]}x{im.size[1]}  "
                  f"{time.time()-t0:.1f}s  ({dst.stat().st_size/1024/1024:.1f} MB)")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(files)}] {src.name} 失败: {e}", file=sys.stderr)
            fail.append((src.name, str(e)[:200]))

    print(f"\n[完成] 成功 {ok} / 跳过 {skip} / 失败 {len(fail)}")
    for n, e in fail:
        print(f"   x {n}: {e}")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
