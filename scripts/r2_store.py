"""
Cloudflare R2 (S3-compatible) storage client for persistent iFit data.

Provides thin wrappers around boto3 for uploading/downloading text and JSON
to an R2 bucket.  All functions gracefully return None / no-op when R2
credentials are not configured, so the rest of the codebase can call them
unconditionally.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from scripts.addon_config import config

_ACCOUNT_ID = config.r2_account_id
_ACCESS_KEY = config.r2_access_key_id
_SECRET_KEY = config.r2_secret_access_key
_BUCKET = config.r2_bucket_name


def is_configured() -> bool:
    return bool(_ACCOUNT_ID and _ACCESS_KEY and _SECRET_KEY and _BUCKET)


@lru_cache(maxsize=1)
def _get_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        region_name="auto",
    )


def _bucket() -> str:
    return _BUCKET


# ── Upload ────────────────────────────────────────────────────────────

def upload_text(key: str, text: str) -> bool:
    if not is_configured():
        return False
    try:
        _get_client().put_object(
            Bucket=_bucket(), Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
        return True
    except Exception as exc:
        print(f"  R2 upload_text error ({key}): {exc}")
        return False


def upload_json(key: str, obj: Any) -> bool:
    if not is_configured():
        return False
    try:
        body = json.dumps(obj, indent=2, default=str)
        _get_client().put_object(
            Bucket=_bucket(), Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )
        return True
    except Exception as exc:
        print(f"  R2 upload_json error ({key}): {exc}")
        return False


# ── Download ──────────────────────────────────────────────────────────

def download_text(key: str) -> str | None:
    if not is_configured():
        return None
    try:
        resp = _get_client().get_object(Bucket=_bucket(), Key=key)
        return resp["Body"].read().decode("utf-8")
    except _get_client().exceptions.NoSuchKey:
        return None
    except Exception as exc:
        if "NoSuchKey" in str(exc) or "404" in str(exc):
            return None
        print(f"  R2 download_text error ({key}): {exc}")
        return None


def download_json(key: str) -> Any | None:
    text = download_text(key)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ── Query ─────────────────────────────────────────────────────────────

def delete(key: str) -> bool:
    """Delete a single object. Returns True on success or if key didn't exist."""
    if not is_configured():
        return False
    try:
        _get_client().delete_object(Bucket=_bucket(), Key=key)
        return True
    except Exception as exc:
        print(f"  R2 delete error ({key}): {exc}")
        return False


def exists(key: str) -> bool:
    if not is_configured():
        return False
    try:
        _get_client().head_object(Bucket=_bucket(), Key=key)
        return True
    except Exception:
        return False


def list_keys(prefix: str, max_keys: int = 50_000) -> list[str]:
    """List all object keys under *prefix*.  Handles pagination."""
    if not is_configured():
        return []
    keys: list[str] = []
    try:
        paginator = _get_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=_bucket(), Prefix=prefix,
            PaginationConfig={"MaxItems": max_keys},
        ):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except Exception as exc:
        print(f"  R2 list_keys error ({prefix}): {exc}")
    return keys
