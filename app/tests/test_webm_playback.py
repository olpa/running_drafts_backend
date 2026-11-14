import hashlib
import subprocess
from pathlib import Path

import pytest

from running_drafts.webm_playback import (
    ParserState,
    build_playback_webm,
    parse_webm,
    scan_webm_append,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mediarecorder"


@pytest.mark.parametrize(
    "case",
    [
        "chromium-completed",
        "chromium-interrupted",
        "firefox-completed",
        "firefox-interrupted",
    ],
)
def test_virtual_playback_preserves_decoded_audio(case: str, tmp_path: Path) -> None:
    source = (FIXTURES / case / "joined.webm").read_bytes()
    playback = build_playback_webm(parse_webm(source))
    playback_path = tmp_path / "playback.webm"
    playback_path.write_bytes(playback)

    def decode(path: Path) -> bytes:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
        )
        return result.stdout

    source_pcm = decode(FIXTURES / case / "joined.webm")
    playback_pcm = decode(playback_path)
    assert len(playback_pcm) == len(source_pcm)
    assert hashlib.sha256(playback_pcm).digest() == hashlib.sha256(source_pcm).digest()


def test_virtual_playback_is_deterministic() -> None:
    source = (FIXTURES / "chromium-completed" / "joined.webm").read_bytes()
    parsed = parse_webm(source)
    assert build_playback_webm(parsed) == build_playback_webm(parsed)


@pytest.mark.parametrize(
    "case",
    [
        "chromium-completed",
        "chromium-interrupted",
        "firefox-completed",
        "firefox-interrupted",
    ],
)
def test_incremental_scanner_handles_real_transport_frame_boundaries(case: str) -> None:
    frames = sorted((FIXTURES / case).glob("frame-*.webm"))
    joined = b""
    state = ParserState()
    references = []
    for frame in frames:
        old_length = len(joined)
        joined += frame.read_bytes()
        base = min(state.parse_offset, old_length)
        result = scan_webm_append(joined[base:], base, state)
        state = result.state
        references.extend(result.packets)

    parsed = parse_webm(joined)
    assert state.track == parsed.track
    assert [joined[item.source_offset : item.source_offset + item.size] for item in references] == [
        packet.payload for packet in parsed.packets
    ]
    assert [item.duration_samples for item in references] == [
        packet.duration_samples for packet in parsed.packets
    ]
