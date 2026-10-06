#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证进度口径：「已完成帧数」必须单调不减，且块内也要递增。

改之前的毛病：done_frames 只在**分块完成的瞬间**上报 → 整个第一块期间界面
都显示「0 / 243」，看着像卡住；编码阶段还会从本块末尾的值掉回已完成块的值。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import BASE   # noqa: E402

if len(sys.argv) < 2:
    print("用法：python probe_frames.py <任务号>   （任务号从 /api/video/jobs 或页面上拿）")
    raise SystemExit(2)
JID = sys.argv[1]


def post(path, body):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                               method="POST", headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(r, timeout=60).status


def get(jid):
    with urllib.request.urlopen(f"{BASE}/api/video/jobs/{jid}", timeout=30) as r:
        return json.loads(r.read())


def main():
    post(f"/api/video/jobs/{JID}/start",
         {"model": "realesr-animevideov3", "scale": 2, "chunk": 60, "crf": 20})
    prev, drops, seen = -1, [], []
    for i in range(40):
        j = get(JID)
        n = j.get("done_frames") or 0
        row = (i + 1, n, j["status"], round(j.get("progress") or 0, 2),
               j.get("stage", ""), j.get("message", ""))
        seen.append(row)
        flag = ""
        if n < prev:
            flag = "  <-- 倒退!"
            drops.append((prev, n, row[4]))
        prev = n
        print(f"  t={row[0]:>2}s {row[2]:8s} pct={row[3]:6.2f} 帧={n}/{j.get('total_frames')}"
              f"  {row[4]}  {row[5]}{flag}")
        if j["status"] in ("done", "warn", "failed", "cancelled"):
            break
        time.sleep(1.0)

    in_chunk = [r[1] for r in seen if r[4].startswith("超分")]
    print()
    print(f"块内采样到的帧数值：{in_chunk}")
    print(f"单调性：{'✓ 全程不减' if not drops else '✗ ' + str(drops)}")
    print(f"块内是否递进：{'✓ 有多个中间值' if len(set(in_chunk)) >= 4 else '✗ 中间值太少'}")
    return 0 if (not drops and len(set(in_chunk)) >= 4) else 1


if __name__ == "__main__":
    sys.exit(main())
