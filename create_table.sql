-- Chat session metadata (one row per user session)
CREATE TABLE IF NOT EXISTS chat_sessions (
    actor_id      TEXT        NOT NULL, -- Entra object id (SSO user)
    session_id    TEXT        NOT NULL,
    title         TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message  TEXT        NOT NULL DEFAULT '',
    agent         TEXT        NOT NULL DEFAULT '',
    message_count INTEGER     NOT NULL DEFAULT 0,
    CONSTRAINT pk_chat_sessions PRIMARY KEY (actor_id, session_id)
);

-- List a user's sessions newest-first (left-nav history)
CREATE INDEX IF NOT EXISTS idx_chat_sessions_actor_updated ON chat_sessions (actor_id, updated_at DESC);

-- Transcript turns (stored separately from metadata)
CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGSERIAL   PRIMARY KEY,
    actor_id    TEXT        NOT NULL,
    session_id  TEXT        NOT NULL,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT        NOT NULL,
    agent       TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_chat_messages_session
        FOREIGN KEY (actor_id, session_id)
        REFERENCES chat_sessions (actor_id, session_id)
        ON DELETE CASCADE
);

-- Load a session transcript in order
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages (actor_id, session_id, created_at, id);
