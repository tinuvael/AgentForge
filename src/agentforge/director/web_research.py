"""Public web research for one explicitly authorized Director request."""

import asyncio
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from pydantic import Field

from agentforge.core.inference import ToolDefinition
from agentforge.director.models import Record

SEARCH_URL = "https://html.duckduckgo.com/html/"
SEARCH_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 3
MAX_OPERATIONS = 8
URL_PATTERN = re.compile(r"""https?://[^\s<>"`]+""", re.IGNORECASE)
NATIVE_V6 = ipaddress.ip_network("2000::/3")
TUNNEL_V6 = ipaddress.ip_network("2002::/16")


class Search(Record):
    query: str = Field(min_length=1, max_length=240)


class ReadPage(Record):
    url: str = Field(min_length=1, max_length=2048)


ARGUMENTS = {"web_search": Search, "web_read": ReadPage}
TOOLS = [
    ToolDefinition(
        name="web_search",
        description=(
            "Search the public web using an exact phrase from this user's request."
        ),
        parameters=Search.model_json_schema(),
    ),
    ToolDefinition(
        name="web_read",
        description="Read a public URL supplied by the user or returned by search.",
        parameters=ReadPage.model_json_schema(),
    ),
]


class WebError(Exception):
    pass


def _url_from_text(text, match):
    url = match[0]
    before = text[match.start() - 1 : match.start()]
    after = text[match.end() : match.end() + 1]
    # Explicit delimiters can distinguish URL punctuation from prose.
    if (before, after) in {("<", ">"), ('"', '"'), ("`", "`")}:
        return url
    if before == "'" and "'" in url:
        return url.rsplit("'", 1)[0]

    if before == "(":
        # An outer prose or Markdown parenthesis closes only after the
        # parentheses within the URL have balanced.
        depth = 0
        for index, char in enumerate(url):
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    return url[:index]
                depth -= 1
        if text.endswith("](", 0, match.start()):
            return url

    url = url.rstrip(".,;!?")
    extra_closers = url.count(")") - url.count("(")
    if extra_closers > 0:
        trailing_closers = len(url) - len(url.rstrip(")"))
        return url[: len(url) - min(extra_closers, trailing_closers)]
    return url


def public_url(value):
    if len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise WebError("invalid_web_url")
    try:
        url = httpx.URL(value)
        port = 443 if url.scheme == "https" else 80
        if (
            url.scheme not in {"https", "http"}
            or not url.host
            or url.userinfo
            or "\\" in value
            or "%" in url.host
            or url.port not in {None, port}
        ):
            raise WebError("invalid_web_url")
        return url.copy_with(fragment=None)
    except (httpx.InvalidURL, ValueError):
        raise WebError("invalid_web_url") from None


async def resolve_public(url):
    host = url.raw_host.decode("ascii")
    if ("." not in host.rstrip(".") and ":" not in host) or host.rstrip(".").endswith(
        (".localhost", ".local")
    ):
        raise WebError("private_web_destination")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host,
            url.port or (443 if url.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        ips = {ipaddress.ip_address(address[4][0]) for address in addresses}
    except (OSError, ValueError):
        raise WebError("web_unavailable") from None
    if not ips or any(
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip.version == 6
        and (ip not in NATIVE_V6 or ip in TUNNEL_V6)
        for ip in ips
    ):
        raise WebError("private_web_destination")
    return sorted(ips, key=lambda ip: (ip.version, int(ip)))[0]


async def fetch(value, *, search=False):
    url = public_url(value)
    try:
        async with asyncio.timeout(12):
            for _ in range(MAX_REDIRECTS + 1):
                if search and (
                    url.scheme != "https" or url.host != "html.duckduckgo.com"
                ):
                    raise WebError("search_unavailable")
                ip = await resolve_public(url)
                # Connect to the validated address while checking TLS against
                # the original hostname. Do not share cookies, pools or proxies.
                async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                    async with client.stream(
                        "GET",
                        url.copy_with(host=str(ip)),
                        headers={
                            "Host": url.netloc.decode("ascii"),
                            "Accept": "text/html, text/plain, application/xhtml+xml",
                            "Accept-Encoding": "identity",
                            "User-Agent": SEARCH_AGENT if search else "AgentForge/1.0",
                        },
                        extensions={"sni_hostname": url.raw_host.decode("ascii")},
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise WebError("web_invalid_redirect")
                            url = public_url(urljoin(str(url), location))
                            continue
                        if response.status_code != 200:
                            raise WebError(
                                "search_unavailable" if search else "web_http_error"
                            )
                        media = (
                            response.headers.get("content-type", "")
                            .split(";")[0]
                            .strip()
                            .lower()
                        )
                        if (
                            media
                            not in {
                                "text/html",
                                "text/plain",
                                "application/xhtml+xml",
                            }
                            or response.headers.get("content-encoding", "identity")
                            != "identity"
                        ):
                            raise WebError("web_unsupported_content")
                        content = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=8192):
                            if len(content) + len(chunk) > MAX_BYTES:
                                raise WebError("web_response_too_large")
                            content.extend(chunk)
                        return (
                            str(url),
                            media,
                            content.decode(
                                response.encoding or "utf-8", errors="replace"
                            ),
                        )
            raise WebError("web_redirect_limit")
    except (TimeoutError, httpx.HTTPError, LookupError, UnicodeError, ValueError):
        raise WebError("search_unavailable" if search else "web_unavailable") from None


class SearchResults(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.target = None
        self.label = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "result__a" in (attrs.get("class") or "").split():
            self.target = attrs.get("href")
            self.label = []

    def handle_data(self, data):
        if self.target:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self.target:
            return
        target, self.target = self.target, None
        if len(self.results) >= 5:
            return
        try:
            href = urlsplit(urljoin(SEARCH_URL, target))
            if (
                href.hostname in {"duckduckgo.com", "html.duckduckgo.com"}
                and href.path == "/l/"
            ):
                target = parse_qs(href.query).get("uddg", [""])[0]
            url = str(public_url(target))
        except (ValueError, WebError):
            return
        if all(result["url"] != url for result in self.results):
            self.results.append(
                {"url": url, "title": " ".join("".join(self.label).split())[:160]}
            )


class PageText(HTMLParser):
    HIDDEN = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__()
        self.hidden = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden.append(tag)
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "td", "tr", "main"}:
            self.handle_data(" ")

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


class WebResearch:
    def __init__(self, text, result_bytes):
        self.user_text = text
        self.allowed_urls = set()
        for match in URL_PATTERN.finditer(text):
            try:
                self.allowed_urls.add(str(public_url(_url_from_text(text, match))))
            except WebError:
                continue
        self.sources = []
        self.operations = 0
        self.result_bytes = result_bytes

    def result_fits(self, result):
        return (
            len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            <= self.result_bytes
        )

    async def call(self, name, arguments):
        if self.operations >= MAX_OPERATIONS:
            return {"error": "web_operation_limit"}
        self.operations += 1
        try:
            if name == "web_search":
                query = arguments["query"].strip()
                if (
                    not query
                    or query not in self.user_text
                    or URL_PATTERN.search(query)
                ):
                    raise WebError("search_phrase_must_come_from_user_request")
                url = httpx.URL(SEARCH_URL, params={"q": query})
                _, _, html = await fetch(str(url), search=True)
                parser = SearchResults()
                parser.feed(html)
                if not parser.results:
                    raise WebError("search_unavailable_or_no_results")
                results = []
                for entry in parser.results:
                    if self.result_fits({"results": [*results, entry]}):
                        results.append(entry)
                if not results:
                    raise WebError("web_result_limit")
                self.allowed_urls.update(result["url"] for result in results)
                return {"results": results}
            url = str(public_url(arguments["url"]))
            if url not in self.allowed_urls:
                raise WebError("web_url_not_authorized_for_this_request")
            final, media, text = await fetch(url)
            if media != "text/plain":
                parser = PageText()
                parser.feed(text)
                text = "".join(parser.text)
            text = " ".join(text.split())
            if not text:
                raise WebError("web_no_readable_text")
            content = text.encode("utf-8")
            text_bytes = min(4500, self.result_bytes // 2)
            result = {
                "url": final,
                "untrusted_page_text": content[:text_bytes].decode("utf-8", "ignore"),
                "truncated": len(content) > text_bytes,
            }
            if not self.result_fits(result):
                raise WebError("web_result_limit")
            if final not in self.sources:
                self.sources.append(final)
            return result
        except WebError as error:
            return {"error": str(error)}
