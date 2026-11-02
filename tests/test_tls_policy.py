"""TLS exceptions never widen to other hosts or redirect destinations."""
import asyncio
import ssl

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.http_client import outbound_client


@pytest.mark.parametrize("value", ["*", "*.example.invalid", "https://example.invalid", "example.invalid:443", "example.invalid/path", "user@example.invalid"])
def test_exception_configuration_requires_exact_dns_names(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, tls_insecure_hosts=value)


def test_exception_configuration_normalizes_and_deduplicates():
    settings = Settings(_env_file=None, tls_insecure_hosts=" EXAMPLE.invalid,example.invalid, ,other.invalid ")
    assert settings.tls_insecure_hosts == "example.invalid,other.invalid"


@pytest.fixture
def recorded_transports(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    created = []
    requests = []

    class RecordedTransport(httpx.MockTransport):
        def __init__(self, **kwargs):
            self.options = kwargs
            self.closed = False
            self.used = False
            created.append(self)
            super().__init__(self.handle)

        def handle(self, request):
            self.used = True
            context = self.options.get("verify", True)
            insecure = isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_NONE
            requests.append((str(request.url), insecure))
            if request.url.path == "/redirect":
                return httpx.Response(302, headers={"Location": "https://other.invalid/final"})
            return httpx.Response(200, content=b"ok")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", RecordedTransport)
    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", RecordedTransport)
    return created, requests


def test_strict_default_and_exact_host_exceptions(recorded_transports):
    created, requests = recorded_transports

    async def run():
        async with outbound_client(Settings(_env_file=None)) as client:
            await client.get("https://example.invalid/")
        async with outbound_client(Settings(_env_file=None, tls_insecure_hosts="example.invalid"), follow_redirects=True) as client:
            for url in ("https://example.invalid/", "https://EXAMPLE.invalid/", "https://sub.example.invalid/",
                        "https://example.invalid.evil.invalid/", "https://model.invalid/", "http://example.invalid/"):
                await client.get(url)
            async with client.stream("GET", "https://example.invalid/redirect") as response:
                assert await response.aread() == b"ok"

    asyncio.run(run())
    assert [insecure for _, insecure in requests] == [False, True, True, False, False, False, False, True, False]
    context = next(t.options["verify"] for t in created if isinstance(t.options.get("verify"), ssl.SSLContext))
    assert context.security_level == 1
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert all(t.closed for t in created)


@pytest.mark.parametrize("no_proxy,expected_proxy", [("", "http://proxy.invalid:8080"), ("example.invalid", None)])
def test_exception_keeps_environment_proxy_route(recorded_transports, monkeypatch, no_proxy, expected_proxy):
    created, requests = recorded_transports
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", no_proxy)

    async def run():
        async with outbound_client(Settings(_env_file=None, tls_insecure_hosts="example.invalid")) as client:
            await client.get("https://example.invalid/")

    asyncio.run(run())
    exception = next(t for t in created if t.used and isinstance(t.options.get("verify"), ssl.SSLContext))
    proxy = exception.options.get("proxy")
    assert (str(proxy.url) if proxy is not None else None) == expected_proxy
    assert requests == [("https://example.invalid/", True)]
