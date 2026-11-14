import hashlib
import sqlite3
from dataclasses import dataclass

from running_drafts.webm_playback import (
    InvalidWebM,
    OpusTrack,
    PacketReference,
    ParserState,
    scan_webm_append,
)

CLUSTER_MAX_SAMPLES = 48_000 * 10
CLUSTER_FIXED_BYTES = 29
SIMPLE_BLOCK_FIXED_BYTES = 9


def zero_snapshot_etag(audio_asset_id: str) -> str:
    return hashlib.sha256(f"running-drafts-playback-v1\0{audio_asset_id}\0".encode()).hexdigest()


def next_snapshot_etag(
    audio_asset_id: str,
    sequence: int,
    frame_sha256: str,
    previous_etag: str | None,
) -> str:
    prior = previous_etag or zero_snapshot_etag(audio_asset_id)
    digest = hashlib.sha256()
    digest.update(bytes.fromhex(prior))
    digest.update(sequence.to_bytes(8, "big"))
    digest.update(bytes.fromhex(frame_sha256))
    return digest.hexdigest()


class PlaybackIndexInvariant(RuntimeError):
    pass


@dataclass(frozen=True)
class PlaybackIndexState:
    parse_offset: int
    phase: str
    cluster_end: int | None
    packet_count: int
    duration_samples: int
    cluster_count: int
    cluster_bytes: int
    track: OpusTrack | None


def state_from_row(row: sqlite3.Row) -> PlaybackIndexState:
    track = None
    if row["playback_source_track_number"] is not None:
        track = OpusTrack(
            source_track_number=row["playback_source_track_number"],
            codec_private=row["playback_codec_private"],
            sample_rate=row["playback_sample_rate"],
            channels=row["playback_channels"],
            codec_delay_ns=row["playback_codec_delay_ns"],
            seek_pre_roll_ns=row["playback_seek_pre_roll_ns"],
        )
    return PlaybackIndexState(
        parse_offset=row["playback_parse_offset"],
        phase=row["playback_parser_phase"],
        cluster_end=row["playback_cluster_end"],
        packet_count=row["playback_packet_count"],
        duration_samples=row["playback_duration_samples"],
        cluster_count=row["playback_cluster_count"],
        cluster_bytes=row["playback_cluster_bytes"],
        track=track,
    )


def _scan_input(
    connection: sqlite3.Connection,
    audio_asset_id: str,
    sequence: int,
    frame_start: int,
    payload: bytes,
    state: PlaybackIndexState,
) -> tuple[bytes, int]:
    if state.parse_offset == frame_start:
        return payload, frame_start
    if state.parse_offset > frame_start:
        raise PlaybackIndexInvariant("playback parser advanced beyond stored audio")
    if sequence == 0:
        raise PlaybackIndexInvariant("initial playback parser offset is invalid")
    previous = connection.execute(
        """
        SELECT source_start, payload
        FROM transport_frames
        WHERE audio_asset_id = ? AND sequence = ?
        """,
        (audio_asset_id, sequence - 1),
    ).fetchone()
    if previous is None:
        raise PlaybackIndexInvariant("previous Transport frame is missing")
    previous_start = previous["source_start"]
    previous_end = previous_start + len(previous["payload"])
    if not previous_start <= state.parse_offset <= previous_end:
        raise PlaybackIndexInvariant("unparsed WebM element spans more than one frame")
    tail = previous["payload"][state.parse_offset - previous_start :]
    return tail + payload, state.parse_offset


def _clusters(packets: tuple[PacketReference, ...]) -> tuple[tuple[PacketReference, ...], ...]:
    groups: list[list[PacketReference]] = []
    current: list[PacketReference] = []
    samples = 0
    for packet in packets:
        if current and samples >= CLUSTER_MAX_SAMPLES:
            groups.append(current)
            current = []
            samples = 0
        current.append(packet)
        samples += packet.duration_samples
    if current:
        groups.append(current)
    return tuple(tuple(group) for group in groups)


def index_accepted_frame(
    connection: sqlite3.Connection,
    *,
    audio_asset_id: str,
    sequence: int,
    frame_start: int,
    payload: bytes,
    asset: sqlite3.Row,
) -> PlaybackIndexState:
    state = state_from_row(asset)
    scan_input, base_offset = _scan_input(
        connection,
        audio_asset_id,
        sequence,
        frame_start,
        payload,
        state,
    )
    result = scan_webm_append(
        scan_input,
        base_offset,
        ParserState(
            phase=state.phase,  # type: ignore[arg-type]
            parse_offset=state.parse_offset,
            cluster_end=state.cluster_end,
            track=state.track,
        ),
    )
    if sequence == 0 and result.state.track is None:
        raise InvalidWebM("first Transport frame does not establish a WebM/Opus track")
    if result.state.parse_offset < frame_start:
        raise InvalidWebM("incomplete WebM element spans more than one Transport frame")

    packet_index = state.packet_count
    duration_samples = state.duration_samples
    cluster_index = state.cluster_count
    cluster_bytes = state.cluster_bytes
    for group in _clusters(result.packets):
        cluster_start_samples = duration_samples
        block_offset = 0
        first_packet_index = packet_index
        for packet in group:
            relative_ticks = (duration_samples - cluster_start_samples) // 24
            if relative_ticks > 32_767:
                raise InvalidWebM("Transport frame requires an oversized WebM Cluster")
            connection.execute(
                """
                INSERT INTO playback_packets (
                    audio_asset_id, packet_index, completed_sequence, cluster_index,
                    source_offset, payload_size, duration_samples, start_samples,
                    relative_ticks, block_offset
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audio_asset_id,
                    packet_index,
                    sequence,
                    cluster_index,
                    packet.source_offset,
                    packet.size,
                    packet.duration_samples,
                    duration_samples,
                    relative_ticks,
                    block_offset,
                ),
            )
            packet_index += 1
            duration_samples += packet.duration_samples
            block_offset += SIMPLE_BLOCK_FIXED_BYTES + packet.size
        cluster_bytes += CLUSTER_FIXED_BYTES + block_offset
        connection.execute(
            """
            INSERT INTO playback_clusters (
                audio_asset_id, cluster_index, completed_sequence,
                first_packet_index, packet_count, start_samples,
                block_bytes, cumulative_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audio_asset_id,
                cluster_index,
                sequence,
                first_packet_index,
                len(group),
                cluster_start_samples,
                block_offset,
                cluster_bytes,
            ),
        )
        cluster_index += 1

    track = result.state.track
    connection.execute(
        """
        UPDATE audio_assets
        SET playback_parse_offset = ?, playback_parser_phase = ?,
            playback_cluster_end = ?, playback_packet_count = ?,
            playback_duration_samples = ?, playback_cluster_count = ?,
            playback_cluster_bytes = ?, playback_source_track_number = ?,
            playback_codec_private = ?, playback_sample_rate = ?,
            playback_channels = ?, playback_codec_delay_ns = ?,
            playback_seek_pre_roll_ns = ?
        WHERE id = ?
        """,
        (
            result.state.parse_offset,
            result.state.phase,
            result.state.cluster_end,
            packet_index,
            duration_samples,
            cluster_index,
            cluster_bytes,
            None if track is None else track.source_track_number,
            None if track is None else track.codec_private,
            None if track is None else track.sample_rate,
            None if track is None else track.channels,
            None if track is None else track.codec_delay_ns,
            None if track is None else track.seek_pre_roll_ns,
            audio_asset_id,
        ),
    )
    return PlaybackIndexState(
        parse_offset=result.state.parse_offset,
        phase=result.state.phase,
        cluster_end=result.state.cluster_end,
        packet_count=packet_index,
        duration_samples=duration_samples,
        cluster_count=cluster_index,
        cluster_bytes=cluster_bytes,
        track=track,
    )
