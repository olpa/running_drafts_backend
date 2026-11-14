import math
import struct
from dataclasses import dataclass
from typing import Literal


class InvalidWebM(ValueError):
    pass


class TrackMismatch(ValueError):
    pass


@dataclass(frozen=True)
class OpusTrack:
    source_track_number: int
    codec_private: bytes
    sample_rate: float
    channels: int
    codec_delay_ns: int
    seek_pre_roll_ns: int


@dataclass(frozen=True)
class OpusPacket:
    payload: bytes
    duration_samples: int


@dataclass(frozen=True)
class ParsedWebM:
    track: OpusTrack
    packets: tuple[OpusPacket, ...]


@dataclass(frozen=True)
class ParserState:
    phase: Literal["start", "top", "cluster"] = "start"
    parse_offset: int = 0
    cluster_end: int | None = None
    track: OpusTrack | None = None


@dataclass(frozen=True)
class PacketReference:
    source_offset: int
    size: int
    duration_samples: int


@dataclass(frozen=True)
class ScanResult:
    state: ParserState
    packets: tuple[PacketReference, ...]


@dataclass(frozen=True)
class _Element:
    element_id: int
    value_start: int
    value_end: int
    unknown_size: bool


@dataclass(frozen=True)
class _IncrementalElement:
    element_id: int
    value_start: int
    declared_end: int | None


_EBML = 0x1A45DFA3
_SEGMENT = 0x18538067
_INFO = 0x1549A966
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_AUDIO = 0xE1
_CLUSTER = 0x1F43B675
_TIMECODE = 0xE7
_SIMPLE_BLOCK = 0xA3
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1

_TOP_LEVEL_IDS = {_INFO, _TRACKS, _CLUSTER, 0x1C53BB6B, 0x114D9B74, 0x1254C367}


def _vint_length(first: int, maximum: int) -> int:
    marker = 0x80
    for length in range(1, maximum + 1):
        if first & marker:
            return length
        marker >>= 1
    raise InvalidWebM("invalid EBML variable-length integer")


def _read_id(data: bytes, offset: int, end: int) -> tuple[int, int]:
    if offset >= end:
        raise InvalidWebM("truncated EBML element ID")
    length = _vint_length(data[offset], 4)
    if offset + length > end:
        raise InvalidWebM("truncated EBML element ID")
    return int.from_bytes(data[offset : offset + length]), offset + length


def _read_size(data: bytes, offset: int, end: int) -> tuple[int, int, bool]:
    if offset >= end:
        raise InvalidWebM("truncated EBML element size")
    length = _vint_length(data[offset], 8)
    if offset + length > end:
        raise InvalidWebM("truncated EBML element size")
    encoded = int.from_bytes(data[offset : offset + length])
    marker = 1 << (7 * length)
    value = encoded ^ marker
    unknown = value == marker - 1
    return value, offset + length, unknown


def _read_element(data: bytes, offset: int, end: int) -> tuple[_Element, int]:
    element_id, after_id = _read_id(data, offset, end)
    size, value_start, unknown = _read_size(data, after_id, end)
    value_end = end if unknown else value_start + size
    if value_end > end:
        raise InvalidWebM("truncated EBML element payload")
    return _Element(element_id, value_start, value_end, unknown), value_end


def _try_incremental_element(
    data: bytes,
    offset: int,
    end: int,
) -> _IncrementalElement | None:
    if offset >= end:
        return None
    try:
        id_length = _vint_length(data[offset], 4)
    except InvalidWebM:
        raise
    if offset + id_length >= end:
        return None
    element_id = int.from_bytes(data[offset : offset + id_length])
    size_offset = offset + id_length
    try:
        size_length = _vint_length(data[size_offset], 8)
    except InvalidWebM:
        raise
    if size_offset + size_length > end:
        return None
    encoded = int.from_bytes(data[size_offset : size_offset + size_length])
    marker = 1 << (7 * size_length)
    value = encoded ^ marker
    value_start = size_offset + size_length
    if value == marker - 1:
        return _IncrementalElement(element_id, value_start, None)
    return _IncrementalElement(element_id, value_start, value_start + value)


def _children(data: bytes, start: int, end: int) -> tuple[_Element, ...]:
    result: list[_Element] = []
    offset = start
    while offset < end:
        element, next_offset = _read_element(data, offset, end)
        if element.unknown_size:
            raise InvalidWebM("unexpected unknown-size nested EBML element")
        result.append(element)
        offset = next_offset
    return tuple(result)


def _unsigned(data: bytes, element: _Element) -> int:
    size = element.value_end - element.value_start
    if not 1 <= size <= 8:
        raise InvalidWebM("invalid unsigned EBML value")
    return int.from_bytes(data[element.value_start : element.value_end])


def _float(data: bytes, element: _Element) -> float:
    value = data[element.value_start : element.value_end]
    if len(value) == 4:
        return struct.unpack(">f", value)[0]
    if len(value) == 8:
        return struct.unpack(">d", value)[0]
    raise InvalidWebM("invalid EBML floating-point value")


def _opus_packet_duration_samples(packet: bytes) -> int:
    if not packet:
        raise InvalidWebM("empty Opus packet")
    toc = packet[0]
    if toc & 0x80:
        samples_per_frame = (48_000 << ((toc >> 3) & 0x3)) // 400
    elif toc & 0x60 == 0x60:
        samples_per_frame = 48_000 // (50 if toc & 0x08 else 100)
    else:
        samples_per_frame = min(
            (48_000 << ((toc >> 3) & 0x3)) // 100,
            48_000 * 60 // 1_000,
        )
    frame_code = toc & 0x03
    if frame_code == 0:
        frame_count = 1
    elif frame_code in {1, 2}:
        frame_count = 2
    else:
        if len(packet) < 2:
            raise InvalidWebM("truncated Opus frame-count byte")
        frame_count = packet[1] & 0x3F
    duration = samples_per_frame * frame_count
    if frame_count == 0 or duration > 48_000 * 120 // 1_000:
        raise InvalidWebM("invalid Opus packet duration")
    return duration


def _parse_block(data: bytes, element: _Element, source_track_number: int) -> OpusPacket:
    offset = element.value_start
    track, payload_start, unknown = _read_size(data, offset, element.value_end)
    if unknown or track != source_track_number:
        raise InvalidWebM("WebM Block references an unexpected track")
    if payload_start + 3 > element.value_end:
        raise InvalidWebM("truncated WebM Block")
    flags = data[payload_start + 2]
    if flags & 0x06:
        raise InvalidWebM("laced WebM Blocks are unsupported")
    packet = data[payload_start + 3 : element.value_end]
    return OpusPacket(packet, _opus_packet_duration_samples(packet))


def _parse_block_reference(
    data: bytes,
    element: _Element,
    source_track_number: int,
    base_offset: int,
) -> PacketReference:
    track, payload_start, unknown = _read_size(data, element.value_start, element.value_end)
    if unknown or track != source_track_number:
        raise InvalidWebM("WebM Block references an unexpected track")
    if payload_start + 3 > element.value_end:
        raise InvalidWebM("truncated WebM Block")
    flags = data[payload_start + 2]
    if flags & 0x06:
        raise InvalidWebM("laced WebM Blocks are unsupported")
    packet_start = payload_start + 3
    packet = data[packet_start : element.value_end]
    return PacketReference(
        source_offset=base_offset + packet_start,
        size=len(packet),
        duration_samples=_opus_packet_duration_samples(packet),
    )


def _parse_track(data: bytes, tracks: _Element) -> OpusTrack:
    entries = [
        child
        for child in _children(data, tracks.value_start, tracks.value_end)
        if child.element_id == _TRACK_ENTRY
    ]
    if len(entries) != 1:
        raise InvalidWebM("WebM must contain exactly one track")
    values = {
        child.element_id: child
        for child in _children(data, entries[0].value_start, entries[0].value_end)
    }
    source_track_number = _unsigned(data, values[0xD7])
    if source_track_number <= 0:
        raise InvalidWebM("invalid WebM TrackNumber")
    if _unsigned(data, values[0x83]) != 2:
        raise InvalidWebM("WebM track must be audio")
    codec = data[values[0x86].value_start : values[0x86].value_end]
    if codec != b"A_OPUS":
        raise InvalidWebM("WebM audio track must use Opus")
    private_element = values.get(0x63A2)
    if private_element is None:
        raise InvalidWebM("Opus track has no CodecPrivate")
    codec_private = data[private_element.value_start : private_element.value_end]
    if not codec_private.startswith(b"OpusHead") or len(codec_private) < 19:
        raise InvalidWebM("invalid Opus CodecPrivate")
    audio = values.get(_AUDIO)
    if audio is None:
        raise InvalidWebM("Opus track has no Audio settings")
    audio_values = {
        child.element_id: child
        for child in _children(data, audio.value_start, audio.value_end)
    }
    sample_rate = _float(data, audio_values[0xB5])
    channels = _unsigned(data, audio_values[0x9F])
    if not math.isfinite(sample_rate) or sample_rate <= 0 or not 1 <= channels <= 255:
        raise InvalidWebM("invalid Opus Audio settings")
    codec_delay = _unsigned(data, values[0x56AA]) if 0x56AA in values else 0
    seek_pre_roll = _unsigned(data, values[0x56BB]) if 0x56BB in values else 80_000_000
    return OpusTrack(
        source_track_number,
        codec_private,
        sample_rate,
        channels,
        codec_delay,
        seek_pre_roll,
    )


def parse_webm(data: bytes) -> ParsedWebM:
    end = len(data)
    ebml, offset = _read_element(data, 0, end)
    if ebml.element_id != _EBML or ebml.unknown_size:
        raise InvalidWebM("missing finite EBML header")
    segment, _ = _read_element(data, offset, end)
    if segment.element_id != _SEGMENT:
        raise InvalidWebM("missing WebM Segment")

    offset = segment.value_start
    track: OpusTrack | None = None
    packets: list[OpusPacket] = []
    while offset < end:
        element, next_offset = _read_element(data, offset, end)
        if element.element_id == _TRACKS:
            if track is not None:
                raise InvalidWebM("duplicate WebM Tracks")
            track = _parse_track(data, element)
        elif element.element_id == _CLUSTER:
            if track is None:
                raise InvalidWebM("WebM Cluster precedes Tracks")
            cluster_end = element.value_end
            child_offset = element.value_start
            while child_offset < cluster_end:
                child_id, _ = _read_id(data, child_offset, cluster_end)
                if element.unknown_size and child_id in _TOP_LEVEL_IDS:
                    next_offset = child_offset
                    break
                child, after_child = _read_element(data, child_offset, cluster_end)
                if child.element_id == _SIMPLE_BLOCK:
                    packets.append(_parse_block(data, child, track.source_track_number))
                elif child.element_id == _BLOCK_GROUP:
                    block = next(
                        (
                            nested
                            for nested in _children(data, child.value_start, child.value_end)
                            if nested.element_id == _BLOCK
                        ),
                        None,
                    )
                    if block is not None:
                        packets.append(_parse_block(data, block, track.source_track_number))
                child_offset = after_child
        offset = next_offset
    if track is None:
        raise InvalidWebM("WebM has no Tracks")
    if not packets:
        raise InvalidWebM("WebM has no complete Opus packets")
    return ParsedWebM(track, tuple(packets))


def scan_webm_append(data: bytes, base_offset: int, state: ParserState) -> ScanResult:
    """Scan complete WebM elements from a bounded suffix of an appended stream."""
    end = len(data)
    offset = state.parse_offset - base_offset
    if offset < 0 or offset > end:
        raise InvalidWebM("parser offset is outside the supplied suffix")
    phase = state.phase
    cluster_end = state.cluster_end
    track = state.track
    packets: list[PacketReference] = []

    if phase == "start":
        ebml = _try_incremental_element(data, offset, end)
        if ebml is None or ebml.declared_end is None or ebml.declared_end > end:
            return ScanResult(state, ())
        if ebml.element_id != _EBML:
            raise InvalidWebM("missing finite EBML header")
        segment = _try_incremental_element(data, ebml.declared_end, end)
        if segment is None:
            return ScanResult(state, ())
        if segment.element_id != _SEGMENT:
            raise InvalidWebM("missing WebM Segment")
        offset = segment.value_start
        phase = "top"

    while offset < end:
        global_offset = base_offset + offset
        if phase == "cluster" and cluster_end is not None and global_offset >= cluster_end:
            if global_offset != cluster_end:
                raise InvalidWebM("WebM Cluster exceeds its declared size")
            phase = "top"
            cluster_end = None
            continue

        candidate = _try_incremental_element(data, offset, end)
        if candidate is None:
            break

        if phase == "cluster" and cluster_end is None and candidate.element_id in _TOP_LEVEL_IDS:
            phase = "top"
            continue

        if phase == "top":
            if candidate.element_id == _CLUSTER:
                phase = "cluster"
                cluster_end = (
                    None
                    if candidate.declared_end is None
                    else base_offset + candidate.declared_end
                )
                offset = candidate.value_start
                continue
            if candidate.declared_end is None:
                raise InvalidWebM("unexpected unknown-size top-level element")
            if candidate.declared_end > end:
                break
            complete = _Element(
                candidate.element_id,
                candidate.value_start,
                candidate.declared_end,
                False,
            )
            if candidate.element_id == _TRACKS:
                parsed_track = _parse_track(data, complete)
                if track is not None and parsed_track != track:
                    raise TrackMismatch("WebM track configuration changed")
                track = parsed_track
            elif candidate.element_id == _EBML:
                raise InvalidWebM("a new WebM stream cannot extend an Audio Asset")
            offset = candidate.declared_end
            continue

        if candidate.declared_end is None:
            raise InvalidWebM("unexpected unknown-size Cluster child")
        if candidate.declared_end > end:
            break
        complete = _Element(
            candidate.element_id,
            candidate.value_start,
            candidate.declared_end,
            False,
        )
        if candidate.element_id == _SIMPLE_BLOCK:
            if track is None:
                raise InvalidWebM("WebM Cluster precedes Tracks")
            packets.append(
                _parse_block_reference(data, complete, track.source_track_number, base_offset)
            )
        elif candidate.element_id == _BLOCK_GROUP:
            if track is None:
                raise InvalidWebM("WebM Cluster precedes Tracks")
            block = next(
                (
                    nested
                    for nested in _children(data, complete.value_start, complete.value_end)
                    if nested.element_id == _BLOCK
                ),
                None,
            )
            if block is not None:
                packets.append(
                    _parse_block_reference(data, block, track.source_track_number, base_offset)
                )
        offset = candidate.declared_end

    return ScanResult(
        ParserState(
            phase=phase,
            parse_offset=base_offset + offset,
            cluster_end=cluster_end,
            track=track,
        ),
        tuple(packets),
    )


def _id(value: int) -> bytes:
    width = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(width, "big")


def _size(value: int, width: int = 8) -> bytes:
    if value < 0 or value >= (1 << (7 * width)) - 1:
        raise ValueError("EBML element is too large")
    return ((1 << (7 * width)) | value).to_bytes(width, "big")


def _element(element_id: int, value: bytes, *, size_width: int = 8) -> bytes:
    return _id(element_id) + _size(len(value), size_width) + value


def _uint(element_id: int, value: int, width: int = 8) -> bytes:
    return _element(element_id, value.to_bytes(width, "big"))


def _float64(element_id: int, value: float) -> bytes:
    return _element(element_id, struct.pack(">d", value))


def _canonical_header(track: OpusTrack, duration_ticks: int) -> tuple[bytes, bytes]:
    ebml = _element(
        _EBML,
        _uint(0x4286, 1, 1)
        + _uint(0x42F7, 1, 1)
        + _uint(0x42F2, 4, 1)
        + _uint(0x42F3, 8, 1)
        + _element(0x4282, b"webm")
        + _uint(0x4287, 4, 1)
        + _uint(0x4285, 2, 1),
    )
    info = _element(
        _INFO,
        _uint(0x2AD7B1, 500_000, 4)
        + _float64(0x4489, float(duration_ticks))
        + _element(0x4D80, b"Running Drafts v1")
        + _element(0x5741, b"Running Drafts v1"),
    )
    audio = _element(
        _AUDIO,
        _float64(0xB5, track.sample_rate) + _uint(0x9F, track.channels, 1),
    )
    entry = _element(
        _TRACK_ENTRY,
        _uint(0xD7, 1, 1)
        + _uint(0x73C5, 1, 8)
        + _uint(0x83, 2, 1)
        + _uint(0x9C, 0, 1)
        + _element(0x86, b"A_OPUS")
        + _element(0x63A2, track.codec_private)
        + _uint(0x56AA, track.codec_delay_ns, 8)
        + _uint(0x56BB, track.seek_pre_roll_ns, 8)
        + audio,
    )
    return ebml, info + _element(_TRACKS, entry)


def _simple_block(relative_ticks: int, packet: bytes) -> bytes:
    if not -32_768 <= relative_ticks <= 32_767:
        raise ValueError("cluster-relative timestamp is out of range")
    payload = b"\x81" + struct.pack(">hB", relative_ticks, 0x80) + packet
    return _element(_SIMPLE_BLOCK, payload, size_width=4)


def simple_block_prefix(relative_ticks: int, payload_size: int) -> bytes:
    if not -32_768 <= relative_ticks <= 32_767:
        raise ValueError("cluster-relative timestamp is out of range")
    value_size = 4 + payload_size
    return (
        _id(_SIMPLE_BLOCK)
        + _size(value_size, 4)
        + b"\x81"
        + struct.pack(">hB", relative_ticks, 0x80)
    )


def cluster_prefix(start_samples: int, block_bytes: int) -> bytes:
    ticks = start_samples // 24
    timecode = _uint(_TIMECODE, ticks, 8)
    return _id(_CLUSTER) + _size(len(timecode) + block_bytes) + timecode


def cue_header(cluster_count: int) -> bytes:
    return _id(0x1C53BB6B) + _size(62 * cluster_count)


def cue_point(start_samples: int, cluster_position: int) -> bytes:
    ticks = start_samples // 24
    track_position = _element(0xB7, _uint(0xF7, 1, 1) + _uint(0xF1, cluster_position, 8))
    return _element(0xBB, _uint(0xB3, ticks, 8) + track_position)


def playback_prefix(
    track: OpusTrack,
    duration_samples: int,
    cluster_count: int,
    cluster_bytes: int,
) -> bytes:
    duration_ticks = duration_samples // 24
    ebml, fixed_prefix = _canonical_header(track, duration_ticks)
    cues_bytes = len(cue_header(cluster_count)) + 62 * cluster_count
    segment_payload_size = len(fixed_prefix) + cues_bytes + cluster_bytes
    return ebml + _id(_SEGMENT) + _size(segment_payload_size) + fixed_prefix


def segment_fixed_prefix_length(track: OpusTrack, duration_samples: int) -> int:
    return len(_canonical_header(track, duration_samples // 24)[1])


def playback_total_length(
    track: OpusTrack,
    duration_samples: int,
    cluster_count: int,
    cluster_bytes: int,
) -> int:
    return (
        len(playback_prefix(track, duration_samples, cluster_count, cluster_bytes))
        + len(cue_header(cluster_count))
        + 62 * cluster_count
        + cluster_bytes
    )


def _clusters(parsed: ParsedWebM) -> tuple[tuple[int, bytes], ...]:
    result: list[tuple[int, bytes]] = []
    start_samples = 0
    cluster_start = 0
    blocks: list[bytes] = []
    for packet in parsed.packets:
        if blocks and start_samples - cluster_start >= 48_000 * 10:
            ticks = cluster_start // 24
            cluster = _element(_CLUSTER, _uint(_TIMECODE, ticks, 8) + b"".join(blocks))
            result.append((ticks, cluster))
            cluster_start = start_samples
            blocks = []
        relative_ticks = (start_samples - cluster_start) // 24
        blocks.append(_simple_block(relative_ticks, packet.payload))
        start_samples += packet.duration_samples
    ticks = cluster_start // 24
    result.append((ticks, _element(_CLUSTER, _uint(_TIMECODE, ticks, 8) + b"".join(blocks))))
    return tuple(result)


def _cues(cluster_ticks: tuple[int, ...], positions: tuple[int, ...]) -> bytes:
    points = []
    for ticks, position in zip(cluster_ticks, positions, strict=True):
        track_position = _element(0xB7, _uint(0xF7, 1, 1) + _uint(0xF1, position, 8))
        points.append(_element(0xBB, _uint(0xB3, ticks, 8) + track_position))
    return _element(0x1C53BB6B, b"".join(points))


def build_playback_webm(parsed: ParsedWebM) -> bytes:
    duration_samples = sum(packet.duration_samples for packet in parsed.packets)
    duration_ticks = duration_samples // 24
    ebml, fixed_prefix = _canonical_header(parsed.track, duration_ticks)
    clusters = _clusters(parsed)
    placeholder_cues = _cues(
        tuple(ticks for ticks, _cluster in clusters),
        tuple(0 for _cluster in clusters),
    )
    cluster_positions: list[int] = []
    position = len(fixed_prefix) + len(placeholder_cues)
    for _ticks, cluster in clusters:
        cluster_positions.append(position)
        position += len(cluster)
    cues = _cues(
        tuple(ticks for ticks, _cluster in clusters),
        tuple(cluster_positions),
    )
    segment_value = fixed_prefix + cues + b"".join(cluster for _ticks, cluster in clusters)
    return ebml + _element(_SEGMENT, segment_value)
