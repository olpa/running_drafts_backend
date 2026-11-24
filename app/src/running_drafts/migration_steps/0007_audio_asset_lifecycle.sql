CREATE TABLE audio_assets_next (
    id TEXT PRIMARY KEY NOT NULL,
    section_id TEXT NOT NULL UNIQUE REFERENCES sections(id) ON DELETE CASCADE,
    media_type TEXT NOT NULL CHECK (media_type = 'audio/webm;codecs=opus'),
    is_sealed INTEGER NOT NULL DEFAULT 0 CHECK (is_sealed IN (0, 1)),
    seal_reason TEXT CHECK (seal_reason IN ('completed', 'interrupted', 'user_resolved')),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    next_expected_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (next_expected_sequence >= 0),
    observed_duration_ms INTEGER NOT NULL DEFAULT 0
        CHECK (observed_duration_ms >= 0),
    stored_bytes INTEGER NOT NULL DEFAULT 0 CHECK (stored_bytes >= 0),
    playback_parse_offset INTEGER NOT NULL DEFAULT 0
        CHECK (playback_parse_offset >= 0),
    playback_parser_phase TEXT NOT NULL DEFAULT 'start'
        CHECK (playback_parser_phase IN ('start', 'top', 'cluster')),
    playback_cluster_end INTEGER CHECK (playback_cluster_end >= 0),
    playback_packet_count INTEGER NOT NULL DEFAULT 0
        CHECK (playback_packet_count >= 0),
    playback_duration_samples INTEGER NOT NULL DEFAULT 0
        CHECK (playback_duration_samples >= 0),
    playback_cluster_count INTEGER NOT NULL DEFAULT 0
        CHECK (playback_cluster_count >= 0),
    playback_cluster_bytes INTEGER NOT NULL DEFAULT 0
        CHECK (playback_cluster_bytes >= 0),
    playback_source_track_number INTEGER,
    playback_codec_private BLOB,
    playback_sample_rate REAL,
    playback_channels INTEGER,
    playback_codec_delay_ns INTEGER,
    playback_seek_pre_roll_ns INTEGER,
    CHECK (
        (is_sealed = 0 AND seal_reason IS NULL)
        OR
        (is_sealed = 1 AND seal_reason IS NOT NULL)
    )
);

INSERT INTO audio_assets_next (
    id, section_id, media_type, is_sealed, seal_reason,
    created_at_ms, updated_at_ms,
    next_expected_sequence, observed_duration_ms, stored_bytes,
    playback_parse_offset, playback_parser_phase, playback_cluster_end,
    playback_packet_count, playback_duration_samples,
    playback_cluster_count, playback_cluster_bytes,
    playback_source_track_number, playback_codec_private,
    playback_sample_rate, playback_channels, playback_codec_delay_ns,
    playback_seek_pre_roll_ns
)
SELECT
    audio_assets.id,
    audio_assets.section_id,
    audio_assets.media_type,
    audio_assets.is_sealed,
    audio_assets.seal_reason,
    projects.created_at_ms,
    CASE
        WHEN audio_assets.is_sealed = 1 THEN audio_assets.sealed_at_ms
        ELSE projects.updated_at_ms
    END,
    audio_assets.next_expected_sequence,
    audio_assets.observed_duration_ms,
    audio_assets.stored_bytes,
    audio_assets.playback_parse_offset,
    audio_assets.playback_parser_phase,
    audio_assets.playback_cluster_end,
    audio_assets.playback_packet_count,
    audio_assets.playback_duration_samples,
    audio_assets.playback_cluster_count,
    audio_assets.playback_cluster_bytes,
    audio_assets.playback_source_track_number,
    audio_assets.playback_codec_private,
    audio_assets.playback_sample_rate,
    audio_assets.playback_channels,
    audio_assets.playback_codec_delay_ns,
    audio_assets.playback_seek_pre_roll_ns
FROM audio_assets
JOIN sections ON sections.id = audio_assets.section_id
JOIN projects ON projects.id = sections.project_id;

DROP TABLE audio_assets;
ALTER TABLE audio_assets_next RENAME TO audio_assets;
