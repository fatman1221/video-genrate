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

from app.database import session_scope  # noqa: E402
from app.models import Asset, Shot  # noqa: E402
from app.services import assets as assets_svc  # noqa: E402
from app.providers.base import GenerationResult  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--device", default="auto", help="auto / cuda:0 / cpu")
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

    todo = []
    for code, sh in lines.items():
        shot = by_code.get(code)
        if not shot:
            print(f"[!] 镜头 {code} 不在项目中，跳过")
            continue
        todo.append({
            "code": code,
            "shot_id": shot.id,
            "scene_id": shot.scene_id,
            "text": sh["voice_script"],
            "instruct": sh.get("voice_instruct", ""),
            "speaker": sh.get("voice_speaker", "Cherry"),
        })
    print(f"[+] 待合成 {len(todo)} 条")

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
