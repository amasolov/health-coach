"""Credential storage abstraction with DB-first, file-fallback strategy.

Stores per-user credentials (Garmin OAuth) and system-wide credentials
(iFit OAuth, Chainlit auth secret) as JSONB in a ``credentials`` table.
Falls back to local JSON files when the database is unreachable.

Module-level API::

    from scripts.credential_store import get_credential, put_credential, delete_credential

    put_credential("ifit_oauth", {"access_token": "..."})
    data = get_credential("ifit_oauth")
    delete_credential("ifit_oauth")

For atomic read-modify-write (e.g. token refresh)::

    from scripts.credential_store import get_credential_locked
    conn = get_conn()
    try:
        conn.autocommit = False
        data = get_credential_locked("ifit_oauth", user_id=None, conn=conn)
        # ... refresh token ...
        put_credential("ifit_oauth", new_data)
        conn.commit()
    finally:
        conn.close()
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class CredentialStore(ABC):

    @abstractmethod
    def get(self, cred_type: str, user_id: int | None = None) -> dict | None:
        ...

    @abstractmethod
    def put(self, cred_type: str, data: dict, user_id: int | None = None) -> None:
        ...

    @abstractmethod
    def delete(self, cred_type: str, user_id: int | None = None) -> None:
        ...


# ---------------------------------------------------------------------------
# DB-backed implementation
# ---------------------------------------------------------------------------

class DBCredentialStore(CredentialStore):

    def _conn(self):
        from scripts.db_pool import get_conn
        return get_conn()

    def get(self, cred_type: str, user_id: int | None = None) -> dict | None:
        conn = self._conn()
        try:
            cur = conn.cursor()
            if user_id is not None:
                cur.execute(
                    "SELECT cred_data FROM credentials "
                    "WHERE cred_type = %s AND user_id = %s",
                    (cred_type, user_id),
                )
            else:
                cur.execute(
                    "SELECT cred_data FROM credentials "
                    "WHERE cred_type = %s AND user_id IS NULL",
                    (cred_type,),
                )
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def put(self, cred_type: str, data: dict, user_id: int | None = None) -> None:
        conn = self._conn()
        try:
            cur = conn.cursor()
            if user_id is not None:
                cur.execute(
                    """INSERT INTO credentials (user_id, cred_type, cred_data, updated_at)
                       VALUES (%s, %s, %s, NOW())
                       ON CONFLICT (user_id, cred_type)
                           WHERE user_id IS NOT NULL
                       DO UPDATE SET cred_data = EXCLUDED.cred_data,
                                     updated_at = NOW()""",
                    (user_id, cred_type, json.dumps(data)),
                )
            else:
                cur.execute(
                    """INSERT INTO credentials (user_id, cred_type, cred_data, updated_at)
                       VALUES (NULL, %s, %s, NOW())
                       ON CONFLICT (cred_type)
                           WHERE user_id IS NULL
                       DO UPDATE SET cred_data = EXCLUDED.cred_data,
                                     updated_at = NOW()""",
                    (cred_type, json.dumps(data)),
                )
            conn.commit()
        finally:
            conn.close()

    def delete(self, cred_type: str, user_id: int | None = None) -> None:
        conn = self._conn()
        try:
            cur = conn.cursor()
            if user_id is not None:
                cur.execute(
                    "DELETE FROM credentials WHERE cred_type = %s AND user_id = %s",
                    (cred_type, user_id),
                )
            else:
                cur.execute(
                    "DELETE FROM credentials WHERE cred_type = %s AND user_id IS NULL",
                    (cred_type,),
                )
            conn.commit()
        finally:
            conn.close()

    def get_locked(
        self,
        cred_type: str,
        user_id: int | None,
        conn: Any,
    ) -> dict | None:
        """Read a credential row with ``FOR UPDATE`` within the caller's
        transaction.  The caller owns ``conn`` and must commit/rollback."""
        cur = conn.cursor()
        if user_id is not None:
            cur.execute(
                "SELECT cred_data FROM credentials "
                "WHERE cred_type = %s AND user_id = %s FOR UPDATE",
                (cred_type, user_id),
            )
        else:
            cur.execute(
                "SELECT cred_data FROM credentials "
                "WHERE cred_type = %s AND user_id IS NULL FOR UPDATE",
                (cred_type,),
            )
        row = cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# File-backed implementation (fallback / HA addon compat)
# ---------------------------------------------------------------------------

class FileCredentialStore(CredentialStore):
    """Stores credentials as JSON files under a base directory.

    Layout::

        base_dir/<cred_type>/system.json     (user_id is None)
        base_dir/<cred_type>/<user_id>.json  (per-user)
    """

    def __init__(self, base_dir: Path | str | None = None):
        if base_dir is None:
            if Path("/config/healthcoach").is_dir():
                base_dir = Path("/config/healthcoach/.credentials")
            else:
                base_dir = Path(__file__).resolve().parent.parent / ".credentials"
        self._base = Path(base_dir)

    def _path(self, cred_type: str, user_id: int | None) -> Path:
        name = "system.json" if user_id is None else f"{user_id}.json"
        return self._base / cred_type / name

    def get(self, cred_type: str, user_id: int | None = None) -> dict | None:
        p = self._path(cred_type, user_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def put(self, cred_type: str, data: dict, user_id: int | None = None) -> None:
        p = self._path(cred_type, user_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))

    def delete(self, cred_type: str, user_id: int | None = None) -> None:
        p = self._path(cred_type, user_id)
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Auto-detection and module-level API
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_cached_store: CredentialStore | None = None

def _get_store(
    *,
    force_redetect: bool = False,
    file_base: Path | str | None = None,
) -> CredentialStore:
    """Return the active credential store, auto-detecting DB availability.

    The result is cached for the process lifetime unless *force_redetect*
    is True (used by tests).
    """
    global _cached_store

    if _cached_store is not None and not force_redetect:
        return _cached_store

    with _store_lock:
        if _cached_store is not None and not force_redetect:
            return _cached_store

        try:
            from scripts.db_pool import get_conn
            conn = get_conn()
            conn.close()
            store: CredentialStore = DBCredentialStore()
            log.debug("credential_store: using DB backend")
        except Exception:
            store = FileCredentialStore(base_dir=file_base)
            log.debug("credential_store: DB unavailable, using file backend")

        _cached_store = store
        return store


def get_credential(cred_type: str, user_id: int | None = None) -> dict | None:
    return _get_store().get(cred_type, user_id=user_id)


def put_credential(
    cred_type: str, data: dict, user_id: int | None = None,
) -> None:
    _get_store().put(cred_type, data, user_id=user_id)


def delete_credential(cred_type: str, user_id: int | None = None) -> None:
    _get_store().delete(cred_type, user_id=user_id)


def get_credential_locked(
    cred_type: str, user_id: int | None, conn: Any,
) -> dict | None:
    """Locked read — only supported by DBCredentialStore."""
    store = _get_store()
    if isinstance(store, DBCredentialStore):
        return store.get_locked(cred_type, user_id=user_id, conn=conn)
    return store.get(cred_type, user_id=user_id)
