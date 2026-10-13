CREATE TABLE projects (
    id TEXT PRIMARY KEY NOT NULL,
    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    creation_key TEXT NOT NULL,
    original_title TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    UNIQUE (owner_id, creation_key)
);

CREATE INDEX projects_owner_list
ON projects(owner_id, updated_at_ms DESC, id);

CREATE TABLE sections (
    id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    creation_key TEXT NOT NULL,
    creation_media_type TEXT NOT NULL
        CHECK (creation_media_type = 'audio/webm;codecs=opus'),
    UNIQUE (project_id, position),
    UNIQUE (project_id, creation_key)
);

CREATE TABLE audio_assets (
    id TEXT PRIMARY KEY NOT NULL,
    section_id TEXT NOT NULL UNIQUE REFERENCES sections(id) ON DELETE CASCADE,
    media_type TEXT NOT NULL CHECK (media_type = 'audio/webm;codecs=opus'),
    is_sealed INTEGER NOT NULL DEFAULT 0 CHECK (is_sealed IN (0, 1)),
    seal_reason TEXT CHECK (seal_reason IN ('completed', 'interrupted')),
    sealed_at_ms INTEGER CHECK (sealed_at_ms >= 0),
    next_expected_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (next_expected_sequence >= 0),
    observed_duration_ms INTEGER NOT NULL DEFAULT 0
        CHECK (observed_duration_ms >= 0),
    stored_bytes INTEGER NOT NULL DEFAULT 0 CHECK (stored_bytes >= 0),
    CHECK (
        (is_sealed = 0 AND seal_reason IS NULL AND sealed_at_ms IS NULL)
        OR
        (is_sealed = 1 AND seal_reason IS NOT NULL AND sealed_at_ms IS NOT NULL)
    )
);

CREATE TABLE transport_frames (
    audio_asset_id TEXT NOT NULL REFERENCES audio_assets(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
    media_type TEXT NOT NULL CHECK (media_type = 'audio/webm;codecs=opus'),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64
        AND sha256 = lower(sha256)
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    payload BLOB NOT NULL CHECK (length(payload) > 0),
    PRIMARY KEY (audio_asset_id, sequence)
);
