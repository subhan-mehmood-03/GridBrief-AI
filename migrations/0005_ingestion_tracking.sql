-- 0005_ingestion_tracking.sql
-- Watermarks/runs make ingestion idempotent and incremental (PRD §5.1).
-- breaking_triggers implements the cooldown + de-dup rules in PRD §8.1.

create table if not exists ingestion_watermarks (
    id               bigserial primary key,
    source_id        bigint not null references sources(id) on delete cascade,
    last_success_at  timestamptz,
    window_end       timestamptz,
    status           text,
    detail_json      jsonb,
    constraint ingestion_watermarks_source_key unique (source_id)
);

create table if not exists ingestion_runs (
    id            bigserial primary key,
    source_id     bigint not null references sources(id) on delete cascade,
    started_at    timestamptz not null default now(),
    completed_at  timestamptz,
    status        text,
    inserted      integer not null default 0,
    updated       integer not null default 0,
    skipped       integer not null default 0,
    error         text
);

create table if not exists breaking_triggers (
    id               bigserial primary key,
    source_ref       text not null,
    topic            text not null,
    severity         text not null,
    fingerprint      text not null,       -- identifies "the same event" for cooldown/de-dup
    fired_at         timestamptz not null default now(),
    cooldown_until   timestamptz,
    constraint breaking_triggers_fingerprint_key unique (fingerprint)
);
