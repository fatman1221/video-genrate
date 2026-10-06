#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频超分：Real-ESRGAN (ncnn-vulkan) + ffmpeg 分块流水线。

只用标准库。外部依赖两个：
  - ffmpeg / ffprobe
  - realesrgan-ncnn-vulkan.exe（含 models/）

核心设计：**分块处理**。x4 从 1280x720 出 5120x2880 时单帧 PNG 就有 11MB，
一段 5 分钟的片子会吃掉 ~80GB 临时盘。所以按 chunk 抽帧 → 超分 → 立刻编码成
segment 再删帧，把峰值临时占用压到「chunk 帧数 × 单帧体积」的 2 倍量级。

每个 segment 落盘后即被复用 → 中断后重跑自动跳过已完成分块（断点续跑）。

用法：
  python upscale_video.py -i in.mp4 -o out.mp4                  # 默认 x2
  python upscale_video.py -i in.mp4 -o out.mp4 -s 4 --preset faster
  python upscale_video.py -i in.mp4 -o out.mp4 --dry-run        # 只看计划
  python upscale_video.py -i in.mp4 -o out.mp4 --compare 12.5   # 出画质对比图

作为库使用（web UI 走这条，见 web/video_jobs.py）：
  plan = build_plan(src=..., out=..., model=..., scale=2)      # 纯计算，可预览
  result = run_pipeline(plan, on_event=cb, cancel=canceller)   # 执行，可取消

**为什么把主循环抽成 run_pipeline：** 网页和 CLI 必须走同一份实现。分块/续跑/
帧数校验这些逻辑一旦分叉，就会出现「命令行对、网页错」或反过来，而且这类错
（分块边界缺帧）在成片里只表现为「某处画面卡一下」，极难归因。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

# ---------------------------------------------------------------- 常量

# 模型 → 该模型支持的放大倍数（写死，因为 ncnn 是「模型文件决定倍数」，
# 传了模型不支持的 -s 只会在运行中途报错）
MODEL_SCALES: dict[str, set[int]] = {
    "realesr-animevideov3": {2, 3, 4},   # 视频专用（SRVGGNetCompact，1.2MB，快 ~17x）
    "realesrgan-x4plus": {4},            # 通用/照片向（RRDBNet，33MB，慢但细节更"实"）
    "realesrgan-x4plus-anime": {4},      # 二次元向
}
DEFAULT_MODEL = "realesr-animevideov3"
DEFAULT_CHUNK = 240          # 默认分块帧数：x4 下峰值临时盘约 2 × 240 × 11MB ≈ 5GB
MANAGED_RGAN = Path.home() / ".workbuddy" / "tools" / "realesrgan-ncnn-vulkan" / "realesrgan-ncnn-vulkan.exe"
MANAGED_FFMPEG_DIR = Path.home() / ".workbuddy" / "binaries" / "ffmpeg" / "bin"

# ncnn 的进度形如 "12.34%"，用 \r 刷新
PCT_RE = re.compile(rb"(\d+(?:\.\d+)?)\s*%")

# 分块内的阶段权重：抽帧 / 超分 / 编码。超分是绝对大头。
W_EXTRACT, W_UPSCALE, W_ENCODE = 0.12, 0.76, 0.12

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class Cancelled(RuntimeError):
    """用户主动取消（区别于真正的失败：取消要保留已完成分块以便续跑）。"""


# ---------------------------------------------------------------- 工具发现

def _pick(*cands: str | Path | None) -> str | None:
    for c in cands:
        if not c:
            continue
        c = str(c)
        if os.path.isfile(c):
            return c
    return None


def find_ffmpeg() -> str:
    exe = _pick(
        os.environ.get("FFMPEG_BIN"),
        MANAGED_FFMPEG_DIR / "ffmpeg.exe",
        MANAGED_FFMPEG_DIR / "ffmpeg",
        shutil.which("ffmpeg"),
    )
    if not exe:
        sys.exit("✗ 找不到 ffmpeg。设 FFMPEG_BIN 环境变量，或把它放进 PATH。")
    return exe


def find_ffprobe(ffmpeg: str) -> str:
    sibling = Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe"))
    exe = _pick(
        os.environ.get("FFPROBE_BIN"),
        sibling,
        MANAGED_FFMPEG_DIR / "ffprobe.exe",
        shutil.which("ffprobe"),
    )
    if not exe:
        sys.exit("✗ 找不到 ffprobe（与 ffmpeg 同目录通常就有）。")
    return exe


def find_realesrgan() -> str:
    here = Path(__file__).resolve().parent
    exe = _pick(
        os.environ.get("REALESRGAN_BIN"),
        MANAGED_RGAN,
        here.parent / "bin" / "realesrgan-ncnn-vulkan.exe",
        here.parent / "bin" / "realesrgan-ncnn-vulkan",
        shutil.which("realesrgan-ncnn-vulkan"),
    )
    if not exe:
        sys.exit(
            "✗ 找不到 realesrgan-ncnn-vulkan。\n"
            "  跑一次：python scripts/setup_realesrgan.py\n"
            "  或设 REALESRGAN_BIN 指向 exe。"
        )
    return exe


def models_dir(exe: str) -> Path:
    """exe 同级的 models/ 目录（可用 REALESRGAN_MODELS 覆盖）。"""
    env = os.environ.get("REALESRGAN_MODELS")
    return Path(env) if env else Path(exe).parent / "models"


# ---------------------------------------------------------------- 执行

def run(cmd: list[str], *, timeout: float | None = None, quiet: bool = True,
        label: str = "") -> subprocess.CompletedProcess:
    """跑子进程（短命令用，如 ffprobe）。失败时把完整命令和 stderr 一起抛出来 —— 不要吞。"""
    t0 = time.time()
    p = subprocess.run(
        cmd, capture_output=True, timeout=timeout,
        # 显式 decode：这个 exe 在 Windows 上会输出本地编码字节，text=True 会解码崩
    )
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    if p.returncode != 0:
        raise RuntimeError(
            f"{label or '命令'} 失败 (exit={p.returncode}, {time.time() - t0:.1f}s)\n"
            f"  命令: {' '.join(cmd)}\n"
            f"  --- stderr ---\n{err[-4000:]}\n"
            f"  --- stdout ---\n{out[-1500:]}"
        )
    if not quiet:
        print(f"    {label} ok {time.time() - t0:.1f}s")
    return p


def iter_lines(stream, chunk_size: int = 4096):
    """按 \\r 或 \\n 切分。**ncnn 的进度输出用的是 \\r**，只用 readline() 会一直卡住。"""
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


def kill_tree(p: subprocess.Popen) -> None:
    """杀进程树。

    只 kill 自己是不够的：ffmpeg / ncnn 都可能再派生子进程，漏网的会一直占着
    GPU 和文件句柄，表现为「取消了但显存没释放 / 临时目录删不掉」。
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=30, creationflags=_NO_WINDOW)
        else:
            p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    try:
        p.wait(timeout=20)
    except Exception:
        pass


class Canceller:
    """协作式取消。

    两个作用缺一不可：
      1) `request()` 由**别的线程**调用时，立刻杀掉当前子进程 —— 否则 ffmpeg 抽帧
         这类「长时间没有任何输出」的阶段会让读取循环一直阻塞，取消要等到它自己跑完。
      2) `check()` 在安全点抛 Cancelled，让主流程干净退出（已完成的分块留在盘上）。
    """

    def __init__(self) -> None:
        self._ev = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def requested(self) -> bool:
        return self._ev.is_set()

    def request(self) -> None:
        self._ev.set()
        with self._lock:
            p = self._proc
        if p is not None and p.poll() is None:
            kill_tree(p)

    def bind(self, p: subprocess.Popen) -> None:
        with self._lock:
            self._proc = p
        if self._ev.is_set():          # 竞态：绑定前就被取消了
            kill_tree(p)

    def unbind(self) -> None:
        with self._lock:
            self._proc = None

    def check(self) -> None:
        if self._ev.is_set():
            raise Cancelled("已取消")


def run_stream(cmd: list[str], *, cancel: Canceller | None = None,
               timeout: float | None = None, label: str = "",
               on_line=None) -> int:
    """跑长命令并把输出**逐行流式**回调出去（用于解析 ncnn 的百分比进度）。"""
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=_NO_WINDOW)
    if cancel is not None:
        cancel.bind(p)
    tail: collections.deque[str] = collections.deque(maxlen=80)
    rc = -1
    try:
        assert p.stdout is not None
        for line in iter_lines(p.stdout):
            tail.append(line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass                       # 回调里的异常不能影响流水线
            if cancel is not None and cancel.requested:
                raise Cancelled(f"{label or '命令'} 被取消")
            if timeout and (time.time() - t0) > timeout:
                raise RuntimeError(f"{label or '命令'} 超时（超过 {timeout:.0f}s）")
        rc = p.wait()
    except BaseException:
        kill_tree(p)
        raise
    finally:
        if cancel is not None:
            cancel.unbind()

    if cancel is not None and cancel.requested:
        raise Cancelled(f"{label or '命令'} 被取消")
    if rc != 0:
        raise RuntimeError(
            f"{label or '命令'} 失败 (exit={rc}, {time.time() - t0:.1f}s)\n"
            f"  命令: {' '.join(cmd)}\n"
            f"  --- 输出尾部 ---\n" + "\n".join(list(tail)[-40:])
        )
    return rc


# ---------------------------------------------------------------- 探测

def probe(ffprobe: str, path: str) -> dict:
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    p = run(cmd, timeout=120, label="ffprobe")
    d = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    vs = [s for s in d.get("streams", []) if s.get("codec_type") == "video"]
    as_ = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
    if not vs:
        raise ValueError(f"输入没有视频流: {path}")
    v = vs[0]
    fmt = d.get("format", {})

    def frac(s: str | None) -> float:
        try:
            return float(Fraction(s)) if s else 0.0
        except Exception:
            return 0.0

    r_rate, avg_rate = frac(v.get("r_frame_rate")), frac(v.get("avg_frame_rate"))
    fps = avg_rate or r_rate or 24.0
    duration = float(v.get("duration") or fmt.get("duration") or 0.0)

    # 帧数的三个来源，可信度不同：
    #   nb_frames       容器标签。**会被 -c copy 切片段搞错**（实测：6.125s@24fps 的片段
    #                   报 nb_frames=258，实际 147）→ 只在它与时长×帧率自洽时才信。
    #   时长×帧率        通常可靠，作为基准。
    #   逐帧解码计数     最可靠但慢，只在上面两者矛盾时用来定案。
    nb = v.get("nb_frames")
    nb_i = int(nb) if nb and str(nb).isdigit() else None
    est = int(round(duration * fps)) if duration and fps else 0
    frames, frames_src, contradiction = est, "时长×帧率", False
    if nb_i is not None:
        if est and abs(nb_i - est) > 1:
            contradiction = True
        else:
            frames, frames_src = nb_i, "nb_frames"
    if not frames and nb_i:
        frames, frames_src = nb_i, "nb_frames"
    return {
        "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
        "fps": fps, "r_fps": r_rate, "avg_fps": avg_rate,
        "vfr": bool(r_rate and avg_rate and abs(r_rate - avg_rate) > 0.01),
        "duration": duration, "frames": frames, "frames_src": frames_src,
        "nb_frames": nb_i, "frames_est": est, "frames_contradiction": contradiction,
        "path": str(path),
        "video_codec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"),
        "has_audio": bool(as_), "audio_codec": as_[0].get("codec_name") if as_ else None,
        "size_bytes": int(fmt.get("size") or 0),
    }


def resolve_frames(ffprobe: str, info: dict, *, on_event=None) -> dict:
    """只在「容器标签」与「时长×帧率」打架时才真解码数一遍。"""
    if not info.get("frames_contradiction"):
        return info
    _emit(on_event, stage="探测帧数", message="帧数元数据自相矛盾，逐帧解码核实")
    real = frame_count(ffprobe, info["path"], deep=True)
    if real > 0:
        info["frames"], info["frames_src"] = real, "逐帧解码"
    return info


# ---------------------------------------------------------------- 人类可读

def hhmmss(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def hbytes(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def frame_count(ffprobe: str, path: str | Path, *, deep: bool = False) -> int:
    """数一个文件的视频帧数。

    默认读容器元数据（快）。**续跑校验必须用 deep=True 真解码数**：
    元数据在「写到一半被杀」的文件上照样可能报出一个数，而实际帧数是少的。
    返回 -1 表示读不出来。
    """
    if not deep:
        try:
            p = run([ffprobe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=nb_frames", "-of", "default=nw=1:nk=1",
                     str(path)], timeout=300, label="ffprobe nb_frames")
            v = p.stdout.decode("utf-8", "replace").strip()
            if v.isdigit():
                return int(v)
        except RuntimeError:
            pass
    try:
        p = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1",
                 str(path)], timeout=7200, label="ffprobe count_frames")
        v = p.stdout.decode("utf-8", "replace").strip()
        return int(v) if v.isdigit() else -1
    except RuntimeError:
        return -1


def rmtree_report(path: Path, *, keep: bool) -> None:
    """删临时目录。**失败要出声** —— 静默 ignore_errors 会留下半成品，
    下次续跑把它当完整分块复用（这正是最难查的一类错）。"""
    if keep or not path.exists():
        return
    err = None
    for attempt in (1, 2):
        try:
            shutil.rmtree(path)
            return
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.0)
    print(f"  ⚠ 临时目录没删干净（{type(err).__name__}: {err}）→ 留在 {path}")
    print("     下次续跑会校验分块帧数，不完整的会被自动重做，不影响正确性。")


# ---------------------------------------------------------------- 事件

def _emit(on_event, **ev) -> None:
    if not on_event:
        return
    try:
        on_event(ev)
    except Exception:
        pass


# ---------------------------------------------------------------- 计划（纯计算）

def build_plan(*, src, out, model: str = DEFAULT_MODEL, scale: int = 2,
               target_width: int = 0, target_height: int = 0,
               chunk: int = DEFAULT_CHUNK, crf: int = 16, preset: str = "",
               tile="0", gpu="", threads="", workdir="",
               ffmpeg: str | None = None, ffprobe: str | None = None,
               rgan: str | None = None, info: dict | None = None,
               check_frames: bool = True, on_event=None) -> dict:
    """探测 + 计算输出尺寸/分块/峰值临时盘。**不产生任何副作用**（除了一次 ffprobe）。

    返回的 dict 可以直接 json.dumps 给前端当「计划预览」。
    """
    src = Path(src).expanduser().resolve()
    if not src.is_file():
        raise ValueError(f"输入不存在: {src}")
    if model not in MODEL_SCALES:
        raise ValueError(f"未知模型 {model}（可选：{sorted(MODEL_SCALES)}）")
    if scale not in MODEL_SCALES[model]:
        raise ValueError(
            f"模型 {model} 不支持 x{scale}（只支持 {sorted(MODEL_SCALES[model])}）")
    if (target_width or target_height) and not (target_width and target_height):
        raise ValueError("target_width / target_height 必须同时给")

    rgan = rgan or find_realesrgan()
    mdir = models_dir(rgan)
    if model == "realesr-animevideov3":
        need = list(mdir.glob(f"{model}-x{scale}.*"))
    else:
        need = list(mdir.glob(f"{model}.*"))
    if not need:
        raise ValueError(f"模型文件缺失：{mdir} 下找不到 {model} x{scale} 的权重。"
                         f"跑一次 setup_realesrgan.py 补装。")

    ffprobe = ffprobe or find_ffprobe(ffmpeg or find_ffmpeg())
    if info is None:
        info = probe(ffprobe, str(src))
    if check_frames:
        info = resolve_frames(ffprobe, info, on_event=on_event)

    out = Path(out).expanduser().resolve()
    out_w, out_h = info["width"] * scale, info["height"] * scale
    if target_width and target_height:
        final_w, final_h = int(target_width), int(target_height)
    else:
        final_w, final_h = out_w, out_h

    fps = info["fps"]
    total = info["frames"]
    chunk = max(1, int(chunk))
    n_chunks = (total + chunk - 1) // chunk if total else 0
    preset = preset or ("faster" if final_w > 3000 else "medium")
    work = Path(workdir).expanduser().resolve() if workdir \
        else out.parent / f".upscale_{out.stem}"

    # 单帧体积经验值（1280x720 量级输入，PNG）：x2≈2.8MB、x4≈11.3MB。
    # 用途只是给个量级感，别当承诺。
    per_frame_mb = {2: 2.8, 3: 5.2, 4: 11.3}.get(scale, 4.0) * \
        (info["width"] * info["height"] / (1280 * 720))
    peak_tmp = 2 * min(chunk, total or chunk) * per_frame_mb * 1e6

    warn = []
    if info["vfr"]:
        warn.append(f"变帧率（r={info['r_fps']:g} avg={info['avg_fps']:g}）：按平均帧率处理，"
                    f"可能累积漂移，建议先转恒定帧率")
    if total and total % chunk:
        warn.append(f"末块只有 {total % chunk} 帧（共 {n_chunks} 块）")
    if final_w > out_w or final_h > out_h:
        warn.append("目标尺寸大于超分输出尺寸，会额外放大（不产生新细节）")

    return {
        "src": str(src), "name": src.name, "out": str(out), "work": str(work),
        "model": model, "model_dir": str(mdir), "scale": scale,
        "src_w": info["width"], "src_h": info["height"], "src_bytes": info["size_bytes"],
        "out_w": out_w, "out_h": out_h, "final_w": final_w, "final_h": final_h,
        "fps": fps, "r_fps": info["r_fps"], "avg_fps": info["avg_fps"], "vfr": info["vfr"],
        "duration": info["duration"], "total": total, "frames_src": info["frames_src"],
        "has_audio": info["has_audio"], "audio_codec": info["audio_codec"],
        "video_codec": info["video_codec"], "pix_fmt": info["pix_fmt"],
        "chunk": chunk, "n_chunks": n_chunks, "crf": int(crf), "preset": preset,
        "tile": str(tile), "gpu": str(gpu), "threads": str(threads),
        "per_frame_mb": round(per_frame_mb, 2), "peak_tmp": peak_tmp,
        "warn": warn,
    }


# ---------------------------------------------------------------- 执行流水线

def run_pipeline(plan: dict, *, ffmpeg: str, ffprobe: str, rgan: str,
                 on_event=None, cancel: Canceller | None = None) -> dict:
    """执行 plan。返回结构化结果（含验收结论）。

    进度通过 on_event 上报，事件形如：
      {"stage": "超分 2/3", "pct": 43.2, "phase": "超分", "frac": 0.51,
       "message": "...", "log": "...", "done_frames": 120, "total": 243}
    pct 是**整片口径**的百分比，已经含当前分块内的插值，调用方直接用即可。
    """
    check = (cancel.check if cancel is not None else (lambda: None))
    src, out = Path(plan["src"]), Path(plan["out"])
    work = Path(plan["work"])
    model, scale = plan["model"], plan["scale"]
    mdir = Path(plan["model_dir"])
    fps, total, chunk, n_chunks = plan["fps"], plan["total"], plan["chunk"], plan["n_chunks"]
    final_w, final_h = plan["final_w"], plan["final_h"]
    out_w, out_h = plan["out_w"], plan["out_h"]

    out.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    frames_in = work / "_frames"
    frames_up = work / "_frames_up"
    segs = work / "_segs"
    segs.mkdir(parents=True, exist_ok=True)

    t_start = time.time()
    done_frames = 0
    n_proc = 0            # 本次真正处理过的帧数（复用分块不计）
    proc_time = 0.0       # 真正花在抽帧/超分/编码上的时间
    rate = None           # 秒/帧，按已完成的帧实测标定，不靠猜
    reused = 0
    # ncnn 每块都会重打一遍 GPU 信息横幅（18 行），不去重的话日志会被刷满，
    # 真正有用的错误行反而被冲掉。同一条非进度行只报一次。
    seen_lines: set[str] = set()

    def emit(pct=None, done=None, **kw):
        # done 可覆盖：块内进度要报「已完成块 + 本块已出帧数」，
        # 否则界面上「帧数」在整个第一块期间都停在 0，看着像卡住了。
        ev = {"done_frames": done_frames if done is None else done,
              "total": total, "rate": rate}
        if pct is not None:
            ev["pct"] = round(max(0.0, min(100.0, pct)), 2)
        ev.update(kw)
        _emit(on_event, **ev)

    emit(pct=0.0, stage="准备", message=f"{n_chunks} 块 × {chunk} 帧")

    for idx in range(n_chunks):
        check()
        start = idx * chunk
        n = min(chunk, total - start)
        seg = segs / f"seg_{idx:05d}.mkv"
        base_pct = done_frames / total * 100.0 if total else 0.0

        if seg.is_file() and seg.stat().st_size > 0:
            # 续跑：只认「帧数对得上」的分块。写到一半被杀的文件会被重做。
            emit(pct=base_pct, stage=f"校验 {idx + 1}/{n_chunks}",
                 message=f"检查已有分块 {seg.name}")
            have = frame_count(ffprobe, seg, deep=True)
            if have == n:
                done_frames += n
                reused += 1
                emit(pct=done_frames / total * 100.0, stage=f"复用 {idx + 1}/{n_chunks}",
                     message=f"{seg.name} 已校验（{n} 帧）",
                     log=f"[{idx + 1}/{n_chunks}] 复用分块 {seg.name}（{n} 帧，已校验）")
                continue
            emit(stage=f"重做 {idx + 1}/{n_chunks}",
                 message=f"分块不完整（{have}/{n} 帧），重做",
                 log=f"[{idx + 1}/{n_chunks}] 分块 {seg.name} 不完整（{have}/{n} 帧）→ 重做")
            seg.unlink()

        for d in (frames_in, frames_up):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)

        # ---- 抽帧
        t0 = time.time()
        ss = start / fps
        got = 0

        def on_extract(_line):
            emit(pct=None)                     # ffmpeg 抽帧不打点，只维持阶段

        emit(pct=base_pct, stage=f"抽帧 {idx + 1}/{n_chunks}", phase="抽帧", frac=0.0,
             message=f"第 {idx + 1} 块，{n} 帧，起点 {hhmmss(ss)}")
        # -ss 放在 -i 前：快速定位；转码时 ffmpeg 默认开启 accurate_seek，会解码到精确时间点
        run_stream([ffmpeg, "-v", "error", "-y", "-ss", f"{ss:.6f}", "-i", str(src),
                    "-an", "-sn", "-frames:v", str(n),
                    "-fps_mode", "passthrough", "-start_number", "1",
                    str(frames_in / "f_%05d.png")],
                   cancel=cancel, timeout=3600, label=f"抽帧 {idx + 1}/{n_chunks}")
        got = len(list(frames_in.glob("*.png")))
        if got != n:
            if idx == n_chunks - 1 and 0 < got < n:
                emit(log=f"  · 末块实际 {got} 帧（预期 {n}），按实际处理")
                n = got
            else:
                raise RuntimeError(
                    f"抽帧数量不符：预期 {n}，实际 {got}（分块 {idx + 1}）。\n"
                    f"  源: {src}\n  起始时间: {ss:.6f}s\n"
                    f"  通常是变帧率或 -ss 落点问题。临时帧保留在 {frames_in}。"
                )
        t_extract = time.time() - t0
        check()

        # ---- 超分
        t0 = time.time()
        _base, _n, _idx = base_pct, n, idx
        _done = done_frames          # 本块开始时已完成的帧数（块内进度要加在它上面）

        # ⚠️ 别拿 exe 的 stdout 百分比当实时进度。它在管道上是**全缓冲**的（stdio 默认
        # 4096B）：实测 120 帧的 960 行进度只分 2 次到达（t=2.6s 和进程结束），
        # 而且逐文件报 0/25/50/75 从不打 100 —— 拿它算 frac 会在开头就跳到该块的终值。
        # 真正可用的进度源是**输出目录的文件数**：ncnn 每超分完一帧就落一张 PNG，
        # 实测 120 帧能观察到 10 个中间值，均匀且真实。
        stop_watch = threading.Event()
        prog = {"got": 0}

        def watch():
            while not stop_watch.wait(0.5):
                got = len(list(frames_up.glob("*.png")))
                if got == prog["got"]:
                    continue
                prog["got"] = got
                frac = min(1.0, got / max(1, _n))
                emit(pct=(_base + (W_EXTRACT + W_UPSCALE * frac) * _n / total * 100.0)
                     if total else 0.0,
                     done=_done + got,
                     stage=f"超分 {_idx + 1}/{n_chunks}", phase="超分",
                     frac=round(frac, 4), message=f"{got}/{_n} 帧")

        def on_up(line: str):
            """exe 输出只用来兜错误信息；进度不看它。"""
            if PCT_RE.search(line.encode("utf-8", "replace")):
                return
            s = line.strip()
            # 只报第一次出现的非进度行（挡掉每块都重打一遍的 GPU 横幅）
            if s and len(s) <= 300 and s not in seen_lines:
                seen_lines.add(s)
                # ⚠️ 必须带上 done：这个 emit 不带的话会把 done_frames 打回本块起点，
                # 界面上「帧数」就会从 8 掉回 0（实测抓到过）。
                emit(log=f"    {s}", done=_done + prog["got"])

        # -m 显式给：默认值是相对路径 models（这个构建版按 exe 位置解析，
        # 但显式传才能让 REALESRGAN_MODELS 覆盖生效）
        cmd = [rgan, "-i", str(frames_in), "-o", str(frames_up), "-m", str(mdir),
               "-n", model, "-s", str(scale), "-t", str(plan.get("tile") or "0")]
        if plan.get("gpu"):
            cmd += ["-g", str(plan["gpu"])]
        if plan.get("threads"):
            cmd += ["-j", str(plan["threads"])]

        emit(pct=(_base + W_EXTRACT * _n / total * 100.0) if total else 0.0,
             stage=f"超分 {_idx + 1}/{n_chunks}", phase="超分", frac=0.0,
             message=f"0/{_n} 帧")
        w = threading.Thread(target=watch, daemon=True)
        w.start()
        try:
            run_stream(cmd, cancel=cancel, timeout=3600 * 6,
                       label=f"超分 {idx + 1}/{n_chunks}", on_line=on_up)
        finally:
            stop_watch.set()
            w.join(timeout=3)

        got_up = len(list(frames_up.glob("*.png")))
        if got_up != n:
            raise RuntimeError(f"超分输出数量不符：预期 {n}，实际 {got_up}。保留现场 {frames_up}")
        t_up = time.time() - t0
        check()

        # ---- 编码分块
        t0 = time.time()
        # 编码阶段这 n 帧已经全超分完了，计数直接给到位 —— 否则「帧数」会
        # 从本块末尾的 50 掉回 0 再跳到 60，看着像倒退。
        emit(pct=base_pct + (W_EXTRACT + W_UPSCALE) * n / total * 100.0 if total else 0.0,
             done=done_frames + n,
             stage=f"编码 {idx + 1}/{n_chunks}", phase="编码", frac=0.0,
             message=f"{n} 帧 → {seg.name}")
        enc = [ffmpeg, "-v", "error", "-y",
               "-framerate", f"{fps:.6f}", "-start_number", "1",
               "-i", str(frames_up / "f_%05d.png"),
               "-frames:v", str(n),
               "-c:v", "libx264", "-crf", str(plan["crf"]), "-preset", plan["preset"],
               "-pix_fmt", "yuv420p"]
        if (final_w, final_h) != (out_w, out_h):
            enc += ["-vf", f"scale={final_w}:{final_h}:flags=lanczos"]
        enc += ["-an", "-f", "matroska", str(seg)]
        run_stream(enc, cancel=cancel, timeout=3600 * 6, label=f"编码 {idx + 1}/{n_chunks}")
        t_enc = time.time() - t0

        have = frame_count(ffprobe, seg)
        if have != n:
            raise RuntimeError(
                f"分块编码后帧数不符：预期 {n}，实际 {have}（{seg}）。\n"
                "  不要带着这个分块继续跑，否则成片会在分块边界缺帧。"
            )

        done_frames += n
        n_proc += n
        proc_time += t_extract + t_up + t_enc
        elapsed = time.time() - t_start
        # s/帧 只按**真正处理过**的帧算：复用分块的零耗时会把这个数拉低，
        # 进而把 ETA 报得过分乐观
        rate = proc_time / n_proc if n_proc else None
        eta = rate * (total - done_frames) if rate else 0.0
        emit(pct=done_frames / total * 100.0, stage=f"完成 {idx + 1}/{n_chunks}",
             phase="", frac=1.0, timings={"extract": round(t_extract, 2),
                                          "upscale": round(t_up, 2),
                                          "encode": round(t_enc, 2)},
             chunk_idx=idx, chunk_frames=n, eta=round(eta, 1),
             elapsed=round(elapsed, 1),
             message=f"{n} 帧  抽帧 {t_extract:.1f}s · 超分 {t_up:.1f}s · 编码 {t_enc:.1f}s",
             log=f"[{idx + 1}/{n_chunks}] {n} 帧  "
                 f"抽帧 {t_extract:.1f}s · 超分 {t_up:.1f}s · 编码 {t_enc:.1f}s  "
                 f"| {done_frames}/{total} ({done_frames / total * 100:.1f}%)  "
                 f"已用 {hhmmss(elapsed)}  剩 ~{hhmmss(eta)}")

        for d in (frames_in, frames_up):
            shutil.rmtree(d, ignore_errors=True)

    if done_frames != total:
        emit(log=f"⚠ 累计帧数 {done_frames} ≠ 源 {total}，成片可能缺帧。")

    # ---- 拼接
    check()
    emit(pct=min(99.0, done_frames / total * 100.0), stage="拼接分块",
         message=f"{n_chunks} 个分块")
    lst = work / "_concat.txt"
    with open(lst, "w", encoding="utf-8") as fh:
        for idx in range(n_chunks):
            fh.write(f"file '{(segs / f'seg_{idx:05d}.mkv').as_posix()}'\n")
    concat_v = work / "_concat.mkv"
    run_stream([ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                "-i", str(lst), "-c", "copy", str(concat_v)],
               cancel=cancel, timeout=3600, label="拼接")

    # ---- 合成成片：视频来自拼接结果，音轨从原片直接 copy（不经重编码）
    check()
    vid_dur = done_frames / fps
    emit(pct=99.0, stage="写最终文件", message="音轨从原片 copy")
    if plan["has_audio"]:
        # 用 -t 卡片长：不要用 -shortest（旁白轨更短时会把画面砍掉且不报错）
        run_stream([ffmpeg, "-v", "error", "-y", "-i", str(concat_v), "-i", str(src),
                    "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                    "-t", f"{vid_dur:.6f}", "-movflags", "+faststart", str(out)],
                   cancel=cancel, timeout=3600, label="合成成片")
    else:
        run_stream([ffmpeg, "-v", "error", "-y", "-i", str(concat_v),
                    "-c", "copy", "-movflags", "+faststart", str(out)],
                   cancel=cancel, timeout=3600, label="输出")

    # ---- 验收：把产出重新探一遍，而不是相信「没报错」
    emit(pct=99.5, stage="验收", message="重新探测产出")
    o = probe(ffprobe, str(out))
    out_frames = frame_count(ffprobe, out, deep=bool(plan.get("verify_frames")))
    ok_dims = (o["width"], o["height"]) == (final_w, final_h)
    ok_audio = plan["has_audio"] == o["has_audio"]
    ok_fps = abs(o["fps"] - fps) < 0.05
    ok_frames = (out_frames == total) if out_frames > 0 else None
    drift = abs(o["duration"] - plan["duration"])

    problems = []
    if not ok_dims:
        problems.append(f"输出尺寸不符（{o['width']}x{o['height']}，预期 {final_w}x{final_h}）")
    if not ok_fps:
        problems.append("帧率不符")
    if not ok_audio:
        problems.append("音轨丢失")
    if drift > 0.5:
        problems.append(f"时长漂移 {drift:.2f}s")
    if ok_frames is False:
        problems.append(f"帧数不符（源 {total} / 成片 {out_frames}）")

    elapsed_total = time.time() - t_start
    result = {
        "out": str(out), "out_name": out.name,
        "width": o["width"], "height": o["height"], "fps": o["fps"],
        "duration": o["duration"], "bytes": o["size_bytes"],
        "frames": out_frames, "total_frames": total,
        "elapsed": round(elapsed_total, 1), "rate": round(rate, 4) if rate else None,
        "reused_chunks": reused, "processed_frames": n_proc,
        "checks": {"dims": ok_dims, "fps": ok_fps, "audio": ok_audio,
                   "frames": ok_frames, "drift": round(drift, 3)},
        "problems": problems, "ok": not problems,
    }
    if not plan.get("keep_temp", False):
        rmtree_report(work, keep=False)
        result["temp_removed"] = True
    else:
        result["temp_removed"] = False
        result["work"] = str(work)

    emit(pct=100.0, stage="完成" if not problems else "有问题", result=result,
         message="；".join(problems) if problems else "全部检查通过")
    return result


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="视频超分（Real-ESRGAN ncnn-vulkan + ffmpeg）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="模型与倍数必须匹配：realesr-animevideov3 支持 2/3/4，x4plus 只支持 4。",
    )
    p.add_argument("-i", "--input", required=True, help="输入视频")
    p.add_argument("-o", "--output", required=True, help="输出视频（.mp4）")
    p.add_argument("-s", "--scale", type=int, default=2, choices=[2, 3, 4],
                   help="放大倍数（默认 2）")
    p.add_argument("-n", "--model", default=DEFAULT_MODEL, choices=sorted(MODEL_SCALES),
                   help=f"超分模型（默认 {DEFAULT_MODEL}）")
    p.add_argument("--target-width", type=int, default=0,
                   help="超分后再缩放到该宽度（超采样抗锯齿，需与 --target-height 同给）")
    p.add_argument("--target-height", type=int, default=0)
    p.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                   help=f"分块帧数（默认 {DEFAULT_CHUNK}）—— 越小临时盘占用越低、往返开销越大")
    p.add_argument("--crf", type=int, default=16, help="x264 CRF（默认 16，越小越清晰越大）")
    p.add_argument("--preset", default=None,
                   help="x264 preset（默认：输出宽 >3000 用 faster，否则 medium）")
    p.add_argument("--tile", default="0", help="ncnn tile 尺寸（默认 0=自动，显存紧张时调小如 128）")
    p.add_argument("--gpu", default="", help="ncnn -g 参数（默认自动选卡；留空即自动）")
    p.add_argument("--threads", default="", help="ncnn -j 参数，如 2:4:4")
    p.add_argument("--workdir", default="", help="临时目录（默认输出同级 .upscale_<名字>）")
    p.add_argument("--keep-temp", action="store_true", help="保留临时帧（排错用）")
    p.add_argument("--verify-frames", action="store_true",
                   help="对最终成片真解码逐帧计数（慢，但能抓出分块边界缺帧）")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    p.add_argument("--compare", type=float, default=None, metavar="SEC",
                   help="在该时间点输出 Lanczos vs Real-ESRGAN 的画质对比图后退出")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def print_plan(plan: dict) -> None:
    print("=" * 68)
    print("视频超分 · 计划")
    print("=" * 68)
    print(f"  输入     {plan['name']}")
    print(f"           {plan['src_w']}x{plan['src_h']} @ {plan['fps']:g}fps  "
          f"{hhmmss(plan['duration'])}  {plan['total']} 帧（{plan['frames_src']}）  "
          f"{hbytes(plan['src_bytes'])}  "
          f"{'含音轨(' + str(plan['audio_codec']) + ')' if plan['has_audio'] else '无音轨'}")
    print(f"  超分     {plan['model']}  x{plan['scale']}  →  {plan['out_w']}x{plan['out_h']}")
    if (plan["final_w"], plan["final_h"]) != (plan["out_w"], plan["out_h"]):
        print(f"  输出     {plan['final_w']}x{plan['final_h']}（超采样后缩放）")
    print(f"  编码     libx264 crf={plan['crf']} preset={plan['preset']}")
    print(f"  分块     {plan['n_chunks']} 块 × {plan['chunk']} 帧   "
          f"峰值临时盘 ≈ {hbytes(plan['peak_tmp'])}")
    print(f"  模型     {plan['model_dir']}")
    for w in plan["warn"]:
        print(f"  ⚠ {w}")
    print("=" * 68)


def main() -> int:
    args = build_parser().parse_args()
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe(ffmpeg)
    rgan = find_realesrgan()

    try:
        plan = build_plan(
            src=args.input, out=args.output, model=args.model, scale=args.scale,
            target_width=args.target_width, target_height=args.target_height,
            chunk=args.chunk, crf=args.crf, preset=args.preset or "",
            tile=args.tile, gpu=args.gpu, threads=args.threads, workdir=args.workdir,
            ffmpeg=ffmpeg, ffprobe=ffprobe, rgan=rgan,
            check_frames=True,
            on_event=(lambda ev: print(f"  · {ev.get('message')}")
                      if ev.get("stage") == "探测帧数" else None),
        )
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    if args.compare is not None:
        return do_compare(args, ffmpeg, rgan, Path(plan["src"]),
                          {"width": plan["src_w"], "height": plan["src_h"]})

    plan["verify_frames"] = args.verify_frames
    plan["keep_temp"] = args.keep_temp
    print_plan(plan)
    print(f"临时目录 {plan['work']}\n")

    if args.dry_run:
        print("--dry-run：未执行。")
        return 0

    def on_event(ev: dict) -> None:
        if ev.get("log"):
            print(ev["log"])
        elif ev.get("result"):
            pass
        elif ev.get("stage") and ev.get("message") and not ev.get("phase"):
            print(f"  · {ev['stage']}：{ev['message']}")

    try:
        res = run_pipeline(plan, ffmpeg=ffmpeg, ffprobe=ffprobe, rgan=rgan,
                           on_event=on_event, cancel=None)
    except Cancelled:
        print("\n中断。分块已完成的会保留，重跑同一命令可续跑。")
        return 130
    except RuntimeError as e:
        print(f"\n✗ {e}")
        return 2

    o, ch = res, res["checks"]
    print("\n" + "=" * 68)
    print("完成")
    print("=" * 68)
    print(f"  输出     {o['out']}")
    print(f"           {o['width']}x{o['height']} @ {o['fps']:g}fps  "
          f"{hhmmss(o['duration'])}  {hbytes(o['bytes'])}")
    if o["rate"]:
        print(f"  耗时     {hhmmss(o['elapsed'])}（{o['rate']:.3f} s/帧 实测）")
    else:
        # 全部分块都被复用时 rate 从未标定过 —— 别拿「只剩拼接的时间」冒充超分耗时
        print(f"  耗时     {hhmmss(o['elapsed'])}（全部分块复用，本次未重新超分）")
    print(f"  检查     尺寸 {'✓' if ch['dims'] else '✗'}   "
          f"帧率 {'✓' if ch['fps'] else '✗'}   "
          f"音轨 {'✓' if ch['audio'] else '✗（源有音轨但输出没有）'}   "
          f"时长差 {ch['drift']:+.2f}s")
    if ch["frames"] is None:
        print(f"  帧数     源 {o['total_frames']} / 成片 {o['frames']}（读不出，未判定）")
    else:
        print(f"  帧数     源 {o['total_frames']} / 成片 {o['frames']} "
              f"{'✓' + ('（逐帧解码）' if args.verify_frames else '') if ch['frames'] else '✗ 缺帧'}")
    if args.keep_temp:
        print(f"  临时保留 {plan['work']}")

    if o["problems"]:
        print("\n✗ " + "；".join(o["problems"]))
        return 1
    print("\n✓ 全部检查通过")
    return 0


def do_compare(args, ffmpeg: str, rgan: str, src: Path, info: dict) -> int:
    """出画质对比图：左 = Lanczos 同倍数，右 = Real-ESRGAN。肉眼判断值不值得。"""
    import tempfile
    sec = args.compare
    w, h = info["width"], info["height"]
    scale = args.scale
    tw, th = w * scale, h * scale
    tmp = Path(tempfile.mkdtemp(prefix="cmp_"))
    try:
        run([ffmpeg, "-v", "error", "-y", "-ss", f"{sec:.3f}", "-i", str(src),
             "-frames:v", "1", str(tmp / "src.png")], timeout=300, label="取帧")
        run([ffmpeg, "-v", "error", "-y", "-i", str(tmp / "src.png"),
             "-vf", f"scale={tw}:{th}:flags=lanczos", str(tmp / "lanczos.png")],
            timeout=300, label="lanczos")
        cmd = [rgan, "-i", str(tmp / "src.png"), "-o", str(tmp / "esrgan.png"),
               "-m", str(models_dir(rgan)),
               "-n", args.model, "-s", str(scale), "-t", str(args.tile)]
        run(cmd, timeout=1800, label="超分")
        cw, ch = min(480, tw), min(480, th)
        x, y = max(0, tw // 2 - cw // 2), max(0, th // 2 - ch // 2)
        for name in ("lanczos", "esrgan"):
            run([ffmpeg, "-v", "error", "-y", "-i", str(tmp / f"{name}.png"),
                 "-vf", f"crop={cw}:{ch}:{x}:{y}", str(tmp / f"{name}_c.png")],
                timeout=300, label=f"裁切 {name}")
        outp = Path(args.output).resolve()
        outp.parent.mkdir(parents=True, exist_ok=True)
        # 左侧 Lanczos / 右侧 Real-ESRGAN。不加 drawtext：无 fontconfig 的机器上会失败
        run([ffmpeg, "-v", "error", "-y", "-i", str(tmp / "lanczos_c.png"),
             "-i", str(tmp / "esrgan_c.png"),
             "-filter_complex", "[0:v][1:v]hstack=inputs=2", str(outp)],
            timeout=300, label="拼接对比")
        print(f"对比图已写出：{outp}")
        print(f"  左 = Lanczos x{scale}（纯插值，无新细节）")
        print(f"  右 = {args.model} x{scale}（模型重建细节）")
        print(f"  时间点 {sec}s，裁切区域 {cw}x{ch} @ ({x},{y})，放大后 {tw}x{th}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断。分块已完成的会保留，重跑同一命令可续跑。")
        sys.exit(130)
    except RuntimeError as e:
        print(f"\n✗ {e}")
        sys.exit(2)
