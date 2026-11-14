import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from running_drafts.webm_playback import (
    OpusTrack,
    cluster_prefix,
    cue_header,
    cue_point,
    playback_prefix,
    playback_total_length,
    segment_fixed_prefix_length,
    simple_block_prefix,
)


@dataclass(frozen=True)
class PlaybackSnapshot:
    audio_asset_id: str
    frame_count: int
    etag: str
    total_length: int
    duration_samples: int
    cluster_count: int
    cluster_bytes: int
    track: OpusTrack | None
    empty_bytes: bytes | None = None


def snapshot_length(
    track: OpusTrack | None,
    duration_samples: int,
    cluster_count: int,
    cluster_bytes: int,
    empty_bytes: bytes,
) -> int:
    if track is None or cluster_count == 0:
        return len(empty_bytes)
    return playback_total_length(track, duration_samples, cluster_count, cluster_bytes)


def _overlap(value: bytes, value_start: int, start: int, end: int) -> bytes | None:
    overlap_start = max(start, value_start)
    overlap_end = min(end, value_start + len(value))
    if overlap_start >= overlap_end:
        return None
    return value[overlap_start - value_start : overlap_end - value_start]


def _source_bytes(
    connection: sqlite3.Connection,
    audio_asset_id: str,
    source_offset: int,
    length: int,
) -> Iterator[bytes]:
    remaining = length
    offset = source_offset
    while remaining:
        row = connection.execute(
            """
            SELECT rowid AS blob_rowid, source_start, length(payload) AS payload_size
            FROM transport_frames
            WHERE audio_asset_id = ?
              AND source_start <= ?
              AND source_start + length(payload) > ?
            ORDER BY source_start DESC
            LIMIT 1
            """,
            (audio_asset_id, offset, offset),
        ).fetchone()
        if row is None:
            raise RuntimeError("playback packet source bytes are missing")
        available = row["payload_size"] - (offset - row["source_start"])
        amount = min(remaining, available)
        with connection.blobopen(
            "transport_frames",
            "payload",
            row["blob_rowid"],
            readonly=True,
        ) as blob:
            blob.seek(offset - row["source_start"])
            while amount:
                chunk = blob.read(min(amount, 64 * 1024))
                if not chunk:
                    raise RuntimeError("playback packet source BLOB ended early")
                yield chunk
                amount -= len(chunk)
                remaining -= len(chunk)
                offset += len(chunk)


def iter_snapshot_range(
    connection: sqlite3.Connection,
    snapshot: PlaybackSnapshot,
    start: int,
    end: int,
) -> Iterator[bytes]:
    if not 0 <= start <= end <= snapshot.total_length:
        raise ValueError("invalid playback byte interval")
    if start == end:
        return
    if snapshot.empty_bytes is not None:
        yield snapshot.empty_bytes[start:end]
        return
    track = snapshot.track
    if track is None:
        raise RuntimeError("non-empty playback snapshot has no track")

    prefix = playback_prefix(
        track,
        snapshot.duration_samples,
        snapshot.cluster_count,
        snapshot.cluster_bytes,
    )
    piece = _overlap(prefix, 0, start, end)
    if piece:
        yield piece

    cues_start = len(prefix)
    cues_header = cue_header(snapshot.cluster_count)
    piece = _overlap(cues_header, cues_start, start, end)
    if piece:
        yield piece
    cue_points_start = cues_start + len(cues_header)
    cues_end = cue_points_start + 62 * snapshot.cluster_count
    if start < cues_end and end > cue_points_start:
        first = max(0, (start - cue_points_start) // 62)
        last = min(snapshot.cluster_count, (end - cue_points_start + 61) // 62)
        fixed_length = segment_fixed_prefix_length(track, snapshot.duration_samples)
        for row in connection.execute(
            """
            SELECT cluster_index, start_samples, block_bytes, cumulative_bytes
            FROM playback_clusters
            WHERE audio_asset_id = ? AND cluster_index >= ? AND cluster_index < ?
            ORDER BY cluster_index
            """,
            (snapshot.audio_asset_id, first, last),
        ):
            cluster_size = 29 + row["block_bytes"]
            prior_cluster_bytes = row["cumulative_bytes"] - cluster_size
            cluster_position = (
                fixed_length
                + len(cues_header)
                + 62 * snapshot.cluster_count
                + prior_cluster_bytes
            )
            point = cue_point(row["start_samples"], cluster_position)
            point_start = cue_points_start + 62 * row["cluster_index"]
            piece = _overlap(point, point_start, start, end)
            if piece:
                yield piece

    cluster_region_start = cues_end
    local_start = max(0, start - cluster_region_start)
    local_end = max(0, end - cluster_region_start)
    if local_start >= snapshot.cluster_bytes or local_end <= 0:
        return
    for cluster in connection.execute(
        """
        SELECT cluster_index, start_samples, block_bytes, cumulative_bytes
        FROM playback_clusters
        WHERE audio_asset_id = ?
          AND cluster_index < ?
          AND cumulative_bytes > ?
          AND cumulative_bytes - (29 + block_bytes) < ?
        ORDER BY cluster_index
        """,
        (
            snapshot.audio_asset_id,
            snapshot.cluster_count,
            local_start,
            local_end,
        ),
    ):
        cluster_size = 29 + cluster["block_bytes"]
        prior_cluster_bytes = cluster["cumulative_bytes"] - cluster_size
        cluster_start = cluster_region_start + prior_cluster_bytes
        header = cluster_prefix(cluster["start_samples"], cluster["block_bytes"])
        piece = _overlap(header, cluster_start, start, end)
        if piece:
            yield piece
        blocks_start = cluster_start + len(header)
        for packet in connection.execute(
            """
            SELECT source_offset, payload_size, relative_ticks, block_offset
            FROM playback_packets
            WHERE audio_asset_id = ? AND cluster_index = ?
              AND block_offset + 9 + payload_size > ?
              AND block_offset < ?
            ORDER BY packet_index
            """,
            (
                snapshot.audio_asset_id,
                cluster["cluster_index"],
                max(0, start - blocks_start),
                max(0, end - blocks_start),
            ),
        ):
            block_start = blocks_start + packet["block_offset"]
            block_header = simple_block_prefix(
                packet["relative_ticks"],
                packet["payload_size"],
            )
            piece = _overlap(block_header, block_start, start, end)
            if piece:
                yield piece
            payload_start = block_start + len(block_header)
            payload_overlap_start = max(start, payload_start)
            payload_overlap_end = min(end, payload_start + packet["payload_size"])
            if payload_overlap_start < payload_overlap_end:
                yield from _source_bytes(
                    connection,
                    snapshot.audio_asset_id,
                    packet["source_offset"] + payload_overlap_start - payload_start,
                    payload_overlap_end - payload_overlap_start,
                )
