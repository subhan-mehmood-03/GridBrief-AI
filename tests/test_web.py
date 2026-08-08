from pathlib import Path

from fastapi.testclient import TestClient

from gridbrief import web as web_module
from gridbrief.config import get_settings
from gridbrief.web import create_app


def test_health_endpoint() -> None:
    app = create_app()
    health_route = next(route for route in app.routes if route.path == "/api/health")

    assert health_route.endpoint() == {"status": "ok"}


def test_required_product_routes_are_registered() -> None:
    paths = {route.path for route in create_app().routes}
    assert {
        "/",
        "/api/health",
        "/api/ready",
        "/api/status",
        "/api/config",
        "/api/metrics",
        "/api/intelligence",
        "/api/daily-use",
        "/api/edition/latest",
        "/api/editions",
        "/api/data/catalog",
        "/api/data/series",
        "/api/data/export.csv",
        "/api/weather",
        "/api/maps",
        "/api/ask",
        "/api/generate",
        "/api/automation",
    } <= paths


def test_local_automation_reports_live_scheduler_state(monkeypatch) -> None:
    monkeypatch.setenv("GRIDBRIEF_AUTOMATIC_REFRESH", "true")
    get_settings.cache_clear()
    web_module._SCHEDULER_STATE.update(
        {"running": True, "started_at": "2026-08-07T12:00:00+00:00"}
    )
    try:
        body = TestClient(create_app()).get("/api/automation").json()
        assert body["enabled"] is True
        assert body["running"] is True
        assert body["manager"] == "Web process"
        assert body["started_at"] == "2026-08-07T12:00:00+00:00"
    finally:
        web_module._SCHEDULER_STATE.update({"running": False, "started_at": None})
        get_settings.cache_clear()


def test_page_assets_and_security_headers() -> None:
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert "The grid," in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "https://fonts.googleapis.com" in response.headers["content-security-policy"]
    assert "https://fonts.gstatic.com" in response.headers["content-security-policy"]


def test_invalid_ask_always_returns_readable_string() -> None:
    response = TestClient(create_app()).post("/api/ask", json={"question": {"bad": True}})
    assert response.status_code == 200
    answer = response.json()["answer"]
    assert isinstance(answer, str)
    assert "[object Object]" not in answer
    assert not answer.lstrip().startswith("{")


def test_frontend_uses_single_safe_text_normalizer() -> None:
    javascript = (
        Path(__file__).parents[1] / "src" / "gridbrief" / "web_static" / "site.js"
    ).read_text(encoding="utf-8")
    assert "function safeText(" in javascript
    assert "displayText" not in javascript
    assert "GridBrief panel render failed" in javascript
    assert "safeText(alert.severity" in javascript
    assert "prefers-reduced-motion" in (
        Path(__file__).parents[1] / "src" / "gridbrief" / "web_static" / "site.css"
    ).read_text(encoding="utf-8")


def test_frontend_humanizes_calculations_freshness_and_live_ticker() -> None:
    static = Path(__file__).parents[1] / "src" / "gridbrief" / "web_static"
    javascript = (static / "site.js").read_text(encoding="utf-8")
    stylesheet = (static / "site.css").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")

    assert "(?:cite|calc)" in javascript
    assert "['ercot','ERCOT operational data']" in javascript
    assert "['eia','EIA-930 history']" in javascript
    assert "['nws','National Weather Service alerts']" in javascript
    assert "ticker-live 32s linear infinite" in stylesheet
    assert "@keyframes ticker-live" in stylesheet
    assert "prefers-reduced-motion:reduce" in stylesheet
    assert "/assets/site.css?v=26" in html
    assert "function startTicker(" in javascript
    assert "requestAnimationFrame(advance)" in javascript
    assert "style.setProperty('transform'" in javascript
    assert "/assets/site.js?v=35" in html


def test_public_generation_requires_admin_key(monkeypatch) -> None:
    monkeypatch.setenv("GRIDBRIEF_PUBLIC_MODE", "true")
    monkeypatch.setenv("GRIDBRIEF_ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    try:
        response = TestClient(create_app()).post(
            "/api/generate", json={"role": "general", "mode": "on_demand"}
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 403
    assert isinstance(response.json()["detail"], str)


def test_costly_endpoint_rate_limit_is_readable() -> None:
    web_module._RATE_BUCKETS.clear()
    client = TestClient(create_app())
    for _ in range(20):
        assert client.post("/api/ask", json={"question": {"invalid": True}}).status_code == 200

    response = client.post("/api/ask", json={"question": {"invalid": True}})

    assert response.status_code == 429
    assert isinstance(response.json()["detail"], str)
    assert "object Object" not in response.json()["detail"]
    web_module._RATE_BUCKETS.clear()
