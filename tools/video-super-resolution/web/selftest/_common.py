#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest 公共部分：服务地址、测试素材、任务目录、免代理 opener。

所有路径都从这里出，避免每个脚本各写一份绝对路径（换台机器就全废）。
优先级：环境变量 > 常见位置；找不到素材时给出可直接粘贴的 ffmpeg 命令。
"""
import os
import shutil
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent                      # web/

# 服务地址：SR_BASE=http://127.0.0.1:8090
BASE = os.environ.get("SR_BASE", "http://127.0.0.1:8090").rstrip("/")

# 任务目录：默认就是服务自己用的那个（server.py 里的 JOBS_DIR）
JOBS_DIR = Path(os.environ.get("SR_JOBS") or (WEB / "jobs-video"))

# ncnn 工具目录：SR_RGAN=C:/path/to/realesrgan-ncnn-vulkan
TOOL = Path(os.environ.get("SR_RGAN")
            or (Path.home() / ".workbuddy/tools/realesrgan-ncnn-vulkan"))


def _no_proxy():
    """本机默认挂了 HTTP_PROXY，不清掉的话打 127.0.0.1 会报假故障。"""
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({})))


def sample_video() -> Path:
    """找一个可用的测试视频。

    顺序：SR_SRC 环境变量 → 本目录下的 sample.mp4 → 常见下载目录里的候选。
    都没有就抛错，并打印一条现成的 ffmpeg 合成命令（省得去翻素材）。
    """
    env = os.environ.get("SR_SRC")
    cands = [Path(env)] if env else []
    cands += [HERE / "sample.mp4"]
    home = Path.home()
    cands += [home / "Downloads" / "MiniMax_H3_00006_.mp4",
              home / "Desktop" / "MiniMax_H3_00006_.mp4",
              home / "Downloads" / "sample.mp4"]
    for c in cands:
        if c.is_file():
            return c
    print("[!] 没找到测试视频。任选一种：\n"
          "    1) 设环境变量  SR_SRC=<你的视频路径>\n"
          "    2) 放一个 sample.mp4 到 " + str(HERE) + "\n"
          "    3) 自己合成一段（不需要素材）：\n"
          '       ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=24 -t 10 '
          '-pix_fmt yuv420p -c:v libx264 -crf 20 -y sample.mp4', file=sys.stderr)
    raise SystemExit(2)


def ffprobe_bin() -> str:
    """独立核验产物用的 ffprobe。

    查找顺序：FFPROBE_BIN 环境变量 → 托管 ffmpeg 目录 → PATH。
    ⚠️ 托管的 ffmpeg **不在 PATH 上**，只查 PATH 会得到一个和「产物有问题」
    一模一样的 FileNotFoundError。
    """
    env = os.environ.get("FFPROBE_BIN")
    if env:
        return env
    exe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    managed = Path.home() / ".workbuddy" / "binaries" / "ffmpeg" / "bin" / exe
    if managed.is_file():
        return str(managed)
    found = shutil.which("ffprobe")
    if found:
        return found
    print("[!] 找不到 ffprobe。设 FFPROBE_BIN 指向它，或把 ffmpeg/bin 加进 PATH：\n"
          f"    预期位置 {managed}", file=sys.stderr)
    raise SystemExit(2)


_no_proxy()
