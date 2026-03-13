"""Generic cache storage abstraction with DB-first, file-fallback strategy.

Replaces the per-script JSON file I/O under ``.ifit_capture/`` with a shared
``cache_store`` DB table.  Falls back to local files when the database is
unreachable, preserving local-dev and HA addon compatibility.

Cache keys use slash-separated namespaces (e.g. ``ifit/library_workouts``,
``hevy/exercises``, ``ifit/r2_sync_state``).

Module-level API::

    from scripts.cache_store import get_cache, put_cache, delete_cache

    put_cache("ifit/library_workouts", [...])
    data = get_cache("ifit/library_workouts")

For plain text (e.g. hevy_exercise_ref.txt)::

    put_cache_text("hevy/exercise_ref", text_content)
    text = get_cache_text("hevy/exercise_ref")
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_TEXT_WRAPPER_KEY = "_text"

# Well-known cache keys matching the old .ifit_capture/ file names
KEY_LIBRARY_WORKOUTS = "ifit/library_workouts"
KEY_TRAINERS = "ifit/trainers"
KEY_EXERCISE_CACHE = "ifit/exercise_cache"
KEY_HEVY_EXERCISES = "hevy/exercises"
KEY_HEVY_CUSTOM_MAP = "hevy/custom_map"
KEY_HEVY_EXERCISE_REF = "hevy/exercise_ref"
KEY_ST101_TRANSCRIPTS = "ifit/st101_transcripts"
KEY_ST101_EXERCISES = "ifit/st101_exercises"
KEY_ST101_EXERCISES_LLM = "ifit/st101_exercises_llm"
KEY_R2_SYNC_STATE = "ifit/r2_sync_state"
KEY_RECOMMENDATIONS = "ifit/recommendations"
KEY_LIBRARY_BY_TRAINER = "ifit/library_by_trainer"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class CacheStore(ABC):

    @abstractmethod
    def get_json(self, key: str) -> Any | None:
        ...

    @abstractmethod
    def put_json(self, key: str, data: Any) -> None:
        ...

    @abstractmethod
    def get_text(self, key: str) -> str | None:
        ...

    @abstractmethod
    def put_text(self, key: str, text: str) -> None:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...


# ---------------------------------------------------------------------------
# DB-backed implementation
# ---------------------------------------------------------------------------

class DBCacheStore(CacheStore):

    def _conn(self):
        from scripts.db_pool import get_conn
        return get_conn()

    def get_json(self, key: str) -> Any | None:
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT data FROM cache_store WHERE cache_key = %s",
                (key,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            data = row[0]
            if isinstance(data, dict) and list(data.keys()) == [_TEXT_WRAPPER_KEY]:
                return None
            return data
        finally:
            conn.close()

    def put_json(self, key: str, data: Any) -> None:
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO cache_store (cache_key, data, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (cache_key)
                   DO UPDATE SET data = EXCLUDED.data,
                                 updated_at = NOW()""",
                (key, json.dumps(data, default=str)),
            )
            conn.commit()
        finally:
            conn.close()

    def get_text(self, key: str) -> str | None:
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT data FROM cache_store WHERE cache_key = %s",
                (key,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            data = row[0]
            if isinstance(data, dict) and _TEXT_WRAPPER_KEY in data:
                return data[_TEXT_WRAPPER_KEY]
            return None
        finally:
            conn.close()

    def put_text(self, key: str, text: str) -> None:
        self.put_json(key, {_TEXT_WRAPPER_KEY: text})

    def delete(self, key: str) -> None:
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM cache_store WHERE cache_key = %s",
                (key,),
            )
            conn.commit()
        finally:
            conn.close()

    def exists(self, key: str) -> bool:
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM cache_store WHERE cache_key = %s",
                (key,),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# File-backed implementation (fallback / local dev / HA addon)
# ---------------------------------------------------------------------------

class FileCacheStore(CacheStore):
    """Stores cache data as JSON/text files under a base directory.

    Layout::

        base_dir/<key>.json   (JSON data)
        base_dir/<key>.txt    (text data)
    """

    def __init__(self, base_dir: Path | str | None = None):
        if base_dir is None:
            if Path("/config/healthcoach/.ifit_capture").is_dir():
                base_dir = Path("/config/healthcoach/.ifit_capture")
            else:
                base_dir = Path(__file__).resolve().parent.parent / ".ifit_capture"
        self._base = Path(base_dir)

    def _json_path(self, key: str) -> Path:
        return self._base / f"{key}.json"

    def _text_path(self, key: str) -> Path:
        return self._base / f"{key}.txt"

    def get_json(self, key: str) -> Any | None:
        p = self._json_path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def put_json(self, key: str, data: Any) -> None:
        p = self._json_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, default=str))

    def get_text(self, key: str) -> str | None:
        p = self._text_path(key)
        if not p.exists():
            return None
        try:
            return p.read_text()
        except OSError:
            return None

    def put_text(self, key: str, text: str) -> None:
        p = self._text_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def delete(self, key: str) -> None:
        self._json_path(key).unlink(missing_ok=True)
        self._text_path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._json_path(key).exists() or self._text_path(key).exists()


# ---------------------------------------------------------------------------
# Auto-detection and module-level API
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_cached_store: CacheStore | None = None


def _get_store(
    *,
    force_redetect: bool = False,
    file_base: Path | str | None = None,
) -> CacheStore:
    """Return the active cache store, auto-detecting DB availability."""
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
            store: CacheStore = DBCacheStore()
            log.debug("cache_store: using DB backend")
        except Exception:
            store = FileCacheStore(base_dir=file_base)
            log.debug("cache_store: DB unavailable, using file backend")

        _cached_store = store
        return store


def get_cache(key: str) -> Any | None:
    return _get_store().get_json(key)


def put_cache(key: str, data: Any) -> None:
    _get_store().put_json(key, data)


def get_cache_text(key: str) -> str | None:
    return _get_store().get_text(key)


def put_cache_text(key: str, text: str) -> None:
    _get_store().put_text(key, text)


def delete_cache(key: str) -> None:
    _get_store().delete(key)


def cache_exists(key: str) -> bool:
    return _get_store().exists(key)
