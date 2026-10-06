#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测：进度上报的真实密度与请求延迟。"""
import json, time, urllib.request, urllib.error, urllib.parse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import BASE, sample_video   # noqa: E402

SRC = sample_video()


def req(method, path, data=None, headers=None):
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    t = time.time()
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read() or b"{}"), time.time() - t
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}"), time.time() - t
        except Exception:
            return {}, time.time() - t


job, _ = req("POST", "/api/video/jobs?" + urllib.parse.urlencode({"name": SRC.name}),
             SRC.read_bytes(), {"Content-Type": "application/octet-stream"})
jid = job["id"]
print("job", jid)

params = {"model": "realesr-animevideov3", "scale": 2, "chunk": 60, "crf": 24}
req("POST", f"/api/video/jobs/{jid}/start", json.dumps(params).encode(),
    {"Content-Type": "application/json"})

t0 = time.time()
rows, lat = [], []
last_pct = None
while time.time() - t0 < 45:
    j, dt = req("GET", f"/api/video/jobs/{jid}")
    lat.append(dt)
    pct = j.get("progress", 0)
    if last_pct is None or pct != last_pct:
        rows.append((round(time.time() - t0, 2), pct, j.get("stage"), j.get("phase")))
        last_pct = pct
    if j["status"] in ("done", "warn", "failed", "cancelled"):
        print("结束:", j["status"], j.get("stage"))
        break
    time.sleep(0.2)

print(f"\n轮询 {len(lat)} 次，平均延迟 {sum(lat)/len(lat)*1000:.0f}ms，最大 {max(lat)*1000:.0f}ms")
print(f"进度变化 {len(rows)} 次（0.2s 轮询 / 45s 窗口）：")
for i, r in enumerate(rows[:40]):
    print(f"  {i:3d}  t={r[0]:6.2f}s  {r[1]:7.2f}%  {r[2]}  [{r[3]}]")
if len(rows) > 40:
    print(f"  … 还有 {len(rows)-40} 次")
req("DELETE", f"/api/video/jobs/{jid}")
