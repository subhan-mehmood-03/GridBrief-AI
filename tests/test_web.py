from gridbrief.web import create_app


def test_health_endpoint() -> None:
    app = create_app()
    health_route = next(route for route in app.routes if route.path == "/api/health")

    assert health_route.endpoint() == {"status": "ok"}
