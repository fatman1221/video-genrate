#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装 Real-ESRGAN 运行体（realesrgan-ncnn-vulkan 便携版 + 模型）。

为什么是 ncnn-vulkan 而不是 pip 的 `realesrgan` 包：
  pip 那条路要拖 torch + BasicSR，而 BasicSR 在新 Python（3.12+）上会因
  `torchvision.transforms.functional_tensor` 被移除而 import 失败，numpy 2.x
  下更糟。ncnn 是单文件 exe + 模型，不依赖 Python 环境，装完即用。

幂等：已经装好就直接退出（--force 强制重下）。

用法：
  python setup_realesrgan.py
  python setup_realesrgan.py --proxy http://127.0.0.1:7897
  python setup_realesrgan.py --dest D:/tools/realesrgan
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "v0.2.5.0"
ASSET = "realesrgan-ncnn-vulkan-20220424-windows.zip"
URL = f"https://github.com/xinntao/Real-ESRGAN/releases/download/{VERSION}/{ASSET}"
DEFAULT_DEST = Path.home() / ".workbuddy" / "tools" / "realesrgan-ncnn-vulkan"

# 装完必须存在的模型（bin + param）
REQUIRED_MODELS = [
    "realesr-animevideov3-x2", "realesr-animevideov3-x3", "realesr-animevideov3-x4",
    "realesrgan-x4plus", "realesrgan-x4plus-anime",
]


def download(url: str, dst: Path, proxy: str | None) -> None:
    print(f"下载 {url}")
    handlers = []
    if proxy:
        print(f"  经代理 {proxy}")
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # 显式忽略环境里的 http_proxy
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "setup-realesrgan/1.0"})
    tmp = dst.with_suffix(dst.suffix + ".part")
    with opener.open(req, timeout=600) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            buf = r.read(1 << 20)
            if not buf:
                break
            f.write(buf)
            got += len(buf)
            if total:
                print(f"\r  {got / 1e6:7.1f}/{total / 1e6:.1f} MB "
                      f"({got / total * 100:5.1f}%)", end="", flush=True)
    print()
    tmp.replace(dst)
    h = hashlib.sha256(dst.read_bytes()).hexdigest()
    print(f"  落盘 {dst}  {dst.stat().st_size / 1e6:.1f}MB  sha256={h[:16]}…")


def verify(dest: Path) -> list[str]:
    """返回问题列表，空列表=通过。"""
    problems: list[str] = []
    exe = dest / "realesrgan-ncnn-vulkan.exe"
    if not exe.is_file():
        problems.append(f"缺少可执行文件 {exe}")
    mdir = dest / "models"
    for m in REQUIRED_MODELS:
        if not (mdir / f"{m}.bin").is_file() or not (mdir / f"{m}.param").is_file():
            problems.append(f"缺少模型 {m}.bin/.param")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="安装 Real-ESRGAN (ncnn-vulkan)")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="安装目录")
    ap.add_argument("--proxy", default=os.environ.get("SR_PROXY", "http://127.0.0.1:7897"),
                    help="下载代理（默认 http://127.0.0.1:7897；传 '' 表示直连）")
    ap.add_argument("--force", action="store_true", help="即使已装好也重下")
    args = ap.parse_args()

    if sys.platform != "win32":
        print(f"⚠ 本脚本下载的是 Windows 便携版。当前平台 {sys.platform}，"
              f"请改用对应平台的 ncnn-vulkan 构建，或从源码编译。")
        return 1

    dest = Path(args.dest).expanduser().resolve()
    if not args.force:
        problems = verify(dest)
        if not problems:
            print(f"✓ 已装好，跳过：{dest}")
            print(f"  可执行 = {dest / 'realesrgan-ncnn-vulkan.exe'}")
            return 0
        if dest.exists():
            print(f"已有目录但校验不通过（{len(problems)} 项）：")
            for p in problems:
                print(f"  - {p}")

    dest.mkdir(parents=True, exist_ok=True)
    zpath = dest / ASSET
    if not zpath.is_file() or args.force:
        download(URL, zpath, args.proxy or None)

    print(f"解包到 {dest}")
    with zipfile.ZipFile(zpath) as z:
        bad = z.testzip()
        if bad:
            print(f"✗ zip 损坏于 {bad}，删掉重下：{zpath}")
            return 2
        z.extractall(dest)
    zpath.unlink(missing_ok=True)

    problems = verify(dest)
    if problems:
        print("✗ 解包后校验不通过：")
        for p in problems:
            print(f"  - {p}")
        return 2

    exe = dest / "realesrgan-ncnn-vulkan.exe"
    # 真跑一次 -h，确认 exe 能启动（缺 VC 运行时会在这里暴露）
    import subprocess
    try:
        p = subprocess.run([str(exe), "-h"], capture_output=True, timeout=120)
        ok = b"Usage" in p.stdout or b"Usage" in p.stderr
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  exe 启动失败：{e}")
    if not ok:
        print("✗ exe 装了但跑不起来。常见原因：缺 VC++ 运行库（装 vc_redist.x64）。")
        return 2

    print(f"\n✓ 安装完成：{dest}")
    print(f"  可执行 = {exe}")
    print(f"  模型   = {dest / 'models'}")
    print("\n下一步自检（应打印出 GPU 名和 Vulkan 能力）：")
    print(f'  "{exe}" -i "{dest / "input.jpg"}" -o out.png -n realesr-animevideov3 -s 2')
    return 0


if __name__ == "__main__":
    sys.exit(main())
