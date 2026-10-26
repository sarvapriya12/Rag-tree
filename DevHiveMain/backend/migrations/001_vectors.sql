-- Run in Supabase SQL Editor before DEVHIVE_RAG_BACKEND=supabase or turbovec.
-- The SQLAlchemy tables must exist first (start the backend once with local RAG mode).
-- Do not expose the devhive schema in Supabase's API settings.
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
CREATE SCHEMA IF NOT EXISTS devhive;
CREATE TABLE IF NOT EXISTS devhive.memory_vectors (
  chunk_id integer PRIMARY KEY REFERENCES devhive.memory_chunks(id) ON DELETE CASCADE,
  embedding extensions.vector(384) NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_vectors_hnsw
  ON devhive.memory_vectors USING hnsw (embedding extensions.vector_cosine_ops);
