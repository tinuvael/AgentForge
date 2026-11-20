import asyncio
import ipaddress
import socket

import httpx
import pytest

from agentforge.director import web_research as web
from agentforge.director.journal import Journal
from agentforge.director.models import VisibleMessage

PARENTHESIZED_URL = "https://en.wikipedia.org/wiki/Python_(programming_language)"


@pytest.mark.parametrize(
    "request_text",
    [
        "Read {url}",
        "Read [Python]({url}).",
        "Read ({url}).",
        "Read the reference (see {url}).",
        "Read {url}, please.",
        "Read {url}!",
    ],
)
def test_read_preserves_parentheses_in_the_authorized_url(monkeypatch, request_text):
    calls = []

    async def fetch(url):
        calls.append(url)
        return url, "text/plain", "Python is a programming language."

    monkeypatch.setattr(web, "fetch", fetch)
    research = web.WebResearch(request_text.format(url=PARENTHESIZED_URL), 1024)

    async def operation():
        result = await research.call("web_read", {"url": PARENTHESIZED_URL})
        assert result["url"] == PARENTHESIZED_URL
        for other_url in (PARENTHESIZED_URL[:-1], "https://example.com/unrelated"):
            assert await research.call("web_read", {"url": other_url}) == {
                "error": "web_url_not_authorized_for_this_request"
            }

    asyncio.run(operation())
    assert calls == [PARENTHESIZED_URL]
    assert research.sources == [PARENTHESIZED_URL]


@pytest.mark.parametrize("request_text", ["Read [link]({url}).", "Read <{url}>."])
def test_explicit_url_delimiters_preserve_trailing_punctuation(request_text):
    url = "https://example.com/?q=what!"
    research = web.WebResearch(request_text.format(url=url), 1024)
    assert research.allowed_urls == {url}


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


def test_source_urls_count_toward_private_history_budget(tmp_path):
    journal = Journal(tmp_path / "journal")
    journal.open()
    try:
        item = journal.create("operator", "gateway")
        sources = [f"https://example.com/{index}/" + "a" * 1900 for index in range(8)]
        item.messages = [
            VisibleMessage(role="assistant", content=f"Result {index}", sources=sources)
            for index in range(12)
        ]
        journal.save(item)
        restored = journal.read(item.id, "operator", "gateway")
        assert len(restored.messages) == 6
        assert restored.messages[-1].content == "Result 11"
        assert restored.messages[-1].sources == sources
    finally:
        journal.close()
