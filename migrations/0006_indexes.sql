-- 0006_indexes.sql
-- Supporting indexes called out in PRD §6.1: time-series metric/location/time,
-- documents published time/topic, editions role/generated time, watermark source.

create index if not exists timeseries_metric_point_ts_idx
    on timeseries (metric, settlement_point, ts);

create index if not exists timeseries_iso_ts_idx
    on timeseries (iso, ts);

create index if not exists documents_published_topic_idx
    on documents (published_at, topic);

create index if not exists editions_role_generated_idx
    on editions (role, generated_at);

create index if not exists raw_items_published_idx
    on raw_items (published_at);

create index if not exists ingestion_runs_source_started_idx
    on ingestion_runs (source_id, started_at);
