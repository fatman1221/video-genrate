#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证实/证伪：tasklist 到底能不能看到 realesrgan-ncnn-vulkan.exe？

背景：e2e_cancel.py 里 `check(mid["rgan"] >= before["rgan"] + 1)` 报了 0，
需要先分清是「检测手段失效」还是「采样时机不对」，否则会误改产品代码。

做法：把 ncnn 拉长到 10s 以上跑，同时高频采样 tasklist，看它什么时候出现。
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import TOOL   # noqa: E402

EXE = TOOL / ("realesrgan-ncnn-vulkan.exe" if sys.platform == "win32"
              else "realesrgan-ncnn-vulkan")
WORK = Path(__file__).resolve().parent / "_procprobe"


def snap():
    """返回 (默认 tasklist 里 realesrgan 计数, 本次 tasklist 原始命中行)。"""
    out = subprocess.run(["tasklist"], capture_output=True).stdout.decode("utf-8", "replace")
    low = out.lower()
    hits = [ln.strip() for ln in out.splitlines() if "realesr" in ln.lower()]
    return low.count("realesrgan"), hits


def main():
    seed = TOOL / "input.jpg"
    if not seed.is_file():
        print(f"[!] 工具目录里没有 input.jpg：{TOOL}\n"
              "    放一张图进去，或设 SR_SAMPLE_IMG 指向任意一张本地图片。", file=sys.stderr)
        return 2

    # 自备输入：把小图复制 400 份，保证 ncnn 至少跑十几秒
    #（400 张 512x512 实测约 6s；只放 9 张的话 1.2s 就结束，采样根本抓不到）
    if not (WORK / "f1.jpg").exists():
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "out2").mkdir(exist_ok=True)
        src = seed.read_bytes()
        for i in range(1, 401):
            (WORK / f"f{i}.jpg").write_bytes(src)
        print(f"已生成 {WORK} / 400 份输入")

    print("基线：", snap())
    t0 = time.time()
    p = subprocess.Popen([str(EXE), "-i", str(WORK), "-o", str(WORK / "out2"),
                          "-n", "realesr-animevideov3", "-s", "2"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"启动 PID={p.pid}")

    seen_max, seen_at, rows = 0, None, []
    while p.poll() is None and time.time() - t0 < 20:
        n, hits = snap()
        if n > seen_max:
            seen_max, seen_at = n, time.time() - t0
            rows = hits
        time.sleep(0.15)

    print(f"进程存活 {time.time() - t0:.1f}s（returncode={p.returncode}）")
    print(f"tasklist 看到 realesrgan 的峰值计数 = {seen_max}  (首次 @ t={seen_at}s)")
    if rows:
        print("命中行：")
        for r in rows[:3]:
            print("   ", r)
    # 清掉 400 份输入，别把仓库/工作区塞满
    if WORK.exists():
        for f in WORK.glob("*.jpg"):
            f.unlink()
        (WORK / "out2").exists() and [
            f.unlink() for f in (WORK / "out2").glob("*") if f.is_file()
        ]
        try:
            (WORK / "out2").rmdir()
        except OSError:
            pass
        try:
            WORK.rmdir()
        except OSError:
            pass
    print()
    if seen_max >= 1:
        print("✓ 结论：tasklist 能看见该进程 —— 若 e2e_cancel 报 0，那是「采样时机」问题，"
              "不是检测手段问题")
        return 0
    print("✗ 结论：tasklist 看不到该进程 —— 检测手段本身不可靠，须换口径")
    return 1


if __name__ == "__main__":
    sys.exit(main())
