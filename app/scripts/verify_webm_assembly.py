#!/usr/bin/env python3
"""Verify the committed real-browser MediaRecorder assembly evidence."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

FIXTURE_ROOT = Path(__file__).parents[1] / "tests/fixtures/mediarecorder"
CASE_NAMES = (
    "chromium-completed",
    "chromium-interrupted",
    "firefox-completed",
    "firefox-interrupted",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _run(command: list[str], data: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, input=data, capture_output=True, check=False)


def _decode(data: bytes) -> subprocess.CompletedProcess[bytes]:
    return _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "pipe:1",
        ],
        data,
    )


def _probe(data: bytes) -> dict[str, Any]:
    result = _run(
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
            "pipe:0",
        ],
        data,
    )
    _require(result.returncode == 0, f"ffprobe failed: {result.stderr.decode()}")
    return json.loads(result.stdout)


def _samples(pcm: bytes) -> tuple[int, ...]:
    return tuple(value[0] for value in struct.iter_unpack("<h", pcm))


def _maximum_near_silent_run(samples: tuple[int, ...]) -> int:
    longest = current = 0
    for sample in samples:
        current = current + 1 if abs(sample) <= 1 else 0
        longest = max(longest, current)
    return longest


def _rms(samples: tuple[int, ...]) -> float:
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def _verify_provenance() -> dict[str, Any]:
    provenance = json.loads((FIXTURE_ROOT / "provenance.json").read_text(encoding="utf-8"))
    declared = set(provenance["files"])
    present = {
        str(path.relative_to(FIXTURE_ROOT))
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path.parent != FIXTURE_ROOT
    }
    _require(present == declared, "fixture files differ from the product provenance manifest")
    for relative_path, expected_hash in provenance["files"].items():
        actual_hash = _sha256((FIXTURE_ROOT / relative_path).read_bytes())
        _require(actual_hash == expected_hash, f"product provenance mismatch: {relative_path}")
    return provenance


def _read_case(case_name: str) -> tuple[dict[str, Any], list[bytes]]:
    directory = FIXTURE_ROOT / case_name
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    frames = metadata["frames"]
    _require(
        [frame["sequence"] for frame in frames] == list(range(len(frames))),
        f"{case_name}: frame sequence is not contiguous from zero",
    )
    frame_bytes = []
    for frame in frames:
        data = (directory / frame["file"]).read_bytes()
        _require(len(data) == frame["bytes"], f"{case_name}: frame byte count changed")
        _require(_sha256(data) == frame["sha256"], f"{case_name}: frame hash changed")
        frame_bytes.append(data)
    return metadata, frame_bytes


def _reject_altered_assembly(
    label: str,
    candidate: bytes,
    approved_byte_hashes: set[str],
    approved_pcm_hashes: set[str],
) -> None:
    _require(
        _sha256(candidate) not in approved_byte_hashes,
        f"{label}: altered bytes were approved",
    )
    decoded = _decode(candidate)
    if decoded.returncode == 0:
        _require(
            _sha256(decoded.stdout) not in approved_pcm_hashes,
            f"{label}: altered audio content was approved",
        )


def _verify_case(
    case_name: str,
    expected: dict[str, Any],
    duration_tolerance: float,
    approved_byte_hashes: set[str],
    approved_pcm_hashes: set[str],
) -> tuple[dict[str, Any], list[bytes]]:
    metadata, frames = _read_case(case_name)
    assembled = b"".join(frames)
    joined = (FIXTURE_ROOT / case_name / metadata["joined"]["file"]).read_bytes()
    _require(assembled == joined, f"{case_name}: joined file is not the ordered raw bytes")
    _require(len(assembled) == metadata["joined"]["bytes"], f"{case_name}: joined size changed")
    _require(
        _sha256(assembled) == metadata["joined"]["sha256"],
        f"{case_name}: joined hash changed",
    )

    probe = _probe(assembled)
    _require(len(probe["streams"]) == 1, f"{case_name}: expected one audio stream")
    stream = probe["streams"][0]
    _require(stream["codec_name"] == "opus", f"{case_name}: codec is not Opus")
    _require(int(stream["channels"]) == 1, f"{case_name}: audio is not mono")
    sample_rate = int(stream["sample_rate"])
    _require(sample_rate == 48_000, f"{case_name}: unexpected decoded sample rate")

    decoded = _decode(assembled)
    _require(decoded.returncode == 0, f"{case_name}: decode failed: {decoded.stderr.decode()}")
    pcm = decoded.stdout
    samples = _samples(pcm)
    _require(
        len(samples) == expected["decodedSamplesPerChannel"],
        f"{case_name}: decoded sample count changed",
    )
    _require(
        _sha256(pcm) == expected["decodedPcmS16leSha256"],
        f"{case_name}: decoded content changed",
    )
    decoded_seconds = len(samples) / sample_rate
    requested_seconds = metadata["requestedDurationMs"] / 1_000
    _require(
        abs(decoded_seconds - requested_seconds) <= duration_tolerance,
        f"{case_name}: decoded duration is outside the documented tolerance",
    )
    _require(
        len(samples) == metadata["joined"]["decoded"]["decodedSamplesPerChannel"],
        f"{case_name}: product decode evidence changed",
    )

    if expected.get("expectedSignal") == "silence":
        _require(not any(samples), f"{case_name}: Firefox synthetic silence changed")
    else:
        _require(
            _maximum_near_silent_run(samples) == expected["maximumNearSilentRunSamples"],
            f"{case_name}: decoded continuity changed",
        )
        tail = samples[-sample_rate // 10 :]
        _require(_rms(tail) >= expected["minimumTail100msRms"], f"{case_name}: tail was lost")

    timeslice_ms = metadata["requestedTimesliceMs"]
    _require(metadata["delayedAtMs"] >= timeslice_ms + 400, f"{case_name}: no delayed event")
    _require(
        metadata["frames"][0]["emittedAtMs"] >= timeslice_ms + 400,
        f"{case_name}: first frame was not delayed",
    )
    _require(
        metadata["frames"][-1]["emittedAtMs"] - metadata["frames"][-2]["emittedAtMs"]
        < timeslice_ms,
        f"{case_name}: final frame is not a partial tail",
    )

    reversed_decode = _decode(b"".join(reversed(frames)))
    _require(reversed_decode.returncode != 0, f"{case_name}: reversed frames unexpectedly decoded")
    _reject_altered_assembly(
        f"{case_name} duplicate",
        frames[0] + assembled,
        approved_byte_hashes,
        approved_pcm_hashes,
    )
    omitted_index = 1 if len(frames) > 2 else len(frames) - 1
    _reject_altered_assembly(
        f"{case_name} omitted frame",
        b"".join(frame for index, frame in enumerate(frames) if index != omitted_index),
        approved_byte_hashes,
        approved_pcm_hashes,
    )
    return metadata, frames


def verify() -> list[str]:
    for executable in ("ffmpeg", "ffprobe"):
        _require(
            shutil.which(executable) is not None,
            f"required executable not found: {executable}",
        )
    provenance = _verify_provenance()
    verification = json.loads((FIXTURE_ROOT / "verification.json").read_text(encoding="utf-8"))
    expected_cases = verification["cases"]
    _require(set(expected_cases) == set(CASE_NAMES), "verification case list changed")
    approved_byte_hashes = {
        json.loads((FIXTURE_ROOT / name / "metadata.json").read_text())["joined"]["sha256"]
        for name in CASE_NAMES
    }
    approved_pcm_hashes = {case["decodedPcmS16leSha256"] for case in expected_cases.values()}
    cases: dict[str, tuple[dict[str, Any], list[bytes]]] = {}
    for case_name in CASE_NAMES:
        cases[case_name] = _verify_case(
            case_name,
            expected_cases[case_name],
            verification["durationToleranceSeconds"],
            approved_byte_hashes,
            approved_pcm_hashes,
        )

    for browser in ("chromium", "firefox"):
        _, completed_frames = cases[f"{browser}-completed"]
        _, interrupted_frames = cases[f"{browser}-interrupted"]
        mixed = completed_frames[0] + b"".join(interrupted_frames[1:])
        mixed_decode = _decode(mixed)
        _require(mixed_decode.returncode == 0, f"{browser}: mixed-run control did not decode")
        _reject_altered_assembly(
            f"{browser} mixed recorder runs",
            mixed,
            approved_byte_hashes,
            approved_pcm_hashes,
        )

    source = f"{provenance['sourceRepository']}@{provenance['sourceCommit']}"
    return [f"verified {name}" for name in CASE_NAMES] + [f"source {source}"]


def main() -> None:
    for result in verify():
        print(result)


if __name__ == "__main__":
    main()
