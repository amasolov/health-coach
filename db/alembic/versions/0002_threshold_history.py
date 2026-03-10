"""Threshold history for date-aware TSS calculation.

Stores a snapshot of key threshold values (FTP, LTHR, resting HR, etc.)
each time they change, keyed by (user_id, effective_date).  The TSS
calculation pipeline looks up the row with effective_date <= activity_date
so that historical activities keep the thresholds that were active when
they occurred.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-11
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS threshold_history (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    effective_date  DATE NOT NULL,
    ftp             SMALLINT,
    rftp            SMALLINT,
    lthr_run        SMALLINT,
    lthr_bike       SMALLINT,
    resting_hr      SMALLINT,
    max_hr          SMALLINT,
    weight_kg       NUMERIC(5,2),
    source          TEXT DEFAULT 'garmin',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, effective_date)
);

CREATE INDEX IF NOT EXISTS idx_threshold_history_lookup
    ON threshold_history (user_id, effective_date DESC);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS threshold_history;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
