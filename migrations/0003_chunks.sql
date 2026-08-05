-- 0003_chunks.sql
-- Semantic retrieval index (PRD §6.2). 768-dim to match BAAI/bge-base-en-v1.5.

create table if not exists chunks (
    chunk_id      bigserial primary key,
    document_id   bigint not null references documents(id) on delete cascade,
    iso           text not null,
    text          text not null,
    embedding     vector(768) not null,
    source        text,
    topic         text,
    published_at  timestamptz,
    url           text
);

-- HNSW index for cosine-similarity search (Person 4 / Person 5 depend on this).
create index if not exists chunks_embedding_hnsw_idx
    on chunks using hnsw (embedding vector_cosine_ops);

create index if not exists chunks_document_id_idx on chunks (document_id);
