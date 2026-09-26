"""探测 zoompan 各运镜模式的耗时/是否卡死（开发诊断用）。"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

# ffmpeg 逐级探测：PATH > ~/.local/bin（本机静态安装位置）
FF = shutil.which("ffmpeg") or str(Path.home() / ".local" / "bin" / "ffmpeg")
IMG = sys.argv[1] if len(sys.argv) > 1 else None
LIMIT = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0

if not IMG or not Path(IMG).exists():
    print("用法: probe_motion.py <图片路径> [每项超时秒]")
    raise SystemExit(2)

dur, fps, w, h = 5.0, 24, 1280, 720
frames = int(dur * fps)

MOTIONS = {
    "zoom_in": "z='min(zoom+0.0012,1.25)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
    "zoom_out": "z='if(lte(zoom,1.0),1.25,max(1.001,zoom-0.0012))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
    "pan_left": "z='1.18':x='iw/2-(iw/zoom/2)+(iw/2-iw/zoom/2)*0.6*(on/%d)':y='ih/2-(ih/zoom/2)'" % frames,
    "pan_right": "z='1.18':x='iw/2-(iw/zoom/2)-(iw/2-iw/zoom/2)*0.6*(on/%d)':y='ih/2-(ih/zoom/2)'" % frames,
}

for name, expr in MOTIONS.items():
    out = f"/tmp/_probe_{name}.mp4"
    vf = (
        f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,"
        f"crop={w*2}:{h*2},"
        f"zoompan={expr}:d={frames}:s={w}x{h}:fps={fps},format=yuv420p"
    )
    cmd = [FF, "-hide_banner", "-nostdin", "-y", "-loop", "1", "-framerate", str(fps),
           "-i", IMG, "-vf", vf, "-t", f"{dur:.3f}", "-r", str(fps),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-an", out]
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    status = "OK"
    try:
        _, err = proc.communicate(timeout=LIMIT)
        if proc.returncode != 0:
            status = f"FAIL(exit={proc.returncode})"
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        status = "HANG"
    el = time.time() - t0
    size = Path(out).stat().st_size if Path(out).exists() else 0
    print(f"{name:10s} {status:16s} {el:6.1f}s  {size/1024:8.1f} KB", flush=True)
