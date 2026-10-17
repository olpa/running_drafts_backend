#!/usr/bin/env python3
"""Generate the deterministic zero-sample WebM/Opus download fixture."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

DEFAULT_OUTPUT = Path(__file__).parents[1] / "src/running_drafts/assets/empty-audio.webm"


def _size(value: int) -> bytes:
    """Encode an EBML element size as the shortest variable-width integer."""
    for width in range(1, 9):
        if value < (1 << (7 * width)) - 1:
            return ((1 << (7 * width)) | value).to_bytes(width, "big")
    raise ValueError("EBML element is too large")


def _element(element_id: str, payload: bytes) -> bytes:
    return bytes.fromhex(element_id) + _size(len(payload)) + payload


def _unsigned(value: int) -> bytes:
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def build_empty_webm() -> bytes:
    """Return a valid mono Opus WebM with an empty cluster and no audio packets."""
    ebml = b"".join(
        (
            _element("4286", _unsigned(1)),  # EBMLVersion
            _element("42f7", _unsigned(1)),  # EBMLReadVersion
            _element("42f2", _unsigned(4)),  # EBMLMaxIDLength
            _element("42f3", _unsigned(8)),  # EBMLMaxSizeLength
            _element("4282", b"webm"),  # DocType
            _element("4287", _unsigned(4)),  # DocTypeVersion
            _element("4285", _unsigned(2)),  # DocTypeReadVersion
        )
    )
    info = b"".join(
        (
            _element("2ad7b1", _unsigned(1_000_000)),  # TimestampScale: 1 ms
            _element("4d80", b"Running Drafts"),  # MuxingApp
            _element("5741", b"Running Drafts"),  # WritingApp
        )
    )
    opus_head = (
        b"OpusHead"
        + bytes((1, 1))  # version, channels
        + struct.pack("<H", 312)  # pre-skip
        + struct.pack("<I", 48_000)  # original input rate
        + struct.pack("<h", 0)  # output gain
        + bytes((0,))  # channel mapping family
    )
    audio = _element("b5", struct.pack(">d", 48_000.0)) + _element("9f", _unsigned(1))
    track = b"".join(
        (
            _element("d7", _unsigned(1)),  # TrackNumber
            _element("73c5", _unsigned(1)),  # TrackUID
            _element("83", _unsigned(2)),  # TrackType: audio
            _element("86", b"A_OPUS"),  # CodecID
            _element("63a2", opus_head),  # CodecPrivate
            _element("56aa", _unsigned(6_500_000)),  # CodecDelay
            _element("56bb", _unsigned(80_000_000)),  # SeekPreRoll
            _element("e1", audio),  # Audio
        )
    )
    empty_cluster = _element("1f43b675", _element("e7", _unsigned(0)))
    segment = _element("1549a966", info) + _element("1654ae6b", _element("ae", track))
    segment += empty_cluster
    return _element("1a45dfa3", ebml) + _element("18538067", segment)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(build_empty_webm())


if __name__ == "__main__":
    main()
