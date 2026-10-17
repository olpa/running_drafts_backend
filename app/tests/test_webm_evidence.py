import hashlib
import json
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

APP_ROOT = Path(__file__).parents[1]
EMPTY_FIXTURE = files("running_drafts").joinpath("assets/empty-audio.webm")
EMPTY_SHA256 = "2f843b2151bfd44876e137cd5f65c1e8a98b2050cbe054865bf4d86e9292b32f"


def test_real_browser_mediarecorder_evidence() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_webm_assembly.py"],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "verified chromium-completed" in result.stdout
    assert "verified firefox-interrupted" in result.stdout
    assert "566520e1d0dfc4d74492686195c7670b457f9fbd" in result.stdout


def test_empty_webm_is_reproducible_and_decodes_to_zero_samples(tmp_path: Path) -> None:
    regenerated = tmp_path / "empty-audio.webm"
    subprocess.run(
        [sys.executable, "scripts/generate_empty_webm.py", str(regenerated)],
        cwd=APP_ROOT,
        check=True,
    )
    committed = EMPTY_FIXTURE.read_bytes()

    assert regenerated.read_bytes() == committed
    assert len(committed) == 171
    assert hashlib.sha256(committed).hexdigest() == EMPTY_SHA256

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,channels,sample_rate",
            "-of",
            "json",
            str(regenerated),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream == {"codec_name": "opus", "sample_rate": "48000", "channels": 1}

    decoded = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(regenerated),
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    assert decoded.stdout == b""
