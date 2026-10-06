#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频超分网页端到端实测：上传 -> 计划 -> 执行 -> 验收 -> Range 播放。

只用一个 stdlib 脚本把 HTTP 全流程走一遍，并**独立**用 ffprobe 核对产物，
不信任 API 自报的尺寸/帧数。
"""
import json
import math
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import BASE, ffprobe_bin, sample_video   # noqa: E402

SRC = sample_video()
FFPROBE = ffprobe_bin()

FAIL = []


def check(ok, label, detail=""):
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAIL.append(label)


def req(method, path, data=None, headers=None, timeout=120, raw=False):
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (body if raw else json.loads(body or b"{}")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}"), dict(e.headers)
        except Exception:
            return e.code, {"raw": body[:300]}, dict(e.headers)


def ffprobe(path):
    p = subprocess.run([str(FFPROBE), "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)],
                       capture_output=True)
    d = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    v = [s for s in d.get("streams", []) if s.get("codec_type") == "video"][0]
    a = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
    return {"w": int(v["width"]), "h": int(v["height"]),
            "fps": v.get("avg_frame_rate"), "nb": v.get("nb_frames"),
            "dur": float(d.get("format", {}).get("duration") or 0),
            "audio": bool(a), "size": int(d.get("format", {}).get("size") or 0)}


def upload(name, blob):
    st, d, _ = req("POST", "/api/video/jobs?" + urllib.parse.urlencode({"name": name}),
                   data=blob, headers={"Content-Type": "application/octet-stream"},
                   timeout=300)
    return st, d


def main():
    print("=" * 78)
    print("视频超分网页 · 端到端实测")
    print("=" * 78)

    # 期望值全部由**源片实测**推导，不写死「1376x768 / 243 帧 / 2752x1536」——
    # 换一段素材就跑不通的测试，等于没有测试。
    si = ffprobe(SRC)
    src_w, src_h = si["w"], si["h"]
    src_frames = int(si["nb"] or 0)
    SCALE = 2
    CHUNK = 120
    exp_w, exp_h = src_w * SCALE, src_h * SCALE
    exp_chunks = math.ceil(src_frames / CHUNK) if src_frames else 0
    print(f"素材 {SRC}")
    print(f"源实测 {src_w}x{src_h}  {src_frames} 帧  {si['dur']:.3f}s  音轨={si['audio']}")

    # ---------------- 1. 上传
    print("\n[1] 上传")
    blob = SRC.read_bytes()
    t0 = time.time()
    st, job = upload(SRC.name, blob)
    check(st == 201, f"上传返回 201", f"HTTP {st}  {time.time() - t0:.1f}s  {len(blob)/1048576:.1f}MB")
    if st != 201:
        print("    ", job)
        return 1
    jid = job["id"]
    s = job["src"]
    print(f"     任务 {jid}")
    print(f"     源 {s['w']}x{s['h']} @{s['fps']:.3f}fps  {s['frames']} 帧（{s['frames_src']}）"
          f"  {s['duration']:.3f}s  音轨={s['has_audio']}({s['audio_codec']})  {s['bytes']}B")
    check((s["w"], s["h"]) == (src_w, src_h), f"探测尺寸 {src_w}x{src_h}")
    check(src_frames == 0 or s["frames"] == src_frames,
          f"探测帧数 {src_frames}", f"服务端 {s['frames']}（{s['frames_src']}）")
    check(s["has_audio"] == si["audio"], "音轨探测与 ffprobe 一致",
          f"服务端={s['has_audio']} ffprobe={si['audio']}")

    # ---------------- 2. 计划
    print("\n[2] 计划预览")
    params = {"model": "realesr-animevideov3", "scale": SCALE, "chunk": CHUNK, "crf": 16}
    st, pl, _ = req("POST", f"/api/video/jobs/{jid}/plan",
                    json.dumps(params).encode(), {"Content-Type": "application/json"})
    check(st == 200, "plan 返回 200", f"HTTP {st}")
    if st != 200:
        print("    ", pl)
        return 1
    print(f"     输出 {pl['final_w']}x{pl['final_h']}  超分 {pl['out_w']}x{pl['out_h']}"
          f"  分块 {pl['n_chunks']}×{pl['chunk']}  峰值临时盘 {pl['peak_tmp']/1048576:.0f}MB")
    print(f"     磁盘余量 {pl['disk_free']/1073741824:.1f}GB  preset={pl['preset']}")
    check((pl["final_w"], pl["final_h"]) == (exp_w, exp_h), f"计划输出 {exp_w}x{exp_h}")
    check(pl["n_chunks"] == exp_chunks, f"分块数 {exp_chunks}")
    # 临时盘占用只做「量级 + 不超可用空间」的合理性判断，不写死兆数
    check(0 < pl["peak_tmp"] <= max(pl["disk_free"], 1),
          "峰值临时盘在可用空间之内", f"{pl['peak_tmp']/1048576:.0f}MB")

    # 计划不应有副作用
    st2, pl2, _ = req("POST", f"/api/video/jobs/{jid}/plan",
                      json.dumps(params).encode(), {"Content-Type": "application/json"})
    check(pl2["out"] == pl["out"] and pl2["total"] == pl["total"], "重复计算计划结果一致")

    # ---------------- 3. 执行 + 进度
    print("\n[3] 执行（记录进度轨迹）")
    st, _, _ = req("POST", f"/api/video/jobs/{jid}/start",
                   json.dumps(params).encode(), {"Content-Type": "application/json"})
    check(st == 201, "start 返回 201", f"HTTP {st}")

    traj, stages, phases = [], [], set()
    t_run = time.time()
    last = None
    while time.time() - t_run < 420:
        st, j, _ = req("GET", f"/api/video/jobs/{jid}")
        pct = j.get("progress", 0)
        if last is None or abs(pct - last) > 0.05:
            traj.append((round(time.time() - t_run, 1), pct, j.get("stage"), j.get("phase")))
            last = pct
        if j.get("stage") and (not stages or stages[-1] != j["stage"]):
            stages.append(j["stage"])
        if j.get("phase"):
            phases.add(j["phase"])
        if j["status"] in ("done", "warn", "failed", "cancelled"):
            break
        time.sleep(1.2)

    el = time.time() - t_run
    print(f"     状态 {j['status']}  {j['stage']}  耗时 {el:.0f}s")
    print(f"     采样 {len(traj)} 个进度点，阶段序列: {' → '.join(stages[:14])}"
          + (" …" if len(stages) > 14 else ""))
    print(f"     出现的子阶段: {sorted(phases)}")
    check(j["status"] in ("done", "warn"), "任务跑完", j["status"])
    check(len(traj) > 8, "进度点足够密（不是假进度条）", f"{len(traj)} 个")
    check("超分" in phases, "上报了「超分」子阶段（来自数输出目录的 PNG 数）")
    check(any(t[1] > 0 and t[1] < 100 for t in traj), "存在中间进度值")
    mono = all(traj[i][1] >= traj[i - 1][1] - 0.6 for i in range(1, len(traj)))
    check(mono, "进度单调不回退")

    res = j.get("result") or {}
    if res:
        print(f"     结果 {res['width']}x{res['height']} {res['bytes']/1048576:.1f}MB "
              f"帧数 {res['frames']}/{res['total_frames']} "
              f"耗时 {res['elapsed']}s rate={res['rate']}")
        print(f"     检查 {res['checks']}  problems={res['problems']}")
        check(res.get("ok"), "验收全部通过", str(res.get("problems")))
        if src_frames:
            check(res.get("frames") == src_frames, f"成片帧数与源一致 {src_frames}",
                  f"{res.get('frames')} / {res.get('total_frames')}")
        check((res.get("width"), res.get("height")) == (exp_w, exp_h),
              f"结果尺寸 {exp_w}x{exp_h}",
              f"{res.get('width')}x{res.get('height')}")
    else:
        check(False, "有结构化结果", str(j.get("error")))

    # ---------------- 4. 独立核验产物
    print("\n[4] 独立核验产物（不信 API 自报）")
    st, d, hdrs = req("GET", f"/api/video/jobs/{jid}/file/{urllib.parse.quote(res['out_name'])}",
                      raw=True)
    check(st == 200, "成片可下载", f"HTTP {st} {len(d)/1048576:.1f}MB")
    tmp = Path(__file__).resolve().parent / "_e2e_out.mp4"
    tmp.write_bytes(d)
    info = ffprobe(tmp)
    print(f"     ffprobe: {info['w']}x{info['h']} nb_frames={info['nb']} "
          f"fps={info['fps']} 音轨={info['audio']} {info['size']/1048576:.1f}MB")
    check((info["w"], info["h"]) == (exp_w, exp_h), f"磁盘文件尺寸 {exp_w}x{exp_h}")
    check(info["audio"] == si["audio"], "磁盘文件音轨与源一致")
    check(abs(info["dur"] - si["dur"]) < 0.1, "时长与源一致",
          f"{info['dur']:.3f}s vs 源 {si['dur']:.3f}s")
    check(info["size"] == res["bytes"], "API 自报大小与磁盘一致",
          f"{res['bytes']} == {info['size']}")

    # ---------------- 5. Range 播放
    print("\n[5] Range 请求（<video> 拖动进度条靠这个）")
    st, body, hdrs = req("GET", f"/api/video/jobs/{jid}/file/{urllib.parse.quote(res['out_name'])}",
                         headers={"Range": "bytes=0-1023"}, raw=True)
    check(st == 206, "Range 返回 206", f"HTTP {st}")
    check(len(body) == 1024, "返回恰好 1024 字节", f"{len(body)}")
    check(hdrs.get("Content-Range", "").startswith("bytes 0-1023/"),
          "带 Content-Range", hdrs.get("Content-Range", ""))
    check(hdrs.get("Accept-Ranges") == "bytes", "带 Accept-Ranges: bytes")

    st, body, hdrs = req("GET", f"/api/video/jobs/{jid}/file/{urllib.parse.quote(res['out_name'])}",
                         headers={"Range": "bytes=-2048"}, raw=True)
    check(st == 206 and len(body) == 2048, "末尾 N 字节形式可用（bytes=-2048）", f"{len(body)}")

    st, body, _ = req("GET", f"/api/video/jobs/{jid}/file/{urllib.parse.quote(res['out_name'])}",
                      headers={"Range": "bytes=99999999-"}, raw=True)
    check(st == 416, "越界 Range 返回 416", f"HTTP {st}")

    # ---------------- 6. 边界
    print("\n[6] 边界与错误处理")
    st, d, _ = req("GET", "/api/video/jobs/nope")
    check(st == 404, "不存在的任务 404", f"HTTP {st}")

    st, d, _ = req("GET", f"/api/video/jobs/{jid}/file/..%2F..%2Fserver.py")
    check(st == 404, "目录穿越 404", f"HTTP {st}")

    st, d, _ = req("GET", f"/api/video/jobs/{jid}/file/_segs")
    check(st == 404, "取内部路径 404", f"HTTP {st}")

    for bad, why in [({"scale": 5}, "倍率越界"),
                     ({"model": "realesrgan-x4plus", "scale": 2}, "模型不支持该倍率"),
                     ({"chunk": 0}, "分块为 0"),
                     ({"crf": 99}, "CRF 越界"),
                     ({"target_width": 100, "target_height": 0}, "只给一半目标尺寸")]:
        st, d, _ = req("POST", f"/api/video/jobs/{jid}/plan",
                       json.dumps(bad).encode(), {"Content-Type": "application/json"})
        check(st == 400, f"拒绝非法参数（{why}）", f"HTTP {st} {d.get('error','')[:50]}")

    st, d = upload("not_a_video.mp4", b"this is definitely not a video" * 100)
    check(st == 400, "非视频文件被拒（且清理临时目录）", f"HTTP {st}")
    # 确认没留下垃圾任务
    st, jobs, _ = req("GET", "/api/video/jobs")
    leftover = [x for x in jobs["jobs"] if x["src"]["name"] == "not_a_video.mp4"]
    check(not leftover, "失败上传没有留下任务记录", f"{len(leftover)} 条")

    print("\n" + "=" * 78)
    tmp = Path(__file__).resolve().parent / "_e2e_out.mp4"
    if tmp.exists():
        tmp.unlink()
    if FAIL:
        print(f"✗ {len(FAIL)} 项未通过：")
        for f in FAIL:
            print("   -", f)
        return 1
    print("✓ 全部通过")
    print(f"  job_id = {jid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
