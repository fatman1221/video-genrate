# -*- coding: utf-8 -*-
"""Qwen3-TTS CustomVoice 推理脚本（带情感的中文配音）。

单条模式:
  python infer.py --text-file t.txt --out v.wav [--speaker Cherry] [--instruct "用温柔的语气说"]

批量模式（一次模型加载，推荐）:
  python infer.py --jobs jobs.json
  jobs.json: [{"text": "...", "out": "a.wav", "instruct": "...", "speaker": "Cherry"}, ...]
"""
import argparse
import json
import sys
from pathlib import Path

DEFAULT_MODEL = str(Path(__file__).parent / "models" / "Qwen3-TTS-12Hz-1.7B-CustomVoice")
DEFAULT_SPEAKER = "Cherry"  # 中文女声，温柔自然


def synthesize_all(jobs: list[dict], model_dir: str, device: str = "auto") -> None:
    import torch
    import soundfile as sf
    from qwen_tts import Qwen3TTSModel

    if device == "auto":
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
    print(f"[qwen3-tts] loading model from {model_dir} on {device} ...", flush=True)
    model = Qwen3TTSModel.from_pretrained(model_dir, device_map=device, dtype=dtype)
    print("[qwen3-tts] model loaded", flush=True)

    for i, job in enumerate(jobs):
        text = (job.get("text") or "").strip()
        out = job["out"]
        if not text:
            print(f"[qwen3-tts] skip empty text: {out}", flush=True)
            continue
        speaker = job.get("speaker") or DEFAULT_SPEAKER
        instruct = (job.get("instruct") or "").strip() or None
        try:
            wavs, sr = model.generate_custom_voice(
                text=[text], speaker=[speaker], language=["Chinese"],
                instruct=[instruct] if instruct else None,
                non_streaming_mode=True,
            )
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            sf.write(out, wavs[0], sr)
            dur = len(wavs[0]) / sr
            print(f"[qwen3-tts] ok ({i+1}/{len(jobs)}) {dur:.1f}s -> {out}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[qwen3-tts] FAILED ({i+1}/{len(jobs)}) {out}: {exc}", flush=True)
            raise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-file", help="单条模式的文本文件（UTF-8）")
    ap.add_argument("--jobs", help="批量模式：任务 JSON 文件")
    ap.add_argument("--out", help="单条模式输出路径")
    ap.add_argument("--speaker", default=DEFAULT_SPEAKER)
    ap.add_argument("--instruct", default="")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default="auto", help="auto / cuda:0 / cpu")
    args = ap.parse_args()

    if args.jobs:
        jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    elif args.text_file and args.out:
        text = Path(args.text_file).read_text(encoding="utf-8")
        jobs = [{"text": text, "out": args.out, "speaker": args.speaker,
                 "instruct": args.instruct}]
    else:
        print("need --jobs or (--text-file + --out)", file=sys.stderr)
        sys.exit(2)

    synthesize_all(jobs, args.model, args.device)


if __name__ == "__main__":
    main()
