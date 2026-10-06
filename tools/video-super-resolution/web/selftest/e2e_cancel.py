#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频超分网页 · 取消 / 续跑 / 孤儿进程 / 崩溃恢复 实测。

这几项是最容易「看起来能用、实际留坑」的地方：
  - 取消后 ffmpeg / ncnn 是否真的死了（不然 GPU 一直被占，还会卡住临时目录）
  - 已完成分块是否保留（决定「再点一次开始」是不是真的续跑）
  - 重跑时会不会复用分块
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import BASE, JOBS_DIR, sample_video   # noqa: E402

SRC = sample_video()
WORK = JOBS_DIR

FAIL = []


def check(ok, label, detail=""):
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAIL.append(label)


def req(method, path, data=None, headers=None, timeout=180):
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def post(path, body):
    return req("POST", path, json.dumps(body).encode(),
               {"Content-Type": "application/json"})


def procs():
    """当前活着的 ffmpeg / realesrgan 进程数（用来判断有没有孤儿）。

    注意：tasklist 映像名列宽 25，`realesrgan-ncnn-vulkan.exe` 会被截成
    `realesrgan-ncnn-vulkan.ex`，但子串仍在，故 count("realesrgan") 有效。
    """
    out = subprocess.run(["tasklist"], capture_output=True).stdout.decode("utf-8", "replace")
    low = out.lower()
    return {"ffmpeg": low.count("ffmpeg.exe"), "rgan": low.count("realesrgan")}


def main():
    print("=" * 78)
    print("视频超分网页 · 取消 / 续跑 / 孤儿进程实测")
    print("=" * 78)

    before = procs()
    print(f"\n[0] 基线进程：ffmpeg={before['ffmpeg']}  realesrgan={before['rgan']}")

    # ---------------- 上传
    st, job = req("POST", "/api/video/jobs?" + urllib.parse.urlencode({"name": SRC.name}),
                  data=SRC.read_bytes(),
                  headers={"Content-Type": "application/octet-stream"}, timeout=300)
    check(st == 201, "上传成功", f"HTTP {st}")
    if st != 201:
        print(job)
        return 1
    jid = job["id"]
    total = job["src"]["frames"]
    print(f"     任务 {jid}  （{total} 帧）")

    # 用小分块 → 多块，第一块很快就能完成，便于制造「有已完成分块」的局面
    CHUNK = 60
    params = {"model": "realesr-animevideov3", "scale": 2, "chunk": CHUNK, "crf": 20}
    n1 = min(CHUNK, total)
    # 三段权重合计 1.0，所以「第 1 块跑完」时的进度 = 100 * 第1块帧数 / 总帧数
    boundary = 100.0 * n1 / max(1, total)

    print("\n[1] 启动并等第一块完成")
    st, _ = post(f"/api/video/jobs/{jid}/start", params)
    check(st == 201, "start 201", f"HTTP {st}")
    t0 = time.time()
    got_chunk1 = False
    # ⚠️ 进程采样必须在整个等待窗口里取「峰值」，不能在跳出循环那一刻单次采样：
    #    progress 跨过块边界的瞬间正好是「前块编码收尾 / 后块抽帧开始」，
    #    ncnn 此刻合理地不在运行 → 单次采样会得到 realesrgan=0 的假阴性。
    rgan_peak, ff_peak, peak_stage = before["rgan"], before["ffmpeg"], ""
    while time.time() - t0 < 120:
        _, j = req("GET", f"/api/video/jobs/{jid}")
        p = procs()
        if p["rgan"] > rgan_peak or p["ffmpeg"] > ff_peak:
            peak_stage = f"{j.get('stage')} / {j.get('phase')}"
        rgan_peak = max(rgan_peak, p["rgan"])
        ff_peak = max(ff_peak, p["ffmpeg"])
        if j.get("progress", 0) >= boundary - 0.1:
            got_chunk1 = True
            break
        if j["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(1.0)
    check(got_chunk1, f"第一块已完成（进度 ≥{boundary:.1f}%）",
          f"progress={j.get('progress')} stage={j.get('stage')}")
    check(rgan_peak >= before["rgan"] + 1, "运行中确实有超分进程",
          f"realesrgan 峰值={rgan_peak}（首次出现于 {peak_stage or '—'}）")
    check(ff_peak >= before["ffmpeg"] + 1, "运行中确实有 ffmpeg 进程",
          f"ffmpeg 峰值={ff_peak}")
    mid = procs()

    print("\n[2] 取消")
    t1 = time.time()
    st, d = post(f"/api/video/jobs/{jid}/cancel", {})
    check(st == 200, "cancel 返回 200", f"HTTP {st}")
    cancelled = False
    while time.time() - t1 < 45:
        _, j = req("GET", f"/api/video/jobs/{jid}")
        if j["status"] == "cancelled":
            cancelled = True
            break
        time.sleep(0.8)
    check(cancelled, "任务进入 cancelled 状态",
          f"{j['status']} / {j.get('stage')}  用时 {time.time() - t1:.1f}s")

    time.sleep(3)
    after = procs()
    check(after["rgan"] <= before["rgan"], "取消后没有残留超分进程（无孤儿）",
          f"realesrgan {mid['rgan']} → {after['rgan']}")
    check(after["ffmpeg"] <= before["ffmpeg"], "取消后没有残留 ffmpeg",
          f"ffmpeg {before['ffmpeg']} → {after['ffmpeg']}")

    segs = sorted((WORK / jid / "work" / "_segs").glob("*.mkv"))
    print(f"     work/_segs 保留分块：{[s.name for s in segs]}")
    check(len(segs) >= 1, "已完成分块被保留（这是续跑的基础）", f"{len(segs)} 个")

    print("\n[3] 重跑应复用分块")
    st, _ = post(f"/api/video/jobs/{jid}/start", params)
    check(st == 201, "重跑 start 201", f"HTTP {st}")
    t0 = time.time()
    while time.time() - t0 < 300:
        _, j = req("GET", f"/api/video/jobs/{jid}")
        if j["status"] in ("done", "warn", "failed", "cancelled"):
            break
        time.sleep(1.5)
    res = j.get("result") or {}
    check(j["status"] in ("done", "warn"), "续跑跑完", j["status"])
    check(res.get("reused_chunks", 0) >= 1, "确实复用了已完成分块",
          f"reused_chunks={res.get('reused_chunks')} 重新处理 {res.get('processed_frames')} 帧")
    check(res.get("ok"), "续跑结果验收通过", str(res.get("problems")))
    exp_w = job["src"]["w"] * params["scale"]
    exp_h = job["src"]["h"] * params["scale"]
    check((res.get("width"), res.get("height")) == (exp_w, exp_h),
          f"续跑输出尺寸正确 {exp_w}x{exp_h}",
          f"{res.get('width')}x{res.get('height')}")
    check(res.get("frames") == total, f"续跑成片帧数完整 {total}", f"{res.get('frames')}")

    print("\n[4] 收尾")
    st, d = req("DELETE", f"/api/video/jobs/{jid}")
    check(st == 200, "删除任务 200", f"HTTP {st}")
    check(not (WORK / jid).exists(), "任务目录已删除")
    st, d = req("DELETE", f"/api/video/jobs/{jid}")
    check(st == 404, "重复删除返回 404", f"HTTP {st}")

    print("\n" + "=" * 78)
    if FAIL:
        print(f"✗ {len(FAIL)} 项未通过：")
        for f in FAIL:
            print("   -", f)
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
