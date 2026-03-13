"""Credential store table for OAuth tokens and signing secrets.

Stores per-user credentials (e.g. Garmin OAuth) and system-wide
credentials (e.g. iFit OAuth, Chainlit auth secret) as JSONB.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS credentials (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    cred_type   TEXT NOT NULL,
    cred_data   JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Per-user credentials (user_id IS NOT NULL): one row per (user, type).
CREATE UNIQUE INDEX IF NOT EXISTS credentials_user_type_uniq
    ON credentials (user_id, cred_type)
    WHERE user_id IS NOT NULL;

-- System-wide credentials (user_id IS NULL): one row per type.
CREATE UNIQUE INDEX IF NOT EXISTS credentials_system_type_uniq
    ON credentials (cred_type)
    WHERE user_id IS NULL;
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS credentials;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
