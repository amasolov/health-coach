"""Weather and route cache tables for outdoor run recommendations.

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS weather_cache (
    id              SERIAL PRIMARY KEY,
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    forecast_date   DATE NOT NULL,
    data            JSONB NOT NULL,
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS weather_cache_loc_date_uniq
    ON weather_cache (lat, lon, forecast_date);

CREATE TABLE IF NOT EXISTS route_cache (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER REFERENCES users(id),
    source              TEXT NOT NULL,
    name                TEXT NOT NULL,
    lat                 DOUBLE PRECISION NOT NULL,
    lon                 DOUBLE PRECISION NOT NULL,
    distance_m          DOUBLE PRECISION,
    elevation_gain_m    DOUBLE PRECISION,
    surface_types       TEXT[],
    is_loop             BOOLEAN,
    popularity_score    DOUBLE PRECISION,
    metadata            JSONB NOT NULL DEFAULT '{}',
    fetched_at          TIMESTAMPTZ DEFAULT NOW(),
    expires_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS route_cache_user_idx ON route_cache (user_id);
CREATE INDEX IF NOT EXISTS route_cache_loc_idx ON route_cache (lat, lon);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS route_cache;
DROP TABLE IF EXISTS weather_cache;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
