import asyncio
import ipaddress
import socket

import httpx
import pytest

from agentforge.director import web_research as web


def test_only_operator_text_and_search_result_urls_can_leave(monkeypatch):
    async def operation():
        calls = []

        async def fetch(url, *, search=False):
            calls.append((url, search))
            if search:
                return (
                    url,
                    "text/html",
                    (
                        '<a class="result__a" href="//duckduckgo.com/l/?uddg='
                        'https%3A%2F%2Fexample.com%2Frelease">Official release</a>'
                    ),
                )
            return url, "text/html", "<script>secret</script><p>Official release.</p>"

        monkeypatch.setattr(web, "fetch", fetch)
        research = web.WebResearch("Check the latest release", 1024)
        assert "error" in await research.call("web_search", {"query": "worker output"})
        assert "error" in await research.call(
            "web_read", {"url": "https://example.com/release"}
        )
        assert calls == []
        result = await research.call("web_search", {"query": "latest release"})
        assert result["results"][0]["url"] == "https://example.com/release"
        page = await research.call("web_read", {"url": "https://example.com/release"})
        assert page["untrusted_page_text"] == "Official release."
        assert research.sources == ["https://example.com/release"]
        assert len(calls) == 2

    asyncio.run(operation())


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "::1",
        "::ffff:192.168.1.1",
        "2002:c0a8:101::",
    ],
)
def test_dns_any_nonpublic_answer_denies_the_host(monkeypatch, address):
    async def operation():
        async def resolve(*args, **kwargs):
            return [
                (socket.AF_INET, 0, 0, "", ("93.184.216.34", 443)),
                (socket.AF_INET, 0, 0, "", (address, 443)),
            ]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        with pytest.raises(web.WebError, match="private_web_destination"):
            await web.resolve_public(web.public_url("https://example.com"))

    asyncio.run(operation())


def test_redirect_is_validated_before_connecting(monkeypatch):
    requests = []

    async def resolve(url):
        if url.host == "localhost":
            raise web.WebError("private_web_destination")
        return ipaddress.ip_address("93.184.216.34")

    def service(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://localhost/private"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(web, "resolve_public", resolve)
    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(service), **kwargs
        ),
    )
    with pytest.raises(web.WebError, match="private_web_destination"):
        asyncio.run(web.fetch("https://example.com/"))
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "example.com"
    assert requests[0].extensions["sni_hostname"] == "example.com"
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "http://example.com:8765/",
        "https://user:pass@example.com/",
        "http://[fe80::1%3]/",
    ],
)
def test_invalid_url_forms_are_rejected(url):
    with pytest.raises(web.WebError, match="invalid_web_url"):
        web.public_url(url)
