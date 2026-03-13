"""Generic key-value cache store for iFit/Hevy data migration.

Replaces local JSON cache files under .ifit_capture/ with a shared DB
table so multi-replica deployments see consistent data.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS cache_store (
    cache_key   TEXT PRIMARY KEY,
    data        JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS cache_store;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
