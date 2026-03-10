-- Persistent Telegram conversation history for cross-channel context.

CREATE TABLE IF NOT EXISTS telegram_messages (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    chat_id     BIGINT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS telegram_messages_user_recent
    ON telegram_messages (user_id, created_at DESC);
