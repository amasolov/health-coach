"""Tests for the credential_store module.

Covers DBCredentialStore, FileCredentialStore, module-level API,
auto-detection, and atomic locking for token refresh.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# DBCredentialStore
# ---------------------------------------------------------------------------

class TestDBCredentialStore:

    def test_put_and_get_user_credential(self, user_id):
        from scripts.credential_store import DBCredentialStore

        store = DBCredentialStore()
        data = {"token": "abc123", "refresh": "xyz"}
        store.put("garmin_oauth", data, user_id=user_id)

        result = store.get("garmin_oauth", user_id=user_id)
        assert result is not None
        assert result["token"] == "abc123"
        assert result["refresh"] == "xyz"

    def test_put_and_get_system_credential(self):
        from scripts.credential_store import DBCredentialStore

        store = DBCredentialStore()
        data = {"secret": "sys-secret-value"}
        store.put("chainlit_auth_secret", data)

        result = store.get("chainlit_auth_secret")
        assert result is not None
        assert result["secret"] == "sys-secret-value"

        store.delete("chainlit_auth_secret")

    def test_get_missing_returns_none(self, user_id):
        from scripts.credential_store import DBCredentialStore

        store = DBCredentialStore()
        assert store.get("nonexistent_type", user_id=user_id) is None

    def test_put_overwrites_existing(self, user_id):
        from scripts.credential_store import DBCredentialStore

        store = DBCredentialStore()
        store.put("garmin_oauth", {"v": 1}, user_id=user_id)
        store.put("garmin_oauth", {"v": 2}, user_id=user_id)

        result = store.get("garmin_oauth", user_id=user_id)
        assert result["v"] == 2

    def test_delete_user_credential(self, user_id):
        from scripts.credential_store import DBCredentialStore

        store = DBCredentialStore()
        store.put("garmin_oauth", {"token": "del-me"}, user_id=user_id)
        store.delete("garmin_oauth", user_id=user_id)
        assert store.get("garmin_oauth", user_id=user_id) is None

    def test_delete_system_credential(self):
        from scripts.credential_store import DBCredentialStore

        store = DBCredentialStore()
        store.put("chainlit_auth_secret", {"s": "x"})
        store.delete("chainlit_auth_secret")
        assert store.get("chainlit_auth_secret") is None

    def test_delete_nonexistent_no_error(self, user_id):
        from scripts.credential_store import DBCredentialStore

        store = DBCredentialStore()
        store.delete("nonexistent_type", user_id=user_id)

    def test_user_credentials_isolated(self, db_conn):
        """Different user_ids get separate credential rows."""
        from scripts.credential_store import DBCredentialStore

        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO users (slug, display_name) VALUES (%s, %s) RETURNING id",
            ("credtest_other", "Other User"),
        )
        other_id = cur.fetchone()[0]
        db_conn.commit()

        store = DBCredentialStore()
        store.put("garmin_oauth", {"user": "a"}, user_id=1)
        store.put("garmin_oauth", {"user": "b"}, user_id=other_id)

        a = store.get("garmin_oauth", user_id=1)
        b = store.get("garmin_oauth", user_id=other_id)
        assert a["user"] == "a"
        assert b["user"] == "b"

        store.delete("garmin_oauth", user_id=1)
        store.delete("garmin_oauth", user_id=other_id)
        cur.execute("DELETE FROM users WHERE slug = 'credtest_other'")
        db_conn.commit()

    def test_get_locked(self, user_id):
        """get_locked acquires a row lock within the caller's transaction."""
        from scripts.credential_store import DBCredentialStore
        from scripts.db_pool import get_conn

        store = DBCredentialStore()
        store.put("ifit_oauth", {"access": "tok"})

        conn = get_conn()
        try:
            conn.autocommit = False
            result = store.get_locked("ifit_oauth", user_id=None, conn=conn)
            assert result is not None
            assert result["access"] == "tok"
            conn.commit()
        finally:
            conn.close()

        store.delete("ifit_oauth")


# ---------------------------------------------------------------------------
# FileCredentialStore
# ---------------------------------------------------------------------------

class TestFileCredentialStore:

    def test_put_and_get(self, tmp_path):
        from scripts.credential_store import FileCredentialStore

        store = FileCredentialStore(base_dir=tmp_path)
        store.put("ifit_oauth", {"token": "file-tok"})

        result = store.get("ifit_oauth")
        assert result is not None
        assert result["token"] == "file-tok"

    def test_get_missing_returns_none(self, tmp_path):
        from scripts.credential_store import FileCredentialStore

        store = FileCredentialStore(base_dir=tmp_path)
        assert store.get("nonexistent") is None

    def test_put_creates_subdirs(self, tmp_path):
        from scripts.credential_store import FileCredentialStore

        store = FileCredentialStore(base_dir=tmp_path)
        store.put("garmin_oauth", {"t": 1}, user_id=42)

        cred_file = tmp_path / "garmin_oauth" / "42.json"
        assert cred_file.exists()

    def test_delete(self, tmp_path):
        from scripts.credential_store import FileCredentialStore

        store = FileCredentialStore(base_dir=tmp_path)
        store.put("ifit_oauth", {"a": 1})
        store.delete("ifit_oauth")
        assert store.get("ifit_oauth") is None

    def test_user_credentials_isolated(self, tmp_path):
        from scripts.credential_store import FileCredentialStore

        store = FileCredentialStore(base_dir=tmp_path)
        store.put("garmin_oauth", {"u": "a"}, user_id=1)
        store.put("garmin_oauth", {"u": "b"}, user_id=2)

        assert store.get("garmin_oauth", user_id=1)["u"] == "a"
        assert store.get("garmin_oauth", user_id=2)["u"] == "b"


# ---------------------------------------------------------------------------
# Module-level API and auto-detection
# ---------------------------------------------------------------------------

class TestModuleAPI:

    def test_get_put_delete_roundtrip(self, user_id):
        """Module-level functions work end-to-end."""
        from scripts import credential_store as cs

        cs.put_credential("garmin_oauth", {"test": True}, user_id=user_id)
        result = cs.get_credential("garmin_oauth", user_id=user_id)
        assert result is not None
        assert result["test"] is True

        cs.delete_credential("garmin_oauth", user_id=user_id)
        assert cs.get_credential("garmin_oauth", user_id=user_id) is None

    def test_fallback_to_file_when_db_unavailable(self, tmp_path):
        """When DB is unreachable, falls back to FileCredentialStore."""
        from scripts.credential_store import (
            FileCredentialStore,
            _get_store,
        )

        with patch("scripts.db_pool.get_conn", side_effect=Exception("no db")):
            store = _get_store(force_redetect=True, file_base=tmp_path)
            assert isinstance(store, FileCredentialStore)


# ---------------------------------------------------------------------------
# Integration: Garmin auth saves/loads via credential store
# ---------------------------------------------------------------------------

class TestGarminCredentialIntegration:

    def test_save_tokens_puts_credential(self, user_id):
        """_save_tokens persists garth tokens to the credential store."""
        mock_garth = MagicMock()
        mock_garth.dumps.return_value = "base64-garth-token-string"
        mock_garth.dump = MagicMock()
        mock_client = MagicMock()
        mock_client.garth = mock_garth

        from scripts.garmin_auth import _save_tokens
        from scripts import credential_store as cs

        _save_tokens("testuser", mock_client)

        cred = cs.get_credential("garmin_oauth", user_id=user_id)
        assert cred is not None
        assert cred["token"] == "base64-garth-token-string"

        cs.delete_credential("garmin_oauth", user_id=user_id)

    def test_load_from_credential_store(self, user_id):
        """try_cached_login checks credential store before falling back
        to file-based token directory."""
        from scripts import credential_store as cs

        cs.put_credential("garmin_oauth", {"token": "fake-garth-b64"}, user_id=user_id)

        from scripts.garmin_auth import try_cached_login

        with patch("scripts.garmin_auth._load_from_garth_str") as mock_load:
            mock_load.return_value = MagicMock()
            result = try_cached_login("testuser")
            mock_load.assert_called_once_with("fake-garth-b64")
            assert result is not None

        cs.delete_credential("garmin_oauth", user_id=user_id)

    def test_load_falls_back_to_file(self, user_id):
        """When no DB credential exists, try_cached_login still checks
        the file-based token directory."""
        from scripts import credential_store as cs
        from scripts.garmin_auth import try_cached_login

        cs.delete_credential("garmin_oauth", user_id=user_id)

        with patch("scripts.garmin_auth._token_dir") as mock_dir, \
             patch("scripts.garmin_auth.Garmin") as MockGarmin:
            fake_dir = Path("/tmp/nonexistent-garmin-tokens")
            mock_dir.return_value = fake_dir
            result = try_cached_login("testuser")
            assert result is None


# ---------------------------------------------------------------------------
# Integration: iFit auth uses credential store
# ---------------------------------------------------------------------------

class TestIfitCredentialIntegration:

    def test_load_cached_from_db(self):
        from scripts import credential_store as cs

        token_data = {
            "access_token": "ifit-access",
            "refresh_token": "ifit-refresh",
            "expires_in": 604800,
            "timestamp": 9999999999,
            "app_client_id": "cid",
            "app_client_secret": "csec",
        }
        cs.put_credential("ifit_oauth", token_data)

        from scripts.ifit_auth import _load_cached
        result = _load_cached()
        assert result is not None
        assert result["access_token"] == "ifit-access"

        cs.delete_credential("ifit_oauth")

    def test_save_cache_writes_to_db(self):
        from scripts.ifit_auth import _save_cache

        data = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 604800,
            "timestamp": 1000,
        }
        _save_cache(data)

        from scripts import credential_store as cs
        result = cs.get_credential("ifit_oauth")
        assert result is not None
        assert result["access_token"] == "new-access"

        cs.delete_credential("ifit_oauth")


# ---------------------------------------------------------------------------
# Integration: Chainlit auth secret uses credential store
# ---------------------------------------------------------------------------

class TestChainlitSecretIntegration:

    def test_reads_from_db(self):
        from scripts import credential_store as cs
        from scripts.addon_config import _resolve_chainlit_secret

        cs.put_credential("chainlit_auth_secret", {"secret": "db-jwt-secret"})
        try:
            result = _resolve_chainlit_secret()
            assert result == "db-jwt-secret"
        finally:
            cs.delete_credential("chainlit_auth_secret")

    def test_migrates_file_to_db(self, tmp_path):
        """When secret exists only in file, it gets migrated to DB."""
        from scripts import credential_store as cs
        from scripts.addon_config import _resolve_chainlit_secret

        cs.delete_credential("chainlit_auth_secret")

        secret_file = tmp_path / ".chainlit_auth_secret"
        secret_file.write_text("file-secret-value")

        with patch("scripts.addon_config._CHAINLIT_SECRET_FILE", secret_file):
            result = _resolve_chainlit_secret()
            assert result == "file-secret-value"

            db_cred = cs.get_credential("chainlit_auth_secret")
            assert db_cred is not None
            assert db_cred["secret"] == "file-secret-value"

        cs.delete_credential("chainlit_auth_secret")

    def test_generates_and_stores_when_missing(self, tmp_path):
        """When no secret exists anywhere, a new one is generated and
        stored in the credential store."""
        from scripts import credential_store as cs
        from scripts.addon_config import _resolve_chainlit_secret

        cs.delete_credential("chainlit_auth_secret")

        secret_file = tmp_path / ".chainlit_auth_secret"
        with patch("scripts.addon_config._CHAINLIT_SECRET_FILE", secret_file):
            result = _resolve_chainlit_secret()
            assert len(result) > 20

            db_cred = cs.get_credential("chainlit_auth_secret")
            assert db_cred is not None
            assert db_cred["secret"] == result

        cs.delete_credential("chainlit_auth_secret")
