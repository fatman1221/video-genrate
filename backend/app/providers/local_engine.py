"""本地执行引擎：ffmpeg / ffprobe / PIL / macOS say。

这是第一阶段真正"能跑"的执行底座：
- 图像：PIL 生成漫画风格分镜占位图（含角色剪影、网点、字幕条）
- 视频：ffmpeg 由关键帧生成带运镜的真实 MP4
- 语音：macOS say 合成本地 TTS
- 音乐 / 音效：ffmpeg lavfi 合成
- 视频处理：concat / mux / 混音 / 烧字幕 / 超分 / 补帧 / 降噪 / 调色

上层 Provider 只调用这里，业务层完全无感知。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..config import settings

ProgressCb = Callable[[int, str], None] | None

FFMPEG = settings.ffmpeg_bin
FFPROBE = settings.ffprobe_bin

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

# macOS 用系统 say；Windows 没有 say，改走 CosyVoice / edge-tts / SAPI（见 say_tts）
SAY = "/usr/bin/say"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh") or ""

_CJK_FONT_CANDIDATES = (
    # ---- Windows ----
    "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",     # 雅黑粗体
    "C:/Windows/Fonts/simhei.ttf",     # 黑体
    "C:/Windows/Fonts/simsun.ttc",     # 宋体
    "C:/Windows/Fonts/Deng.ttf",       # 等线
    # ---- macOS ----
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
)

# 烧字幕时 langass 需要的字体名（force_style 的 FontName）。
# 不指定的话，libass 在 Windows 上常常找不到字体 → 中文渲染成方块或整条不显示。
_SUBTITLE_FONT_CANDIDATES = (
    "Microsoft YaHei",   # Windows 微软雅黑
    "SimHei",            # Windows 黑体
    "PingFang SC",       # macOS
    "Hiragino Sans GB",
    "Noto Sans CJK SC",
    "Arial Unicode MS",
)

_FONT_CACHE: dict[int, Any] = {}


class EngineError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True, detail: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.detail = detail


# --------------------------------------------------------------------------- #
# ffmpeg 基础调用
# --------------------------------------------------------------------------- #
def ffmpeg_available() -> bool:
    return bool(shutil.which(FFMPEG) or Path(FFMPEG).exists())


def ffprobe_available() -> bool:
    return bool(shutil.which(FFPROBE) or Path(FFPROBE).exists())


def run_ffmpeg(
    args: list[str],
    *,
    timeout: int = 1800,
    progress_cb: ProgressCb = None,
    total_duration: float = 0.0,
    label: str = "ffmpeg",
) -> str:
    """执行 ffmpeg，支持进度回调。返回 stderr 尾部（用于诊断）。

    超时由独立的看门狗线程执行，因此即使 ffmpeg 完全不向 stdout 输出
    （例如 libpng 解码卡死），也能被可靠杀死，不会把任务永久挂住。
    """
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
    err_file = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    proc: subprocess.Popen | None = None
    killed = {"by": None}

    def _kill_tree(reason: str) -> None:
        if proc is None or proc.poll() is not None:
            return
        killed["by"] = reason
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _watchdog() -> None:
        if proc is None:
            return
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree("timeout")

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=err_file, text=True, bufsize=1,
            start_new_session=True,
        )
        assert proc.stdout is not None
        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()
        for line in proc.stdout:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key in ("out_time_us", "out_time_ms") and progress_cb and total_duration > 0:
                try:
                    done_s = int(value) / 1_000_000
                except ValueError:
                    continue
                pct = max(0, min(97, int(done_s / total_duration * 100)))
                progress_cb(pct, f"{label} {done_s:.1f}s/{total_duration:.1f}s")
        proc.wait(timeout=30)
        wd.join(timeout=1)
        err_file.seek(0)
        stderr_tail = "".join(err_file.readlines()[-30:])
        if killed["by"] == "timeout":
            raise EngineError(f"{label} 超时（>{timeout}s）", retryable=True, detail=stderr_tail.strip())
        if proc.returncode != 0:
            raise EngineError(
                f"{label} 执行失败（exit={proc.returncode}）",
                retryable=True,
                detail=stderr_tail.strip(),
            )
        if progress_cb:
            progress_cb(99, f"{label} 完成")
        return stderr_tail
    finally:
        if proc is not None and proc.poll() is None:
            _kill_tree("cleanup")
        err_file.close()
        try:
            os.unlink(err_file.name)
        except OSError:
            pass


def ffprobe(path: str | Path) -> dict[str, Any]:
    """返回视频/音频元数据。"""
    cmd = [
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:  # pragma: no cover
        raise EngineError("ffprobe 不可用", retryable=False) from exc
    if out.returncode != 0:
        raise EngineError(f"ffprobe 读取失败: {path}", retryable=False, detail=out.stderr.strip())
    data = json.loads(out.stdout or "{}")
    fmt = data.get("format", {})
    info: dict[str, Any] = {
        "duration": float(fmt.get("duration") or 0.0),
        "size_bytes": int(fmt.get("size") or 0),
        "format": (fmt.get("format_name") or "").split(",")[0],
        "has_video": False,
        "has_audio": False,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "sample_rate": 0,
        "channels": 0,
    }
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not info["has_video"]:
            info["has_video"] = True
            info["width"] = int(stream.get("width") or 0)
            info["height"] = int(stream.get("height") or 0)
            rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
            try:
                num, _, den = rate.partition("/")
                info["fps"] = round(float(num) / float(den or 1), 3)
            except (ValueError, ZeroDivisionError):
                info["fps"] = 0.0
            if not info["duration"] and stream.get("duration"):
                info["duration"] = float(stream["duration"])
        elif stream.get("codec_type") == "audio":
            info["has_audio"] = True
            info["sample_rate"] = int(stream.get("sample_rate") or 0)
            info["channels"] = int(stream.get("channels") or 0)
    return info


def audio_duration(path: str | Path) -> float:
    try:
        return ffprobe(path)["duration"]
    except EngineError:
        return 0.0


# --------------------------------------------------------------------------- #
# 图像生成（PIL，漫画教学风格）
# --------------------------------------------------------------------------- #
def _font(size: int) -> Any:
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    for candidate in _CJK_FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, size, index=0)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


_PALETTES = (
    ((242, 236, 255), (150, 108, 240), (72, 52, 128)),   # 紫
    ((232, 244, 255), (94, 154, 240), (36, 62, 122)),    # 蓝
    ((255, 240, 235), (240, 132, 96), (124, 56, 36)),    # 橙
    ((235, 250, 240), (86, 190, 140), (24, 88, 66)),     # 绿
    ((255, 246, 224), (238, 188, 76), (120, 84, 16)),    # 金
    ((252, 236, 246), (232, 118, 176), (116, 36, 84)),   # 粉
)


def _stable_seed(*parts: Any) -> int:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def make_storyboard_image(
    *,
    out_path: str,
    width: int = 1280,
    height: int = 720,
    title: str = "",
    body: str = "",
    badge: str = "",
    seed_key: str = "",
    style_tag: str = "",
) -> str:
    """生成漫画教学风格的分镜占位图（真实 PNG 文件）。"""
    seed = _stable_seed(seed_key or title, width, height)
    bg, accent, dark = _PALETTES[seed % len(_PALETTES)]

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # 1) 斜向渐变底
    for y in range(height):
        ratio = y / max(height - 1, 1)
        blend = tuple(int(bg[i] * (1 - ratio * 0.35) + accent[i] * ratio * 0.35) for i in range(3))
        draw.line([(0, y), (width, y)], fill=blend)

    # 2) 漫画网点（halftone）
    dot_layer = Image.new("L", (width, height), 0)
    dd = ImageDraw.Draw(dot_layer)
    step = max(int(width / 46), 12)
    radius = max(int(step / 5), 2)
    offset = seed % step
    for gy in range(-step, height + step, step):
        for gx in range(-step, width + step, step):
            jitter = ((gx * 7 + gy * 13 + offset) % 5) - 2
            dd.ellipse(
                [gx + jitter - radius, gy + jitter - radius, gx + jitter + radius, gy + jitter + radius],
                fill=70,
            )
    img = Image.composite(Image.new("RGB", (width, height), dark), img, dot_layer.point(lambda v: 0 if v == 0 else 26))
    draw = ImageDraw.Draw(img)

    # 3) 地平线 / 场景区块
    horizon = int(height * 0.62)
    draw.polygon(
        [(0, horizon), (width * 0.35, horizon - height * 0.16), (width * 0.62, horizon),
         (width, horizon - height * 0.1), (width, height), (0, height)],
        fill=tuple(int(c * 0.92) for c in accent),
    )

    # 4) 角色剪影（数量由 seed 决定）
    figure_count = 1 + seed % 3
    for idx in range(figure_count):
        cx = int(width * (0.28 + 0.22 * idx) + (seed % 40) - 20)
        base_y = horizon + int(height * 0.06)
        head_r = int(height * 0.075)
        body_w = int(head_r * 2.4)
        draw.ellipse([cx - head_r, base_y - head_r * 3.4, cx + head_r, base_y - head_r * 1.4], fill=dark)
        draw.rounded_rectangle(
            [cx - body_w // 2, base_y - head_r * 1.4, cx + body_w // 2, base_y + head_r * 1.6],
            radius=int(head_r * 0.6), fill=dark,
        )

    # 5) 对话气泡
    bubble = [int(width * 0.55), int(height * 0.12), int(width * 0.94), int(height * 0.36)]
    draw.rounded_rectangle(bubble, radius=int(height * 0.03), fill=(255, 255, 255), outline=dark, width=3)
    draw.polygon(
        [(bubble[0] + 24, bubble[3]), (bubble[0] + 60, bubble[3]), (bubble[0] + 20, bubble[3] + 26)],
        fill=(255, 255, 255), outline=dark,
    )

    # 6) 顶部标签条
    if badge:
        pad = int(height * 0.028)
        f = _font(max(int(height * 0.042), 18))
        bbox = draw.textbbox((0, 0), badge, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.rounded_rectangle(
            [pad, pad, pad + tw + pad * 2, pad + th + pad * 1.6],
            radius=int(pad * 1.2), fill=dark,
        )
        draw.text((pad + pad * 0.9, pad + pad * 0.7 - bbox[1] * 0.3), badge, font=f, fill=(255, 255, 255))

    # 7) 标题（气泡内）+ 正文（底部字幕条）
    if title:
        f_title = _font(max(int(height * 0.034), 16))
        wrapped = textwrap.fill(title, width=13)[:60]
        draw.multiline_text(
            (bubble[0] + 18, bubble[1] + 16), wrapped, font=f_title, fill=(28, 24, 40),
            spacing=int(height * 0.012),
        )
    if body:
        f_body = _font(max(int(height * 0.03), 14))
        text = textwrap.fill(body, width=32)[:120]
        strip_top = int(height * 0.84)
        draw.rectangle([0, strip_top, width, height], fill=(18, 16, 28))
        draw.multiline_text(
            (int(width * 0.035), strip_top + int(height * 0.025)), text,
            font=f_body, fill=(238, 236, 248), spacing=int(height * 0.012),
        )

    # 8) 风格标签
    if style_tag:
        f_tag = _font(max(int(height * 0.028), 14))
        bbox = draw.textbbox((0, 0), style_tag, font=f_tag)
        draw.text(
            (width - (bbox[2] - bbox[0]) - int(width * 0.03), int(height * 0.045)),
            style_tag, font=f_tag, fill=dark,
        )

    img = img.filter(ImageFilter.SMOOTH)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    # 注意：不要使用 optimize=True。PIL 的 optimize 会生成特定 zlib 过滤组合的 PNG，
    # 实测会让 ffmpeg(libpng) 在 -loop 1 读取时无限阻塞（仅个别图像触发，概率约 1/4）。
    # 使用默认参数写出的标准 PNG 可稳定被 ffmpeg 解码。
    _save_png_std(img, out_path)
    return out_path


def _save_png_std(img: "Image.Image", out_path: str | Path) -> None:
    """以"ffmpeg 友好"的方式写 PNG：RGB、无隔行、默认压缩。"""
    tmp = Path(out_path)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    img.save(tmp, format="PNG", interlace=False)


# --------------------------------------------------------------------------- #
# 视频生成 / 处理
# --------------------------------------------------------------------------- #
def image_to_video(
    *,
    image_path: str,
    out_path: str,
    duration: float = 5.0,
    fps: int = 24,
    width: int = 1280,
    height: int = 720,
    motion: str = "auto",
) -> str:
    """由关键帧生成带运镜的真实 MP4（Ken Burns）。"""
    frames = max(int(duration * fps), 1)
    seed = _stable_seed(image_path, motion)
    motions = {
        "zoom_in": "z='min(zoom+0.0012,1.25)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
        "zoom_out": "z='if(lte(zoom,1.0),1.25,max(1.001,zoom-0.0012))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
        "pan_left": "z='1.18':x='iw/2-(iw/zoom/2)+(iw/2-iw/zoom/2)*0.6*(on/%d)':y='ih/2-(ih/zoom/2)'" % frames,
        "pan_right": "z='1.18':x='iw/2-(iw/zoom/2)-(iw/2-iw/zoom/2)*0.6*(on/%d)':y='ih/2-(ih/zoom/2)'" % frames,
    }
    if motion == "auto":
        motion = ("zoom_in", "zoom_out", "pan_left", "pan_right")[seed % 4]
    expr = motions.get(motion, motions["zoom_in"])

    vf = (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan={expr}:d={frames}:s={width}x{height}:fps={fps},"
        f"format=yuv420p"
    )
    # 输入归一化：把任意来源（PIL optimize / ComfyUI / 云端 / 用户上传）的图片
    # 重编码为 ffmpeg 友好的标准 PNG，避免解码器卡死。
    tmp_png = Path(tempfile.gettempdir()) / f"_norm_{seed}_{int(time.time()*1000)}.png"
    try:
        with Image.open(image_path) as src:
            _save_png_std(src.convert("RGB"), tmp_png)
        render_input = str(tmp_png)
    except Exception:
        render_input = image_path

    args = [
        "-loop", "1", "-framerate", str(fps), "-i", render_input,
        "-vf", vf, "-t", f"{duration:.3f}",
        "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", out_path,
    ]
    try:
        run_ffmpeg(args, total_duration=duration, label="镜头视频", timeout=180,
                   progress_cb=None)
    finally:
        try:
            tmp_png.unlink()
        except OSError:
            pass
    return out_path


def concat_videos(*, video_paths: list[str], out_path: str,
                  progress_cb: ProgressCb = None, work_dir: str | None = None) -> str:
    """按顺序拼接多个 MP4（统一重编码，保证兼容）。"""
    if not video_paths:
        raise EngineError("没有可拼接的视频", retryable=False)
    if len(video_paths) == 1:
        shutil.copyfile(video_paths[0], out_path)
        return out_path

    work = Path(work_dir or tempfile.mkdtemp(prefix="concat_"))
    work.mkdir(parents=True, exist_ok=True)
    list_file = work / "concat_list.txt"
    list_file.write_text(
        "".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in video_paths), encoding="utf-8",
    )
    total = sum(audio_duration(p) or 5.0 for p in video_paths)
    args = [
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(settings.default_fps),
        "-movflags", "+faststart", "-an", out_path,
    ]
    run_ffmpeg(args, progress_cb=progress_cb, total_duration=total, label="拼接")
    return out_path


def mux_audio(*, video_path: str, audio_path: str, out_path: str,
              volume: float = 1.0, progress_cb: ProgressCb = None) -> str:
    """给视频加上一条音轨（配音）。"""
    total = audio_duration(video_path)
    args = [
        "-i", video_path, "-i", audio_path,
        "-filter_complex", f"[1:a]volume={volume}[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart", out_path,
    ]
    run_ffmpeg(args, progress_cb=progress_cb, total_duration=total, label="混入配音")
    return out_path


def mix_audio_tracks(*, video_path: str, tracks: list[dict[str, Any]],
                     out_path: str, keep_original: bool = True,
                     progress_cb: ProgressCb = None) -> str:
    """混合多条音轨（配音 / 音乐 / 音效）。

    tracks: [{"path":..., "volume":0.2, "loop":True, "delay":0.0}, ...]

    注意：会自动探测主视频是否带音轨。若没有音轨（例如镜头视频是无声渲染的），
    自动关闭 keep_original，避免引用不存在的 [0:a] 导致 ffmpeg 失败。
    """
    info = ffprobe(video_path)
    total = float(info.get("duration") or 0.0)
    keep = bool(keep_original and info.get("has_audio"))
    tracks = [t for t in tracks if t.get("path") and Path(t["path"]).exists()]
    if not tracks and not keep:
        raise EngineError("混音失败：主视频没有音轨，且未提供任何附加音轨", retryable=False)

    inputs = ["-i", video_path]
    for t in tracks:
        if t.get("loop"):
            inputs += ["-stream_loop", "-1", "-i", t["path"]]
        else:
            inputs += ["-i", t["path"]]

    filters: list[str] = []
    labels: list[str] = []
    for idx, t in enumerate(tracks, start=1):
        delay = float(t.get("delay") or 0.0)
        parts = [f"volume={float(t.get('volume', 1.0)):.3f}"]
        if t.get("loop"):
            parts.append("atrim=0:%d" % max(int(math.ceil(total or 5)), 1))
        if delay > 0:
            parts.append(f"adelay={int(delay * 1000)}|{int(delay * 1000)}")
        filters.append(f"[{idx}:a]" + ",".join(parts) + f"[t{idx}]")
        labels.append(f"[t{idx}]")

    if keep:
        filters.append("[0:a]volume=1.000[base]")
        labels.insert(0, "[base]")

    if len(labels) == 1:
        filters.append(f"{labels[0]}anull[aout]")
    else:
        filters.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
            "dropout_transition=0:normalize=0[aout]"
        )

    args = [
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart", out_path,
    ]
    run_ffmpeg(args, progress_cb=progress_cb, total_duration=total, label="混音")
    return out_path


def build_voice_timeline(
    *, segments: list[dict[str, Any]], out_path: str,
    total_duration: float = 0.0, progress_cb: ProgressCb = None,
) -> str:
    """把逐镜头旁白按时间轴对齐成一条完整音轨。

    segments: [{"path": "...", "start": 0.0, "slot": 5.0}, ...]
    - start：该镜头在成片中的起始秒数
    - slot ：该镜头时长；若旁白比镜头长，用 atempo 轻微加速以塞进 slot，避免与下一镜头旁白重叠
    """
    valid = [s for s in segments if s.get("path") and Path(s["path"]).exists()]
    if not valid:
        raise EngineError("没有可用的旁白音轨", retryable=False)

    inputs: list[str] = []
    for seg in valid:
        inputs += ["-i", seg["path"]]

    filters: list[str] = []
    labels: list[str] = []
    for idx, seg in enumerate(valid):
        dur = audio_duration(seg["path"])
        slot = float(seg.get("slot") or 0.0)
        parts: list[str] = []
        if dur and slot and dur > slot:
            tempo = min(max(dur / slot, 1.0), 1.6)
            parts.append(f"atempo={tempo:.3f}")
        parts.append(f"volume={float(seg.get('volume', 1.0)):.3f}")
        delay = int(float(seg.get("start") or 0.0) * 1000)
        if delay > 0:
            parts.append(f"adelay={delay}|{delay}")
        filters.append(f"[{idx}:a]" + ",".join(parts) + f"[v{idx}]")
        labels.append(f"[v{idx}]")

    if len(labels) == 1:
        filters.append(f"{labels[0]}anull[aout]")
    else:
        filters.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
            "dropout_transition=0:normalize=0[aout]"
        )

    args = [
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[aout]", "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100",
    ]
    if total_duration > 0:
        args += ["-t", f"{total_duration:.3f}"]
    args.append(out_path)
    run_ffmpeg(args, progress_cb=progress_cb, total_duration=sum(
        audio_duration(s["path"]) for s in valid), label="对齐旁白")
    return out_path



def subtitle_font_name() -> str:
    """挑一个本机确实存在的中文字体名，交给 libass。

    不给 FontName 时 libass 会自行找字体，Windows 上常找不到中文字形，
    结果是字幕整条不显示或渲染成方块。
    """
    if IS_WINDOWS:
        return "Microsoft YaHei"
    if IS_MACOS:
        return "PingFang SC"
    for name in _SUBTITLE_FONT_CANDIDATES:
        if name not in ("Microsoft YaHei", "SimHei", "PingFang SC", "Hiragino Sans GB"):
            return name
    return _SUBTITLE_FONT_CANDIDATES[0]


def burn_subtitles(*, video_path: str, subtitle_path: str, out_path: str,
                   font_size: int = 22, progress_cb: ProgressCb = None) -> str:
    """烧录字幕（libass）。"""
    total = audio_duration(video_path)
    style = (
        f"FontName={subtitle_font_name()},FontSize={font_size},PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H96000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=28"
    )
    escaped = str(subtitle_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    args = [
        "-i", video_path,
        "-vf", f"subtitles='{escaped}':force_style='{style}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path,
    ]
    run_ffmpeg(args, progress_cb=progress_cb, total_duration=total, label="烧录字幕")
    return out_path


def embed_soft_subtitles(*, video_path: str, subtitle_path: str, out_path: str) -> str:
    """软字幕内嵌（mov_text），烧录失败时的降级方案。"""
    args = [
        "-i", video_path, "-i", subtitle_path,
        "-map", "0", "-map", "1:0",
        "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
        "-movflags", "+faststart", out_path,
    ]
    run_ffmpeg(args, label="内嵌字幕")
    return out_path


def scale_video(*, video_path: str, out_path: str, width: int, height: int,
                sharpen: bool = True, progress_cb: ProgressCb = None) -> str:
    total = audio_duration(video_path)
    vf = f"scale={width}:{height}:flags=lanczos"
    if sharpen:
        vf += ",unsharp=5:5:0.9:5:5:0.0"
    args = [
        "-i", video_path, "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "copy", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path,
    ]
    run_ffmpeg(args, progress_cb=progress_cb, total_duration=total, label="超分")
    return out_path


def interpolate_video(*, video_path: str, out_path: str, fps: int = 48,
                      progress_cb: ProgressCb = None) -> str:
    total = audio_duration(video_path)
    args = [
        "-i", video_path,
        "-vf", f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:vsbmc=1:me_mode=bidir",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-an", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path,
    ]
    run_ffmpeg(args, progress_cb=progress_cb, total_duration=total, label="补帧")
    return out_path


def filter_video(*, video_path: str, out_path: str, filters: str,
                 keep_audio: bool = True, progress_cb: ProgressCb = None) -> str:
    total = audio_duration(video_path)
    args = [
        "-i", video_path, "-vf", filters,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    args += ["-c:a", "copy"] if keep_audio else ["-an"]
    args.append(out_path)
    run_ffmpeg(args, progress_cb=progress_cb, total_duration=total, label="视频滤镜")
    return out_path


def trim_video(*, video_path: str, out_path: str, start: float, end: float) -> str:
    args = [
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", video_path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path,
    ]
    run_ffmpeg(args, total_duration=max(end - start, 0.1), label="裁剪")
    return out_path


# --------------------------------------------------------------------------- #
# 音频生成
# --------------------------------------------------------------------------- #
def _run_coro(coro: Any) -> Any:
    """在同步上下文里跑协程；若当前已有事件循环，则丢到独立线程执行。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _edge_tts_available() -> bool:
    try:
        import edge_tts  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


_COSYVOICE_ENTRY: list[list[str] | None] = []


def _cosyvoice_entry() -> list[str] | None:
    """定位 CosyVoice 推理入口，返回命令前缀；找不到返回 None。

    优先用 settings.cosyvoice_tts_cmd；否则探测仓库内 tools/cosyvoice/infer.py
    （那目录自带独立 venv，避免把 torch 装进 Studio 的运行环境）。
    """
    if _COSYVOICE_ENTRY:
        return _COSYVOICE_ENTRY[0]
    entry: list[str] | None = None
    if settings.cosyvoice_tts_cmd:
        entry = settings.cosyvoice_tts_cmd.split()
    else:
        root = Path(__file__).resolve().parents[3]
        cand = root / "tools" / "cosyvoice"
        script = cand / "infer.py"
        if script.exists():
            for py in (cand / ".venv" / "Scripts" / "python.exe",
                       cand / ".venv" / "bin" / "python"):
                if py.exists():
                    entry = [str(py), str(script)]
                    break
            else:
                entry = [sys.executable, str(script)]
    _COSYVOICE_ENTRY.append(entry)
    return entry


def tts_available() -> bool:
    """本机是否至少有一条可用的 TTS 通道。"""
    if _cosyvoice_entry():
        return True
    if _edge_tts_available():
        return True
    if IS_MACOS and Path(SAY).exists():
        return True
    return bool(IS_WINDOWS and POWERSHELL)


def list_voices() -> list[str]:
    """列出本机各通道可用音色（用于界面下拉）。"""
    voices: list[str] = []
    if IS_MACOS and Path(SAY).exists():
        try:
            out = subprocess.run([SAY, "-v", "?"], capture_output=True, text=True, timeout=20)
            voices += [ln.split()[0] for ln in out.stdout.splitlines() if ln.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    if IS_WINDOWS and POWERSHELL:
        try:
            proc = subprocess.run(
                [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                 "Add-Type -AssemblyName System.Speech;"
                 "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                 ".GetInstalledVoices()|%{$_.VoiceInfo.Name}"],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
            voices += [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    if _edge_tts_available():
        try:
            import edge_tts

            for v in _run_coro(edge_tts.list_voices()):
                if str(v.get("Locale", "")).startswith(("zh", "en")):
                    voices.append(v["ShortName"])
        except Exception:  # noqa: BLE001
            pass
    return voices


def _wpm_to_edge_rate(wpm: int) -> str:
    """macOS 的 words-per-minute → edge-tts 的百分比（180 wpm 约等于正常语速）。"""
    if not wpm:
        return "+0%"
    pct = int(round((wpm - 180) / 180 * 100))
    return f"{max(-50, min(100, pct)):+d}%"


def _macos_say(*, text: str, out_path: str, voice: str, rate: int) -> None:
    """macOS say：输出 aiff 再转目标格式。"""
    if not Path(SAY).exists():
        raise EngineError("本机没有 say 命令", retryable=False)
    tmp_aiff = tempfile.NamedTemporaryFile(suffix=".aiff", delete=False).name
    cmd = [SAY, "-o", tmp_aiff]
    if voice:
        cmd += ["-v", voice]
    if rate:
        cmd += ["-r", str(rate)]
    cmd.append(text)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            # 语音名不存在时降级到系统默认音色
            retry = [SAY, "-o", tmp_aiff]
            if rate:
                retry += ["-r", str(rate)]
            retry.append(text)
            proc = subprocess.run(retry, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise EngineError("say 合成失败", detail=(proc.stderr or "").strip()[:400])
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        codec = "-c:a libmp3lame -b:a 192k" if out_path.endswith(".mp3") else "-c:a pcm_s16le"
        run_ffmpeg(["-i", tmp_aiff, *codec.split(), "-ar", "44100", "-ac", "2", out_path],
                   label="转码语音")
    finally:
        try:
            os.unlink(tmp_aiff)
        except OSError:
            pass


def _sapi_tts(*, text: str, out_path: str, voice: str, rate: int) -> None:
    """Windows 内置语音（SAPI）。

    文本经 UTF-8 文件传入，不经命令行 —— 否则中文在 GBK 代码页下会乱码。
    """
    if not POWERSHELL:
        raise EngineError("本机没有 PowerShell，无法调用 SAPI", retryable=False)
    workdir = Path(tempfile.mkdtemp(prefix="sapi_"))
    txt = workdir / "text.txt"
    wav = workdir / "voice.wav"
    txt.write_text(text, encoding="utf-8")
    # SAPI 的音速是 -10..10，macOS 是 words-per-minute，按 20wpm≈1 档换算
    step = max(-10, min(10, int(round(((rate or 180) - 180) / 20))))
    pick = ""
    if voice:
        esc = voice.replace("'", "''")
        pick = (f"try{{$s.SelectVoice('{esc}')}}catch{{}};")
    script = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.Rate={step};"
        f"{pick}"
        f"$s.SetOutputToWaveFile('{wav}');"
        f"$t=[IO.File]::ReadAllText('{txt}',[Text.Encoding]::UTF8);"
        "$s.Speak($t);$s.Dispose();"
    )
    proc = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not wav.exists():
        raise EngineError("SAPI 合成失败",
                          detail=((proc.stderr or proc.stdout or "").strip())[:400])
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    codec = "-c:a libmp3lame -b:a 192k" if out_path.endswith(".mp3") else "-c:a pcm_s16le"
    run_ffmpeg(["-i", str(wav), *codec.split(), "-ar", "44100", "-ac", "2", out_path],
               label="转码语音")


def _edge_tts_synthesize(*, text: str, out_path: str, voice: str, rate: int) -> None:
    """edge-tts（微软神经网络语音，需联网）。直接产出 mp3。"""
    import edge_tts

    v = voice if (voice and "-" in voice) else settings.tts_voice_edge
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    async def _go() -> None:
        await edge_tts.Communicate(text, v, rate=_wpm_to_edge_rate(rate)).save(out_path)

    _run_coro(_go())


def _cosyvoice_synthesize(*, text: str, out_path: str, voice: str, rate: int,
                          entry: list[str]) -> None:
    """调用独立的 CosyVoice（阿里开源）推理进程。文本走文件传递，避免中文编码问题。"""
    workdir = Path(tempfile.mkdtemp(prefix="cosy_"))
    txt = workdir / "text.txt"
    txt.write_text(text, encoding="utf-8")
    cmd = [*entry, "--text-file", str(txt), "--out", str(out_path)]
    if voice:
        cmd += ["--voice", voice]
    if rate:
        cmd += ["--rate", str(rate)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise EngineError("CosyVoice 合成失败",
                          detail=((proc.stderr or proc.stdout or "").strip())[-800:])
    if not Path(out_path).exists() or Path(out_path).stat().st_size == 0:
        raise EngineError("CosyVoice 未产出音频",
                          detail=(proc.stdout or "").strip()[-400:])


def say_tts(*, text: str, out_path: str, voice: str = "", rate: int = 0) -> str:
    """合成旁白/台词到 out_path（mp3 或 wav）。

    多级降级（settings.tts_engine 控制）：
      1. CosyVoice —— 阿里开源，本机 GPU 推理，中文效果最好
      2. edge-tts  —— 微软神经网络语音，需联网
      3. 系统内置  —— macOS say / Windows SAPI
    auto 模式下逐级尝试，任一级成功即返回；全部失败才抛错。
    """
    if not text.strip():
        raise EngineError("TTS 文本为空", retryable=False)

    rate = int(rate or settings.tts_rate or 180)
    pref = (settings.tts_engine or "auto").lower()
    order = ["cosyvoice", "edge", "system"] if pref == "auto" else [pref]
    errors: list[str] = []

    for tier in order:
        try:
            if tier == "cosyvoice":
                entry = _cosyvoice_entry()
                if not entry:
                    errors.append("cosyvoice: 未安装（缺 tools/cosyvoice/infer.py）")
                    continue
                _cosyvoice_synthesize(text=text, out_path=out_path, voice=voice,
                                      rate=rate, entry=entry)
            elif tier == "edge":
                if not _edge_tts_available():
                    errors.append("edge: edge-tts 未安装")
                    continue
                _edge_tts_synthesize(text=text, out_path=out_path, voice=voice, rate=rate)
            elif tier == "say":
                _macos_say(text=text, out_path=out_path, voice=voice, rate=rate)
            elif tier == "sapi":
                _sapi_tts(text=text, out_path=out_path, voice=voice, rate=rate)
            elif tier == "system":
                if IS_MACOS and Path(SAY).exists():
                    _macos_say(text=text, out_path=out_path, voice=voice, rate=rate)
                elif IS_WINDOWS and POWERSHELL:
                    _sapi_tts(text=text, out_path=out_path, voice=voice, rate=rate)
                else:
                    errors.append("system: 无可用系统 TTS")
                    continue
            else:
                raise EngineError(f"未知 TTS 引擎：{tier}", retryable=False)

            if Path(out_path).exists() and Path(out_path).stat().st_size > 0:
                return out_path
            errors.append(f"{tier}: 产出为空文件")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{tier}: {exc}")

    raise EngineError("所有 TTS 通道均失败", retryable=False, detail="; ".join(errors))


# --------------------------------------------------------------------------- #
# Qwen3-TTS（本地情感配音：音色 + 自然语言情感指令）
# --------------------------------------------------------------------------- #

#: Qwen3-TTS CustomVoice 的可用音色，取自模型 config.json 的 spk_id。
#: label / desc 供 Web UI 直接渲染，避免前端硬编码。
QWEN3_SPEAKERS: tuple[dict[str, str], ...] = (
    {"name": "sohee", "label": "sohee · 明亮女声", "desc": "音色最亮，适合活泼、日常 vlog 风"},
    {"name": "vivian", "label": "vivian · 温暖女声", "desc": "中低女声，叙事感强"},
    {"name": "serena", "label": "serena · 温柔女声", "desc": "气声偏重，适合安静独白"},
    {"name": "ono_anna", "label": "ono_anna · 高音女声", "desc": "音调偏高，偏动漫感"},
    {"name": "uncle_fu", "label": "uncle_fu · 中年男声", "desc": "适合大叔语气与沉稳旁白"},
    {"name": "ryan", "label": "ryan · 沉稳男声", "desc": "低音男声"},
    {"name": "aiden", "label": "aiden · 年轻男声", "desc": "轻快男声"},
    {"name": "dylan", "label": "dylan · 北京话男声", "desc": "自带北京方言"},
    {"name": "eric", "label": "eric · 四川话男声", "desc": "自带四川方言"},
)

#: 子进程环境：剥掉宿主注入的 PYTHONPATH。
#: 这台机器上 PYTHONPATH 指向的 shim 会劫持 sitecustomize，导致独立 venv 里的
#: 解释器行为异常（pip 被劫持、包解析串味），跑外部工具前必须清掉。
_SUBPROCESS_ENV = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def _qwen3tts_paths() -> tuple[Path, Path]:
    """定位 Qwen3-TTS 的独立解释器与推理脚本。"""
    root = settings.repo_root
    if settings.qwen3tts_python:
        py = Path(settings.qwen3tts_python)
    else:
        py = root / "tools" / "qwen3-tts" / "venv" / (
            "Scripts/python.exe" if IS_WINDOWS else "bin/python"
        )
    infer = Path(settings.qwen3tts_infer) if settings.qwen3tts_infer else (
        root / "tools" / "qwen3-tts" / "infer.py"
    )
    return py, infer


def qwen3tts_available() -> bool:
    """Qwen3-TTS 是否可用（开关打开 + 解释器与脚本都在）。"""
    if not settings.qwen3tts_enabled:
        return False
    py, infer = _qwen3tts_paths()
    return py.exists() and infer.exists()


def qwen3_tts_synthesize(*, text: str, out_path: str, speaker: str = "",
                         instruct: str = "", device: str = "auto") -> str:
    """合成一条情感配音，输出 mp3。

    torch 依赖与后端隔离，统一以子进程调用 ``tools/qwen3-tts/infer.py``；
    单条也走 jobs 批量接口，与 ``scripts/rebuild_voices.py`` 共用同一条代码路径。
    """
    if not text.strip():
        raise EngineError("TTS 文本为空", retryable=False)
    py, infer = _qwen3tts_paths()
    if not py.exists():
        raise EngineError(f"Qwen3-TTS 解释器不存在：{py}", retryable=False)
    if not infer.exists():
        raise EngineError(f"Qwen3-TTS 推理脚本不存在：{infer}", retryable=False)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qwen3tts_") as td:
        wav = Path(td) / "out.wav"
        jobs_path = Path(td) / "jobs.json"
        jobs_path.write_text(json.dumps([{
            "text": text,
            "out": str(wav),
            "speaker": speaker or settings.qwen3tts_speaker,
            "instruct": instruct or "",
        }], ensure_ascii=False), encoding="utf-8")
        try:
            proc = subprocess.run(
                [str(py), str(infer), "--jobs", str(jobs_path),
                 "--device", device or settings.qwen3tts_device],
                capture_output=True, text=True, env=_SUBPROCESS_ENV,
                timeout=settings.qwen3tts_timeout, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError(
                f"Qwen3-TTS 合成超时（>{settings.qwen3tts_timeout}s）", detail=str(exc)
            ) from exc
        if proc.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
            tail = ((proc.stderr or "") + (proc.stdout or ""))[-800:]
            raise EngineError("Qwen3-TTS 合成失败", detail=tail)
        run_ffmpeg(
            ["-y", "-v", "error", "-i", str(wav), "-c:a", "libmp3lame",
             "-b:a", "192k", "-ar", "44100", "-ac", "2", str(out)],
            label="配音转码",
        )
    return str(out)


def _bgm_paths() -> tuple[Path, Path]:
    """定位 BGM 合成脚本与它需要的解释器（默认复用 Qwen3-TTS 的 venv）。"""
    root = settings.repo_root
    script = Path(settings.bgm_script) if settings.bgm_script else root / "scripts" / "gen_bgm.py"
    if settings.bgm_python:
        py = Path(settings.bgm_python)
    else:
        py = _qwen3tts_paths()[0]
    return script, py


def bgm_synth_available() -> bool:
    script, py = _bgm_paths()
    return script.exists() and py.exists()


def synth_bgm(*, out_path: str, duration: float = 30.0, style: str = "pop",
              peak_db: float = -9.0) -> str:
    """用 ``scripts/gen_bgm.py`` 合成配乐（和弦分解 + 鼓组/贝斯/拨弦，非正弦垫音）。

    峰值默认 -9dBFS：合成阶段 BGM 还会被乘 0.16（约 -16dB），
    这样混出来大约比人声低 10dB，是标准的背景乐位置。
    """
    script, py = _bgm_paths()
    if not script.exists():
        raise EngineError(f"BGM 合成脚本不存在：{script}", retryable=False)
    if not py.exists():
        raise EngineError(f"BGM 合成解释器不存在：{py}", retryable=False)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [str(py), str(script), "--out", str(out), "--duration", f"{duration:.2f}",
             "--style", style or "pop", "--peak-db", str(peak_db)],
            capture_output=True, text=True, env=_SUBPROCESS_ENV,
            timeout=900, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineError("BGM 合成超时", detail=str(exc)) from exc
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        tail = ((proc.stderr or "") + (proc.stdout or ""))[-800:]
        raise EngineError("BGM 合成失败", detail=tail)
    return str(out)



_MOOD_SCALES = {
    "calm": (261.63, 329.63, 392.00, 440.00),
    "warm": (220.00, 261.63, 329.63, 392.00),
    "happy": (293.66, 369.99, 440.00, 587.33),
    "tense": (220.00, 233.08, 329.63, 349.23),
    "epic": (196.00, 246.94, 293.66, 392.00),
    "tech": (261.63, 311.13, 392.00, 466.16),
}


def synth_music(*, out_path: str, duration: float = 30.0, mood: str = "calm",
                prompt: str = "") -> str:
    """用 lavfi 合成环境配乐（和弦垫 + 缓慢起伏 + 轻微回声）。"""
    notes = _MOOD_SCALES.get((mood or "").lower(), _MOOD_SCALES["calm"])
    if prompt:
        lowered = prompt.lower()
        for key in _MOOD_SCALES:
            if key in lowered:
                notes = _MOOD_SCALES[key]
                break
    parts = [f"0.20*sin(2*PI*{n}*t)" for n in notes[:3]]
    parts.append("0.10*sin(2*PI*110*t)")
    # 音量 LFO 让垫音有呼吸感
    expr = "(" + "+".join(parts) + ")*(0.65+0.35*sin(2*PI*0.07*t))"
    args = [
        "-f", "lavfi",
        "-i", f"aevalsrc=exprs='{expr}':s=44100:d={duration:.2f}",
        "-af", "aecho=0.8:0.85:120:0.25,lowpass=f=3200,volume=0.9",
        "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", out_path,
    ]
    run_ffmpeg(args, total_duration=duration, label="配乐")
    return out_path


def synth_sfx(*, out_path: str, duration: float = 1.5, prompt: str = "") -> str:
    """用 lavfi 合成音效。"""
    lowered = (prompt or "").lower()
    if any(k in lowered for k in ("whoosh", "转场", "swoosh", "扫过")):
        af = "afade=in:st=0:d=0.3,afade=out:st=0.9:d=0.4,highpass=f=350,lowpass=f=8000,volume=0.7"
        src = f"anoisesrc=d={duration:.2f}:c=pink:a=0.6"
    elif any(k in lowered for k in ("ding", "提示", "完成", "success")):
        af = "afade=out:st=0.9:d=0.4,volume=0.8"
        src = f"aevalsrc=exprs='0.35*sin(2*PI*880*t)+0.2*sin(2*PI*1320*t)':s=44100:d={duration:.2f}"
    elif any(k in lowered for k in ("click", "点击", "key")):
        af = "afade=out:st=0.08:d=0.12,volume=0.9"
        src = f"aevalsrc=exprs='0.4*sin(2*PI*1600*t)':s=44100:d={duration:.2f}"
    else:
        af = "afade=in:st=0:d=0.05,afade=out:st=1.2:d=0.3,volume=0.7"
        src = f"anoisesrc=d={duration:.2f}:c=white:a=0.5"
    args = [
        "-f", "lavfi", "-i", src, "-af", af,
        "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", out_path,
    ]
    run_ffmpeg(args, total_duration=duration, label="音效")
    return out_path


# --------------------------------------------------------------------------- #
# 字幕
# --------------------------------------------------------------------------- #
def _srt_timestamp(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms, s = 0, s + 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(*, out_path: str, segments: list[dict[str, Any]]) -> str:
    """segments: [{"index":1,"start":0.0,"end":3.2,"text":"..."}]"""
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        idx = int(seg.get("index") or i)
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{_srt_timestamp(float(seg.get('start') or 0))} --> {_srt_timestamp(float(seg.get('end') or 0))}")
        lines.append(text)
        lines.append("")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_ass(*, out_path: str, segments: list[dict[str, Any]],
              width: int = 1280, height: int = 720, font_size: int = 24) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,PingFang SC,{font_size},&H00FFFFFF,&H000000FF,&H96000000,&H96000000,0,0,0,0,100,100,0,0,3,1,0,2,40,40,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    for seg in segments:
        text = str(seg.get("text") or "").strip().replace("\n", "\\N")
        if not text:
            continue
        start = _srt_timestamp(float(seg.get("start") or 0)).replace(",", ".")
        end = _srt_timestamp(float(seg.get("end") or 0)).replace(",", ".")
        rows.append(f"Dialogue: 0,{start[:-1]},{end[:-1]},Default,,0,0,0,,{text}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
    return out_path
