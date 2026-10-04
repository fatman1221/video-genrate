#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片超分操作台 —— Real-ESRGAN (ncnn-vulkan) 本地 Web UI

用法：
    python web/server.py                 # 默认 http://127.0.0.1:8090
    python web/server.py --port 8090 --open

设计要点
--------
1. **只用标准库**（不依赖工程 .venv）。PIL 仅在「母版 -> 各档位」降采样派生时用。
2. **一次超分、派生多档**：只跑一次最重的母版超分，2K/4K 等档位由母版降采样得到。
   比「每档各跑一次超分」更省时，且各档观感严格一致（不会两档细节风格不同）。
3. **真实进度**：进度直接解析 exe stdout 的 `xx.xx%`，不是估出来的假进度条。
4. 任务**串行**执行（GPU 独占），排队中可取消。
5. 记录落盘到 `web/jobs/<id>/`，刷新页面/重启服务后历史仍在（源图与成图都在）。

与 Skill 的关系：这是 `scripts/upscale_video.py` 的**图片版操作界面**，工具发现规则
（REALESRGAN_BIN / REALESRGAN_MODELS / 托管目录）与那支脚本保持一致。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------- 路径

HERE = Path(__file__).resolve().parent
JOBS_DIR = HERE / "jobs"
INDEX_HTML = HERE / "index.html"

MANAGED_RGAN = (Path.home() / ".workbuddy" / "tools"
                / "realesrgan-ncnn-vulkan" / "realesrgan-ncnn-vulkan.exe")

# ---------------------------------------------------------------- 常量

# 模型 → 支持的放大倍数。**倍数由模型文件决定**，传了不支持的 -s 会在运行中途才报错，
# 所以这里写死并在提交前拦截。
MODEL_SPECS: dict[str, dict] = {
    "realesrgan-x4plus": {
        "scales": [4],
        "label": "写实 / 照片",
        "note": "RRDBNet 33MB，细节最扎实，通用首选",
    },
    "realesrgan-x4plus-anime": {
        "scales": [4],
        "label": "动漫 / 插画",
        "note": "线条更干净，二次元向",
    },
    "realesr-animevideov3": {
        "scales": [2, 3, 4],
        "label": "动漫 / 视频帧",
        "note": "SRVGGNetCompact 1.2MB，快约 17 倍，支持 2x/3x",
    },
}
DEFAULT_MODEL = "realesrgan-x4plus"

FORMATS = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}

MAX_UPLOAD = 200 * 1024 * 1024        # 请求体上限（base64 后）
PCT_RE = re.compile(rb"(\d+(?:\.\d+)?)\s*%")

# 进度区间分配：准备 0-5，超分 5-80，派生 80-100
P_PREP, P_SR_MAX, P_DERIVE = 5.0, 80.0, 80.0

# ---------------------------------------------------------------- 工具发现


def _pick(*cands) -> Path | None:
    for c in cands:
        if not c:
            continue
        p = Path(c)
        if p.is_file():
            return p
    return None


def find_realesrgan() -> Path | None:
    """候选顺序与 scripts/upscale_video.py 的 find_realesrgan() 保持一致。"""
    return _pick(
        os.environ.get("REALESRGAN_BIN"),
        MANAGED_RGAN,
        HERE.parent / "bin" / "realesrgan-ncnn-vulkan.exe",
        HERE.parent / "bin" / "realesrgan-ncnn-vulkan",
        shutil.which("realesrgan-ncnn-vulkan"),
    )


def models_dir(exe: Path) -> Path:
    env = os.environ.get("REALESRGAN_MODELS")
    return Path(env) if env else exe.parent / "models"


def model_available(mdir: Path, model: str, scale: int | None = None) -> bool:
    """模型文件按倍率命名，两种命名法都要认：
         realesr-animevideov3-x2.param   （带倍数后缀，每个倍数一份）
         realesrgan-x4plus.param         （不带后缀，模型本身就固定 4 倍）
    忽略这一点就会把明明装好的 animevideov3 判成「未安装」。
    """
    if model == "realesr-animevideov3":
        scales = [scale] if scale else MODEL_SPECS[model]["scales"]
        return any((mdir / f"{model}-x{s}.param").is_file() for s in scales)
    return (mdir / f"{model}.param").is_file()


def tool_report() -> dict:
    exe = find_realesrgan()
    if not exe:
        return {"ok": False, "exe": None, "models": [], "available": {},
                "hint": "跑一次 python scripts/setup_realesrgan.py，或设 REALESRGAN_BIN 指向 exe"}
    mdir = models_dir(exe)
    found, avail = [], {}
    for m, spec in MODEL_SPECS.items():
        want = [s for s in spec["scales"] if model_available(mdir, m, s)]
        avail[m] = want
        if want:
            found.append(m)
    return {"ok": bool(found), "exe": str(exe), "models_dir": str(mdir),
            "models": found, "available": avail,
            "hint": "" if found else f"模型目录里没有可用模型：{mdir}"}


# ---------------------------------------------------------------- 任务存储

JOBS: dict[str, dict] = {}
LOCK = threading.RLock()
TASK_Q: "queue.Queue[str]" = queue.Queue()

# 只有子进程句柄是纯运行时的；log 要保留 —— 它要经 API 返回给界面，
# 也要落进 meta.json，这样重启后仍能看到失败原因。
_EPHEMERAL = {"proc"}


def _now() -> float:
    return time.time()


def public_job(job: dict) -> dict:
    """剔除运行时字段（子进程句柄等）。"""
    return {k: v for k, v in job.items() if k not in _EPHEMERAL}


def save_job(job: dict) -> None:
    try:
        d = JOBS_DIR / job["id"]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(public_job(job), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(d / "meta.json")
    except Exception:
        traceback.print_exc()


def load_jobs_from_disk() -> None:
    if not JOBS_DIR.is_dir():
        return
    for meta in sorted(JOBS_DIR.glob("*/meta.json")):
        try:
            j = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        # 上次进程死掉时留下的 running/queued 一律标记为中断，避免界面永远转圈
        if j.get("status") in ("queued", "running"):
            j["status"] = "failed"
            j["error"] = j.get("error") or "服务重启，任务已中断"
        j.setdefault("log", [])
        JOBS[j["id"]] = j


def set_stage(job: dict, stage: str, progress: float, msg: str = "") -> None:
    with LOCK:
        job["stage"] = stage
        job["progress"] = round(float(progress), 1)
        if msg:
            job["message"] = msg
    save_job(job)


def log_line(job: dict, line: str) -> None:
    with LOCK:
        lg = job.setdefault("log", [])
        lg.append(line)
        if len(lg) > 300:
            del lg[:-300]


# ---------------------------------------------------------------- 派生


def _target_long_edge(t: dict, src_long: int, exec_scale: int) -> int | None:
    if t.get("kind") == "scale":
        return src_long * exec_scale
    try:
        v = int(t.get("value"))
    except Exception:
        return None
    return v if v > 0 else None


def target_label(t: dict, exec_scale: int) -> str:
    if t.get("kind") == "scale":
        return f"x{exec_scale}"
    v = int(t.get("value"))
    return {2048: "2K", 3840: "4K", 1920: "1080p", 2560: "1440p"}.get(v, f"{v}px")


def _fit(long_edge: int, w: int, h: int) -> tuple[int, int]:
    if w >= h:
        tw = long_edge
        th = max(1, round(h * long_edge / w))
    else:
        th = long_edge
        tw = max(1, round(w * long_edge / h))
    return (tw // 2 * 2 or 2, th // 2 * 2 or 2)      # 偶数化，编码器友好


def derive_outputs(job: dict, master: Path, src_w: int, src_h: int,
                   exec_scale: int, out_dir: Path, stem: str) -> list[dict]:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None

    fmt = job["params"].get("format", "png")
    pil_fmt = FORMATS.get(fmt, "PNG")
    quality = int(job["params"].get("quality", 95))
    src_long = max(src_w, src_h)

    targets = job["params"]["targets"]
    made: list[dict] = []
    seen: set[tuple[int, int]] = set()

    img = Image.open(master)
    img.load()
    mw, mh = img.size

    for idx, t in enumerate(targets):
        long_edge = _target_long_edge(t, src_long, exec_scale)
        if not long_edge:
            continue
        label = target_label(t, exec_scale)
        tw, th = _fit(long_edge, src_w, src_h)
        if (tw, th) in seen:
            continue
        seen.add((tw, th))

        note = ""
        if (tw, th) == (mw, mh):
            out = img.copy()                     # 与母版同尺寸：直接复用，不重编码
        else:
            out = img.resize((tw, th), Image.LANCZOS)
            if tw > mw or th > mh:
                note = (f"母版只有 {mw}x{mh}，该档由母版上采样得到（非模型原生）"
                        f"—— 想拿到真细节请提高倍率")

        conv = {"JPEG": "RGB"}.get(pil_fmt)
        if conv and out.mode != conv:
            out = out.convert(conv)

        name = f"{idx + 1:02d}_{stem}_{label}.{fmt}"
        dest = out_dir / name
        kw = {"quality": quality} if pil_fmt in ("JPEG", "WEBP") else {}
        out.save(dest, pil_fmt, **kw)

        made.append({
            "label": label, "file": name, "w": tw, "h": th,
            "bytes": dest.stat().st_size, "from_master": (tw, th) == (mw, mh),
            "note": note,
        })
        job["progress"] = round(min(99.0, job["progress"] + 20.0 / max(1, len(targets))), 1)
        del out
    img.close()
    return made


# ---------------------------------------------------------------- 执行


def _iter_lines(stream, chunk_size: int = 4096):
    """按 \\r 或 \\n 切分。ncnn 的进度输出用的是 \\r，不能只按 readline。"""
    buf = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            s = p.strip()
            if s:
                yield s.decode("utf-8", "replace")
    if buf.strip():
        yield buf.decode("utf-8", "replace")


def run_job(jid: str) -> None:
    with LOCK:
        job = JOBS[jid]
        job["status"] = "running"
        job["t_start"] = _now()
        job["error"] = ""
        job["outputs"] = []
    save_job(job)

    try:
        jdir = JOBS_DIR / jid
        src_path = jdir / job["src"]["file"]
        exe = find_realesrgan()
        if not exe:
            raise RuntimeError("找不到 realesrgan-ncnn-vulkan，先跑 scripts/setup_realesrgan.py")
        mdir = models_dir(exe)

        model = job["params"]["model"]
        exec_scale = int(job["params"]["scale"])
        src_w, src_h = job["src"]["w"], job["src"]["h"]

        set_stage(job, "超分中", P_PREP, f"{model} ×{exec_scale}")

        master = jdir / "_master.png"
        cmd = [str(exe), "-i", str(src_path), "-o", str(master),
               "-s", str(exec_scale), "-n", model, "-m", str(mdir)]
        tile = int(job["params"].get("tile") or 0)
        if tile:
            cmd += ["-t", str(tile)]
        gpu = str(job["params"].get("gpu") or 0)
        if gpu not in ("", "0"):
            cmd += ["-g", gpu]

        log_line(job, "$ " + " ".join(cmd))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        t_sr = _now()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=flags)
        with LOCK:
            job["proc"] = proc

        last = P_PREP
        for line in _iter_lines(proc.stdout):
            m = PCT_RE.search(line.encode("utf-8", "replace"))
            if m:
                pct = float(m.group(1))
                p = P_PREP + (P_SR_MAX - P_PREP) * pct / 100.0
                # 进度只增不减（多张图时 exe 会重新从 0 开始）
                if p > last:
                    last = p
                    with LOCK:
                        job["progress"] = round(p, 1)
            else:
                log_line(job, line)

        rc = proc.wait()
        with LOCK:
            job["proc"] = None
        if job["status"] == "cancelled":
            raise RuntimeError("已取消")
        if rc != 0:
            raise RuntimeError(f"超分进程退出码 {rc}")
        if not master.is_file():
            raise RuntimeError("超分进程没有产出文件")
        log_line(job, f"[超分完成] {_now() - t_sr:.1f}s  ->  {master.name} {master.stat().st_size / 1048576:.1f}MB")

        set_stage(job, "派生中", P_DERIVE, "生成各档位")
        stem = Path(job["src"]["name"]).stem
        outs = derive_outputs(job, master, src_w, src_h, exec_scale, jdir, stem)
        if not outs:
            raise RuntimeError("没有生成任何档位（检查输出档位设置）")

        with LOCK:
            job["outputs"] = outs
            job["master"] = {"w": None, "h": None}
            job["status"] = "done"
            job["progress"] = 100.0
            job["stage"] = "完成"
            job["t_end"] = _now()
            job["elapsed"] = round(job["t_end"] - job["t_start"], 1)
            job["message"] = f"{len(outs)} 个档位"
        save_job(job)

    except Exception as e:
        with LOCK:
            job["proc"] = None
            job["status"] = "cancelled" if job.get("status") == "cancelled" else "failed"
            job["error"] = str(e)
            job["stage"] = "已取消" if job["status"] == "cancelled" else "失败"
            job["t_end"] = _now()
            if job.get("t_start"):
                job["elapsed"] = round(job["t_end"] - job["t_start"], 1)
        log_line(job, f"[错误] {e}")
        save_job(job)


def worker_loop() -> None:
    while True:
        jid = TASK_Q.get()
        try:
            run_job(jid)
        except Exception:
            traceback.print_exc()
        finally:
            TASK_Q.task_done()


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "SRStudio/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 工具方法

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError(f"请求体过大（{n / 1048576:.0f}MB > {MAX_UPLOAD / 1048576:.0f}MB）")
        return self.rfile.read(n) if n else b""

    def _file(self, path: Path, ctype: str | None = None, download: bool = False) -> None:
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            self._err(404, "文件不存在")
            return
        ctype = ctype or {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".html": "text/html; charset=utf-8",
        }.get(path.suffix.lower(), "application/octet-stream")
        extra = {}
        if download:
            extra["Content-Disposition"] = f'attachment; filename="{path.name}"'
        self._send(200, blob, ctype, extra)

    def _safe_child(self, jid: str, name: str) -> Path | None:
        """只允许取 job 目录下的直接子文件（防目录穿越）。"""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", jid):
            return None
        if name != Path(name).name or name.startswith("_"):
            return None
        d = (JOBS_DIR / jid).resolve()
        p = (d / name).resolve()
        return p if p.parent == d and p.is_file() else None

    # ---- 路由

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                if not INDEX_HTML.is_file():
                    self._err(500, "index.html 缺失")
                    return
                self._file(INDEX_HTML)
                return

            if path == "/api/config":
                rep = tool_report()
                self._json({
                    "tool": rep,
                    "models": [
                        {"id": m, "scales": s["scales"],
                         "available_scales": rep["available"].get(m, []),
                         "label": s["label"], "note": s["note"],
                         "available": m in rep["models"]}
                        for m, s in MODEL_SPECS.items()
                    ],
                    "default_model": DEFAULT_MODEL,
                    "formats": list(FORMATS),
                    "max_upload_mb": MAX_UPLOAD // 1048576,
                })
                return

            if path == "/api/jobs":
                with LOCK:
                    items = [public_job(j) for j in
                             sorted(JOBS.values(), key=lambda x: x.get("created", 0), reverse=True)]
                for it in items:
                    it.pop("log", None)      # 列表不带日志，详情才给（省流量）
                self._json({"jobs": items})
                return

            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)", path)
            if m:
                with LOCK:
                    job = JOBS.get(m.group(1))
                if not job:
                    self._err(404, "任务不存在")
                    return
                self._json(public_job(job))
                return

            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)/source", path)
            if m:
                with LOCK:
                    job = JOBS.get(m.group(1))
                if not job:
                    self._err(404, "任务不存在")
                    return
                p = self._safe_child(m.group(1), job["src"]["file"])
                if not p:
                    self._err(404, "源图不存在")
                    return
                self._file(p)
                return

            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)/file/([^/]+)", path)
            if m:
                dl = "download=1" in (urlparse(self.path).query or "")
                p = self._safe_child(m.group(1), m.group(2))
                if not p:
                    self._err(404, "文件不存在")
                    return
                self._file(p, download=dl)
                return

            self._err(404, "not found")
        except Exception as e:
            traceback.print_exc()
            self._err(500, str(e))

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/jobs":
                self._create_job()
                return

            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)/cancel", path)
            if m:
                jid = m.group(1)
                with LOCK:
                    job = JOBS.get(jid)
                    if not job:
                        self._err(404, "任务不存在")
                        return
                    if job["status"] in ("done", "failed", "cancelled"):
                        self._json({"ok": True, "noop": True})
                        return
                    job["status"] = "cancelled"
                    job["stage"] = "已取消"
                    proc = job.get("proc")
                if proc and proc.poll() is None:
                    proc.terminate()
                save_job(job)
                self._json({"ok": True})
                return

            self._err(404, "not found")
        except Exception as e:
            traceback.print_exc()
            self._err(500, str(e))

    def do_DELETE(self) -> None:
        path = unquote(urlparse(self.path).path)
        m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)", path)
        if not m:
            self._err(404, "not found")
            return
        jid = m.group(1)
        with LOCK:
            job = JOBS.get(jid)
            if job and job.get("proc") and job["proc"].poll() is None:
                self._err(409, "任务正在运行，先取消再删除")
                return
            JOBS.pop(jid, None)
        shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
        self._json({"ok": True})

    # ---- 建任务

    def _create_job(self) -> None:
        raw = self._body()
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:
            self._err(400, "请求体不是合法 JSON")
            return

        rep = tool_report()
        if not rep["ok"]:
            self._err(400, rep["hint"] or "超分工具不可用")
            return

        model = req.get("model") or DEFAULT_MODEL
        if model not in MODEL_SPECS:
            self._err(400, f"未知模型：{model}")
            return
        allowed = MODEL_SPECS[model]["scales"]
        try:
            scale = int(req.get("scale") or allowed[-1])
        except Exception:
            self._err(400, "倍率不合法")
            return
        if scale not in allowed:
            self._err(400, f"模型 {model} 只支持 {allowed} 倍，不支持 {scale} 倍")
            return
        mdir = Path(rep.get("models_dir") or "")
        if not model_available(mdir, model, scale):
            self._err(400, f"模型文件缺失：{mdir} 下找不到 {model} 的 ×{scale} 权重，"
                           f"跑一次 scripts/setup_realesrgan.py 补装")
            return

        targets = req.get("targets") or [{"kind": "scale"}]
        if not isinstance(targets, list) or not targets:
            self._err(400, "至少选一个输出档位")
            return
        clean_targets = []
        for t in targets:
            if not isinstance(t, dict):
                continue
            if t.get("kind") == "scale":
                clean_targets.append({"kind": "scale"})
            elif t.get("kind") == "long":
                try:
                    v = int(t.get("value"))
                except Exception:
                    continue
                if 64 <= v <= 16384:
                    clean_targets.append({"kind": "long", "value": v})
        if not clean_targets:
            self._err(400, "输出档位不合法")
            return

        data = req.get("data") or ""
        if "," in data[:64] and data.lstrip().startswith("data:"):
            data = data.split(",", 1)[1]
        try:
            blob = base64.b64decode(data, validate=False)
        except Exception:
            self._err(400, "图片数据解码失败")
            return
        if not blob:
            self._err(400, "没有收到图片")
            return

        name = Path(str(req.get("name") or "image.png")).name
        ext = Path(name).suffix.lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"):
            ext = ".png"

        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        jdir = JOBS_DIR / jid
        jdir.mkdir(parents=True, exist_ok=True)
        src_file = f"source{ext}"
        (jdir / src_file).write_bytes(blob)

        try:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            with Image.open(jdir / src_file) as im:
                w, h = im.size
        except Exception as e:
            shutil.rmtree(jdir, ignore_errors=True)
            self._err(400, f"读不出图片尺寸（文件可能损坏或格式不支持）：{e}")
            return

        maxp = max(w, h) * scale
        job = {
            "id": jid,
            "created": _now(),
            "status": "queued",
            "stage": "排队中",
            "progress": 0.0,
            "message": "",
            "error": "",
            "src": {"name": name, "file": src_file, "w": w, "h": h, "bytes": len(blob)},
            "params": {
                "model": model, "scale": scale, "targets": clean_targets,
                "tile": int(req.get("tile") or 0), "gpu": str(req.get("gpu") or "0"),
                "format": req.get("format") if req.get("format") in FORMATS else "png",
                "quality": max(50, min(100, int(req.get("quality") or 95))),
            },
            "outputs": [],
            "warn": (f"母版将达 {maxp}px 级，耗时和显存占用会明显上升"
                     if maxp > 10000 else ""),
            "log": [],
        }
        with LOCK:
            JOBS[jid] = job
        save_job(job)
        TASK_Q.put(jid)
        self._json({"job_id": jid}, 201)

    # ---- 日志

    def log_message(self, fmt: str, *a) -> None:
        if os.environ.get("SR_STUDIO_VERBOSE"):
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % a))


# ---------------------------------------------------------------- 启动


def main() -> None:
    ap = argparse.ArgumentParser(description="图片超分操作台（Real-ESRGAN）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    load_jobs_from_disk()
    threading.Thread(target=worker_loop, daemon=True).start()

    rep = tool_report()
    url = f"http://{args.host}:{args.port}/"
    print("=" * 64)
    print("  图片超分操作台  Real-ESRGAN (ncnn-vulkan)")
    print("=" * 64)
    if rep["ok"]:
        print(f"  引擎   {rep['exe']}")
        print(f"  模型   {', '.join(rep['models'])}")
    else:
        print(f"  ✗ 引擎不可用：{rep['hint']}")
    n_done = sum(1 for j in JOBS.values() if j.get("status") == "done")
    print(f"  历史   {len(JOBS)} 个任务（{n_done} 个已完成）  产物目录 web/jobs/")
    print(f"  地址   {url}")
    print("  停止   Ctrl+C")
    print("=" * 64)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
