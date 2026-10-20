CREATE TABLE provider_identities (
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    account_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    email TEXT,
    email_verified INTEGER CHECK (email_verified IN (0, 1)),
    display_name TEXT,
    given_name TEXT,
    family_name TEXT,
    picture_url TEXT,
    locale TEXT,
    microsoft_tenant_id TEXT,
    first_login_at_ms INTEGER NOT NULL CHECK (first_login_at_ms >= 0),
    last_login_at_ms INTEGER NOT NULL CHECK (last_login_at_ms >= first_login_at_ms),
    PRIMARY KEY (issuer, subject)
);

CREATE INDEX provider_identities_provider
ON provider_identities(provider, account_id);

CREATE TABLE application_sessions (
    token_digest BLOB PRIMARY KEY NOT NULL CHECK (length(token_digest) = 32),
    account_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms > created_at_ms)
);

CREATE INDEX application_sessions_account
ON application_sessions(account_id, expires_at_ms);

CREATE INDEX application_sessions_expiry
ON application_sessions(expires_at_ms);

CREATE TABLE oidc_login_attempts (
    state_digest BLOB PRIMARY KEY NOT NULL CHECK (length(state_digest) = 32),
    provider TEXT NOT NULL,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    return_to TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms > created_at_ms)
);

CREATE INDEX oidc_login_attempts_expiry
ON oidc_login_attempts(expires_at_ms);
