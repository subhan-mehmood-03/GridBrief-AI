from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from gridbrief.models import Document, RawItem, Source, Timeseries


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_source_upsert_round_trip(repo):
    s1 = repo.upsert_source(name="ercot_api", kind="ercot_api", base_url="https://api.ercot.com")
    s2 = repo.upsert_source(name="ercot_api", kind="ercot_api", base_url="https://api.ercot.com/v2")
    assert s1.id == s2.id
    assert s2.base_url == "https://api.ercot.com/v2"

    count = repo.session.execute(select(func.count()).select_from(Source)).scalar_one()
    assert count == 1


def test_raw_item_upsert_is_idempotent(repo):
    source = repo.upsert_source(name="ercot_rss", kind="rss")
    now = _utc(2026, 8, 1, 12, 0, 0)

    repo.upsert_raw_item(
        source_id=source.id,
        source_ref="notice-123",
        kind="document",
        published_at=now,
        url="https://ercot.com/notice-123",
        raw_hash="abc123",
        ingested_at=now,
    )
    # Re-ingesting the same item (e.g. a re-run of the poller) must not
    # create a duplicate row.
    repo.upsert_raw_item(
        source_id=source.id,
        source_ref="notice-123",
        kind="document",
        published_at=now,
        url="https://ercot.com/notice-123-updated",
        raw_hash="abc123",
        ingested_at=now,
    )

    count = repo.session.execute(select(func.count()).select_from(RawItem)).scalar_one()
    assert count == 1


def test_timeseries_upsert_applies_revisions(repo):
    source = repo.upsert_source(name="eia_api", kind="eia_api")
    ts = _utc(2026, 8, 1, 6, 0, 0)

    repo.upsert_timeseries_point(
        iso="ERCOT",
        metric="system_load",
        settlement_point="",
        ts=ts,
        value=45000.0,
        unit="MW",
        source_id=source.id,
    )
    # EIA/ERCOT restate values; re-ingesting the same (iso, metric,
    # settlement_point, ts) key should overwrite, not duplicate (PRD §5.1).
    repo.upsert_timeseries_point(
        iso="ERCOT",
        metric="system_load",
        settlement_point="",
        ts=ts,
        value=45210.5,
        unit="MW",
        source_id=source.id,
    )

    count = repo.session.execute(select(func.count()).select_from(Timeseries)).scalar_one()
    assert count == 1

    rows = repo.get_timeseries(
        metric="system_load",
        settlement_point="",
        start=_utc(2026, 8, 1, 0, 0, 0),
        end=_utc(2026, 8, 1, 23, 59, 59),
    )
    assert len(rows) == 1
    assert rows[0].value == 45210.5


def test_document_upsert_on_source_ref(repo):
    source = repo.upsert_source(name="eia_today_in_energy", kind="rss")

    repo.upsert_document(
        source_id=source.id,
        source_ref="article-42",
        title="Original title",
        url="https://eia.gov/todayinenergy/article-42",
        published_at=_utc(2026, 8, 1, 9, 0, 0),
        text="Draft text.",
        topic="policy",
        importance=0.4,
    )
    repo.upsert_document(
        source_id=source.id,
        source_ref="article-42",
        title="Updated title",
        url="https://eia.gov/todayinenergy/article-42",
        published_at=_utc(2026, 8, 1, 9, 0, 0),
        text="Final text.",
        topic="policy",
        importance=0.6,
    )

    count = repo.session.execute(select(func.count()).select_from(Document)).scalar_one()
    assert count == 1

    doc = repo.session.execute(
        select(Document).where(Document.source_ref == "article-42")
    ).scalar_one()
    assert doc.title == "Updated title"
    assert doc.importance == 0.6


def test_watermark_upsert_and_ingestion_run(repo):
    source = repo.upsert_source(name="nws_api", kind="nws_api")
    started = _utc(2026, 8, 1, 5, 0, 0)

    run = repo.start_ingestion_run(source_id=source.id, started_at=started)
    repo.finish_ingestion_run(
        run.id,
        completed_at=_utc(2026, 8, 1, 5, 1, 0),
        status="success",
        inserted=12,
        updated=3,
        skipped=0,
    )
    repo.upsert_watermark(
        source_id=source.id,
        last_success_at=_utc(2026, 8, 1, 5, 1, 0),
        window_end=_utc(2026, 8, 1, 5, 0, 0),
        status="ok",
    )

    watermark = repo.get_watermark(source.id)
    assert watermark is not None
    assert watermark.status == "ok"


def test_breaking_trigger_is_idempotent_per_fingerprint(repo):
    fired_at = _utc(2026, 8, 1, 14, 0, 0)
    _, created_first = repo.fire_breaking_trigger(
        source_ref="ercot-eea-notice-9",
        topic="grid",
        severity="EEA2",
        fingerprint="eea2-2026-08-01",
        fired_at=fired_at,
        cooldown_until=_utc(2026, 8, 1, 15, 0, 0),
    )
    _, created_second = repo.fire_breaking_trigger(
        source_ref="ercot-eea-notice-9",
        topic="grid",
        severity="EEA2",
        fingerprint="eea2-2026-08-01",
        fired_at=fired_at,
        cooldown_until=_utc(2026, 8, 1, 15, 0, 0),
    )
    assert created_first is True
    assert created_second is False


def test_edition_and_claims_round_trip(repo):
    edition = repo.save_edition(
        iso="ERCOT",
        role="market_analyst",
        cycle_date=_utc(2026, 8, 1, 0, 0, 0).date(),
        generated_at=_utc(2026, 8, 1, 7, 0, 0),
        status="published",
        markdown="# Market Analyst Brief",
        html="<h1>Market Analyst Brief</h1>",
        json={"sections": []},
    )
    claim = repo.add_edition_claim(
        edition_id=edition.id,
        claim_text="North Hub RT price rose 12% overnight.",
        cited_chunk_ids=[1, 2],
        verified=True,
        groundedness=0.95,
    )
    assert claim.edition_id == edition.id

    latest = repo.get_latest_edition(role="market_analyst")
    assert latest is not None
    assert latest.id == edition.id
