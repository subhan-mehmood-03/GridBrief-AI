from types import SimpleNamespace

from gridbrief.adapters.rss import RSSAdapter


def test_rss_fetch_retries_transient_network_failure(monkeypatch) -> None:
    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"feed"

    def fake_open(*args, **kwargs):
        del args, kwargs
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("temporary upstream failure")
        return Response()

    parser = SimpleNamespace(
        parse=lambda content: SimpleNamespace(bozo=False, entries=[], content=content)
    )
    monkeypatch.setattr("gridbrief.adapters.rss.urlopen", fake_open)
    monkeypatch.setattr("gridbrief.adapters.rss.time.sleep", lambda seconds: None)

    result = RSSAdapter._fetch_feed(parser, "https://example.com/feed.xml")

    assert len(attempts) == 2
    assert result.entries == []
