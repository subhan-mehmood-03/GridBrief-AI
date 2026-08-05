-- 0002_core_tables.sql
-- Core structured tables from PRD §6.1.
-- All timestamps are timezone-aware UTC per PRD §18.

create table if not exists sources (
    id          bigserial primary key,
    name        text not null,
    kind        text not null,          -- e.g. 'ercot_api' | 'eia_api' | 'nws_api' | 'rss'
    base_url    text,
    constraint sources_name_key unique (name)
);

create table if not exists raw_items (
    id            bigserial primary key,
    source_id     bigint not null references sources(id) on delete cascade,
    source_ref    text not null,        -- natural id from the upstream source
    kind          text not null check (kind in ('timeseries', 'document')),
    published_at  timestamptz,
    url           text,
    raw_hash      text,                 -- content hash for de-dup (PRD §5.1)
    ingested_at   timestamptz not null default now(),
    constraint raw_items_source_ref_key unique (source_id, source_ref)
);

-- Unique raw_hash only enforced when present, so sources that never
-- populate a hash aren't forced into artificial collisions.
create unique index if not exists raw_items_raw_hash_key
    on raw_items (raw_hash)
    where raw_hash is not null;

create table if not exists timeseries (
    id                bigserial primary key,
    iso               text not null,              -- e.g. 'ERCOT'
    metric            text not null,               -- lmp | spp | system_load | wind_gen | ...
    settlement_point  text not null default '',    -- '' when the metric has no location dimension
    ts                timestamptz not null,
    value             double precision not null,
    unit              text not null,
    source_id         bigint not null references sources(id) on delete restrict,
    constraint timeseries_observation_key
        unique (iso, metric, settlement_point, ts)
);

create table if not exists documents (
    id            bigserial primary key,
    source_id     bigint not null references sources(id) on delete restrict,
    source_ref    text,                 -- natural id from the upstream source; drives upsert (PRD §5.1)
    title         text,
    url           text,
    published_at  timestamptz,
    text          text,
    topic         text,
    importance    double precision,
    chunk_ids     bigint[] not null default '{}'
);

create unique index if not exists documents_source_ref_key
    on documents (source_id, source_ref)
    where source_ref is not null;
