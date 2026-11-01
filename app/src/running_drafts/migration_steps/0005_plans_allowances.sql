ALTER TABLE users
ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'
CHECK (plan IN ('free', 'pro'));

ALTER TABLE users
ADD COLUMN stored_audio_bytes INTEGER NOT NULL DEFAULT 0
CHECK (stored_audio_bytes >= 0);

ALTER TABLE users
ADD COLUMN suspended_at_ms INTEGER
CHECK (suspended_at_ms IS NULL OR suspended_at_ms >= 0);

UPDATE users
SET stored_audio_bytes = COALESCE((
    SELECT SUM(audio_assets.stored_bytes)
    FROM projects
    JOIN sections ON sections.project_id = projects.id
    JOIN audio_assets ON audio_assets.section_id = sections.id
    WHERE projects.owner_id = users.id
), 0);

CREATE TABLE allowance_grants (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('operator', 'downgrade')),
    stored_bytes INTEGER NOT NULL DEFAULT 0 CHECK (stored_bytes >= 0),
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    CHECK (kind = 'operator' OR duration_ms = 0)
);

CREATE INDEX allowance_grants_account
ON allowance_grants(account_id, kind);

CREATE UNIQUE INDEX allowance_grants_one_downgrade
ON allowance_grants(account_id)
WHERE kind = 'downgrade';
