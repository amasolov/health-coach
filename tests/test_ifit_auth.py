"""Tests for iFit authentication and token refresh."""

import json
import time
from unittest.mock import patch, MagicMock

import pytest


class TestRefreshTokenPersistence:
    """refresh_token must persist the new timestamp to the DB via locked_conn,
    not through _save_cache which reuses the same pool connection."""

    def test_refresh_saves_timestamp_via_locked_conn(self):
        """After a successful refresh, the locked_conn must have executed an
        UPSERT with the fresh timestamp and been committed."""
        from scripts.ifit_auth import refresh_token

        old_data = {
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "expires_in": 604800,
            "timestamp": time.time() - 900_000,
            "app_client_id": "cid",
            "app_client_secret": "csecret",
        }

        refresh_response = MagicMock()
        refresh_response.status_code = 200
        refresh_response.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 604800,
        }

        executed_sql: list[tuple] = []

        class FakeCursor:
            def execute(self, sql, params=None):
                executed_sql.append((sql, params))

            def fetchone(self):
                return (old_data,)

        class FakeConn:
            autocommit = True
            closed = False
            _committed = False

            def cursor(self):
                return FakeCursor()

            def commit(self):
                self._committed = True

            def rollback(self):
                pass

            def close(self):
                pass

        fake_conn = FakeConn()

        with patch("scripts.ifit_auth.httpx.post", return_value=refresh_response), \
             patch("scripts.db_pool.get_conn", return_value=fake_conn), \
             patch("scripts.credential_store.get_credential_locked",
                   side_effect=lambda *a, **kw: old_data), \
             patch("scripts.ifit_auth._save_cache"):
            before = time.time()
            result = refresh_token()
            after = time.time()

        assert result is not None
        assert result["access_token"] == "new_access"

        assert fake_conn._committed, "locked_conn must be committed"

        upsert_calls = [
            (sql, params) for sql, params in executed_sql
            if "UPDATE" in sql.upper() or "INSERT" in sql.upper()
        ]
        assert len(upsert_calls) >= 1, (
            f"Expected a DB write via locked_conn, got SQL: {executed_sql}"
        )
        written_json = upsert_calls[-1][1][1]
        written_data = json.loads(written_json)
        assert before <= written_data["timestamp"] <= after, (
            f"DB must have fresh timestamp, got {written_data['timestamp']}"
        )


class TestSaveFileHelper:
    """_save_file should write token data to disk."""

    def test_writes_json(self, tmp_path):
        from scripts.ifit_auth import _save_file

        token_file = tmp_path / "token.json"
        data = {"access_token": "test123", "timestamp": 42}

        with patch("scripts.ifit_auth.TOKEN_FILE", str(token_file)):
            _save_file(data)

        assert token_file.exists()
        assert json.loads(token_file.read_text())["access_token"] == "test123"


class TestGetValidTokenLogging:
    """get_valid_token should log whether the token is valid or expired."""

    def test_logs_valid_token(self, caplog):
        import logging
        from scripts.ifit_auth import get_valid_token

        fresh_data = {
            "access_token": "tok",
            "timestamp": time.time() - 10,
            "expires_in": 604800,
        }

        with patch("scripts.ifit_auth._load_cached", return_value=fresh_data), \
             caplog.at_level(logging.INFO, logger="scripts.ifit_auth"):
            token = get_valid_token()

        assert token == "tok"
        assert any("valid" in r.message.lower() for r in caplog.records)

    def test_logs_expired_token(self, caplog):
        import logging
        from scripts.ifit_auth import get_valid_token

        old_data = {
            "access_token": "tok",
            "timestamp": time.time() - 900_000,
            "expires_in": 604800,
        }

        with patch("scripts.ifit_auth._load_cached", return_value=old_data), \
             patch("scripts.ifit_auth.refresh_token", return_value=None), \
             caplog.at_level(logging.INFO, logger="scripts.ifit_auth"):
            token = get_valid_token()

        assert token is None
        assert any("expired" in r.message.lower() for r in caplog.records)
