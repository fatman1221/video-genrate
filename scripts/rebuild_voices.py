# -*- coding: utf-8 -*-
"""用 Qwen3-TTS（带情感指令）重建项目全部镜头配音。

用法:
  python scripts/rebuild_voices.py --spec examples/paipai_project.json --project proj_xxx

流程:
  1. 读规格 JSON 里每个镜头的 voice_script / voice_instruct / voice_speaker
  2. 生成 jobs.json，调 tools/qwen3-tts/infer.py 批量合成（单次模型加载）
  3. wav -> mp3，ingest 成 VOICE asset，更新 shot.voice_asset_id / voice_status
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import os  # noqa: E402

# 数据库路径是相对 backend/ 的（sqlite:///./video_agent_studio.db）
os.chdir(ROOT / "backend")

QWEN_PY = ROOT / "tools" / "qwen3-tts" / "venv" / "Scripts" / "python.exe"
INFER = ROOT / "tools" / "qwen3-tts" / "infer.py"
FFMPEG = Path.home() / ".workbuddy" / "binaries" / "ffmpeg" / "bin" / "ffmpeg.exe"

# 模型自带音色（唯一事实来源：模型的 config.json → talker_config.spk_id）
SPK_CONFIG = (ROOT / "tools" / "qwen3-tts" / "models"
              / "Qwen3-TTS-12Hz-1.7B-CustomVoice" / "config.json")
_FALLBACK_SPEAKERS = ("serena", "vivian", "uncle_fu", "ryan", "aiden",
                      "ono_anna", "sohee", "eric", "dylan")


def known_speakers() -> set[str]:
    """读模型配置得到合法音色名（小写）。读不到时用内置清单兜底。"""
    try:
        cfg = json.loads(SPK_CONFIG.read_text(encoding="utf-8"))
        return {str(k).lower() for k in cfg["talker_config"]["spk_id"]}
    except Exception:
        return set(_FALLBACK_SPEAKERS)


def resolve_speaker(code: str, raw: str, cast: dict[str, str],
                    default_voice: str, known: set[str]) -> str:
    """把规格里的 voice_speaker 解析成**模型认得的真实音色名**。

    规格里通常写角色代号（M / AI 这种语义名），需要经 top-level `voice_cast` 映射；
    也允许直接写真音色名。**解析不出来就报错**——不能静默兜底，
    因为 Qwen3-TTS 收到未知音色名会直接 raise NotImplementedError，
    而且默认值 'Cherry' 同样不在合法清单里。
    """
    cand = (raw or "").strip()
    if cand:
        if cand.upper() in cast:
            spk = cast[cand.upper()]
            if spk not in known:
                raise SystemExit(f"[!] 镜头 {code}：voice_cast['{cand}'] = '{spk}' 不是合法音色\n"
                                 f"    合法音色：{', '.join(sorted(known))}")
            return spk
        if cand.lower() in known:
            return cand.lower()
        raise SystemExit(
            f"[!] 镜头 {code}：voice_speaker='{cand}' 既不是 voice_cast 里的角色代号，"
            f"也不是合法音色名\n"
            f"    合法音色：{', '.join(sorted(known))}\n"
            f"    角色代号：{', '.join(sorted(cast)) or '（voice_cast 为空）'}")
    if default_voice:
        if default_voice not in known:
            raise SystemExit(f"[!] 顶层 voice='{default_voice}' 不是合法音色，"
                             f"合法值：{', '.join(sorted(known))}")
        return default_voice
    raise SystemExit(f"[!] 镜头 {code} 没有 voice_speaker，且顶层 voice 为空，"
                     f"无法确定音色。请在规格里补 voice_cast 或 voice。")

from app.database import session_scope  # noqa: E402
from app.models import Asset, Shot  # noqa: E402
from app.services import assets as assets_svc  # noqa: E402
from app.providers.base import GenerationResult  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--device", default="auto", help="auto / cuda:0 / cpu")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析规格并打印将合成的音色/文本，不加载模型、不占 GPU")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.is_absolute():
        spec_path = ROOT / spec_path
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    lines: dict[str, dict] = {}
    for sc in spec.get("scenes", []):
        for sh in sc.get("shots", []):
            if sh.get("voice_script"):
                lines[sh["code"]] = sh

    with session_scope() as db:
        shots = db.query(Shot).filter(Shot.project_id == args.project).all()
        by_code = {s.code: s for s in shots}

    # 角色代号 -> 真实音色名（规格顶层 voice_cast），以及全局兜底音色
    cast = {str(k).strip().upper(): str(v).strip().lower()
            for k, v in (spec.get("voice_cast") or {}).items()}
    default_voice = str(spec.get("voice") or "").strip().lower()
    known = known_speakers()

    todo = []
    used: dict[str, list[str]] = {}
    for code, sh in lines.items():
        shot = by_code.get(code)
        if not shot:
            print(f"[!] 镜头 {code} 不在项目中，跳过")
            continue
        speaker = resolve_speaker(code, str(sh.get("voice_speaker") or ""),
                                  cast, default_voice, known)
        used.setdefault(speaker, []).append(code)
        todo.append({
            "code": code,
            "shot_id": shot.id,
            "scene_id": shot.scene_id,
            "text": sh["voice_script"],
            "instruct": sh.get("voice_instruct", ""),
            "speaker": speaker,
        })
    print(f"[+] 待合成 {len(todo)} 条")
    for spk, codes in sorted(used.items()):
        print(f"    音色 {spk:<9} {len(codes):>2} 条 -> {','.join(codes)}")

    if args.dry_run:
        print("\n--- dry-run：不调用 TTS，仅预览 ---")
        for t in todo:
            print(f"  {t['code']}  [{t['speaker']}]  {t['text'][:46]}")
            print(f"        instruct: {t['instruct'][:60] or '（无）'}")
        print(f"\n[✓] 规格解析通过，共 {len(todo)} 条；去掉 --dry-run 即可真正合成。")
        return

    with tempfile.TemporaryDirectory(prefix="qwentts_") as td:
        jobs = [{"text": t["text"], "out": str(Path(td) / f"{t['code'].replace('-', '_')}.wav"),
                 "instruct": t["instruct"], "speaker": t["speaker"]} for t in todo]
        jobs_path = Path(td) / "jobs.json"
        jobs_path.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")

        t0 = time.time()
        proc = subprocess.run(
            [str(QWEN_PY), str(INFER), "--jobs", str(jobs_path), "--device", args.device],
            capture_output=True, text=True, timeout=3600, encoding="utf-8", errors="replace",
        )
        print(proc.stdout[-3000:])
        if proc.returncode != 0:
            print(proc.stderr[-2000:], file=sys.stderr)
            sys.exit(1)
        print(f"[+] TTS 合成完成，耗时 {(time.time()-t0)/60:.1f} min")

        for t in todo:
            wav = Path(td) / f"{t['code'].replace('-', '_')}.wav"
            if not wav.exists() or wav.stat().st_size == 0:
                print(f"[!] {t['code']} 缺少音频，跳过")
                continue
            mp3 = wav.with_suffix(".mp3")
            subprocess.run([str(FFMPEG), "-y", "-v", "error", "-i", str(wav),
                            "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                            str(mp3)], check=True)
            info = subprocess.run([str(FFMPEG), "-i", str(mp3), "-f", "null", "-"],
                                  capture_output=True, text=True)
            dur = 0.0
            for ln in info.stderr.splitlines():
                if "time=" in ln:
                    part = ln.split("time=")[1].split(" ")[0]
                    try:
                        h, m, s = part.split(":")
                        dur = int(h) * 3600 + int(m) * 60 + float(s)
                    except Exception:
                        pass

            result = GenerationResult(
                file_path=str(mp3), provider="qwen3tts", model="Qwen3-TTS-12Hz-1.7B-CustomVoice",
                workflow="qwen3_tts_custom_voice", parameters={"instruct": t["instruct"]},
                duration=dur, format="mp3", size_bytes=mp3.stat().st_size,
                prompt=t["text"], elapsed_ms=0,
            )
            with session_scope() as db:
                shot = db.get(Shot, t["shot_id"])
                asset = assets_svc.ingest_result(
                    db, project_id=args.project, result=result, asset_type="VOICE",
                    name=f"{t['code']} 旁白", scene_id=shot.scene_id, shot_id=shot.id,
                    extra={"role": "shot_voice", "speaker": t["speaker"],
                           "instruct": t["instruct"]},
                )
                shot.voice_asset_id = asset.id
                shot.voice_status = "READY"
                if shot.video_asset_id:
                    from app.models import ShotStatus
                    shot.status = ShotStatus.READY
                db.commit()
            print(f"[✓] {t['code']} {dur:.1f}s")


if __name__ == "__main__":
    main()
