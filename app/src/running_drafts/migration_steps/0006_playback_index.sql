ALTER TABLE transport_frames
ADD COLUMN source_start INTEGER NOT NULL DEFAULT 0 CHECK (source_start >= 0);

ALTER TABLE transport_frames
ADD COLUMN playback_etag TEXT CHECK (
    playback_etag IS NULL OR (
        length(playback_etag) = 64
        AND playback_etag = lower(playback_etag)
        AND playback_etag NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX transport_frames_playback_source
ON transport_frames(audio_asset_id, source_start);

ALTER TABLE audio_assets
ADD COLUMN playback_parse_offset INTEGER NOT NULL DEFAULT 0
    CHECK (playback_parse_offset >= 0);

ALTER TABLE audio_assets
ADD COLUMN playback_parser_phase TEXT NOT NULL DEFAULT 'start'
    CHECK (playback_parser_phase IN ('start', 'top', 'cluster'));

ALTER TABLE audio_assets
ADD COLUMN playback_cluster_end INTEGER CHECK (playback_cluster_end >= 0);

ALTER TABLE audio_assets
ADD COLUMN playback_packet_count INTEGER NOT NULL DEFAULT 0
    CHECK (playback_packet_count >= 0);

ALTER TABLE audio_assets
ADD COLUMN playback_duration_samples INTEGER NOT NULL DEFAULT 0
    CHECK (playback_duration_samples >= 0);

ALTER TABLE audio_assets
ADD COLUMN playback_cluster_count INTEGER NOT NULL DEFAULT 0
    CHECK (playback_cluster_count >= 0);

ALTER TABLE audio_assets
ADD COLUMN playback_cluster_bytes INTEGER NOT NULL DEFAULT 0
    CHECK (playback_cluster_bytes >= 0);

ALTER TABLE audio_assets ADD COLUMN playback_source_track_number INTEGER;
ALTER TABLE audio_assets ADD COLUMN playback_codec_private BLOB;
ALTER TABLE audio_assets ADD COLUMN playback_sample_rate REAL;
ALTER TABLE audio_assets ADD COLUMN playback_channels INTEGER;
ALTER TABLE audio_assets ADD COLUMN playback_codec_delay_ns INTEGER;
ALTER TABLE audio_assets ADD COLUMN playback_seek_pre_roll_ns INTEGER;

CREATE TABLE playback_clusters (
    audio_asset_id TEXT NOT NULL REFERENCES audio_assets(id) ON DELETE CASCADE,
    cluster_index INTEGER NOT NULL CHECK (cluster_index >= 0),
    completed_sequence INTEGER NOT NULL CHECK (completed_sequence >= 0),
    first_packet_index INTEGER NOT NULL CHECK (first_packet_index >= 0),
    packet_count INTEGER NOT NULL CHECK (packet_count > 0),
    start_samples INTEGER NOT NULL CHECK (start_samples >= 0),
    block_bytes INTEGER NOT NULL CHECK (block_bytes > 0),
    cumulative_bytes INTEGER NOT NULL CHECK (cumulative_bytes > 0),
    PRIMARY KEY (audio_asset_id, cluster_index)
);

CREATE INDEX playback_clusters_snapshot
ON playback_clusters(audio_asset_id, completed_sequence, cluster_index);

CREATE INDEX playback_clusters_range
ON playback_clusters(audio_asset_id, cumulative_bytes);

CREATE TABLE playback_packets (
    audio_asset_id TEXT NOT NULL REFERENCES audio_assets(id) ON DELETE CASCADE,
    packet_index INTEGER NOT NULL CHECK (packet_index >= 0),
    completed_sequence INTEGER NOT NULL CHECK (completed_sequence >= 0),
    cluster_index INTEGER NOT NULL CHECK (cluster_index >= 0),
    source_offset INTEGER NOT NULL CHECK (source_offset >= 0),
    payload_size INTEGER NOT NULL CHECK (payload_size > 0),
    duration_samples INTEGER NOT NULL CHECK (duration_samples > 0),
    start_samples INTEGER NOT NULL CHECK (start_samples >= 0),
    relative_ticks INTEGER NOT NULL CHECK (relative_ticks BETWEEN 0 AND 32767),
    block_offset INTEGER NOT NULL CHECK (block_offset >= 0),
    PRIMARY KEY (audio_asset_id, packet_index),
    FOREIGN KEY (audio_asset_id, cluster_index)
        REFERENCES playback_clusters(audio_asset_id, cluster_index)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX playback_packets_cluster
ON playback_packets(audio_asset_id, cluster_index, packet_index);

CREATE INDEX playback_packets_snapshot
ON playback_packets(audio_asset_id, completed_sequence, packet_index);
