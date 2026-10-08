"""INGEST: transcripts + audio -> CallRecord JSON Lines."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from vaniloop.jsonl import read_jsonl
from vaniloop.models import CallRecord, Silence, Turn

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "transcripts"
AUDIO_DIR = ROOT / "data" / "raw" / "call_recordings"
OUT_PATH = ROOT / "runs" / "calls.jsonl"

_NUM = r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"  # ffmpeg may print e.g. 2.26757e-05
_START = re.compile(r"silence_start:\s*" + _NUM)
_END = re.compile(r"silence_end:\s*" + _NUM)
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def load_transcript(path: str | Path, audio_dir: str | Path = AUDIO_DIR) -> CallRecord:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    turns = []
    for i, t in enumerate(data["turns"]):
        speaker = "user" if t["speaker"] == "seller" else t["speaker"]
        turns.append(
            Turn(idx=i, speaker=speaker, start_sec=t["start_sec"], end_sec=t["end_sec"], text=t["text"])
        )
    audio = Path(audio_dir) / f"{data['call_id']}.mp3"
    return CallRecord(
        call_id=data["call_id"],
        audio_path=str(audio) if audio.exists() else None,
        duration_sec=data["duration_sec"],
        language=data.get("language", "hinglish"),
        turns=turns,
    )


def detect_silences(mp3_path: str | Path, min_sec: float = 2.0, noise_db: float = -35) -> list[Silence]:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(mp3_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_sec}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {mp3_path}: {proc.stderr[-500:]}")
    err = proc.stderr
    duration = None
    m = _DURATION.search(err)
    if m:
        duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    silences: list[Silence] = []
    open_start: float | None = None
    for line in err.splitlines():
        s = _START.search(line)
        if s:
            open_start = max(0.0, float(s[1]))
            continue
        e = _END.search(line)
        if e and open_start is not None:
            silences.append(Silence(start_sec=open_start, end_sec=float(e[1])))
            open_start = None
    if open_start is not None and duration is not None and duration > open_start:
        silences.append(Silence(start_sec=open_start, end_sec=duration))
    return silences


def build_call_records(
    transcripts_dir: str | Path = TRANSCRIPTS_DIR,
    audio_dir: str | Path = AUDIO_DIR,
    out_path: str | Path = OUT_PATH,
) -> list[CallRecord]:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    records: list[CallRecord] = []
    if out.exists():
        for row in read_jsonl(out):
            rec = CallRecord.model_validate(row)
            done.add(rec.call_id)
            records.append(rec)
    with out.open("a", encoding="utf-8") as f:
        for p in sorted(Path(transcripts_dir).glob("*.json")):
            if p.name.startswith("_"):
                continue
            rec = load_transcript(p, audio_dir)
            if rec.call_id in done:
                continue
            if rec.audio_path:
                rec.silences = detect_silences(rec.audio_path)
            f.write(rec.model_dump_json() + "\n")
            f.flush()
            done.add(rec.call_id)
            records.append(rec)
    return records


def main() -> None:
    recs = build_call_records()
    print(f"{len(recs)} records in {OUT_PATH}")


if __name__ == "__main__":
    main()
