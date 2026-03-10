#!/usr/bin/env python3
"""Standalone CLI for ingesting PDFs into the RAG knowledge base.

Usage:
    # Ingest all PDFs in a directory
    python scripts/ingest_books.py --dir books/

    # Ingest a single file
    python scripts/ingest_books.py --file books/starting_strength.pdf

    # Specify a user slug (defaults to global/shared)
    python scripts/ingest_books.py --dir books/ --user alexeym

Requires:
    - .env loaded (DB connection, embedding config)
    - Ollama running locally  (make ollama-start)
    - nomic-embed-text pulled (make ollama-pull)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from scripts.knowledge_store import (
    ingest_pdf,
    _extract_text_from_pdf,
    _chunk_pages,
    _get_conn,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _resolve_user_id(slug: str | None) -> int | None:
    if slug is None:
        return None
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if not row:
            log.error("User '%s' not found in database", slug)
            sys.exit(1)
        return row[0]
    finally:
        conn.close()


def _estimate_tokens(pdf_path: Path) -> tuple[int, int]:
    """Quick pre-scan: returns (page_count, chunk_count) for progress."""
    pages = _extract_text_from_pdf(pdf_path)
    chunks = _chunk_pages(pages) if pages else []
    return len(pages), len(chunks)


def ingest_one(pdf_path: Path, user_id: int | None) -> None:
    log.info("── %s", pdf_path.name)

    pages, chunks = _estimate_tokens(pdf_path)
    log.info("   %d pages, ~%d chunks to embed", pages, chunks)

    t0 = time.monotonic()
    result = ingest_pdf(pdf_path, user_id=user_id)
    elapsed = time.monotonic() - t0

    status = result.get("status", result.get("error", "unknown"))
    if status == "indexed":
        rate = result["chunk_count"] / elapsed if elapsed > 0 else 0
        log.info(
            "   ✓ indexed — %d chunks in %.1fs (%.0f chunks/s)",
            result["chunk_count"], elapsed, rate,
        )
    elif status == "already_indexed":
        log.info("   ⏭ already indexed (doc #%d)", result["document_id"])
    else:
        log.error("   ✗ %s", status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest PDFs into RAG knowledge base")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", type=Path, help="Directory of PDFs to ingest")
    group.add_argument("--file", type=Path, help="Single PDF to ingest")
    parser.add_argument("--user", type=str, default=None, help="User slug (omit for global)")
    args = parser.parse_args()

    api_base = os.environ.get("EMBEDDING_API_BASE", "")
    model = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    log.info("Embedding backend: %s via %s", model, api_base or "api.openai.com")

    user_id = _resolve_user_id(args.user)
    scope = f"user '{args.user}'" if args.user else "global"
    log.info("Scope: %s", scope)

    if args.file:
        if not args.file.exists():
            log.error("File not found: %s", args.file)
            sys.exit(1)
        ingest_one(args.file, user_id)
    else:
        if not args.dir.is_dir():
            log.error("Directory not found: %s", args.dir)
            sys.exit(1)
        pdfs = sorted(args.dir.glob("*.pdf"))
        if not pdfs:
            log.warning("No PDFs found in %s", args.dir)
            sys.exit(0)
        log.info("Found %d PDFs in %s", len(pdfs), args.dir)
        for pdf in pdfs:
            ingest_one(pdf, user_id)

    log.info("Done.")


if __name__ == "__main__":
    main()
