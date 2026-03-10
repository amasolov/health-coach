-- RAG knowledge base: pgvector extension, documents, and chunks tables.
-- Requires pgvector to be installed in the TimescaleDB image (included since 2.11+).

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- Documents (metadata about uploaded PDFs / files)
-- ============================================================================
CREATE TABLE IF NOT EXISTS documents (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id),  -- NULL = global/shared
    filename        TEXT NOT NULL,
    title           TEXT,
    sha256          TEXT NOT NULL,
    page_count      INTEGER,
    chunk_count     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (sha256, user_id)
);

-- ============================================================================
-- Knowledge chunks (text + embedding vectors)
-- ============================================================================
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id              SERIAL PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    page_number     INTEGER,
    embedding       vector(768) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS knowledge_chunks_document_idx
    ON knowledge_chunks (document_id);
