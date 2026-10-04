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
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
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
    """跑子进程。失败时把完整命令和 stderr 一起抛出来 —— 不要吞。"""
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


# ---------------------------------------------------------------- 探测

def probe(ffprobe: str, path: str) -> dict:
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    p = run(cmd, timeout=120, label="ffprobe")
    d = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    vs = [s for s in d.get("streams", []) if s.get("codec_type") == "video"]
    as_ = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
    if not vs:
        sys.exit(f"✗ 输入没有视频流: {path}")
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


# ---------------------------------------------------------------- 主流程

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


def main() -> int:
    args = build_parser().parse_args()
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe(ffmpeg)
    rgan = find_realesrgan()

    src = Path(args.input).resolve()
    if not src.is_file():
        sys.exit(f"✗ 输入不存在: {src}")

    if args.scale not in MODEL_SCALES[args.model]:
        sys.exit(
            f"✗ 模型 {args.model} 不支持 x{args.scale}（只支持 "
            f"{sorted(MODEL_SCALES[args.model])}）。\n"
            "  想要 x2/x3 就用 realesr-animevideov3；x4plus 只能 x4。"
        )
    mdir = models_dir(rgan)
    need = list(mdir.glob(f"{args.model}-x{args.scale}.*")) if args.model == "realesr-animevideov3" \
        else list(mdir.glob(f"{args.model}.*"))
    if not need:
        sys.exit(f"✗ 模型文件缺失：{mdir} 下找不到 {args.model} x{args.scale} 的 .bin/.param。\n"
                 f"  跑 setup_realesrgan.py 补装，或检查 --model 名。")

    info = probe(ffprobe, str(src))
    if args.compare is not None:
        # 对比模式不需要总帧数，先走掉，省一次逐帧解码
        return do_compare(args, ffmpeg, rgan, src, info)
    if info["frames_contradiction"]:
        # 容器标签与「时长×帧率」打架 → 真解码数一遍定案（只在矛盾时才付这个代价）
        print(f"⚠ 帧数元数据自相矛盾：nb_frames={info['nb_frames']}，"
              f"时长×帧率={info['frames_est']} → 逐帧解码核实（慢，但只有这一次）")
        real = frame_count(ffprobe, src, deep=True)
        if real > 0:
            info["frames"], info["frames_src"] = real, "逐帧解码"
            print(f"  实测 {real} 帧（容器标签不可信，典型成因：-c copy 切片段）")
        else:
            print("  解不出帧数，按「时长×帧率」继续")
    out_w, out_h = info["width"] * args.scale, info["height"] * args.scale
    if args.target_width or args.target_height:
        if not (args.target_width and args.target_height):
            sys.exit("✗ --target-width / --target-height 必须同时给。")
        final_w, final_h = args.target_width, args.target_height
    else:
        final_w, final_h = out_w, out_h

    fps = info["fps"]
    total = info["frames"]
    chunk = max(1, args.chunk)
    n_chunks = (total + chunk - 1) // chunk
    preset = args.preset or ("faster" if final_w > 3000 else "medium")

    # 单帧体积经验值（1280x720 量级输入，PNG）：x2≈2.8MB、x4≈11.3MB。
    # 用途只是给个量级感，别当承诺。
    per_frame_mb = {2: 2.8, 3: 5.2, 4: 11.3}.get(args.scale, 4.0) * (info["width"] * info["height"] / (1280 * 720))
    peak_tmp = 2 * min(chunk, total) * per_frame_mb * 1e6

    print("=" * 68)
    print("视频超分 · 计划")
    print("=" * 68)
    print(f"  输入     {src.name}")
    print(f"           {info['width']}x{info['height']} @ {fps:g}fps  "
          f"{hhmmss(info['duration'])}  {total} 帧（{info['frames_src']}）  "
          f"{hbytes(info['size_bytes'])}  "
          f"{'含音轨(' + str(info['audio_codec']) + ')' if info['has_audio'] else '无音轨'}")
    print(f"  超分     {args.model}  x{args.scale}  →  {out_w}x{out_h}")
    if (final_w, final_h) != (out_w, out_h):
        print(f"  输出     {final_w}x{final_h}（超采样后缩放）")
    print(f"  编码     libx264 crf={args.crf} preset={preset}")
    print(f"  分块     {n_chunks} 块 × {chunk} 帧   峰值临时盘 ≈ {hbytes(peak_tmp)}")
    print(f"  模型     {mdir}")
    if info["vfr"]:
        print(f"  ⚠ 变帧率（r={info['r_fps']:g} avg={info['avg_fps']:g}）：按平均帧率处理，"
              f"可能累积漂移。建议先转成恒定帧率再超分。")
    print("=" * 68)

    if args.dry_run:
        print("--dry-run：未执行。")
        return 0

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(args.workdir).resolve() if args.workdir else out.parent / f".upscale_{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    frames_in = work / "_frames"
    frames_up = work / "_frames_up"
    segs = work / "_segs"
    segs.mkdir(parents=True, exist_ok=True)

    print(f"临时目录 {work}\n")
    t_start = time.time()
    done_frames = 0
    n_proc = 0        # 本次真正处理过的帧数（复用分块不计）
    proc_time = 0.0   # 真正花在抽帧/超分/编码上的时间
    rate = None       # 秒/帧，按已完成的帧实测标定，不靠猜

    for idx in range(n_chunks):
        start = idx * chunk
        n = min(chunk, total - start)
        seg = segs / f"seg_{idx:05d}.mkv"

        if seg.is_file() and seg.stat().st_size > 0:
            # 续跑：只认「帧数对得上」的分块。写到一半被杀的文件会被重做。
            have = frame_count(ffprobe, seg, deep=True)
            if have == n:
                done_frames += n
                print(f"[{idx + 1}/{n_chunks}] 复用分块 {seg.name}（{n} 帧，已校验）")
                continue
            print(f"[{idx + 1}/{n_chunks}] 分块 {seg.name} 不完整（{have}/{n} 帧）→ 重做")
            seg.unlink()

        for d in (frames_in, frames_up):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)

        t0 = time.time()
        ss = start / fps
        # -ss 放在 -i 前：快速定位；转码时 ffmpeg 默认开启 accurate_seek，会解码到精确时间点
        run([ffmpeg, "-v", "error", "-y", "-ss", f"{ss:.6f}", "-i", str(src),
             "-an", "-sn", "-frames:v", str(n),
             "-fps_mode", "passthrough", "-start_number", "1",
             str(frames_in / "f_%05d.png")],
            timeout=3600, label=f"抽帧 {idx + 1}/{n_chunks}")
        got = len(list(frames_in.glob("*.png")))
        if got != n:
            if idx == n_chunks - 1 and 0 < got < n:
                print(f"  · 末块实际 {got} 帧（预期 {n}），按实际处理")
                n = got
            else:
                raise RuntimeError(
                    f"抽帧数量不符：预期 {n}，实际 {got}（分块 {idx + 1}）。\n"
                    f"  源: {src}\n  起始时间: {ss:.6f}s\n"
                    f"  通常是变帧率或 -ss 落点问题。临时帧保留在 {frames_in}。"
                )
        t_extract = time.time() - t0

        t0 = time.time()
        # -m 显式给：默认值是相对路径 models（这个构建版按 exe 位置解析，
        # 但显式传才能让 REALESRGAN_MODELS 覆盖生效）
        cmd = [rgan, "-i", str(frames_in), "-o", str(frames_up), "-m", str(mdir),
               "-n", args.model, "-s", str(args.scale), "-t", str(args.tile)]
        if args.gpu:
            cmd += ["-g", args.gpu]
        if args.threads:
            cmd += ["-j", args.threads]
        run(cmd, timeout=3600 * 6, label=f"超分 {idx + 1}/{n_chunks}")
        got_up = len(list(frames_up.glob("*.png")))
        if got_up != n:
            raise RuntimeError(f"超分输出数量不符：预期 {n}，实际 {got_up}。保留现场 {frames_up}")
        t_up = time.time() - t0

        t0 = time.time()
        enc = [ffmpeg, "-v", "error", "-y",
               "-framerate", f"{fps:.6f}", "-start_number", "1",
               "-i", str(frames_up / "f_%05d.png"),
               "-frames:v", str(n),
               "-c:v", "libx264", "-crf", str(args.crf), "-preset", preset,
               "-pix_fmt", "yuv420p"]
        if (final_w, final_h) != (out_w, out_h):
            enc += ["-vf", f"scale={final_w}:{final_h}:flags=lanczos"]
        enc += ["-an", "-f", "matroska", str(seg)]
        run(enc, timeout=3600 * 6, label=f"编码 {idx + 1}/{n_chunks}")
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
        print(f"[{idx + 1}/{n_chunks}] {n} 帧  "
              f"抽帧 {t_extract:.1f}s · 超分 {t_up:.1f}s · 编码 {t_enc:.1f}s  "
              f"| {done_frames}/{total} ({done_frames / total * 100:.1f}%)  "
              f"已用 {hhmmss(elapsed)}  剩 ~{hhmmss(eta)}")

        if not args.keep_temp:
            for d in (frames_in, frames_up):
                shutil.rmtree(d, ignore_errors=True)

    if done_frames != total:
        print(f"\n⚠ 累计帧数 {done_frames} ≠ 源 {total}，成片可能缺帧。")
        print(f"  若源是变帧率，先转成恒定帧率再超分。")

    # 拼接
    print("\n拼接分块 …")
    lst = work / "_concat.txt"
    with open(lst, "w", encoding="utf-8") as fh:
        for idx in range(n_chunks):
            fh.write(f"file '{(segs / f'seg_{idx:05d}.mkv').as_posix()}'\n")
    concat_v = work / "_concat.mkv"
    run([ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(concat_v)], timeout=3600, label="拼接")

    # 合成成片：视频来自拼接结果，音轨从原片直接 copy（不经重编码）
    vid_dur = done_frames / fps
    print("写最终文件（音轨从原片 copy）…")
    if info["has_audio"]:
        # 用 -t 卡片长：不要用 -shortest（旁白轨更短时会把画面砍掉且不报错）
        run([ffmpeg, "-v", "error", "-y", "-i", str(concat_v), "-i", str(src),
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
             "-t", f"{vid_dur:.6f}", "-movflags", "+faststart", str(out)],
            timeout=3600, label="合成成片")
    else:
        run([ffmpeg, "-v", "error", "-y", "-i", str(concat_v),
             "-c", "copy", "-movflags", "+faststart", str(out)],
            timeout=3600, label="输出")

    # 验收：把产出重新探一遍，而不是相信"没报错"
    o = probe(ffprobe, str(out))
    out_frames = frame_count(ffprobe, out, deep=args.verify_frames)
    ok_dims = (o["width"], o["height"]) == (final_w, final_h)
    ok_audio = info["has_audio"] == o["has_audio"]
    ok_fps = abs(o["fps"] - fps) < 0.05
    ok_frames = (out_frames == total) if out_frames > 0 else None
    drift = abs(o["duration"] - info["duration"])
    print("\n" + "=" * 68)
    print("完成")
    print("=" * 68)
    print(f"  输出     {out}")
    print(f"           {o['width']}x{o['height']} @ {o['fps']:g}fps  "
          f"{hhmmss(o['duration'])}  {hbytes(o['size_bytes'])}")
    elapsed_total = time.time() - t_start
    if rate:
        print(f"  耗时     {hhmmss(elapsed_total)}（{rate:.3f} s/帧 实测）")
    else:
        # 全部分块都被复用时 rate 从未标定过 —— 别拿「只剩拼接的时间」冒充超分耗时
        print(f"  耗时     {hhmmss(elapsed_total)}（全部分块复用，本次未重新超分）")
    print(f"  检查     尺寸 {'✓' if ok_dims else '✗'}   "
          f"帧率 {'✓' if ok_fps else '✗'}   "
          f"音轨 {'✓' if ok_audio else '✗（源有音轨但输出没有）'}   "
          f"时长差 {drift:+.2f}s")
    if ok_frames is None:
        print(f"  帧数     源 {total} / 成片 {out_frames}（读不出，未判定）")
    else:
        print(f"  帧数     源 {total} / 成片 {out_frames} "
              f"{'✓' + ('（逐帧解码）' if args.verify_frames else '') if ok_frames else '✗ 缺帧'}")
    rmtree_report(work, keep=args.keep_temp)
    if args.keep_temp:
        print(f"  临时保留 {work}")

    problems = []
    if not ok_dims:
        problems.append("输出尺寸不符")
    if not ok_fps:
        problems.append("帧率不符")
    if not ok_audio:
        problems.append("音轨丢失")
    if drift > 0.5:
        problems.append(f"时长漂移 {drift:.2f}s")
    if ok_frames is False:
        problems.append(f"帧数不符（源 {total} / 成片 {out_frames}）")
    if problems:
        print("\n✗ " + "；".join(problems))
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
