# -*- coding: utf-8 -*-
"""numpy 合成 BGM，两种风格：

  pop  （默认）: 抖音/vlog 流行风。108 BPM，4536 和弦走向（F-G-Em-Am），
                 鼓组（底鼓/军鼓/踩镲）+ 贝斯 + 明亮拨弦和弦 + 五声旋律线。
  warm          : 温暖治愈钢琴风（C-G-Am-F，76 BPM）。

用法:
  tools/qwen3-tts/venv/Scripts/python.exe scripts/gen_bgm.py \
      --out backend/storage/music/proj_xxx/music_xxx.mp3 --duration 306 --style pop
"""
import argparse
import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 44100


# --------------------------------------------------------------------------- #
# 基础音色
# --------------------------------------------------------------------------- #
def piano_note(freq: float, dur: float, vel: float = 1.0) -> np.ndarray:
    n = int(SR * dur)
    t = np.arange(n) / SR
    wavef = (1.00 * np.sin(2 * np.pi * freq * t)
             + 0.45 * np.sin(2 * np.pi * freq * 2 * t)
             + 0.18 * np.sin(2 * np.pi * freq * 3 * t)
             + 0.08 * np.sin(2 * np.pi * freq * 4 * t))
    env = np.exp(-t * 2.2) * (1 - np.exp(-t / 0.008))
    env *= 1 / (1 + freq / 800)
    return (wavef * env * vel).astype(np.float32)


def pad_chord(freqs: list[float], dur: float, vel: float = 1.0) -> np.ndarray:
    n = int(SR * dur)
    t = np.arange(n) / SR
    out = np.zeros(n, dtype=np.float32)
    for f in freqs:
        out += 0.5 * np.sin(2 * np.pi * f * t) + 0.15 * np.sin(2 * np.pi * f * 2 * t + 0.5)
    attack = 1 - np.exp(-t / (dur * 0.3))
    release = np.exp(-np.maximum(t - dur * 0.7, 0) / (dur * 0.25))
    return (out * attack * release * vel / len(freqs)).astype(np.float32)


def pluck(freq: float, dur: float, vel: float = 1.0) -> np.ndarray:
    """明亮拨弦（电吉他/拨弦合成器感）：谐波更密、衰减更快。"""
    n = int(SR * dur)
    t = np.arange(n) / SR
    wavef = (np.sin(2 * np.pi * freq * t)
             + 0.6 * np.sin(2 * np.pi * freq * 2 * t)
             + 0.35 * np.sin(2 * np.pi * freq * 3 * t)
             + 0.2 * np.sin(2 * np.pi * freq * 4 * t)
             + 0.1 * np.sin(2 * np.pi * freq * 6 * t))
    env = np.exp(-t * 6.0) * (1 - np.exp(-t / 0.004))
    return (wavef * env * vel).astype(np.float32)


def bass_note(freq: float, dur: float, vel: float = 1.0) -> np.ndarray:
    """电贝斯：基频+二三次谐波，圆润。"""
    n = int(SR * dur)
    t = np.arange(n) / SR
    wavef = (np.sin(2 * np.pi * freq * t)
             + 0.5 * np.sin(2 * np.pi * freq * 2 * t)
             + 0.15 * np.sin(2 * np.pi * freq * 3 * t))
    env = np.exp(-t * 3.5) * (1 - np.exp(-t / 0.006))
    return (wavef * env * vel).astype(np.float32)


# --------------------------------------------------------------------------- #
# 鼓组
# --------------------------------------------------------------------------- #
def kick(vel: float = 1.0) -> np.ndarray:
    dur = 0.28
    n = int(SR * dur)
    t = np.arange(n) / SR
    freq = 45 + 115 * np.exp(-t * 38)          # 160→45Hz 快速下扫
    phase = 2 * np.pi * np.cumsum(freq) / SR
    env = np.exp(-t * 14)
    click = np.exp(-t * 300) * 0.3 * np.random.default_rng(7).standard_normal(n)
    return ((np.sin(phase) * env + click) * vel).astype(np.float32)


def snare(vel: float = 1.0) -> np.ndarray:
    dur = 0.18
    n = int(SR * dur)
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(n)
    for _ in range(2):                          # 两阶差分 ≈ 高通
        noise = np.diff(noise, prepend=noise[0])
    noise /= (np.abs(noise).max() or 1)
    t = np.arange(n) / SR
    tone = 0.5 * np.sin(2 * np.pi * 190 * t) * np.exp(-t * 30)
    env = np.exp(-t * 22)
    return ((noise * 0.8 + tone) * env * vel).astype(np.float32)


def hihat(open_: bool = False, vel: float = 1.0) -> np.ndarray:
    dur = 0.12 if open_ else 0.045
    n = int(SR * dur)
    rng = np.random.default_rng(13 if open_ else 17)
    noise = rng.standard_normal(n)
    for _ in range(3):                          # 三阶差分 ≈ 更亮的高通
        noise = np.diff(noise, prepend=noise[0])
    noise /= (np.abs(noise).max() or 1)
    t = np.arange(n) / SR
    env = np.exp(-t * (18 if open_ else 60))
    return (noise * env * vel * 0.5).astype(np.float32)


def place(buf: np.ndarray, note: np.ndarray, start_sec: float) -> None:
    i = int(start_sec * SR)
    j = min(i + len(note), len(buf))
    if i < len(buf):
        buf[i:j] += note[: j - i]


def reverb(x: np.ndarray, decay: float = 0.3, taps: int = 6) -> np.ndarray:
    out = x.copy()
    for k in range(1, taps + 1):
        d = int(SR * 0.037 * k * (1 + 0.13 * (k % 3)))
        wet = np.zeros_like(x)
        wet[d:] = x[:-d] * (decay ** k) * (1 if k % 2 else 0.7)
        out += wet
    return out


# --------------------------------------------------------------------------- #
# pop 风格
# --------------------------------------------------------------------------- #
def build_pop(total: float, bpm: float, peak_db: float) -> np.ndarray:
    beat = 60.0 / bpm
    bar = beat * 4
    n_bars = int(np.ceil(total / bar)) + 1

    F = {"C3": 130.81, "D3": 146.83, "E3": 164.81, "F3": 174.61, "G3": 196.0, "A3": 220.0, "B3": 246.94,
         "C2": 65.41, "F2": 87.31, "G2": 98.0, "A2": 110.0, "E2": 82.41,
         "C4": 261.63, "D4": 293.66, "E4": 329.63, "F4": 349.23, "G4": 392.0, "A4": 440.0,
         "B4": 493.88, "C5": 523.25, "D5": 587.33, "E5": 659.26}

    # 4536 经典流行走向：F - G - Em - Am
    PROG = [
        # (贝斯根音, 和弦音, 拨弦节奏音组)
        ("F2", ["F3", "A3", "C4", "F4"], ["F4", "A4", "C5", "A4"]),
        ("G2", ["G3", "B3", "D4", "G4"], ["G4", "B4", "D5", "B4"]),
        ("E2", ["E3", "G3", "B3", "E4"], ["E4", "G4", "B4", "G4"]),
        ("A2", ["A3", "C4", "E4", "A4"], ["A4", "C5", "E5", "C5"]),
    ]
    # 五声旋律：两小节一句，四句轮换（F/G/Em/Am 各配一句）
    MELODY = [
        ["A4", "C5", None, "D5", "C5", None, "A4", None],
        ["B4", "D5", None, "E5", "D5", "B4", None, None],
        ["G4", "B4", "E5", None, "D5", "B4", None, None],
        ["A4", "E4", None, "A4", "C5", None, "A4", None],
    ]

    K, S, H, HO = kick(), snare(), hihat(), hihat(open_=True)
    buf = np.zeros(int(SR * (n_bars * bar + 2)), dtype=np.float32)
    rng = np.random.default_rng(2024)

    for b in range(n_bars):
        t0 = b * bar
        bass_root, chord, mel_chord = PROG[b % 4]
        drop = b % 8 == 7                       # 每 8 小节最后一小节留白，制造呼吸

        # --- 鼓组 ---
        if b >= 2 and not drop:                 # 前两小节纯音乐进入
            place(buf, K, t0)
            place(buf, K, t0 + 2 * beat)
            if b % 8 == 6:
                place(buf, K, t0 + 3.5 * beat)  # 推进填充
            place(buf, S, t0 + beat)
            place(buf, S, t0 + 3 * beat)
            for k in range(8):                  # 踩镲八分
                place(buf, HO if (k == 7 and b % 2) else H, t0 + k * beat / 2)

        # --- 贝斯：八分音符根音，带八度跳动 ---
        if b >= 1:
            for k in range(8):
                if drop and k % 2:
                    continue
                f = F[bass_root] * (2 if k in (3, 7) else 1)
                place(buf, bass_note(f, beat * 0.55, vel=0.42), t0 + k * beat / 2)

        # --- 拨弦和弦：反拍律动 ---
        for k in range(8):
            if k in (0, 4) or drop:
                continue
            place(buf, pluck(F[chord[k % 4]], beat * 0.8, vel=0.20),
                  t0 + k * beat / 2 + beat * 0.25)
        place(buf, pluck(F[chord[0]], beat * 1.2, vel=0.24), t0)         # 正拍柱式
        place(buf, pluck(F[chord[2]], beat * 1.0, vel=0.20), t0 + 2 * beat)

        # --- 弦乐垫 ---
        place(buf, pad_chord([F[chord[0]], F[chord[1]], F[chord[2]]], bar * 1.02, vel=0.16), t0)

        # --- 旋律：两小节一句 ---
        if b >= 4 and not drop:
            phrase = MELODY[(b // 2) % 4]
            note8 = phrase[(b % 2) * 4: (b % 2) * 4 + 4]
            for k, name in enumerate(note8):
                if name is None or rng.random() < 0.1:
                    continue
                place(buf, pluck(F[name], beat * 1.6, vel=0.26), t0 + k * beat)

    # 淡入淡出
    fade_in = int(SR * 2)
    buf[:fade_in] *= np.linspace(0, 1, fade_in) ** 1.5
    fade_out = int(SR * 5)
    buf[-fade_out:] *= np.linspace(1, 0, fade_out) ** 1.5
    buf = buf[: int(SR * total)]

    buf = reverb(buf, decay=0.22, taps=5)
    peak = np.max(np.abs(buf)) or 1.0
    return (buf / peak * (10 ** (peak_db / 20))).astype(np.float32)


# --------------------------------------------------------------------------- #
# warm 风格（旧版保留）
# --------------------------------------------------------------------------- #
def build_warm(total: float, bpm: float, peak_db: float) -> np.ndarray:
    beat = 60.0 / bpm
    bar = beat * 4
    n_bars = int(np.ceil(total / bar)) + 1

    F = {"C2": 65.41, "G2": 98.0, "A2": 110.0, "F2": 87.31,
         "C3": 130.81, "E3": 164.81, "G3": 196.0, "A3": 220.0, "B3": 246.94,
         "C4": 261.63, "D4": 293.66, "E4": 329.63, "F4": 349.23, "G4": 392.0, "A4": 440.0,
         "B4": 493.88, "C5": 523.25, "D5": 587.33, "E5": 659.26}
    PROG = [
        ("C2", ["C3", "E3", "G3", "C4", "E4", "G4"]),
        ("G2", ["G3", "B3", "D4", "G4", "B4", "D5"]),
        ("A2", ["A3", "C4", "E4", "A4", "C5", "E5"]),
        ("F2", ["A3", "C4", "F4", "A4", "C5", "F4"]),
    ]
    MELODY = [
        ["E4", "G4", None, "C5", None, None, "G4", None],
        ["D5", "B4", None, "G4", None, "A4", None, None],
        ["C5", "E5", None, "A4", None, None, "E4", "G4"],
        ["A4", "F4", None, "C5", None, "A4", None, None],
    ]
    buf = np.zeros(int(SR * (n_bars * bar + 2)), dtype=np.float32)
    pad_buf = np.zeros_like(buf)
    rng = np.random.default_rng(42)
    for b in range(n_bars):
        t0 = b * bar
        root, arp = PROG[b % 4]
        place(buf, piano_note(F[root], bar * 1.8, vel=0.5), t0)
        place(pad_buf, pad_chord([F[arp[0]], F[arp[2]], F[arp[3]]], bar * 1.05, vel=0.22), t0)
        pattern = [0, 1, 2, 3, 4, 3, 2, 1]
        for k, idx in enumerate(pattern):
            vel = 0.34 if k in (0, 4) else 0.24
            if rng.random() < 0.06:
                continue
            place(buf, piano_note(F[arp[idx]], beat * 2.2, vel=vel), t0 + k * beat / 2)
        mel = MELODY[b % 4]
        if b >= n_bars - 2:
            continue
        for k, name in enumerate(mel):
            if name is None or rng.random() < 0.12:
                continue
            place(buf, piano_note(F[name], beat * 3.0, vel=0.30), t0 + k * beat / 2)
    buf += pad_buf
    fade_in = int(SR * 3)
    buf[:fade_in] *= np.linspace(0, 1, fade_in) ** 2
    fade_out = int(SR * 6)
    buf[-fade_out:] *= np.linspace(1, 0, fade_out) ** 2
    buf = buf[: int(SR * total)]
    buf = reverb(buf, decay=0.32, taps=7)
    kernel = 9
    buf = np.convolve(buf, np.ones(kernel) / kernel, mode="same")
    peak = np.max(np.abs(buf)) or 1.0
    return (buf / peak * (10 ** (peak_db / 20))).astype(np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float, default=306.0)
    ap.add_argument("--bpm", type=float, default=0.0, help="0 = 按风格默认（pop 108 / warm 76）")
    ap.add_argument("--style", choices=["pop", "warm"], default="pop")
    ap.add_argument("--peak-db", type=float, default=-9.0,
                    help="输出峰值 dBFS（合成链路会对 BGM 再乘 0.16，故文件本身可偏响）")
    args = ap.parse_args()

    bpm = args.bpm or (108.0 if args.style == "pop" else 76.0)
    builder = build_pop if args.style == "pop" else build_warm
    buf = builder(args.duration, bpm, args.peak_db)

    # 立体声：右声道轻微延迟制造宽度
    delay = int(SR * 0.012)
    right = np.concatenate([np.zeros(delay, dtype=np.float32), buf[:-delay]])
    stereo = np.stack([buf, right], axis=1)

    wav_path = Path(args.out).with_suffix(".tmp.wav")
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((stereo * 32767).astype(np.int16).tobytes())

    ffmpeg = Path.home() / ".workbuddy" / "binaries" / "ffmpeg" / "bin" / "ffmpeg.exe"
    subprocess.run([str(ffmpeg), "-y", "-v", "error", "-i", str(wav_path),
                    "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100",
                    str(args.out)], check=True)
    wav_path.unlink()
    print(f"[✓] BGM 已生成: {args.out} ({args.duration:.0f}s, {args.style} {bpm:.0f} BPM, peak {args.peak_db} dBFS)")


if __name__ == "__main__":
    main()
