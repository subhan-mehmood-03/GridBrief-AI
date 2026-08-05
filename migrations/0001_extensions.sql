-- 0001_extensions.sql
-- Enables pgvector for semantic retrieval (PRD §6.2).
create extension if not exists vector;
