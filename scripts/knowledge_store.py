"""
RAG knowledge base: PDF ingestion, chunking, embedding, and retrieval.

Uses PyMuPDF for text extraction, an OpenAI-compatible embedding API
(OpenAI or Ollama), and pgvector on TimescaleDB for vector storage/search.

Configuration via environment variables:
    EMBEDDING_API_BASE  — API base URL (default: OpenAI; set to
                          http://localhost:11434/v1 for local Ollama)
    EMBEDDING_MODEL     — Model name (default: text-embedding-3-small)
    EMBEDDING_DIM       — Vector dimensions (default: 768)
    OPENAI_API_KEY      — API key (required for OpenAI; any value for Ollama)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from openai import OpenAI

log = logging.getLogger(__name__)

EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "768"))
CHUNK_TARGET_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50
APPROX_CHARS_PER_TOKEN = 4

_openai_client: OpenAI | None = None

_EMBED_BATCH_SIZE = 512


# ---------------------------------------------------------------------------
# DB connection (reuse pattern from health_tools)
# ---------------------------------------------------------------------------

def _get_conn():
    from scripts.db_pool import get_conn
    return get_conn()


# ---------------------------------------------------------------------------
# Embedding client (OpenAI-compatible: works with OpenAI API and Ollama)
# ---------------------------------------------------------------------------

def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key and not EMBEDDING_API_BASE:
            raise RuntimeError(
                "OPENAI_API_KEY is not set and no EMBEDDING_API_BASE configured. "
                "Set OPENAI_API_KEY for OpenAI embeddings, or point "
                "EMBEDDING_API_BASE at a local Ollama instance."
            )
        kwargs: dict[str, Any] = {"api_key": api_key or "ollama"}
        if EMBEDDING_API_BASE:
            kwargs["base_url"] = EMBEDDING_API_BASE
        _openai_client = OpenAI(**kwargs)
    return _openai_client


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, batching to stay within API limits."""
    client = _get_openai_client()
    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH_SIZE):
        batch = texts[i : i + _EMBED_BATCH_SIZE]
        kwargs: dict[str, Any] = {"model": EMBEDDING_MODEL, "input": batch}
        if not EMBEDDING_API_BASE:
            kwargs["dimensions"] = EMBEDDING_DIM
        resp = client.embeddings.create(**kwargs)
        all_embeddings.extend(item.embedding for item in resp.data)
    return all_embeddings


def _embed_query(query: str) -> list[float]:
    client = _get_openai_client()
    kwargs: dict[str, Any] = {"model": EMBEDDING_MODEL, "input": query}
    if not EMBEDDING_API_BASE:
        kwargs["dimensions"] = EMBEDDING_DIM
    resp = client.embeddings.create(**kwargs)
    return resp.data[0].embedding


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_text_from_pdf(pdf_path: str | Path) -> list[dict]:
    """Extract text from a PDF, returning a list of {page_number, text} dicts."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pages = []
    for page_num, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            pages.append({"page_number": page_num + 1, "text": text})
    return pages


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk_pages(pages: list[dict]) -> list[dict]:
    """Split pages into overlapping chunks of ~CHUNK_TARGET_TOKENS tokens.

    Returns list of {content, page_number, chunk_index}.
    """
    char_target = CHUNK_TARGET_TOKENS * APPROX_CHARS_PER_TOKEN
    char_overlap = CHUNK_OVERLAP_TOKENS * APPROX_CHARS_PER_TOKEN

    full_text_parts: list[tuple[str, int]] = []
    for page in pages:
        paragraphs = re.split(r"\n{2,}", page["text"].strip())
        for para in paragraphs:
            para = para.strip()
            if para:
                full_text_parts.append((para, page["page_number"]))

    chunks: list[dict] = []
    current_text = ""
    current_page = pages[0]["page_number"] if pages else 1
    chunk_idx = 0

    for para, page_num in full_text_parts:
        if len(current_text) + len(para) + 1 > char_target and current_text:
            chunks.append({
                "content": current_text.strip(),
                "page_number": current_page,
                "chunk_index": chunk_idx,
            })
            chunk_idx += 1
            overlap_start = max(0, len(current_text) - char_overlap)
            current_text = current_text[overlap_start:] + "\n" + para
            current_page = page_num
        else:
            if current_text:
                current_text += "\n" + para
            else:
                current_text = para
                current_page = page_num

    if current_text.strip():
        chunks.append({
            "content": current_text.strip(),
            "page_number": current_page,
            "chunk_index": chunk_idx,
        })

    return chunks


# ---------------------------------------------------------------------------
# SHA-256 helper
# ---------------------------------------------------------------------------

def _file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def ingest_pdf(pdf_path: str | Path, user_id: int | None = None) -> dict[str, Any]:
    """Ingest a PDF into the knowledge base.

    Returns document metadata dict.  Skips if the same file (by SHA-256)
    is already indexed for the same user scope.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return {"error": f"File not found: {pdf_path}"}

    sha = _file_sha256(pdf_path)
    conn = _get_conn()
    try:
        cur = conn.cursor()

        # Deduplication check
        if user_id is not None:
            cur.execute(
                "SELECT id, filename FROM documents WHERE sha256 = %s AND user_id = %s",
                (sha, user_id),
            )
        else:
            cur.execute(
                "SELECT id, filename FROM documents WHERE sha256 = %s AND user_id IS NULL",
                (sha,),
            )
        existing = cur.fetchone()
        if existing:
            return {
                "status": "already_indexed",
                "document_id": existing[0],
                "filename": existing[1],
            }

        # Extract text
        pages = _extract_text_from_pdf(pdf_path)
        if not pages:
            return {"error": "No extractable text found in PDF"}

        # Chunk
        chunks = _chunk_pages(pages)
        if not chunks:
            return {"error": "No chunks produced from PDF"}

        log.info("Embedding %d chunks from %s ...", len(chunks), pdf_path.name)
        texts = [c["content"] for c in chunks]
        embeddings = _embed_texts(texts)

        # Derive title from filename
        title = pdf_path.stem.replace("_", " ").replace("-", " ").strip()

        # Insert document
        cur.execute(
            """INSERT INTO documents (user_id, filename, title, sha256, page_count, chunk_count)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (user_id, pdf_path.name, title, sha, len(pages), len(chunks)),
        )
        doc_id = cur.fetchone()[0]

        # Bulk insert chunks with embeddings
        values = []
        for chunk, emb in zip(chunks, embeddings):
            values.append((
                doc_id,
                chunk["chunk_index"],
                chunk["content"],
                chunk["page_number"],
                emb,
            ))

        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO knowledge_chunks (document_id, chunk_index, content, page_number, embedding)
               VALUES %s""",
            values,
            template="(%s, %s, %s, %s, %s::vector)",
        )

        conn.commit()
        log.info("Indexed %s: %d pages, %d chunks", pdf_path.name, len(pages), len(chunks))

        return {
            "status": "indexed",
            "document_id": doc_id,
            "filename": pdf_path.name,
            "title": title,
            "page_count": len(pages),
            "chunk_count": len(chunks),
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ingest_directory(dir_path: str | Path, user_id: int | None = None) -> list[dict]:
    """Ingest all PDFs in a directory. Returns list of per-file results."""
    dir_path = Path(dir_path)
    if not dir_path.is_dir():
        log.info("Knowledge directory %s does not exist, skipping.", dir_path)
        return []

    results = []
    for pdf in sorted(dir_path.glob("*.pdf")):
        try:
            result = ingest_pdf(pdf, user_id=user_id)
            results.append(result)
            log.info("  %s: %s", pdf.name, result.get("status", result.get("error")))
        except Exception as exc:
            log.error("  %s: FAILED - %s", pdf.name, exc)
            results.append({"filename": pdf.name, "error": str(exc)})

    return results


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def search_knowledge(
    query: str,
    user_id: int | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Semantic search across the knowledge base.

    Returns top_k chunks matching the query, filtered to documents visible
    to the given user (their own + global docs).
    """
    query_embedding = _embed_query(query)

    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT
                   kc.content,
                   kc.page_number,
                   kc.chunk_index,
                   d.filename,
                   d.title,
                   d.id AS document_id,
                   1 - (kc.embedding <=> %s::vector) AS similarity
               FROM knowledge_chunks kc
               JOIN documents d ON d.id = kc.document_id
               WHERE d.user_id IS NULL OR d.user_id = %s
               ORDER BY kc.embedding <=> %s::vector
               LIMIT %s""",
            (query_embedding, user_id, query_embedding, top_k),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Document management
# ---------------------------------------------------------------------------

def list_documents(user_id: int | None = None) -> list[dict[str, Any]]:
    """List all documents visible to a user (their own + global)."""
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT id, user_id, filename, title, page_count, chunk_count, created_at
               FROM documents
               WHERE user_id IS NULL OR user_id = %s
               ORDER BY created_at DESC""",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def delete_document(document_id: int) -> dict[str, Any]:
    """Delete a document and all its chunks (cascade)."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT filename FROM documents WHERE id = %s", (document_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"Document {document_id} not found"}

        cur.execute("DELETE FROM documents WHERE id = %s", (document_id,))
        conn.commit()
        log.info("Deleted document %d (%s)", document_id, row[0])
        return {"status": "deleted", "document_id": document_id, "filename": row[0]}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def document_count(user_id: int | None = None) -> int:
    """Return the number of documents visible to a user."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM documents WHERE user_id IS NULL OR user_id = %s",
            (user_id,),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()
