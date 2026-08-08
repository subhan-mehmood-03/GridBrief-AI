from datetime import UTC, datetime, timedelta

from gridbrief.adapters.nws import NWSAdapter


def test_active_alert_snapshot_preserves_risk_metadata(monkeypatch) -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    adapter = NWSAdapter("operator@example.com")
    monkeypatch.setattr(
        adapter,
        "_get",
        lambda _url: {
            "features": [
                {
                    "id": "https://api.weather.gov/alerts/example",
                    "properties": {
                        "id": "alert-1",
                        "sent": (now - timedelta(hours=1)).isoformat(),
                        "event": "Flood Warning",
                        "headline": "Flood Warning for Example County",
                        "description": "Flooding is occurring.",
                        "severity": "Severe",
                        "areaDesc": "Example County; Sample County",
                        "effective": now.isoformat(),
                        "expires": (now + timedelta(hours=2)).isoformat(),
                    },
                }
            ]
        },
    )

    items = adapter._fetch_alerts(now - timedelta(hours=24), now)

    alert, snapshot = items
    assert "Severity: Severe" in alert.payload["text"]
    assert "Areas: Example County; Sample County" in alert.payload["text"]
    assert "Expires:" in alert.payload["text"]
    assert snapshot.source_ref == "active-alert-snapshot"
    assert snapshot.payload["topic"] == "weather_snapshot"
    assert '"alert-1"' in snapshot.payload["text"]
