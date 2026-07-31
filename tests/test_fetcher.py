import asyncio
import logging

from wayback_tool.fetcher import WaybackFetcher, _redact_proxy_url


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FakeSession:
    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.status)


def test_proxy_check_enables_working_proxy(caplog) -> None:
    fetcher = WaybackFetcher(proxy="http://user:secret@proxy.test:8080")
    session = FakeSession(200)
    fetcher._session = session

    with caplog.at_level(logging.INFO):
        asyncio.run(fetcher._configure_proxy())

    assert fetcher.active_proxy == "http://user:secret@proxy.test:8080"
    assert session.calls[0][1]["proxy"] == fetcher.active_proxy
    assert "check succeeded" in caplog.text
    assert "secret" not in caplog.text


def test_proxy_check_falls_back_to_direct_connection(caplog) -> None:
    fetcher = WaybackFetcher(proxy="http://proxy.test:8080")
    fetcher._session = FakeSession(502)

    with caplog.at_level(logging.WARNING):
        asyncio.run(fetcher._configure_proxy())

    assert fetcher.active_proxy is None
    assert "falling back to a direct connection" in caplog.text


def test_proxy_credentials_are_redacted() -> None:
    assert (
        _redact_proxy_url("http://user:secret@proxy.test:8080")
        == "http://***:***@proxy.test:8080"
    )
