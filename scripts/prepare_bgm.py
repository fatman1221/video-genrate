# -*- coding: utf-8 -*-
"""把外部音乐（例：北北昼《囚笼》）加工成项目可用的 BGM，并登记为 MUSIC 资产。

为什么需要这一步，而不是直接把 mp3 丢给管线：
    混音那一路是 `-stream_loop -1` + `atrim=0:<成片时长>` + 恒定 `volume`，
    中间**没有交叉淡化、也没有旁白闪避**。一首有结构的器乐作品这样硬循环，
    每圈接缝都会"咔"一声并从小提琴引子重来；恒定音量又会让安静的镜头被
    高能量主段压死。所以要在文件层面先把这些事做掉。

用法：
    # 0) 先看结构：时长 / 峰值 / 每个 5s 的能量条，用来挑循环点
    python scripts/prepare_bgm.py inspect --file "囚笼.mp3"

    # 1) 循环铺满 300s：第一遍完整播，之后从 24s 进主段循环，接缝交叉淡化
    python scripts/prepare_bgm.py build --file "囚笼.mp3" \\
        --project proj_4f2f0c178a37 --target 300 --loop-from 24 --xfade 3 \\
        --fade-out 8 --peak-db -6 --register

    # 2) 按情绪曲线手工编曲（引子给 S01、主段给 S03~S05、尾奏给 S08）
    python scripts/prepare_bgm.py build --file "囚笼.mp3" --project proj_xxx \\
        --arrange examples/bgm_arrange.json --register

    # 3) 旁白闪避：自动从配音音轨检测说话区间，说话时 BGM 降 8dB
    python scripts/prepare_bgm.py build --file "囚笼.mp3" --project proj_xxx \\
        --duck --duck-db 8 --register

编曲 JSON（--arrange）格式：
    {
      "target": 300,
      "xfade": 0.8,
      "fade_out": 8,
      "clips": [
        {"src": [0, 24],   "at": 0,   "fade_in": 3},
        {"src": [24, 120], "at": 24},
        {"src": [120, 168],"at": 300, "comment": "尾奏交给 S08"}
      ]
    }
    src 是**源文件**里的秒区间，at 是它在成片时间轴上的起点；允许留空（静音）。

注意：外部商业音乐仅限内部预览。公开发布（B 站 / 朋友圈都算）需要权利人授权，
      本脚本只在本地做加工与登记，不改变版权归属。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SR = 44100


# --------------------------------------------------------------------------- #
# ffmpeg / ffprobe
# --------------------------------------------------------------------------- #
def _bin(name: str, env_key: str) -> str:
    v = os.environ.get(env_key)
    if v and Path(v).exists():
        return v
    p = Path.home() / ".workbuddy" / "binaries" / "ffmpeg" / "bin" / f"{name}.exe"
    if p.exists():
        return str(p)
    return name  # 交给 PATH


def ffmpeg() -> str:
    return _bin("ffmpeg", "FFMPEG_BIN")


def ffprobe() -> str:
    return _bin("ffprobe", "FFPROBE_BIN")


def probe(path: Path) -> dict:
    out = subprocess.run(
        [ffprobe(), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    fmt = info.get("format") or {}
    astream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {
        "duration": float(fmt.get("duration") or astream.get("duration") or 0.0),
        "sample_rate": int(astream.get("sample_rate") or 0),
        "channels": int(astream.get("channels") or 0),
        "codec": astream.get("codec_name") or "",
        "bit_rate": int(fmt.get("bit_rate") or 0),
    }


# --------------------------------------------------------------------------- #
# 解码 / 编码
# --------------------------------------------------------------------------- #
def decode(path: Path, sr: int = SR, af: str | None = None) -> np.ndarray:
    """解码成 (n, 2) float32。af 可传 ffmpeg 的 -af 滤镜链（用于渲染变体）。"""
    cmd = [ffmpeg(), "-v", "error", "-i", str(path)]
    if af:
        cmd += ["-af", af]
    cmd += ["-f", "f32le", "-ac", "2", "-ar", str(sr), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype="<f4")
    if a.size % 2:
        a = a[:-1]
    return a.reshape(-1, 2).astype(np.float32)


def render_source_map(src_path: Path, variants: dict[str, str] | None) -> dict[str, np.ndarray]:
    """把源文件解码成若干版本：'' 是原始，其余键名对应 ffmpeg 滤镜链。

    例：{"dark": "lowpass=f=380"} → 同一首歌的低通版，用来做"退远/只剩嗡鸣"。
    用 ffmpeg 出变体而不是自己写滤波器，是因为逐样本 IIR 在 Python 里太慢，
    而 ffmpeg 的滤波质量与相位特性都更可控。
    """
    out = {"": decode(src_path)}
    for name, chain in (variants or {}).items():
        out[name] = decode(src_path, af=chain)
        print(f"[i] 变体 {name!r}: -af {chain}")
    return out


def encode(buf: np.ndarray, out: Path, bitrate: str = "192k") -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [ffmpeg(), "-y", "-v", "error", "-f", "f32le", "-ac", "2", "-ar", str(SR),
         "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", bitrate, str(out)],
        stdin=subprocess.PIPE)
    p.communicate(np.ascontiguousarray(buf, dtype="<f4").tobytes())
    if p.returncode:
        raise SystemExit(f"[!] 编码失败: {out}")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def eq_ramp(n: int) -> tuple[np.ndarray, np.ndarray]:
    """等功率交叉淡化曲线：(淡入, 淡出)。"""
    if n <= 0:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)
    return np.sin(t * np.pi / 2), np.cos(t * np.pi / 2)


def micro_fade(seg: np.ndarray, n: int) -> None:
    """给片段的头尾各加一点点淡化，避免硬切爆音（原地修改）。"""
    n = min(int(n), len(seg) // 2)
    if n <= 0:
        return
    fin, _ = eq_ramp(n)
    _, fout = eq_ramp(n)
    seg[:n] *= fin[:, None]
    seg[-n:] *= fout[:, None]


def amp_db(buf: np.ndarray) -> tuple[float, float]:
    """返回 (峰值dBFS, RMS dBFS)。"""
    if not len(buf):
        return -120.0, -120.0
    peak = float(np.max(np.abs(buf)))
    rms = float(np.sqrt(np.mean(buf.astype(np.float64) ** 2)))
    return (20 * np.log10(peak) if peak > 0 else -120.0,
            20 * np.log10(rms) if rms > 0 else -120.0)


def snap(sec: float, bpm: float) -> float:
    """把秒对齐到最近的拍（用于循环点，避免切在半拍上）。"""
    if bpm <= 0:
        return sec
    beat = 60.0 / bpm
    return round(sec / beat) * beat


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def loop_fill(src: np.ndarray, target: int, *, loop_from: float = 0.0,
              xfade: float = 2.0, bpm: float = 0.0) -> tuple[np.ndarray, list[float]]:
    """第一遍完整播 src，之后循环 src[loop_from:]，接缝等功率交叉淡化。

    返回 (成品, 接缝时间点列表)。
    """
    if bpm > 0:
        loop_from = snap(loop_from, bpm)
    x = int(max(xfade, 0.0) * SR)
    head = src[int(loop_from * SR):]
    if len(head) <= x:
        head = src.copy()          # 循环体太短，退回整首
        x = min(x, len(head) // 4)

    out = src.copy()
    seams: list[float] = []
    guard = 0
    while len(out) - x < target:
        seams.append(len(out) / SR)
        if x > 0 and len(out) >= x:
            fin, fout = eq_ramp(x)
            mixed = out[-x:] * fout[:, None] + head[:x] * fin[:, None]
            out = np.concatenate([out[:-x], mixed, head[x:]])
        else:
            out = np.concatenate([out, head])
        guard += 1
        if guard > 10000:
            raise SystemExit("[!] 循环次数异常，检查 --loop-from / 源时长")
    return out[:target], seams


def build_arrange(spec: dict, sources: dict[str, np.ndarray], target: int) -> tuple[np.ndarray, list[str]]:
    """按编曲 JSON 把源文件的若干段落摆到时间轴上。

    clip 可选字段：variant（用哪个滤波变体）、gain_db、fade_in、fade_out。
    留白即静音 —— 想给某个场留呼吸，就不放 clip。
    """
    out = np.zeros((target + SR, 2), dtype=np.float32)
    log: list[str] = []
    default_fade = float(spec.get("xfade", 0.06))
    for clip in spec.get("clips", []):
        s0, s1 = clip["src"]
        at = float(clip.get("at", 0.0))
        vname = str(clip.get("variant", ""))
        src = sources.get(vname)
        if src is None:
            log.append(f"[!] 未定义的变体 {vname!r}，跳过")
            continue
        seg = src[int(s0 * SR):int(s1 * SR)].copy()
        if not len(seg):
            log.append(f"[!] src [{s0}, {s1}] 超出源长度，跳过")
            continue
        gain_db = float(clip.get("gain_db", 0.0))
        if gain_db:
            seg *= 10 ** (gain_db / 20)
        fin = float(clip.get("fade_in", default_fade))
        fout = float(clip.get("fade_out", default_fade))
        micro_fade(seg, int(max(fin, default_fade) * SR))
        if int(fout * SR) > int(default_fade * SR):
            n = min(int(fout * SR), len(seg) // 2)
            _, r = eq_ramp(n)
            seg[-n:] *= r[:, None]
        i0 = int(at * SR)
        j = min(i0 + len(seg), len(out))
        if i0 >= len(out):
            log.append(f"[!] at={at}s 超出目标时长，跳过")
            continue
        out[i0:j] += seg[: j - i0]
        tag = f" [{vname}]" if vname else ""
        log.append(f"  src[{s0:>6.1f}, {s1:>6.1f}]{tag:<8} -> {at:>6.1f}s  "
                   f"({len(seg) / SR:5.1f}s, {gain_db:+.1f}dB)")
    return out[:target], log


def apply_fades(buf: np.ndarray, fade_in: float, fade_out: float) -> np.ndarray:
    if fade_in > 0:
        n = min(int(fade_in * SR), len(buf))
        fin, _ = eq_ramp(n)
        buf[:n] *= fin[:, None]
    if fade_out > 0:
        n = min(int(fade_out * SR), len(buf))
        _, fout = eq_ramp(n)
        buf[-n:] *= fout[:, None]
    return buf


# --------------------------------------------------------------------------- #
# 旁白闪避
# --------------------------------------------------------------------------- #
def speech_gain_curve(voice: np.ndarray, *, length: int, depth_db: float,
                      thr_db: float = 7.0, hop: float = 0.02,
                      attack: float = 0.12, release: float = 0.45) -> np.ndarray:
    """从配音音轨的短时能量里找出说话区间，生成等长的增益曲线。

    说话时压低 depth_db，其余保持 1.0；起落各做一阶平滑（attack 快、release 慢），
    免得"啪"地一声抽掉音乐。
    """
    mono = voice.mean(axis=1).astype(np.float64)
    n = max(int(hop * SR), 1)
    m = len(mono) // n
    total_frames = max(int(np.ceil(length / n)), 1)
    if m < 4:
        return np.ones(length, dtype=np.float32)
    # 配音音轨比 BGM 短时，尾部按静音处理 —— 否则最后一个说话状态会被
    # 一路插值到末尾，BGM 就永远压在低音量上了。
    frames = np.zeros(total_frames, dtype=np.float64)
    keep = min(m, total_frames)
    rms = np.sqrt((mono[: keep * n].reshape(keep, n) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    floor = float(np.percentile(db, 20))
    frames[:keep] = (db > floor + thr_db).astype(np.float64)
    mask = frames

    # 帧率上做一阶平滑（attack / release 不同系数）
    a_at = 1 - np.exp(-hop / max(attack, 1e-3))
    a_rel = 1 - np.exp(-hop / max(release, 1e-3))
    smooth = np.empty_like(mask)
    cur = mask[0]
    for i, v in enumerate(mask):
        a = a_at if v > cur else a_rel
        cur += a * (v - cur)
        smooth[i] = cur

    gain_floor = 10 ** (-depth_db / 20)
    frame_gain = 1.0 - smooth * (1.0 - gain_floor)
    # 帧 -> 采样：线性插值 + 首尾补齐
    idx = (np.arange(length, dtype=np.float64) / n)
    idx = np.clip(idx, 0, len(frame_gain) - 1)
    g = np.interp(idx, np.arange(len(frame_gain)), frame_gain)
    return g.astype(np.float32)


# --------------------------------------------------------------------------- #
# 登记为 MUSIC 资产
# --------------------------------------------------------------------------- #
def register(out: Path, project_id: str, meta: dict) -> None:
    # 必须先把路径转成**绝对**路径：下面要 chdir 到 backend/ 才能 import app.*，
    # 之后相对路径（如 backend/storage/music/...）会被解析到 backend/backend/... 而找不到文件。
    out = out.resolve()
    os.chdir(ROOT / "backend")
    sys.path.insert(0, str(ROOT / "backend"))
    from app.database import session_scope  # noqa: E402
    from app.models import Asset  # noqa: E402
    from app.providers.base import GenerationResult  # noqa: E402
    from app.services import assets as assets_svc  # noqa: E402

    res = GenerationResult(
        file_path=str(out), provider="import", model="external",
        duration=meta.get("duration"), format="mp3",
        size_bytes=out.stat().st_size, prompt=meta.get("title", ""), elapsed_ms=0,
    )
    with session_scope() as db:
        asset = assets_svc.ingest_result(
            db, project_id=project_id, result=res, asset_type="MUSIC",
            name=meta.get("name") or out.stem, source="imported",
            extra={"role": "bgm", "origin": "external", **meta},
        )
        article_id, url = asset.id, asset.url
        db.commit()
    print(f"[✓] 已登记 MUSIC 资产 {article_id}")
    print(f"    {url}")
    print("    compose_video 取的是项目**最新**一条 MUSIC 资产，这条会被自动选用。")


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #
def cmd_inspect(args) -> None:
    src_path = Path(args.file)
    info = probe(src_path)
    buf = decode(src_path)
    peak, rms = amp_db(buf)
    dur = len(buf) / SR
    print(f"[i] {src_path.name}")
    print(f"    编码 {info['codec']} · {info['sample_rate']}Hz · {info['channels']}ch "
          f"· {info['bit_rate'] / 1000:.0f}kbps")
    print(f"    时长 {info['duration']:.1f}s（解码后 {dur:.1f}s）")
    print(f"    峰值 {peak:+.1f} dBFS · 整体 RMS {rms:+.1f} dB")
    clipped = int((np.abs(buf) >= 0.999).sum())
    if clipped:
        pct = clipped / max(len(buf), 1) * 100
        print(f"    [!] 削顶样本 {clipped} 个（{pct:.3f}%）—— 母带已顶到满刻度，"
              f"加工时务必留余量（脚本末尾会归一，勿在中间叠加）")
    # 1s 级 RMS 曲线，用来看宏观弧线与段落边界
    n = SR
    m = len(buf) // n
    if m >= 8:
        db = 20 * np.log10(np.maximum(
            np.sqrt((buf[: m * n].reshape(m, n, 2).astype(np.float64) ** 2).mean(axis=(1, 2))), 1e-9))
        step = max(m // 8, 1)
        print("    宏观弧线（等分 RMS）：")
        for i in range(0, m, step):
            j = min(i + step, m)
            v = float(db[i:j].mean())
            print(f"      {i:>5d}-{j:<5d}s {v:+6.1f} dB {'#' * max(int((v + 30) / 1.5), 1)}")
        d = np.abs(np.diff(db))
        marks = sorted(int(x) for x in np.argsort(-d)[:8])
        print("    能量突变点（可能的段落边界，秒）: " + ", ".join(str(x) for x in marks))

    # 每 5s 一条能量柱，用来挑循环点
    step = 5.0
    mono = buf.mean(axis=1)
    print("    能量分布（每 5s，1 格 ≈ 3dB，相对峰值）：")
    for i in range(0, int(dur // step) + 1):
        seg = mono[int(i * step * SR):int((i + 1) * step * SR)]
        if not len(seg):
            continue
        r = 20 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-9))
        rel = max(r - (-60.0), 0.0)
        bars = int(min(rel / 60.0 * 20, 20))
        print(f"      {i * step:>6.1f}s |{'#' * max(bars, 1)}")


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def cmd_build(args) -> None:
    src_path = Path(args.file)
    if not src_path.exists():
        raise SystemExit(f"[!] 找不到源文件: {src_path}")
    src = decode(src_path)
    src_dur = len(src) / SR
    target = float(args.target)
    print(f"[i] 源 {src_path.name}  {src_dur:.1f}s → 目标 {target:.0f}s")

    if args.arrange:
        spec = json.loads(Path(args.arrange).read_text(encoding="utf-8"))
        target = float(spec.get("target") or target)
        sources = render_source_map(src_path, spec.get("variants"))
        buf, log = build_arrange(spec, sources, int(target * SR))
        print(f"[i] 按编曲表 {args.arrange} 拼接 {len(log)} 段：")
        for line in log:
            print(line)
        fade_in = float(spec.get("fade_in", args.fade_in))
        fade_out = float(spec.get("fade_out", args.fade_out))
    else:
        if src_dur >= target - 0.05:
            buf = src[:int(target * SR)].copy()
            print("[i] 源时长足够，直接截取（不做循环）")
        elif args.fit == "trim":
            buf = np.zeros((int(target * SR), 2), dtype=np.float32)
            buf[:len(src)] = src
            print("[i] --fit trim：截到目标长度，不足部分静音")
        else:
            buf, seams = loop_fill(src, int(target * SR), loop_from=args.loop_from,
                                   xfade=args.xfade, bpm=args.bpm)
            pts = "  ".join(f"{s:.1f}s" for s in seams[:8]) or "无"
            print(f"[i] --fit loop：循环 {len(seams)} 次，接缝 {pts}"
                  f"{' …' if len(seams) > 8 else ''}")
            if len(seams) > 6:
                print("[i] 提示：接缝偏多。更好的做法是把曲子重编成一段 300s 的"
                      "完整编曲（--arrange），而不是反复循环。")
        fade_in, fade_out = args.fade_in, args.fade_out

    buf = apply_fades(buf, fade_in, fade_out)

    if args.duck:
        vpath = Path(args.voice) if args.voice else None
        if vpath is None:
            vpath = latest_voice_path(args.project)
        if not vpath or not Path(vpath).exists():
            print("[!] 找不到配音音轨，跳过闪避（配音还没跑？先跑 generate_voice）")
        else:
            voice = decode(Path(vpath))
            g = speech_gain_curve(voice, length=len(buf), depth_db=args.duck_db)
            ducked = float((g < 0.999).mean() * 100)
            buf = buf * g[:, None]
            print(f"[i] 旁白闪避 {args.duck_db:.1f}dB：{ducked:.0f}% 的时长被压低"
                  f"（音轨 {Path(vpath).name}）")

    peak, rms = amp_db(buf)
    if args.peak_db is not None and peak > -119:
        gain = 10 ** ((args.peak_db - peak) / 20)
        buf *= gain
        print(f"[i] 峰值归一：{peak:+.1f} → {args.peak_db:+.1f} dBFS")
    if args.gain_db:
        buf *= 10 ** (args.gain_db / 20)
        print(f"[i] 额外增益 {args.gain_db:+.1f} dB")

    peak, rms = amp_db(buf)
    out = Path(args.out)
    encode(buf, out)
    print(f"[✓] 成品 {out}  ({len(buf) / SR:.1f}s · 峰值 {peak:+.1f} dBFS · RMS {rms:+.1f} dB"
          f" · {out.stat().st_size / 1e6:.1f}MB)")

    print("\n下一步（混音时音乐音量不要再沿用 0.16，那是给合成 BGM 定的）：")
    print("  invoke compose_video 时传 payload: {\"music_volume\": 0.30, \"with_music\": true}")

    if args.register:
        if not args.project:
            raise SystemExit("[!] --register 需要 --project")
        register(out, args.project, {
            "name": args.name or out.stem,
            "title": args.title or src_path.stem,
            "artist": args.artist or "",
            "source_file": str(src_path),
            "target_duration": target,
            "notch": "external-copyright",
        })


def latest_voice_path(project_id: str | None) -> str | None:
    if not project_id:
        return None
    import sqlite3
    db = sqlite3.connect(str(ROOT / "backend" / "video_agent_studio.db"))
    db.row_factory = sqlite3.Row
    row = db.execute(
        "select file_path from assets where project_id=? and type='VOICE' "
        "order by created_at desc limit 1", (project_id,)).fetchone()
    db.close()
    return row["file_path"] if row else None


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="外部音乐 → 项目 BGM（加工 + 入库）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("inspect", help="看源文件结构与能量分布")
    p1.add_argument("--file", required=True)

    p2 = sub.add_parser("build", help="加工并（可选）登记为 MUSIC 资产")
    p2.add_argument("--file", required=True, help="源音乐文件（mp3/flac/wav 均可）")
    p2.add_argument("--out", help="输出 mp3 路径，默认放到项目 music 目录")
    p2.add_argument("--project", help="项目 id（--register / --duck 需要）")
    p2.add_argument("--target", type=float, default=0.0, help="目标时长秒，默认取项目时长")
    p2.add_argument("--fit", choices=["loop", "trim"], default="loop")
    p2.add_argument("--loop-from", type=float, default=0.0,
                    help="从源的第几秒开始循环（跳过引子），如 24")
    p2.add_argument("--xfade", type=float, default=2.0, help="循环接缝交叉淡化秒数")
    p2.add_argument("--bpm", type=float, default=0.0, help="给出 BPM 会把循环点吸附到拍上")
    p2.add_argument("--fade-in", type=float, default=2.0)
    p2.add_argument("--fade-out", type=float, default=8.0)
    p2.add_argument("--arrange", help="手工编曲 JSON（见文件头注释）")
    p2.add_argument("--duck", action="store_true", help="按旁白时间轴自动压低 BGM")
    p2.add_argument("--voice", help="配音音轨路径，默认自动取项目最新 VOICE 资产")
    p2.add_argument("--duck-db", type=float, default=8.0, help="说话时压低多少 dB")
    p2.add_argument("--peak-db", type=float, default=-6.0, help="峰值归一目标 dBFS")
    p2.add_argument("--gain-db", type=float, default=0.0, help="额外增益")
    p2.add_argument("--register", action="store_true", help="登记为项目 MUSIC 资产")
    p2.add_argument("--name", help="资产名，默认取输出文件名")
    p2.add_argument("--title", help="曲名（写进资产元数据）")
    p2.add_argument("--artist", help="艺术家")

    args = ap.parse_args()

    if args.cmd == "inspect":
        cmd_inspect(args)
        return

    # 补齐默认值：目标时长 / 输出路径
    if not args.target:
        args.target = project_duration(args.project) or 300.0
    if not args.out:
        base = ROOT / "backend" / "storage" / "music" / (args.project or "library")
        args.out = str(base / f"bgm_{Path(args.file).stem}_{int(args.target)}s.mp3")
    cmd_build(args)


def project_duration(project_id: str | None) -> float:
    if not project_id:
        return 0.0
    import sqlite3
    db = sqlite3.connect(str(ROOT / "backend" / "video_agent_studio.db"))
    row = db.execute("select target_duration from projects where id=?",
                     (project_id,)).fetchone()
    db.close()
    return float(row[0] or 0.0) if row else 0.0


if __name__ == "__main__":
    main()
