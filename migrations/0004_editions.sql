-- 0004_editions.sql
-- Generated newsletter editions and their verification traces (PRD §6.1, §7, §8).

create table if not exists editions (
    id            bigserial primary key,
    iso           text not null,
    role          text not null,        -- general | market_analyst | grid_operations
    cycle_date    date not null,
    generated_at  timestamptz not null default now(),
    status        text not null default 'draft',
    markdown      text,
    html          text,
    json          jsonb
);

create table if not exists edition_claims (
    id                bigserial primary key,
    edition_id        bigint not null references editions(id) on delete cascade,
    claim_text        text not null,
    cited_chunk_ids   bigint[] not null default '{}',
    verified          boolean not null default false,
    groundedness      double precision
);

create table if not exists eval_runs (
    id           bigserial primary key,
    edition_id   bigint references editions(id) on delete cascade,
    metric       text not null,
    value        double precision,
    detail_json  jsonb,
    created_at   timestamptz not null default now()
);
