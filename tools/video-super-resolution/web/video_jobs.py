#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频超分任务的存储与执行（供 web/server.py 调用）。

设计要点
--------
1. **复用 scripts/upscale_video.py**，绝不另写一套流水线。取消、断点续跑、分块帧数
   校验这些逻辑一旦分叉就会出现「命令行对、网页错」，而这类错（分块边界缺帧）在成片
   里只表现为「某处卡一下」，极难归因。
2. **大文件走原始字节流，不走 base64**。一段 300MB 的片子 base64 后要 400MB 的
   JSON 字符串，只能用来传图。这里按 Content-Length 边收边落盘（1MB 一块），
   内存占用与文件大小无关。
3. **取消要真的停住**。`uv.Canceller` 会从别的线程直接杀掉当前子进程树 —— 抽帧
   阶段 ffmpeg 长时间没有任何输出，只靠读循环是等不到取消的。
4. 任务**串行**执行（GPU 独占）。产物落 `web/jobs-video/<id>/`，其中 `work/_segs/`
   保留已完成分块 → 取消/失败后重跑自动续跑。
"""

from __future__ import annotations

import json
import queue
import shutil
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

# 复用引擎脚本（纯标准库，直接 import）
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import upscale_video as uv          # noqa: E402

HERE = Path(__file__).resolve().parent
JOBS_DIR = HERE / "jobs-video"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv",
             ".ts", ".mpg", ".mpeg", ".wmv", ".m2ts"}

MAX_UPLOAD = 4 * 1024 * 1024 * 1024          # 4GB，够用且不至于把盘写爆
CHUNK_READ = 1024 * 1024                      # 上传落盘的块大小

DEFAULT_PARAMS: dict = {
    "model": uv.DEFAULT_MODEL,
    "scale": 2,
    "target_width": 0,
    "target_height": 0,
    "chunk": uv.DEFAULT_CHUNK,
    "crf": 16,
    "preset": "",
    "tile": "0",
    "gpu": "",
    "threads": "",
    "verify_frames": False,
    "keep_temp": False,
}

# 只有取消句柄是纯运行时的
_EPHEMERAL = {"canceller", "probe"}

_SCALE_CHOICES = {2, 3, 4}
_MIN_CHUNK, _MAX_CHUNK = 1, 5000
_MIN_CRF, _MAX_CRF = 0, 51
_TARGET_RANGE = (64, 16384)


def _now() -> float:
    return time.time()


def disk_free(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return -1


class VideoService:
    def __init__(self, verbose: bool = False) -> None:
        self.jobs: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.q: "queue.Queue[str]" = queue.Queue()
        self.verbose = verbose
        self._exe: str | None = None
        self._ff: tuple[str, str] | None = None

    # ------------------------------------------------ 工具

    def tools(self) -> dict:
        """引擎可用性与可用倍率。模型清单以引擎脚本的 MODEL_SCALES 为准。"""
        if self._exe is None:
            try:
                self._exe = uv.find_realesrgan()
            except SystemExit:
                self._exe = ""
        exe = self._exe or ""
        if not exe:
            return {"ok": False, "exe": None, "models": [], "available": {},
                    "hint": "跑一次 python scripts/setup_realesrgan.py，"
                            "或设 REALESRGAN_BIN 指向 exe"}
        mdir = uv.models_dir(exe)
        found, avail = [], {}
        for m, scales in uv.MODEL_SCALES.items():
            got = []
            for s in sorted(scales):
                if m == "realesr-animevideov3":
                    hit = (mdir / f"{m}-x{s}.param").is_file()
                else:
                    hit = (mdir / f"{m}.param").is_file()
                if hit:
                    got.append(s)
            avail[m] = got
            if got:
                found.append(m)
        return {"ok": bool(found), "exe": exe, "models_dir": str(mdir),
                "models": found, "available": avail,
                "hint": "" if found else f"模型目录里没有可用模型：{mdir}"}

    def _ffmpeg(self) -> tuple[str, str]:
        if self._ff is None:
            ff = uv.find_ffmpeg()
            self._ff = (ff, uv.find_ffprobe(ff))
        return self._ff

    # ------------------------------------------------ 持久化

    def public(self, job: dict) -> dict:
        return {k: v for k, v in job.items() if k not in _EPHEMERAL}

    def save(self, job: dict) -> None:
        try:
            d = JOBS_DIR / job["id"]
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "meta.json.tmp"
            tmp.write_text(json.dumps(self.public(job), ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(d / "meta.json")
        except Exception:
            traceback.print_exc()

    def load(self) -> None:
        if not JOBS_DIR.is_dir():
            return
        for meta in sorted(JOBS_DIR.glob("*/meta.json")):
            try:
                j = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            if j.get("status") in ("queued", "running"):
                j["status"] = "failed"
                j["error"] = j.get("error") or "服务重启，任务已中断（分块已保留，重跑可续）"
            j.setdefault("log", [])
            self.jobs[j["id"]] = j

    # ------------------------------------------------ 上传

    def begin_upload(self, name: str) -> tuple[str, Path]:
        """建任务骨架，返回 (job_id, 落盘路径)。调用方把字节流写进去后调 finish_upload。"""
        clean = Path(str(name or "video.mp4")).name
        ext = Path(clean).suffix.lower()
        if ext not in VIDEO_EXT:
            ext = ".mp4"
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        jdir = JOBS_DIR / jid
        (jdir / "work").mkdir(parents=True, exist_ok=True)
        return jid, jdir / f"source{ext}"

    def finish_upload(self, jid: str, name: str, src_path: Path, size: int) -> dict:
        """探测源片并登记任务。探测失败会清掉整个任务目录（不留垃圾）。"""
        jdir = JOBS_DIR / jid
        clean = Path(str(name or src_path.name)).name
        try:
            ff, fp = self._ffmpeg()
            info = uv.probe(fp, str(src_path))
            info = uv.resolve_frames(fp, info)     # 只在元数据自相矛盾时才逐帧解码
            if not info["width"] or not info["height"]:
                raise ValueError("探测不到有效画面尺寸")
            if not info["frames"]:
                raise ValueError("探测不到帧数（文件可能不完整）")
        except Exception as e:
            shutil.rmtree(jdir, ignore_errors=True)
            raise ValueError(f"读不出视频信息（文件可能损坏或不是视频）：{e}") from e

        job = {
            "id": jid,
            "created": _now(),
            "status": "ready",            # ready = 已上传，等配置参数
            "stage": "待设置",
            "progress": 0.0,
            "message": "",
            "error": "",
            "src": {
                "name": clean, "file": src_path.name, "bytes": size,
                "w": info["width"], "h": info["height"],
                "fps": round(info["fps"], 6),
                "duration": round(info["duration"], 3),
                "frames": info["frames"], "frames_src": info["frames_src"],
                "has_audio": info["has_audio"], "audio_codec": info["audio_codec"],
                "video_codec": info["video_codec"], "pix_fmt": info["pix_fmt"],
                "vfr": info["vfr"],
            },
            "probe": info,                # 运行时字段，不落 meta.json
            "params": dict(DEFAULT_PARAMS),
            "plan": None,
            "result": None,
            "log": [],
        }
        with self.lock:
            self.jobs[jid] = job
        self.save(job)
        return self.public(job)

    # ------------------------------------------------ 参数

    def clean_params(self, raw: dict, base: dict | None = None) -> dict:
        """校验并归一化参数。任何越界都抛 ValueError，不要静默改值。"""
        p = dict(base or DEFAULT_PARAMS)
        if raw:
            p.update({k: v for k, v in raw.items() if k in DEFAULT_PARAMS})

        model = str(p.get("model") or uv.DEFAULT_MODEL)
        if model not in uv.MODEL_SCALES:
            raise ValueError(f"未知模型：{model}")
        raw_scale = p.get("scale")
        if raw_scale is None or raw_scale == "":
            raw_scale = 2
        try:
            scale = int(raw_scale)
        except Exception:
            raise ValueError("倍率不合法")
        if scale not in _SCALE_CHOICES:
            raise ValueError(f"倍率只能是 {sorted(_SCALE_CHOICES)}")
        if scale not in uv.MODEL_SCALES[model]:
            raise ValueError(f"模型 {model} 不支持 x{scale}"
                             f"（只支持 {sorted(uv.MODEL_SCALES[model])}）")
        p["model"], p["scale"] = model, scale

        try:
            tw = int(p.get("target_width") or 0)
            th = int(p.get("target_height") or 0)
        except Exception:
            raise ValueError("目标尺寸不合法")
        if (tw or th) and not (tw and th):
            raise ValueError("目标宽高必须同时给（或都不给）")
        lo, hi = _TARGET_RANGE
        if tw and not (lo <= tw <= hi and lo <= th <= hi):
            raise ValueError(f"目标尺寸应在 {lo}~{hi} 之间")
        p["target_width"], p["target_height"] = tw, th

        # 注意别写 int(p.get("chunk") or DEFAULT)：显式传 0 会被 `or` 吃掉变成默认值，
        # 于是「越界参数」静默通过。先取出原值再判空。
        raw_chunk = p.get("chunk")
        if raw_chunk is None or raw_chunk == "":
            raw_chunk = uv.DEFAULT_CHUNK
        try:
            chunk = int(raw_chunk)
        except Exception:
            raise ValueError("分块帧数不合法")
        if not (_MIN_CHUNK <= chunk <= _MAX_CHUNK):
            raise ValueError(f"分块帧数应在 {_MIN_CHUNK}~{_MAX_CHUNK} 之间")
        p["chunk"] = chunk

        raw_crf = p.get("crf")
        if raw_crf is None or raw_crf == "":
            raw_crf = 16
        try:
            crf = int(raw_crf)
        except Exception:
            raise ValueError("CRF 不合法")
        if not (_MIN_CRF <= crf <= _MAX_CRF):
            raise ValueError(f"CRF 应在 {_MIN_CRF}~{_MAX_CRF} 之间")
        p["crf"] = crf

        preset = str(p.get("preset") or "")
        allowed = ["", "ultrafast", "superfast", "veryfast", "faster", "fast",
                   "medium", "slow", "slower", "veryslow"]
        if preset not in allowed:
            raise ValueError(f"preset 不合法：{preset}")
        p["preset"] = preset

        p["tile"] = str(p.get("tile") or "0")
        p["gpu"] = str(p.get("gpu") or "")
        p["threads"] = str(p.get("threads") or "")
        p["verify_frames"] = bool(p.get("verify_frames"))
        p["keep_temp"] = bool(p.get("keep_temp"))
        return p

    # ------------------------------------------------ 计划预览

    def plan(self, jid: str, raw: dict | None = None) -> dict:
        job = self.get(jid)
        if not job:
            raise ValueError("任务不存在")
        if job["status"] in ("running", "queued"):
            raise ValueError("任务正在运行，无法预览计划")
        rep = self.tools()
        if not rep["ok"]:
            # 别在这里落到 uv.find_realesrgan()：那个函数找不到工具会 sys.exit，
            # 在 HTTP 线程里抛 SystemExit 会变成连接被掐断（界面上只看到「请求失败」）
            raise ValueError(rep["hint"] or "超分引擎不可用")
        p = self.clean_params(raw, job.get("params"))
        info = job.get("probe")
        if not info:
            ff, fp = self._ffmpeg()
            info = uv.probe(fp, str(JOBS_DIR / jid / job["src"]["file"]))
            info = uv.resolve_frames(fp, info)
        out = JOBS_DIR / jid / self._out_name(job, p)
        pl = uv.build_plan(
            src=JOBS_DIR / jid / job["src"]["file"], out=out,
            model=p["model"], scale=p["scale"],
            target_width=p["target_width"], target_height=p["target_height"],
            chunk=p["chunk"], crf=p["crf"], preset=p["preset"],
            tile=p["tile"], gpu=p["gpu"], threads=p["threads"],
            workdir=JOBS_DIR / jid / "work",
            info=info, check_frames=False, ffmpeg=self._ffmpeg()[0],
            ffprobe=self._ffmpeg()[1], rgan=rep["exe"],
        )
        free = disk_free(JOBS_DIR)
        pl["disk_free"] = free
        if free > 0 and pl["peak_tmp"] * 1.6 > free:
            pl["warn"] = list(pl["warn"]) + [
                f"磁盘余量 {free / 1073741824:.1f}GB，可能不足以支撑临时帧"
                f"（峰值 ≈{pl['peak_tmp'] / 1073741824:.1f}GB）→ 调小分块帧数"]
        pl["params"] = p
        return pl

    @staticmethod
    def _out_name(job: dict, p: dict) -> str:
        stem = Path(job["src"]["name"]).stem or "video"
        tw, th = p.get("target_width"), p.get("target_height")
        if tw and th:
            return f"{stem}_{tw}x{th}.mp4"
        w = job["src"]["w"] * p["scale"]
        h = job["src"]["h"] * p["scale"]
        return f"{stem}_x{p['scale']}_{w}x{h}.mp4"

    # ------------------------------------------------ 执行

    def start(self, jid: str, raw: dict | None = None) -> dict:
        job = self.get(jid)
        if not job:
            raise ValueError("任务不存在")
        if job["status"] in ("queued", "running"):
            raise ValueError("任务已在队列或正在运行")
        rep = self.tools()
        if not rep["ok"]:
            raise ValueError(rep["hint"] or "超分引擎不可用")
        p = self.clean_params(raw, job.get("params"))
        pl = self.plan(jid, p)

        with self.lock:
            job["params"] = p
            job["plan"] = pl
            job["status"] = "queued"
            job["stage"] = "排队中"
            job["progress"] = 0.0
            job["message"] = ""
            job["error"] = ""
            job["result"] = None
            job["log"] = []
            job["done_frames"] = 0
            job["total_frames"] = pl["total"]
            job["t_start"] = None
            job["eta"] = None
            job["rate"] = None
        self.save(job)
        self.q.put(jid)
        return self.public(job)

    def cancel(self, jid: str) -> dict:
        job = self.get(jid)
        if not job:
            raise ValueError("任务不存在")
        with self.lock:
            if job["status"] in ("done", "warn", "failed", "cancelled", "ready"):
                return {"ok": True, "noop": True}
            job["status"] = "cancelled"
            job["stage"] = "正在取消"
            c = job.get("canceller")
        if c is not None:
            c.request()          # 立刻杀掉当前子进程（可能正卡在无输出的抽帧阶段）
        self.save(job)
        return {"ok": True}

    def run(self, jid: str) -> None:
        job = self.jobs.get(jid)
        if not job:
            return
        pl = job.get("plan")
        if not pl:
            with self.lock:
                job["status"] = "failed"
                job["error"] = "缺少执行计划"
            self.save(job)
            return

        canceller = uv.Canceller()
        with self.lock:
            job["status"] = "running"
            job["stage"] = "启动中"
            job["t_start"] = _now()
            job["canceller"] = canceller
            if job.get("log"):
                pass
        self.save(job)

        ff, fp = self._ffmpeg()
        rep = self.tools()               # 确保 self._exe 已解析（可能是重启后首次执行）
        last_save = [0.0]

        def on_event(ev: dict) -> None:
            with self.lock:
                if "pct" in ev:
                    job["progress"] = round(float(ev["pct"]), 2)
                if ev.get("stage"):
                    job["stage"] = ev["stage"]
                if ev.get("message"):
                    job["message"] = ev["message"]
                if "done_frames" in ev:
                    # 运行中不回退：任何一个 emit 点漏带 done 都会让「帧数」倒退，
                    # 而它在界面上表现为「跑着跑着归零」。一次运行内单调是硬要求
                    # （每次 start 已在上面重置为 0，所以这里钳制不会掩盖新流程）。
                    job["done_frames"] = max(job.get("done_frames") or 0,
                                             ev["done_frames"] or 0)
                if "total" in ev:
                    job["total_frames"] = ev["total"]
                if ev.get("eta") is not None:
                    job["eta"] = ev["eta"]
                if ev.get("rate") is not None:
                    job["rate"] = ev["rate"]
                if ev.get("phase"):
                    job["phase"] = ev["phase"]
                if ev.get("timings"):
                    job.setdefault("timings", []).append(
                        {"chunk": ev.get("chunk_idx"), "frames": ev.get("chunk_frames"),
                         **ev["timings"]})
                    job["timings"] = job["timings"][-60:]
                if ev.get("log"):
                    lg = job.setdefault("log", [])
                    lg.append(f"{time.strftime('%H:%M:%S')} {ev['log']}")
                    if len(lg) > 400:
                        del lg[:-400]
                if ev.get("result"):
                    job["result"] = ev["result"]
            # 落盘节流：进度事件很密，每次写盘会拖慢流水线
            t = _now()
            if ev.get("log") or ev.get("result") or not ev.get("phase") \
                    or (t - last_save[0]) > 1.0:
                last_save[0] = t
                self.save(job)

        try:
            res = uv.run_pipeline(pl, ffmpeg=ff, ffprobe=fp, rgan=self._exe,
                                  on_event=on_event, cancel=canceller)
            with self.lock:
                job["result"] = res
                job["status"] = "done" if res["ok"] else "warn"
                job["stage"] = "完成" if res["ok"] else "有问题"
                job["progress"] = 100.0
                job["t_end"] = _now()
                job["elapsed"] = round(job["t_end"] - job["t_start"], 1)
                job["message"] = ("；".join(res["problems"]) if res["problems"]
                                  else "全部检查通过")
        except uv.Cancelled:
            with self.lock:
                job["status"] = "cancelled"
                job["stage"] = "已取消"
                job["t_end"] = _now()
                if job.get("t_start"):
                    job["elapsed"] = round(job["t_end"] - job["t_start"], 1)
                job["message"] = "已取消（已完成分块保留，重跑可续）"
        except Exception as e:            # noqa: BLE001
            if self.verbose:
                traceback.print_exc()
            with self.lock:
                job["status"] = "failed"
                job["stage"] = "失败"
                job["error"] = str(e)
                job["t_end"] = _now()
                if job.get("t_start"):
                    job["elapsed"] = round(job["t_end"] - job["t_start"], 1)
            with self.lock:
                lg = job.setdefault("log", [])
                lg.append(f"{time.strftime('%H:%M:%S')} [错误] {e}")
        finally:
            with self.lock:
                job["canceller"] = None
            self.save(job)

    def worker_loop(self) -> None:
        while True:
            jid = self.q.get()
            try:
                self.run(jid)
            except Exception:
                traceback.print_exc()
            finally:
                self.q.task_done()

    def start_worker(self) -> None:
        threading.Thread(target=self.worker_loop, daemon=True).start()

    # ------------------------------------------------ 查询 / 删除

    def get(self, jid: str) -> dict | None:
        with self.lock:
            return self.jobs.get(jid)

    def list(self) -> list[dict]:
        with self.lock:
            items = [self.public(j) for j in
                     sorted(self.jobs.values(), key=lambda x: x.get("created", 0),
                            reverse=True)]
        for it in items:
            it.pop("log", None)
        return items

    def delete(self, jid: str) -> bool:
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                return False
            if job["status"] in ("queued", "running"):
                raise ValueError("任务正在运行，先取消再删除")
            self.jobs.pop(jid, None)
        shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
        return True

    def file_path(self, jid: str, name: str) -> Path | None:
        """只允许取任务目录下的直接子文件（防目录穿越）。"""
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]+", jid):
            return None
        if name != Path(name).name or name.startswith("_"):
            return None
        d = (JOBS_DIR / jid).resolve()
        p = (d / name).resolve()
        return p if p.parent == d and p.is_file() else None
