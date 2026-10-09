import subprocess
from pathlib import Path

import pytest

from vaniloop.ingest import AUDIO_DIR, TRANSCRIPTS_DIR, build_call_records, detect_silences, load_transcript

HAS_DATA = (TRANSCRIPTS_DIR / "10_90797297_EMBDVXFH5.json").exists()
needs_data = pytest.mark.skipif(not HAS_DATA, reason="local call data not present")


def synthetic_transcripts(d: Path, n: int = 3) -> Path:
    """Write n tiny transcripts so ingest tests do not depend on real call data."""
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"syn{i}.json").write_text(
            f'{{"call_id":"syn{i}","duration_sec":4,"turns":[{{"speaker":"bot","start_sec":0,"end_sec":2,"text":"hello"}},'
            f'{{"speaker":"user","start_sec":2,"end_sec":4,"text":"haan"}}]}}'
        )
    return d


COUNTS = {
    "10_90797297_EMBDVXFH5": 9,
    "133_90852837_DSXGKXVH5": 3,
    "282_90685047_X413J1UG5": 35,
}


@needs_data
@pytest.mark.parametrize("call_id,n", COUNTS.items())
def test_load_transcript_turn_counts(call_id, n):
    rec = load_transcript(TRANSCRIPTS_DIR / f"{call_id}.json")
    assert len(rec.turns) == n
    assert [t.idx for t in rec.turns] == list(range(n))
    assert {t.speaker for t in rec.turns} <= {"bot", "user"}
    assert rec.audio_path == str(AUDIO_DIR / f"{call_id}.mp3")


def test_seller_maps_to_user(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(
        '{"call_id":"x","duration_sec":1,"turns":[{"speaker":"seller","start_sec":0,"end_sec":1,"text":"hi"}]}'
    )
    rec = load_transcript(p, tmp_path)
    assert rec.turns[0].speaker == "user"
    assert rec.audio_path is None


@needs_data
def test_detect_silences_real_call():
    sil = detect_silences(AUDIO_DIR / "10_90797297_EMBDVXFH5.mp3")
    print(sil)
    assert all(s.end_sec > s.start_sec for s in sil)


def _make(tmp_path: Path, graph: str) -> Path:
    out = tmp_path / "t.mp3"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", graph, str(out)],
        check=True,
    )
    return out


def test_detect_silences_synthetic(tmp_path):
    f = _make(
        tmp_path,
        "sine=frequency=440:duration=2[a];anullsrc=r=44100:cl=mono,atrim=duration=3[s];"
        "sine=frequency=440:duration=2[b];[a][s][b]concat=n=3:v=0:a=1",
    )
    sil = detect_silences(f)
    assert len(sil) == 1
    assert sil[0].start_sec == pytest.approx(2.0, abs=0.3)
    assert sil[0].duration == pytest.approx(3.0, abs=0.4)


def test_trailing_silence(tmp_path):
    f = _make(
        tmp_path,
        "sine=frequency=440:duration=2[a];anullsrc=r=44100:cl=mono,atrim=duration=3[s];[a][s]concat=n=2:v=0:a=1",
    )
    sil = detect_silences(f)
    assert len(sil) == 1
    assert sil[0].end_sec == pytest.approx(5.0, abs=0.4)


def test_build_resumable(tmp_path):
    out = tmp_path / "calls.jsonl"
    src = synthetic_transcripts(tmp_path / "t")
    r1 = build_call_records(src, tmp_path, out)
    assert len(r1) == 3
    r2 = build_call_records(src, tmp_path, out)
    assert len(r2) == 3
    assert len(out.read_text().splitlines()) == 3
