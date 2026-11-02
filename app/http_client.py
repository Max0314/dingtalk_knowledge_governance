"""Outbound HTTPS policy, scoped to exact administrator-configured hosts."""
from __future__ import annotations

import ssl

import httpx

from .config import Settings


class _HostTLSAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, hosts: set[str]):
        self.hosts = hosts
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        # Support the observed gateway without enabling TLS 1.0/1.1.
        context.set_ciphers("DEFAULT:@SECLEVEL=1")
        # Inner clients preserve HTTPX's proxy, NO_PROXY and CA-bundle rules.
        # Only the outer client follows redirects, choosing TLS again per hop.
        self.strict = httpx.AsyncClient()
        self.compatible = httpx.AsyncClient(verify=context)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        compatible = request.url.scheme == "https" and request.url.host in self.hosts
        client = self.compatible if compatible else self.strict
        return await client.send(request, stream=True, follow_redirects=False)

    async def aclose(self) -> None:
        try:
            await self.strict.aclose()
        finally:
            await self.compatible.aclose()


def outbound_client(settings: Settings, *, timeout: float = 5.0,
                    follow_redirects: bool = False) -> httpx.AsyncClient:
    options = {"timeout": timeout, "follow_redirects": follow_redirects}
    if not settings.tls_insecure_hosts:
        return httpx.AsyncClient(**options)
    transport = _HostTLSAsyncTransport(set(settings.tls_insecure_hosts.split(",")))
    return httpx.AsyncClient(transport=transport, **options)
