from __future__ import annotations

import hashlib
import gzip
import io
import html
import http.client
import json
import ipaddress
import re
import socket
import urllib.parse
import urllib.request
import urllib.error
import ssl
import os
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from .db import connect
from .triage import MAX_AUTOMATIC_RETRIES, screen as screen_item, schedule_retry
from .quality import editorial_issues, attributed_report_supported
from .ai import request_response, AIResponseError, FILTER_VERSION, analyze as analyze_with_ai, get_api_key

NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

_RUNTIME_LOG_PATH: Path | None = None


def configure_runtime_log(path: str | Path) -> None:
    """Write structured timing events to the same local log read by `health`."""
    global _RUNTIME_LOG_PATH
    _RUNTIME_LOG_PATH = Path(path)


def _log_timing(event: str, **fields) -> None:
    line = json.dumps({"timestamp": NOW(), "event": event, **fields}, ensure_ascii=False)
    print(line, flush=True)
    if _RUNTIME_LOG_PATH is not None:
        try:
            _RUNTIME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _RUNTIME_LOG_PATH.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            # A timing-log failure must never interrupt collection or delivery.
            pass


def _development_date_issue(result: dict, source: dict | None, freshness_hours: int) -> tuple[str, str] | None:
    """Require a dated, source-grounded event before treating a fresh article as fresh news."""
    if result.get("publication_recommendation") != "AUTO_PUBLISH":
        return None
    raw_date = str(result.get("development_date") or "").strip()
    evidence = str(result.get("development_date_evidence") or "").strip()
    content = str((source or {}).get("content") or "")
    if not raw_date or not evidence or not content:
        return ("unverified", "Не установлена подтверждённая дата самого события; дата свежей статьи не заменяет её.")
    try:
        event_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        return ("unverified", "Дата события указана не в формате YYYY-MM-DD.")
    normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold().replace("ё", "е")
    if normalize(evidence) not in normalize(content):
        return ("unverified", "Цитата для даты события отсутствует в прочитанном первичном источнике.")
    date_tokens = {
        event_date.isoformat(),
        f"{event_date.day}.{event_date.month}.{event_date.year}",
        event_date.strftime("%d.%m.%Y"),
        f"{event_date.day}/{event_date.month}/{event_date.year}",
        event_date.strftime("%d/%m/%Y"),
        event_date.strftime("%B %d, %Y"),
        event_date.strftime("%B %d %Y"),
        event_date.strftime("%b %d, %Y"),
        event_date.strftime("%b %d %Y"),
    }
    russian_months = (
        ("января", "янв."), ("февраля", "фев."), ("марта", "мар."),
        ("апреля", "апр."), ("мая",), ("июня", "июн."),
        ("июля", "июл."), ("августа", "авг."), ("сентября", "сен."),
        ("октября", "окт."), ("ноября", "нояб."), ("декабря", "дек."),
    )[event_date.month - 1]
    date_tokens.update(f"{event_date.day} {month} {event_date.year}" for month in russian_months)
    evidence_norm = normalize(evidence)
    if not any(normalize(token) in evidence_norm for token in date_tokens):
        return ("unverified", "Цитата источника не подтверждает указанную календарную дату события.")
    today = datetime.now(timezone.utc).date()
    if event_date > today:
        return ("unverified", "Дата события находится в будущем.")
    if (today - event_date).total_seconds() > max(1, freshness_hours) * 3600:
        return ("stale", f"Последнее подтверждённое изменение датировано {event_date.isoformat()} и старше окна свежести; новой стадии или более позднего изменения источник не подтверждает.")
    return None


def _trusted_tls_context():
    try:
        import certifi
    except ImportError:
        try:
            from pip._vendor import certifi
        except ImportError:
            return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


TLS_CONTEXT = _trusted_tls_context()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonicalize(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if not k.lower().startswith("utm_") and k.lower() not in {"ref", "fbclid", "gclid"}]
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urllib.parse.urlencode(query), ""))


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return date.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            return None


def _public_host_addresses(host: str, port: int = 443) -> list[str]:
    host = host.rstrip(".").encode("idna").decode("ascii")
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        try:
            addresses = {result[4][0].split("%", 1)[0]
                         for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise ValueError("URL_HOST_UNRESOLVABLE") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global
                            or ipaddress.ip_address(address).is_multicast for address in addresses):
        raise ValueError("URL_NOT_PUBLIC")
    return sorted(addresses)


def _validate_public_http_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError("URL_NOT_ALLOWED")
    _public_host_addresses(parsed.hostname)


class _PublicHttpsConnection(http.client.HTTPSConnection):
    def connect(self):
        # Connect to a checked numeric address, retaining the hostname for TLS.
        # A second DNS lookup by the socket must not undo the public-address gate.
        if self.port != 443 or self._tunnel_host:
            raise ValueError("URL_NOT_ALLOWED")
        addresses = _public_host_addresses(self.host, self.port)
        deadline = time.monotonic() + float(self.timeout)
        for index, address in enumerate(addresses):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("PUBLIC_CONNECTION_TIMEOUT")
            try:
                sock = socket.create_connection((address, self.port), remaining, self.source_address)
            except OSError:
                if index == len(addresses) - 1:
                    raise
                continue
            try:
                sock.settimeout(max(0.001, deadline - time.monotonic()))
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise
            return


class _PublicHttpsHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHttpsConnection, req, context=self._context,
                            check_hostname=self._check_hostname)


class _PublicHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_public_http_url(target)
        if urllib.parse.urlsplit(req.full_url).scheme.lower() == "https" and urllib.parse.urlsplit(target).scheme.lower() != "https":
            raise ValueError("HTTPS_REDIRECT_DOWNGRADE")
        return super().redirect_request(req, fp, code, msg, headers, target)


def _request_with_url(url: str, timeout: int = 20, public_only: bool = False) -> tuple[bytes, str, str]:
    if public_only:
        _validate_public_http_url(url)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _PublicHttpsHandler(context=TLS_CONTEXT),
            _PublicHttpsRedirectHandler())
    else:
        opener = None
    request_headers = [
        {"User-Agent": "KovalskyNewsroom/0.1 (+RSS reader)", "Accept": "text/html,application/xhtml+xml,application/pdf,*/*"},
        {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
         "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
         "Accept-Language": "ru,en-US;q=0.8,en;q=0.6"},
    ]
    for attempt, headers in enumerate(request_headers):
        req = urllib.request.Request(url, headers=headers)
        try:
            response_context = (opener.open(req, timeout=timeout) if opener else
                                urllib.request.urlopen(req, timeout=timeout, context=TLS_CONTEXT))
            with response_context as response:
                payload = response.read(10_000_001)
                if len(payload) > 10_000_000:
                    raise ValueError("RESPONSE_TOO_LARGE")
                # Some publishers send gzip even without Accept-Encoding.
                if payload.startswith(b"\x1f\x8b"):
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
                            payload = compressed.read(10_000_001)
                    except (OSError, EOFError) as exc:
                        raise ValueError("INVALID_GZIP_RESPONSE") from exc
                    if len(payload) > 10_000_000:
                        raise ValueError("RESPONSE_TOO_LARGE")
                return payload, response.geturl(), response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            if attempt == 0 and (exc.code == 403 or exc.code == 429 or 500 <= exc.code <= 599):
                time.sleep(0.25 if exc.code == 403 else 0.4)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            reason = str(getattr(exc, "reason", exc)).lower()
            timed_out = "timed out" in reason or "timeout" in reason
            transient_connection_error = "handshake" in reason or "connection reset" in reason
            # A timeout has already spent the full request budget. Retrying it
            # immediately doubles the wait during a broad outage; the source is
            # retried in a later collection cycle instead.
            if attempt == 0 and not timed_out and transient_connection_error:
                time.sleep(0.4)
                continue
            raise
    raise RuntimeError("NETWORK_RETRY_EXHAUSTED")


def _request(url: str) -> bytes:
    return _request_with_url(url)[0]


def _safe_source_error(exc: Exception) -> str:
    """Return a useful error class without recording URLs, credentials, or response bodies."""
    if isinstance(exc, AIResponseError):
        return exc.code
    message = str(exc).lower()
    if message in {"publisher_browser_challenge", "response_too_large", "invalid_gzip_response", "telegram_preview_unavailable", "telegram_pagination_stalled"}:
        return message.upper()
    if "certificate_verify_failed" in message or "certificate verify failed" in message:
        return "TLS_CERTIFICATE_ERROR"
    if isinstance(getattr(exc, "reason", exc), TimeoutError) or "timed out" in message or "timeout" in message:
        return "NETWORK_TIMEOUT"
    if "handshake" in message or "ssl" in message or "tls" in message:
        return "TLS_CONNECTION_ERROR"
    code = re.search(r"(?:http error|http)[_\s]*(\d{3})", message)
    if code:
        return f"HTTP_{code.group(1)}"
    if "не найдена объявленная rss/atom-лента" in message:
        return "FEED_NOT_FOUND"
    if isinstance(exc, ValueError):
        if "google news" in message:
            return "GOOGLE_NEWS_RESOLUTION_ERROR"
        if "readable article text" in message or "html or pdf" in message:
            return "ARTICLE_FORMAT_ERROR"
        return "SOURCE_FORMAT_ERROR"
    if "xml" in message or isinstance(exc, ET.ParseError):
        return "INVALID_FEED"
    return type(exc).__name__.upper()


class PDFText(str):
    def __new__(cls, value: str, ocr_used: bool = False):
        instance = super().__new__(cls, value)
        instance.ocr_used = ocr_used
        return instance


def _extract_pdf_text(payload: bytes, timeout: int = 15) -> str:
    """Extract bounded PDF text; use macOS Vision OCR when the PDF has little text."""
    if len(payload) > 10_000_000:
        raise ValueError("PDF exceeds 10 MB safety limit")
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader is not None:
        import io
        reader = PdfReader(io.BytesIO(payload), strict=False)
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:80])
    else:
        text = ""
    ocr_used = False
    if len(text.strip()) < 80 and os.path.exists("/usr/bin/swift") and os.path.exists(os.path.join(os.path.dirname(__file__), "pdf_text.swift")):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf_file:
            pdf_file.write(payload)
            pdf_file.flush()
            module_cache = os.path.join(tempfile.gettempdir(), "kovalsky-swift-modules")
            os.makedirs(module_cache, mode=0o700, exist_ok=True)
            swift_env = os.environ.copy()
            swift_env["SWIFT_MODULECACHE_PATH"] = module_cache
            swift_env["CLANG_MODULE_CACHE_PATH"] = module_cache
            result = subprocess.run(
                ["/usr/bin/swift", os.path.join(os.path.dirname(__file__), "pdf_text.swift"), pdf_file.name],
                capture_output=True, text=True, timeout=max(timeout, 30), check=False, env=swift_env,
            )
        if result.returncode:
            raise ValueError("PDF_TEXT_EXTRACTION_FAILED")
        text = result.stdout
        ocr_used = "PDF_OCR_USED" in result.stderr
    elif not text.strip() and PdfReader is None:
        raise ValueError("PDF_READER_UNAVAILABLE")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 80:
        raise ValueError("PDF_HAS_NO_READABLE_TEXT")
    return PDFText(text[:12000], ocr_used=ocr_used)


class GoogleArticleParamsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.params = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "div" and all(key in values for key in ("data-n-a-id", "data-n-a-sg", "data-n-a-ts")):
            self.params = values


def decode_google_news_url(url: str) -> str:
    """Resolve Google's opaque RSS redirect via its article metadata endpoint."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname not in {"news.google.com", "www.news.google.com"}:
        return url
    article_id = parsed.path.rstrip("/").split("/")[-1]
    if not article_id:
        raise ValueError("Google News article link has no ID")
    page_url = "https://news.google.com/rss/articles/" + article_id
    page = _request(page_url).decode("utf-8", errors="replace")
    parser = GoogleArticleParamsParser()
    parser.feed(page)
    if not parser.params:
        raise ValueError("Google News article metadata is unavailable")
    params = parser.params
    try:
        timestamp = int(params["data-n-a-ts"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Google News article timestamp is invalid") from exc
    request_data = [
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        params["data-n-a-id"], timestamp, params["data-n-a-sg"],
    ]
    rpc_request = ["Fbv4je", json.dumps(request_data, separators=(",", ":"))]
    body = urllib.parse.urlencode({"f.req": json.dumps([[rpc_request]], separators=(",", ":"))}).encode()
    req = urllib.request.Request(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                 "Referer": page_url, "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20, context=TLS_CONTEXT) as response:
        result_text = response.read(1_000_000).decode("utf-8", errors="replace")
    try:
        batch = json.loads(result_text.split("\n\n", 1)[1])
        rpc_result = next(row[2] for row in batch if isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr" and row[1] == "Fbv4je")
        decoded = json.loads(rpc_result)[1]
    except (IndexError, KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Google News could not resolve the publisher URL") from exc
    target = urllib.parse.urlsplit(decoded)
    if target.scheme not in {"http", "https"} or not target.hostname or target.hostname.endswith("google.com"):
        raise ValueError("Google News returned no publisher article URL")
    return decoded


class PublisherArticleParser(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form"}
    TEXT_TAGS = {"h1", "h2", "h3", "p", "blockquote", "li", "time"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta_title = ""
        self.meta_description = ""
        self.canonical_url = ""
        self.title_parts = []
        self.blocks = []
        self.headings = []
        self.skip_stack = []
        self.title_open = False
        self.block_tag = None
        self.block_data = []
        self.links = []
        self.embedded_urls = []
        self.script_chunks = []
        self.script_char_count = 0
        self.script_open = False
        self.anchor = None
        self.jsonld_open = False
        self.jsonld_data = []
        self.structured_article_bodies = []
        self.published_at = None
        self.updated_at = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "time" and values.get("datetime"):
            date_value = parse_date(values.get("datetime"))
            class_names = (values.get("class") or "").lower().split()
            if date_value and any(token in " ".join(class_names) for token in ("updated", "modified")):
                self.updated_at = self.updated_at or date_value
            elif date_value:
                self.published_at = self.published_at or date_value
        if tag == "script":
            self.script_open = True
            if values.get("type", "").split(";", 1)[0].strip().lower() == "application/ld+json":
                self.jsonld_open = True
                self.jsonld_data = []
        if tag in {"iframe", "embed", "object", "source"}:
            target = values.get("src") or values.get("data")
            mime = (values.get("type") or "").lower()
            if target and ("pdf" in mime or tag in {"iframe", "embed", "object", "source"}):
                self.embedded_urls.append({"href": target, "text": values.get("title") or values.get("aria-label") or "embedded document", "embedded": True})
        if tag == "param" and values.get("name", "").lower() in {"src", "url", "filename"} and values.get("value"):
            self.embedded_urls.append({"href": values["value"], "text": "embedded document", "embedded": True})
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key in {"article:published_time", "datepublished", "pubdate"}:
                self.published_at = parse_date(values.get("content"))
            elif key in {"article:modified_time", "datemodified"}:
                self.updated_at = parse_date(values.get("content"))
            if key in {"og:title", "twitter:title"} and values.get("content"):
                self.meta_title = values["content"]
            elif key in {"og:description", "description", "twitter:description"} and values.get("content") and not self.meta_description:
                self.meta_description = values["content"]
        elif tag == "link" and "canonical" in values.get("rel", "").lower():
            self.canonical_url = values.get("href", "")
        related_classes = {"related", "related-posts", "related-articles", "recommendations"}
        if (tag in self.SKIP
                or related_classes.intersection((values.get("class") or "").lower().split())
                or (self.skip_stack and tag == self.skip_stack[-1])):
            self.skip_stack.append(tag)
            return
        if self.skip_stack:
            return
        if tag == "title":
            self.title_open = True
        if tag in self.TEXT_TAGS and self.block_tag is None:
            self.block_tag = tag
            self.block_data = []
        if tag == "a" and values.get("href"):
            self.anchor = {"href": values["href"], "text": []}

    def handle_endtag(self, tag):
        if tag == "script":
            self.script_open = False
        if tag == "script" and self.jsonld_open:
            raw = "".join(self.jsonld_data).strip()
            self.jsonld_open = False
            try:
                document = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                document = None
            article_types = {"article", "newsarticle", "report", "blogposting"}
            def collect_article_bodies(value):
                if isinstance(value, list):
                    for child in value:
                        collect_article_bodies(child)
                elif isinstance(value, dict):
                    types = value.get("@type", [])
                    types = [types] if isinstance(types, str) else types
                    if article_types.intersection(str(item).lower() for item in types):
                        self.published_at = parse_date(value.get("datePublished")) or self.published_at
                        self.updated_at = parse_date(value.get("dateModified")) or self.updated_at
                        body = value.get("articleBody")
                        if isinstance(body, str) and len(body.strip()) >= 100:
                            self.structured_article_bodies.append(body.strip())
                    for child in value.values():
                        if isinstance(child, (dict, list)):
                            collect_article_bodies(child)
            collect_article_bodies(document)
        if tag in self.skip_stack:
            for index in range(len(self.skip_stack) - 1, -1, -1):
                if self.skip_stack[index] == tag:
                    del self.skip_stack[index:]
                    break
            return
        if self.skip_stack:
            return
        if tag == "a" and self.anchor is not None:
            label = re.sub(r"\s+", " ", " ".join(self.anchor["text"])).strip()
            self.links.append({"href": self.anchor["href"], "text": label})
            self.anchor = None
        if tag == "title":
            self.title_open = False
        if tag == self.block_tag:
            value = re.sub(r"\s+", " ", " ".join(self.block_data)).strip()
            if value:
                self.blocks.append(value)
                if tag == "h1":
                    self.headings.append(value)
            self.block_tag = None
            self.block_data = []

    def handle_data(self, data):
        if self.script_open and self.script_char_count < 500_000:
            chunk = data[:max(0, 500_000 - self.script_char_count)]
            self.script_chunks.append(chunk)
            self.script_char_count += len(chunk)
        if self.jsonld_open:
            self.jsonld_data.append(data)
            return
        if self.skip_stack:
            return
        if self.title_open:
            self.title_parts.append(data)
        if self.block_tag:
            self.block_data.append(data)
        if self.anchor is not None:
            self.anchor["text"].append(data)


OFFICIAL_SOURCE_DOMAINS = {
    "cbr.ru", "kremlin.ru", "government.ru", "publication.pravo.gov.ru", "pravo.gov.ru",
    "duma.gov.ru", "minfin.gov.ru", "nalog.gov.ru", "fas.gov.ru", "rosfinmonitoring.gov.ru",
    "sec.gov", "cftc.gov", "federalreserve.gov", "ecb.europa.eu", "ec.europa.eu",
    "eur-lex.europa.eu", "esma.europa.eu", "eba.europa.eu", "fca.org.uk",
}
OFFICIAL_SOURCE_SUFFIXES = (".gov", ".gov.ru", ".gov.uk", ".gov.au", ".gov.ca", ".gov.in", ".gov.sg", ".gov.br", ".gouv.fr")
PRIMARY_LINK_LABEL = re.compile(
    r"(?:пресс.?релиз|официальн\w+\s+(?:сообщени\w+|заявлени\w+|документ\w*)|"
    r"законопроект|постановлени\w+|приказ\w+|указ\w+|документ\w+|текст\s+закона|"
    r"press\s+release|official\s+(?:statement|announcement|document)|bill|regulation|"
    r"filing|court\s+(?:order|ruling)|full\s+report|restricted\s+counterparty\s+list|"
    r"counterparty\s+list|list\s+of\s+(?:restricted\s+)?counterparties|official\s+list)",
    re.IGNORECASE,
)


def _is_official_source_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_SOURCE_DOMAINS) or host.endswith(OFFICIAL_SOURCE_SUFFIXES)


# Identity comes from the fetched host, never the RSS publisher label or canonical tag.
ORIGINAL_REPORTING_PUBLISHERS = {
    "tass.ru": r"ТАСС", "ria.ru": r"РИА\s+Новости", "interfax.ru": r"Интерфакс(?:у|а)?",
    "rbc.ru": r"РБК", "kommersant.ru": r"(?:Коммерсант(?:ъ|у|а)?|Ъ)",
    "vedomosti.ru": r"Ведомост(?:и|ям|ей)", "reuters.com": r"Reuters",
    "bloomberg.com": r"Bloomberg",
}


def _original_reporting_kind(url: str, content: str) -> str | None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    brand = next((pattern for domain, pattern in ORIGINAL_REPORTING_PUBLISHERS.items()
                  if host == domain or host.endswith("." + domain)), None)
    if not brand:
        return None
    name = rf'[«“"\s]*(?:{brand})[»”"]*'
    interview = rf"(?:сообщил\w*|рассказал\w*|пояснил\w*|заявил\w*)\s+{name}|(?:в\s+интервью|в\s+беседе|в\s+комментарии)\s+{name}|(?:told|interview\s+with)\s+{name}"
    document = rf"(?:в\s+распоряжении\s+{name}|(?:ознакомил\w*|располагает)\s+{name}|{name}\s+(?:ознакомил\w*|располагает))|(?:seen|obtained|reviewed)\s+by\s+{name}"
    # Only explicit attribution near the beginning can establish the story's origin;
    # a background quote deep in a roundup must not promote the whole article.
    lead = content[:2500]
    if re.search(document, lead, re.I):
        return "ORIGINAL_MEDIA_REPORT"
    if re.search(interview, lead, re.I):
        return "ORIGINAL_MEDIA_INTERVIEW"
    return None


def _looks_like_pdf_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parsed.path).lower()
    query = urllib.parse.parse_qs(parsed.query)
    return (path.endswith(".pdf") or any(".pdf" in value.lower() for values in query.values() for value in values)
            or query.get("type", [""])[0].lower() == "application/pdf")


def _embedded_document_candidates(parser: PublisherArticleParser, article_url: str) -> list[dict]:
    links = list(parser.links) + list(parser.embedded_urls)
    raw_scripts = " ".join(parser.script_chunks)
    raw_scripts = (raw_scripts.replace(r"\/", "/").replace(r"\u002F", "/")
                   .replace(r"\u002f", "/").replace(r"\u0026", "&")
                   .replace("&amp;", "&"))
    for match in re.finditer(r"(?:https?:)?//[^\s\"'<>\\]{1,1000}?(?:\.pdf|%2[eE]pdf)(?:\?[^\s\"'<>\\]*)?|/(?:[^\s\"'<>\\]{0,900})(?:\.pdf|%2[eE]pdf)(?:\?[^\s\"'<>\\]*)?", raw_scripts, re.IGNORECASE):
        context = raw_scripts[match.start():match.end() + 180]
        if re.search(r"политик.{0,12}конфиденциальност|privacy[ _-]*policy|обработк.{0,20}персональных данных", context, re.I):
            continue  # Consent/footer templates are not documents cited by the article.
        links.append({"href": match.group(0), "text": "embedded PDF", "embedded": True})
    results = []
    seen = set()
    for link in links:
        if re.search(r"политик.{0,12}конфиденциальност|privacy[ _-]*policy|обработк.{0,20}персональных данных", link.get("text", ""), re.I):
            continue
        target = urllib.parse.urljoin(article_url, html.unescape(link.get("href", "")).strip())
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in {"http", "https"}:
            continue
        # PDF.js and similar browser viewers wrap the actual document in a `file=` parameter.
        query = urllib.parse.parse_qs(parsed.query)
        normalized_query = {key.lower(): values for key, values in query.items()}
        wrapped = next((value for key in ("file", "pdf", "document", "src", "url", "source")
                        for value in normalized_query.get(key, []) if _looks_like_pdf_url(value)), None)
        if wrapped:
            target = urllib.parse.urljoin(target, wrapped)
        if not _looks_like_pdf_url(target) or target in seen:
            continue
        seen.add(target)
        label = link.get("text", "")
        results.append({"url": target, "kind": "OFFICIAL" if _is_official_source_host(parsed.hostname or "") else "LINKED_DOCUMENT",
                        "label": label, "embedded": bool(link.get("embedded"))})
    return results


def _primary_link_candidates(links: list[dict], article_url: str) -> list[dict]:
    article_host = (urllib.parse.urlsplit(article_url).hostname or "").lower()
    ranked = []
    seen = set()
    for index, link in enumerate(links):
        target = urllib.parse.urljoin(article_url, link["href"])
        parsed = urllib.parse.urlsplit(target)
        host = (parsed.hostname or "").lower()
        same_site = host == article_host or host.endswith("." + article_host) or article_host.endswith("." + host)
        direct_pdf = _looks_like_pdf_url(target)
        if parsed.scheme not in {"http", "https"} or not host or (same_site and not direct_pdf) or host.endswith(".google.com"):
            continue
        if target in seen:
            continue
        seen.add(target)
        official = _is_official_source_host(host)
        document_label = bool(PRIMARY_LINK_LABEL.search(link.get("text", "")))
        explicit_document_url = direct_pdf or bool(re.search(r"restricted-counterparty-list|counterparty-list|press-release|pressrelease", parsed.path, re.IGNORECASE))
        if official or document_label or explicit_document_url:
            ranked.append((0 if document_label or direct_pdf or explicit_document_url else 1, index, target, "OFFICIAL" if official else "LINKED_DOCUMENT"))
    return [{"url": entry[2], "kind": entry[3]} for entry in sorted(ranked)[:4]]


def _fetch_bybit_restricted_counterparty_pdf(url: str, publisher_name: str,
                                            published_at: str | None,
                                            timeout: int) -> dict:
    """Resolve Bybit's client-rendered legal page to its official PDF via Bybit's public API."""
    api_url = "https://api.bybit.com/compliance/v1/wall/site-legal-terms?" + urllib.parse.urlencode(
        {"category_name": "additional-terms-and-disclosures"}
    )
    payload, _, content_type = _request_with_url(api_url, timeout=timeout)
    if "json" not in content_type.lower():
        raise ValueError("Bybit legal API returned a non-JSON response")
    response = json.loads(payload.decode("utf-8", errors="replace"))
    if response.get("ret_code") != 0:
        raise ValueError("Bybit legal API request failed")
    document = next(
        (item for item in response.get("result", {}).get("item", [])
         if item.get("name") == "Restricted-Counterparty-List"),
        None,
    )
    if not document or not document.get("docLink"):
        raise ValueError("Bybit restricted counterparty PDF link is unavailable")
    pdf_url = urllib.parse.urljoin("https://www.bybit.com", document["docLink"])
    pdf_payload, final_url, pdf_type = _request_with_url(pdf_url, timeout=timeout)
    if "pdf" not in pdf_type.lower() and not pdf_payload.startswith(b"%PDF-"):
        raise ValueError("Bybit document link did not return a PDF")
    content = _extract_pdf_text(pdf_payload, timeout=timeout)
    title = "Restricted Counterparty List"
    update_time = document.get("updateTime")
    try:
        updated_at = datetime.fromtimestamp(float(update_time), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        updated_at = parse_date(update_time)
    return {
        "url": url,
        "title": title,
        "description": "",
        "content": str(content),
        "author": None,
        "published_at": published_at,
        "updated_at": updated_at,
        "publisher_name": publisher_name or "Bybit",
        "primary_source_url": final_url,
        "primary_source_title": title,
        "primary_source_content": str(content)[:12000],
        "primary_source_type": "LINKED_DOCUMENT",
        "primary_source_publisher": "Bybit",
        "primary_source_status": "OCR_REVIEW" if getattr(content, "ocr_used", False) else "READ",
    }


def fetch_publisher_article(url: str, publisher_name: str, published_at: str | None,
                            discover_primary: bool = True, timeout: int = 20,
                            public_only: bool = False) -> dict:
    parsed_url = urllib.parse.urlsplit(url)
    if (not public_only and (parsed_url.hostname or "").lower().removeprefix("www.") == "bybit.com"
            and parsed_url.path.rstrip("/").lower() ==
            "/en/legal/additional-terms-and-disclosures/restricted-counterparty-list"):
        return _fetch_bybit_restricted_counterparty_pdf(url, publisher_name, published_at, timeout)
    payload, final_url, content_type = _request_with_url(url, timeout=timeout, public_only=public_only)
    final_host = urllib.parse.urlsplit(final_url).hostname or ""
    if final_host.endswith("google.com") or "news.google" in final_host:
        raise ValueError("Publisher link still points to Google News")
    if ("pdf" in content_type.lower() or urllib.parse.urlsplit(final_url).path.lower().endswith(".pdf")
            or payload.startswith(b"%PDF-")):
        content = _extract_pdf_text(payload, timeout=timeout)
        title = urllib.parse.unquote(urllib.parse.urlsplit(final_url).path.rsplit("/", 1)[-1]) or publisher_name
        host = urllib.parse.urlsplit(final_url).hostname or ""
        ocr_used = bool(getattr(content, "ocr_used", False))
        article = {"url": final_url, "title": title, "description": "", "content": content,
                   "author": None, "published_at": published_at, "updated_at": None,
                   "publisher_name": publisher_name or host,
                   "primary_source_url": final_url, "primary_source_title": title,
                   "primary_source_content": str(content)[:12000],
                   "primary_source_type": "OFFICIAL" if _is_official_source_host(host) else "LINKED_DOCUMENT",
                   "primary_source_publisher": publisher_name or host,
                   "primary_source_status": "OCR_REVIEW" if ocr_used else "READ"}
        return article
    if "html" not in content_type.lower():
        raise ValueError("Publisher article is not an HTML or PDF page")
    page_text = payload.decode("utf-8", errors="replace")
    if "<js-challenge-loader" in page_text.lower():
        raise ValueError("PUBLISHER_BROWSER_CHALLENGE")
    parser = PublisherArticleParser()
    parser.feed(page_text)
    embedded_candidates = _embedded_document_candidates(parser, final_url) if discover_primary else []
    blocks = list(dict.fromkeys(parser.blocks))
    content = "\n".join(blocks)
    description = html.unescape(parser.meta_description).strip()
    structured_body = max(parser.structured_article_bodies, key=len, default="")
    if len(structured_body) >= 100:
        content = re.sub(r"\s+", " ", html.unescape(structured_body)).strip()
    article_body_read = len(content) >= 100
    if len(content) < 100 and len(description) >= 100:
        content = description
    if len(content) < 100 and not embedded_candidates:
        raise ValueError("Publisher page has no readable article text")
    meta_title = parser.meta_title.strip()
    heading = parser.headings[0] if parser.headings else ""
    generic_meta_title = meta_title.casefold() in {"fincen.gov", "home", "news"}
    title = (heading if heading and (not meta_title or generic_meta_title)
             else meta_title or " ".join(parser.title_parts) or heading or (blocks[0] if blocks else "")).strip()
    canonical_url = urllib.parse.urljoin(final_url, parser.canonical_url) if parser.canonical_url else final_url
    canonical_host = urllib.parse.urlsplit(canonical_url).hostname or ""
    if canonical_host.endswith("google.com") or "news.google" in canonical_host:
        canonical_url = final_url
    article = {"url": canonical_url, "title": title, "description": description,
               "content": content[:12000], "author": None, "published_at": parser.published_at or published_at,
               "updated_at": parser.updated_at, "publisher_name": publisher_name or canonical_host,
               "material_read": article_body_read, "material_url": canonical_url,
               "primary_source_url": None, "primary_source_title": None,
               "primary_source_content": "", "primary_source_type": None,
               "primary_source_publisher": None, "primary_source_status": "NOT_CHECKED"}
    if discover_primary and _is_official_source_host(canonical_host) and not embedded_candidates:
        article["primary_source_url"] = canonical_url
        article["primary_source_title"] = title
        article["primary_source_content"] = content[:12000]
        article["primary_source_type"] = "OFFICIAL"
        article["primary_source_publisher"] = publisher_name or canonical_host
        article["primary_source_status"] = "READ"
    elif discover_primary:
        candidates = embedded_candidates or _primary_link_candidates(parser.links + parser.embedded_urls, final_url)
        article["primary_source_status"] = "UNREADABLE" if candidates else "NO_LINK"
        for candidate in candidates:
            candidate_url = candidate["url"]
            try:
                primary = fetch_publisher_article(
                    candidate_url,
                    urllib.parse.urlsplit(candidate_url).hostname or "Официальный источник",
                    published_at,
                    discover_primary=False,
                    timeout=8,
                    public_only=public_only,
                )
            except Exception:
                # Do not substitute an unrelated official link when the article's cited source is unavailable.
                article["primary_source_url"] = candidate_url
                article["primary_source_title"] = ""
                article["primary_source_content"] = ""
                article["primary_source_type"] = candidate["kind"]
                article["primary_source_publisher"] = urllib.parse.urlsplit(candidate_url).hostname or ""
                article["primary_source_status"] = "UNREADABLE"
                article["primary_source_error"] = "PRIMARY_SOURCE_NOT_READ"
                break
            article["primary_source_url"] = primary.get("primary_source_url") or primary["url"]
            article["primary_source_title"] = primary["title"]
            article["primary_source_content"] = primary["content"][:12000]
            article["primary_source_type"] = candidate["kind"]
            article["primary_source_publisher"] = primary["publisher_name"]
            article["primary_source_status"] = ("OCR_REVIEW" if primary.get("primary_source_status") == "OCR_REVIEW" else "READ")
            if len(content) < 100 and primary.get("content"):
                article["content"] = primary["content"][:12000]
                article["title"] = primary.get("title") or title
            break
    # A document remains preferred. If unavailable, original reporting can support
    # attributed claims, never pretend that the underlying document was read.
    kind = _original_reporting_kind(final_url, content) if article_body_read else None
    if discover_primary and article["primary_source_status"] != "READ" and kind:
        article.update(primary_source_url=final_url, primary_source_title=title,
                       primary_source_content=content[:12000], primary_source_type=kind,
                       primary_source_publisher=publisher_name or final_host,
                       primary_source_status="READ")
    return article


class FetchedItems(list):
    def __init__(self, values=(), diagnostics=None):
        super().__init__(values)
        self.diagnostics = diagnostics or []


MULTI_LEVEL_SUFFIXES = {"ac.uk", "co.uk", "gov.uk", "org.uk", "com.au", "net.au", "org.au", "co.nz",
                        "co.jp", "com.br", "com.cn", "com.hk", "com.sg", "co.in", "com.mx", "co.za"}


def _registrable_domain(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) < 2:
        return host.lower().rstrip(".")
    suffix = ".".join(labels[-2:])
    if suffix in MULTI_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix


def _independent_candidates(item: dict, candidates: list[dict], trusted_domains: set[str], limit: int = 2) -> list[dict]:
    """Select matching publisher articles on distinct domains, excluding known syndication."""
    own_host = (urllib.parse.urlsplit(item.get("url", "")).hostname or "").lower()
    primary_host = (urllib.parse.urlsplit(item.get("primary_source_url", "")).hostname or "").lower()
    own_domain = _registrable_domain(own_host) if own_host else ""
    primary_domain = _registrable_domain(primary_host) if primary_host else ""
    trusted_domains = {_registrable_domain(domain) for domain in trusted_domains}
    own_primary = canonicalize(item.get("primary_source_url") or "") if item.get("primary_source_url") else ""
    own_title = item.get("title", "")
    ranked = []
    seen = set()
    for candidate in candidates:
        candidate_url = candidate.get("url", "")
        candidate_host = (urllib.parse.urlsplit(candidate_url).hostname or "").lower()
        if not candidate_host or not own_host:
            continue
        candidate_domain = _registrable_domain(candidate_host)
        if candidate_domain in {own_domain, primary_domain}:
            continue
        reputable_publisher = candidate_domain in trusted_domains
        if not reputable_publisher and not _is_official_source_host(candidate_host):
            continue
        candidate_primary = candidate.get("primary_source_url") or ""
        candidate_primary_host = (urllib.parse.urlsplit(candidate_primary).hostname or "").lower()
        same_input_source = canonicalize(candidate_primary) == canonicalize(item.get("url", ""))
        same_primary_domain = bool(primary_host and _registrable_domain(candidate_primary_host) == primary_domain)
        if candidate_primary and (same_input_source or (own_primary and canonicalize(candidate_primary) == own_primary) or same_primary_domain):
            continue
        canonical_url = canonicalize(candidate_url)
        if not canonical_url or canonical_url == canonicalize(item.get("url", "")) or canonical_url in seen:
            continue
        title_score = similarity(own_title, candidate.get("title", ""))
        shared_terms = terms(own_title) & terms(candidate.get("title", ""))
        if title_score < 0.16 or len(shared_terms) < 2:
            continue
        own_content = (item.get("content") or item.get("description") or "")[:2400]
        other_content = (candidate.get("content") or candidate.get("description") or "")[:2400]
        if own_content and other_content and similarity(own_content, other_content) >= 0.82:
            continue
        seen.add(canonical_url)
        ranked.append((title_score, {"publisher": candidate.get("publisher_name") or candidate_host,
                                     "title": candidate.get("title", ""), "url": candidate_url,
                                     "published_at": candidate.get("published_at"),
                                     "primary_source_url": candidate_primary or None,
                                     "primary_source_title": candidate.get("primary_source_title"),
                                     "content": other_content[:3000]}))
    return [entry for _, entry in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:limit]]


def fetch_google_news(url: str) -> list[dict]:
    """Use Google News for discovery, then resolve and read each publisher article."""
    root = ET.fromstring(_request(url))
    entries = root.findall(".//item")[:6]

    def read_entry(node):
        fields = {child.tag.rsplit("}", 1)[-1].lower(): child for child in node}
        link = (fields.get("link").text or "").strip() if fields.get("link") is not None else ""
        source_node = fields.get("source")
        publisher = " ".join(source_node.itertext()).strip() if source_node is not None else ""
        if not link:
            return None, None
        published_at = parse_date(fields.get("pubdate").text if fields.get("pubdate") is not None else None)
        article_host = ""
        read_started = time.perf_counter()
        try:
            original_url = decode_google_news_url(link)
            article_host = (urllib.parse.urlsplit(original_url).hostname or "").lower()
            article = fetch_publisher_article(original_url, publisher, published_at)
            _log_timing("primary_source_read_timing",
                        source=article.get("primary_source_publisher") or publisher,
                        title=article.get("title", "")[:160],
                        seconds=round(time.perf_counter() - read_started, 3), result="OK")
            return article, None
        except Exception as exc:
            # Record only a public host and a safe error class, never a URL or response body.
            code = _safe_source_error(exc)
            safe_host = re.sub(r"[^a-z0-9.-]", "", article_host)[:253]
            diagnostic = f"{code}@{safe_host}" if safe_host else code
            _log_timing("primary_source_read_timing", source=publisher,
                        seconds=round(time.perf_counter() - read_started, 3),
                        result="ERROR", error_type=type(exc).__name__)
            return None, diagnostic

    if not entries:
        return FetchedItems([], [])
    with ThreadPoolExecutor(max_workers=min(4, len(entries))) as pool:
        fetched = list(pool.map(read_entry, entries))
    result = [article for article, _ in fetched if article is not None]
    diagnostics = [diagnostic for _, diagnostic in fetched if diagnostic]
    return FetchedItems(result, diagnostics)

def _read_discovery_links(links: list[dict], max_results: int = 8, page_timeout: int = 20) -> list[dict]:
    """Read a bounded set of search results concurrently, preserving result order."""
    candidates = [link for link in links[:max(1, min(8, int(max_results)))]
                  if str(link.get("url", "")).startswith(("https://", "http://"))]
    if not candidates:
        return FetchedItems([], [])

    def read_link(link):
        url = link.get("url", "")
        try:
            article = fetch_publisher_article(url, urllib.parse.urlsplit(url).hostname or "",
                                              link.get("published_at"), timeout=max(3, min(20, int(page_timeout))),
                                              public_only=True)
            article["published_at"] = article.get("published_at") or link.get("published_at")
            return article, None
        except Exception as exc:
            return None, _safe_source_error(exc) + "@" + (urllib.parse.urlsplit(url).hostname or "")

    with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
        fetched = list(pool.map(read_link, candidates))
    result = [article for article, _ in fetched if article is not None]
    diagnostics = [diagnostic for _, diagnostic in fetched if diagnostic]
    return FetchedItems(result, diagnostics)


def fetch_web_search(query: str | list[str | dict], ai_settings: dict,
                     interest_exclusions: list[str] | None = None,
                     max_results: int = 8, page_timeout: int = 20) -> list[dict]:
    """Use Responses web_search for discovery, then read publisher pages."""
    raw_scopes = [query] if isinstance(query, str) else query
    scopes = []
    for value in raw_scopes:
        if isinstance(value, dict):
            scope_query = str(value.get("query", "")).strip()
            scope_exclusions = [str(term).strip() for term in value.get("interest_exclusions", []) if str(term).strip()]
        else:
            scope_query = str(value).strip()
            scope_exclusions = []
        if scope_query:
            scopes.append({"query": scope_query, "interest_exclusions": scope_exclusions})
    scopes = scopes or [{"query": "cryptocurrency digital assets Russia CIS regulation exchange stablecoin",
                         "interest_exclusions": []}]
    query_text = "\n".join(
        f"{index}. {scope['query']}" + (f"\n   Avoid: {'; '.join(scope['interest_exclusions'])}"
                                         if scope["interest_exclusions"] else "")
        for index, scope in enumerate(scopes, 1))
    try:
        data = request_response({"model": ai_settings.get("search_model", ai_settings.get("model", "gpt-6-luna")),
            "store": False, "tools": [{"type": "web_search"}], "max_output_tokens": 1800,
            "input": "Search each query scope below for recent news. Return titles, dates and URLs for the strongest relevant results from all scopes. "
                     "Prefer material published within the last 24 hours and retain the publisher URL. Query scopes:\n"
                     f"{query_text}\n"
                     f"Respect this user's negative interest examples and avoid similar topics: {'; '.join(interest_exclusions or [])}"}, ai_settings)
        if data.get("status") == "incomplete":
            raise AIResponseError("SEARCH_INCOMPLETE")
    except AIResponseError as exc:
        # Keep the query scopes together in one scheduled request so every
        # configured discovery topic is refreshed on the same three-minute slot.
        # On API failure, try each scope through the independent public index.
        fallback_items, fallback_diagnostics, fallback_errors = [], [], []
        for scope in scopes:
            exclusions = list(dict.fromkeys((interest_exclusions or []) + scope["interest_exclusions"]))
            negative_terms = " ".join('-"' + term.replace('"', '') + '"' for term in exclusions[:12])
            search_query = (scope["query"] + " " + negative_terms).strip()
            url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
                {"q": search_query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"})
            try:
                items = fetch_google_news(url)
                fallback_items.extend(items)
                fallback_diagnostics.extend(getattr(items, "diagnostics", []))
            except Exception as fallback_exc:
                fallback_errors.append(_safe_source_error(fallback_exc))
        deduplicated, seen_urls = [], set()
        for item in sorted(fallback_items, key=lambda value: value.get("published_at") or "", reverse=True):
            canonical = canonicalize(item.get("url") or "")
            if not canonical or canonical in seen_urls:
                continue
            seen_urls.add(canonical)
            deduplicated.append(item)
            if len(deduplicated) >= 8:
                break
        if not deduplicated and fallback_errors:
            # Keep safe codes so fallback failure is distinguishable from an
            # API timeout without retaining URLs or response text.
            raise AIResponseError(
                f"SEARCH_FALLBACK_FAILED:{exc.code}:{','.join(fallback_errors[:3])}") from None
        return FetchedItems(deduplicated, fallback_diagnostics + ["SEARCH_FALLBACK:" + exc.code])
    links, seen = [], set()
    for output in data.get("output", []):
        for block in output.get("content", []):
            for annotation in block.get("annotations", []):
                if annotation.get("type") == "url_citation":
                    url = annotation.get("url", "")
                    if url and url not in seen:
                        seen.add(url)
                        links.append({"url": url, "title": annotation.get("title", "")})
    return _read_discovery_links(links, max_results=max_results, page_timeout=page_timeout)


def fetch_x_recent(query: str, x_settings: dict) -> list[dict]:
    """Fetch public recent X posts and retain the API text as the direct source."""
    token = get_api_key({"api_key_env": x_settings.get("token_env", "X_BEARER_TOKEN"),
                         "api_key_file": x_settings.get("token_file"),
                         "keychain_service": x_settings.get("keychain_service"),
                         "keychain_account": x_settings.get("keychain_account")})
    if not token:
        raise RuntimeError("X_CREDENTIALS_MISSING")
    params = urllib.parse.urlencode({"query": query, "max_results": 100,
                                     "tweet.fields": "created_at,author_id,lang",
                                     "expansions": "author_id", "user.fields": "username,name"})
    req = urllib.request.Request("https://api.x.com/2/tweets/search/recent?" + params,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=int(x_settings.get("timeout_seconds", 20)), context=TLS_CONTEXT) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"X_API_HTTP_{exc.code}") from None
    users = {user["id"]: user for user in data.get("includes", {}).get("users", [])}
    result = []
    for post in data.get("data", []):
        user = users.get(post.get("author_id"), {})
        username = user.get("username")
        url = f"https://x.com/{username}/status/{post['id']}" if username else f"https://x.com/i/web/status/{post['id']}"
        text = post.get("text", "")
        publisher = user.get("name") or username
        result.append({"url": url, "title": text[:160], "description": text, "content": text,
                       "author": publisher, "published_at": parse_date(post.get("created_at")), "updated_at": None,
                       "primary_source_status": "READ", "primary_source_url": url,
                       "primary_source_title": text[:160], "primary_source_content": text,
                       "primary_source_type": "SOCIAL_POST", "primary_source_publisher": publisher,
                       "primary_source": {"url": url, "title": text[:160], "content": text,
                                          "type": "SOCIAL_POST", "publisher": publisher, "status": "READ"}})
    return result


def fetch_rss(url: str) -> list[dict]:
    payload = _request(url)
    root = ET.fromstring(payload)
    entries = root.findall(".//item")
    if not entries:
        entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "entry"]
    result = []
    for node in entries:
        fields = {child.tag.rsplit("}", 1)[-1].lower(): child for child in node}
        def val(*names: str) -> str:
            for name in names:
                child = fields.get(name)
                if child is not None:
                    if name == "link" and child.attrib.get("href"):
                        return child.attrib["href"]
                    return " ".join(child.itertext()).strip()
            return ""
        link = val("link", "guid", "id")
        if not link:
            continue
        content = val("encoded", "content", "description", "summary")
        content = re.sub(r"<[^>]*>", " ", html.unescape(content))
        result.append({"url": link, "title": val("title") or link, "description": val("description", "summary"),
                       "content": re.sub(r"\s+", " ", content).strip(), "author": val("author", "creator"),
                       "published_at": parse_date(val("pubdate", "published", "updated", "date")),
                       "updated_at": parse_date(val("updated"))})
    return result


class FeedLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        feed_hint = (a.get("type", "") + a.get("title", "")).lower()
        if tag.lower() == "link" and "alternate" in a.get("rel", "").lower() and ("rss" in feed_hint or "atom" in feed_hint):
            if a.get("href"):
                self.links.append(a["href"])


class TelegramPreviewParser(HTMLParser):
    def __init__(self, channel: str):
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.depth = 0
        self.msg_depth = 0
        self.msg_start_depth = 0
        self.current = None
        self.items: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        is_wrap = tag == "div" and "tgme_widget_message_wrap" in classes
        if is_wrap:
            self.depth = 1
            post = attrs.get("data-post", "")
            self.current = {"post": post, "parts": [], "date": None, "links": [], "forwarded": False}
        elif self.depth and tag == "div":
            self.depth += 1
        if self.depth and tag == "div" and self.current and attrs.get("data-post"):
            # Telegram puts data-post on the nested message element, not its wrapper.
            self.current["post"] = attrs["data-post"]
        if self.depth and tag == "div" and "tgme_widget_message_text" in classes:
            self.msg_depth = 1
            self.msg_start_depth = self.depth
        if self.depth and self.current and "tgme_widget_message_forwarded_from" in classes:
            self.current["forwarded"] = True
        if self.msg_depth and self.current and tag == "a" and attrs.get("href"):
            self.current["links"].append({"href": attrs["href"], "text": ""})
            self.current["active_link"] = self.current["links"][-1]
        if self.depth and tag == "time" and self.current:
            self.current["date"] = attrs.get("datetime")

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            self.current.pop("active_link", None)
        if tag == "div" and self.depth:
            if self.msg_depth and self.depth == self.msg_start_depth:
                self.msg_depth = 0
            self.depth -= 1
            if not self.depth and self.current:
                text = re.sub(r"\s+", " ", " ".join(self.current["parts"])).strip()
                post = self.current["post"]
                if text and post:
                    url = "https://t.me/" + post
                    self.items.append({"url": url, "title": text[:160], "description": text,
                                       "content": text, "published_at": parse_date(self.current["date"]),
                                       "updated_at": None, "discovery_links": self.current["links"],
                                       "telegram_forwarded": self.current["forwarded"],
                                       "material_read": True, "material_url": url})
                self.current = None

    def handle_data(self, data):
        if self.depth and self.msg_depth and self.current:
            self.current["parts"].append(data)
            if self.current.get("active_link") is not None:
                self.current["active_link"]["text"] += data


class TelegramBatch(list):
    recovery_before = None
    recovery_error = None


def fetch_telegram(channel_url: str, since: str | None = None,
                   before: int | None = None, max_pages: int = 5) -> list[dict]:
    """Read the latest page plus bounded, resumable history after an outage."""
    channel = urllib.parse.urlsplit(channel_url).path.strip("/").split("/")[-1]
    if not channel:
        raise ValueError("TELEGRAM_CHANNEL_MISSING")
    batch = TelegramBatch()
    seen = set()
    cursor = None
    resume = before
    for page_index in range(max(2, max_pages)):
        preview = f"https://t.me/s/{channel}"
        if cursor is not None:
            preview += f"?before={cursor}"
        try:
            markup = _request(preview).decode("utf-8", errors="replace")
            parser = TelegramPreviewParser(channel)
            parser.feed(markup)
            ids = [int(value) for value in re.findall(
                r'data-post="' + re.escape(channel) + r'/(\d+)"', markup)]
            if not ids:
                # A private/deleted channel or access page is not a successful empty feed.
                if cursor is None or "tgme_channel_history" not in markup:
                    raise ValueError("TELEGRAM_PREVIEW_UNAVAILABLE")
                batch.recovery_before = None
                return batch
            oldest = min(ids)
            if cursor is not None and oldest >= cursor:
                raise ValueError("TELEGRAM_PAGINATION_STALLED")
        except Exception as exc:
            if cursor is None:
                raise
            batch.recovery_before = cursor
            batch.recovery_error = _safe_source_error(exc)
            return batch
        for item in parser.items:
            if item["url"] not in seen:
                batch.append(item)
                seen.add(item["url"])
        dates = [parse_date(value) for value in re.findall(r'datetime="([^"]+)"', markup)]
        dates = [value for value in dates if value]
        reached = since is not None and dates and min(dates) <= since
        if page_index == 0 and resume is not None:
            cursor = resume
            resume = None
        elif since is None or reached or oldest <= 1:
            batch.recovery_before = None
            return batch
        else:
            cursor = oldest
        batch.recovery_before = cursor
    return batch


def fetch_web(url: str) -> list[dict]:
    """Use a publisher's advertised RSS/Atom endpoint; never scrape page lists as if they were feeds."""
    parser = FeedLinkParser()
    parser.feed(_request(url).decode("utf-8", errors="replace"))
    if not parser.links:
        raise ValueError("На странице не найдена объявленная RSS/Atom-лента")
    feed_url = urllib.parse.urljoin(url, parser.links[0])
    return fetch_rss(feed_url)


STOP = set("это как который которая которые чтобы если что где когда после перед было быть для при они она оно его ее их мы вы они тот эта эти этот а и в во на по из к от за с со под над до не ни или же но также заявил сообщает сообщила сообщают".split())


def terms(value: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[а-яёa-z0-9]{3,}", value) if word.lower() not in STOP}


def is_relevant(text: str, configured_terms: list[str]) -> bool:
    text = html.unescape(text).casefold()
    for term in configured_terms:
        term = term.casefold().strip()
        if not term:
            continue
        digital_topic = re.fullmatch(r"цифров[а-яё]+\s+(валют[а-яё]*|актив[а-яё]*|рубл[а-яё]*)", term)
        if digital_topic:
            noun = next(stem for stem in ("валют", "актив", "рубл") if digital_topic.group(1).startswith(stem))
            pattern = rf"(?<![а-яёa-z0-9])цифров[а-яё]+\s+{noun}[а-яё]*(?![а-яёa-z0-9])"
        elif term.startswith("цифровой депозитар"):
            # Match Russian case forms such as "цифрового депозитария" too.
            pattern = r"(?<![а-яёa-z0-9])цифров[а-яё]*\s+депозитар[а-яё]*(?![а-яёa-z0-9])"
        elif " " in term or len(term) < 5:
            pattern = rf"(?<![а-яёa-z0-9]){re.escape(term)}(?![а-яёa-z0-9])"
        else:
            # Prefix matching is limited to the start of a word: "ставк" no longer matches "отставка".
            pattern = rf"(?<![а-яёa-z0-9]){re.escape(term)}"
        if re.search(pattern, text):
            return True
    return False


def is_non_news_telegram_format(source: dict, item: dict) -> bool:
    """Reject channel roundups and entertainment posts as standalone news candidates."""
    try:
        source_type = source["type"]
    except (KeyError, IndexError, TypeError):
        source_type = None
    title = html.unescape(str(item.get("title") or "")).strip()
    normalized = re.sub(r"^\s*(?:[^\wа-яё#]+\s*)+", "", title, flags=re.IGNORECASE)
    roundup = re.compile(
        r"^(?:#КАЛЕНДАРЬ|КАЛЕНДАРЬ\s+(?:КЛЮЧЕВЫХ\s+)?СОБЫТИЙ|ЧТО\s+СЛУЧИЛОСЬ\s+НА\s+КРИПТОРЫНКЕ|(?:УТРЕННИЙ|ВЕЧЕРНИЙ)\s+ОБЗОР|ТОП\s+(?:ДНЯ|НЕДЕЛИ)|ДАЙДЖЕСТ|ГЛАВНОЕ\s+ЗА\s+(?:ДЕНЬ|НЕДЕЛЮ)|ИТОГИ\s+ДНЯ|НОВОСТИ\s+ДНЯ)(?:\b|\s|:)",
        re.IGNORECASE,
    )
    entertainment = re.compile(
        r"^(?:#фильмынавыходные|(?:#[\w]+\s*)*(?:фильмы(?:\s+и\s+сериалы)?|сериалы|подборка\s+фильмов|что\s+посмотреть|книги\s+на\s+выходные|игры\s+на\s+выходные))\b",
        re.IGNORECASE,
    )
    return bool(roundup.search(normalized) or entertainment.search(normalized))


def similarity(a: str, b: str) -> float:
    left, right = terms(a), terms(b)
    return len(left & right) / max(1, len(left | right))


def _limit_headline(headline: str, maximum: int = 115) -> str:
    headline = re.sub(r"\s+", " ", html.unescape(headline)).strip()
    if len(headline) <= maximum:
        return headline
    budget = max(1, maximum - 1)
    excerpt = headline[:budget]
    boundary = excerpt.rfind(" ")
    if boundary >= int(budget * 0.55):
        excerpt = excerpt[:boundary]
    return excerpt.rstrip(" ,;:—-") + "…"


def make_post(headline: str, description: str, source_name: str, url: str, max_length: int) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
    brands = {"tass.ru":"ТАСС", "ria.ru":"РИА Новости", "rbc.ru":"РБК", "interfax.ru":"Интерфакс",
              "cbr.ru":"Банк России", "minfin.gov.ru":"Минфин России", "duma.gov.ru":"Госдума", "rg.ru":"Российская газета"}
    source_name = brands.get(host, re.sub(r"\s*\((?:Telegram|RSS)\)\s*$", "", source_name))
    headline = _limit_headline(headline)
    body = html.unescape(description).replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [re.sub(r"[ \t]+", " ", part).strip() for part in re.split(r"\n\s*\n", body)]
    # Citation is assembled once by the application, even if the model returned
    # its own footer while following the human-facing editorial standard.
    body = "\n\n".join(part for part in paragraphs if part and not part.startswith(('Источник:', 'Источники:')))
    if body.casefold() == headline.casefold():
        body = ""
    elif body.casefold().startswith(headline.casefold()):
        body = body[len(headline):].lstrip(" —–:;,.\t\n")
    footer = f"Источник: [{source_name}]({url})"
    budget = max(80, max_length - len(footer) - len(headline) - 4)
    if len(body) > budget:
        excerpt = body[:budget]
        minimum_boundary = int(budget * 0.60)
        boundaries = [match.end() for match in re.finditer(r"[.!?…](?:[»”\"')\]]*)\s+", excerpt)]
        boundaries.extend(match.start() for match in re.finditer(r"\n\n", excerpt))
        safe_boundary = max((point for point in boundaries if point >= minimum_boundary), default=0)
        if not safe_boundary:
            safe_boundary = excerpt.rfind(" ", minimum_boundary)
        truncated = excerpt[:safe_boundary or budget - 1].rstrip(" ,;:\n")
        body = truncated + "…"
    return f"{headline}\n\n{body + chr(10) + chr(10) if body else ''}{footer}"


def _primary_source_from_item(item: dict, status: str) -> dict | None:
    if not item.get("primary_source_url") or not item.get("primary_source_content"):
        return None
    return {
        "type": item.get("primary_source_type") or "LINKED_DOCUMENT",
        "publisher": item.get("primary_source_publisher") or "",
        "url": item["primary_source_url"],
        "title": item.get("primary_source_title") or "",
        "content": item["primary_source_content"][:12000],
        "status": status,
        **({"published_at": item["primary_source_published_at"]}
           if item.get("primary_source_published_at") else {}),
        **({"document_url": item["primary_source_document_url"]}
           if item.get("primary_source_document_url") else {}),
    }


def _read_feed_article(item: dict, publisher_name: str) -> str:
    """Read a fresh RSS/Atom article page and its linked primary source once."""
    try:
        article = fetch_publisher_article(item["url"], publisher_name, item.get("published_at"), timeout=8)
    except Exception as exc:
        item["material_read"] = False
        item["primary_source_status"] = "ARTICLE_UNREADABLE"
        item["primary_source_error"] = type(exc).__name__
        return item.get("content") or item.get("description") or item["title"]
    item.update({
        "title": article["title"] or item["title"],
        "description": article.get("description") or item.get("description", ""),
        "content": article["content"],
        "publisher_name": article.get("publisher_name") or publisher_name,
        "material_read": article.get("material_read", False),
        "material_url": article.get("material_url", article["url"]),
        "primary_source_url": article.get("primary_source_url"),
        "primary_source_title": article.get("primary_source_title"),
        "primary_source_content": article.get("primary_source_content", ""),
        "primary_source_type": article.get("primary_source_type"),
        "primary_source_publisher": article.get("primary_source_publisher"),
        "primary_source_status": article.get("primary_source_status", "NO_LINK"),
    })
    return item["content"]


def _read_telegram_primary(item: dict, source) -> str:
    """Read links inside the exact post; only configured first-party channels are primary."""
    if "discovery_links" not in item:
        path = urllib.parse.urlsplit(item["url"]).path.strip("/").split("/")
        if len(path) != 2 or not path[1].isdigit():
            item["primary_source_status"] = "ARTICLE_UNREADABLE"
            return item.get("content") or item["title"]
        parser = TelegramPreviewParser(path[0])
        try:
            payload = _request_with_url(f"https://t.me/s/{path[0]}?before={int(path[1]) + 1}", timeout=8)[0]
            parser.feed(payload.decode("utf-8", errors="replace"))
            exact = next((post for post in parser.items if canonicalize(post["url"]) == canonicalize(item["url"])), None)
            if exact is None:
                raise ValueError("Telegram post unavailable")
            item.update(exact)
        except Exception as exc:
            item["primary_source_status"] = "ARTICLE_UNREADABLE"
            item["primary_source_error"] = _safe_source_error(exc)
            return item.get("content") or item["title"]
    links = item.get("discovery_links", [])
    candidates = _primary_link_candidates(links, item["url"])
    # Media links are discovery leads only, never evidence by themselves.
    if not candidates:
        seen = set()
        for link in links:
            url = link.get("href", "")
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname.lower() in {"t.me", "telegram.me"} or url in seen:
                continue
            seen.add(url)
            candidates.append({"url": url, "kind": "DISCOVERY"})
            if len(candidates) == 2:
                break
    item["primary_source_status"] = "NO_LINK"
    for candidate in candidates[:2]:
        try:
            primary = fetch_publisher_article(candidate["url"], urllib.parse.urlsplit(candidate["url"]).hostname or "", item.get("published_at"),
                                              discover_primary=candidate["kind"] == "DISCOVERY", timeout=8)
        except Exception as exc:
            item["primary_source_status"] = "UNREADABLE"
            item["primary_source_error"] = _safe_source_error(exc)
            if candidate["kind"] != "DISCOVERY":
                break
            continue
        if candidate["kind"] == "DISCOVERY":
            if primary.get("primary_source_status") not in {"READ", "OCR_REVIEW"}:
                item["primary_source_status"] = primary.get("primary_source_status", "NO_LINK")
                continue
            item.update({key: value for key, value in primary.items() if key.startswith("primary_source_")})
        else:
            item.update(primary_source_url=primary["url"], primary_source_title=primary["title"],
                        primary_source_content=primary["content"], primary_source_type=candidate["kind"],
                        primary_source_publisher=primary.get("publisher_name", ""),
                        primary_source_status="OCR_REVIEW" if primary.get("primary_source_status") == "OCR_REVIEW" else "READ")
        return item.get("content") or item["title"]
    # Reading the exact configured post is not verification of its claims.
    # Candidate provenance is decided by the editor and checked again before publication.
    role = source["source_role"] if "source_role" in source.keys() else "aggregator"
    channel = urllib.parse.urlsplit(source["url"]).path.strip("/").casefold()
    post_channel = urllib.parse.urlsplit(item["url"]).path.strip("/").split("/")[0].casefold()
    text = item.get("content", "").strip()
    if (role in {"participant", "publisher", "expert"} and channel == post_channel
            and not item.get("telegram_forwarded") and len(text) >= 100):
        item.update(primary_source_url=item["url"], primary_source_title=item["title"],
                    primary_source_content=text, primary_source_type="ORIGINAL_SOCIAL_" + role.upper(),
                    primary_source_publisher=source["name"], primary_source_status="READ")
        return text
    # A configured publisher's own Telegram interview is a read original source,
    # even when its website is temporarily unavailable. Forwarded text is excluded.
    channel_path = urllib.parse.urlsplit(source["url"]).path.strip("/").casefold()
    post_path = urllib.parse.urlsplit(item["url"]).path.strip("/").split("/")[0].casefold()
    if source["reputation"] == "reputable_media" and channel_path == post_path and not item.get("telegram_forwarded"):
        for domain, brand in ORIGINAL_REPORTING_PUBLISHERS.items():
            if re.search(brand, source["name"], re.I):
                kind = _original_reporting_kind("https://" + domain, item.get("content", ""))
                if kind:
                    item.update(primary_source_url=item["url"], primary_source_title=item["title"],
                                primary_source_content=item["content"], primary_source_type=kind,
                                primary_source_publisher=source["name"], primary_source_status="READ")
                    return item["content"]
    # A forwarded post cannot inherit the channel's first-party provenance.
    channel = urllib.parse.urlsplit(source["url"]).path.strip("/").casefold()
    post_channel = urllib.parse.urlsplit(item["url"]).path.strip("/").split("/")[0].casefold()
    if not candidates and source["reputation"] == "primary_source" and channel == post_channel and not item.get("telegram_forwarded"):
        item.update(primary_source_url=item["url"], primary_source_title=item["title"],
                    primary_source_content=item.get("content") or item["title"], primary_source_type="OFFICIAL_SOCIAL_POST",
                    primary_source_publisher=source["name"], primary_source_status="READ")
    return item.get("content") or item["title"]


def _stored_primary(item: dict, primary: dict | None, status: str) -> str:
    record = dict(primary or {"status": status, "error": item.get("primary_source_error")})
    record["_material_read"] = item.get("material_read") is True
    record["_material_url"] = item.get("material_url", item.get("url"))
    record["_material_publisher"] = item.get("publisher_name")
    if "discovery_links" in item:
        record["_discovery_links"] = item["discovery_links"]
        record["_telegram_forwarded"] = item.get("telegram_forwarded", False)
    return json.dumps(record, ensure_ascii=False)


def _impact_evidence_is_grounded(ai_result: dict, primary_source: dict | None) -> bool:
    evidence = re.sub(r"\s+", " ", str(ai_result.get("impact_evidence") or "").strip().strip('"“”«»'))
    source_text = re.sub(r"\s+", " ", str((primary_source or {}).get("content") or "").strip())
    return len(evidence) >= 24 and evidence.casefold() in source_text.casefold()


def _restore_exact_social_headline_evidence(ai_result: dict, item: dict,
                                            primary_source: dict | None) -> dict:
    """Repair a missing quote only when the read post contains its exact headline."""
    if not primary_source or not str(primary_source.get("type", "")).startswith("ORIGINAL_SOCIAL_"):
        return ai_result
    audit = ai_result.get("original_reporting_check") or {}
    evidence = str(audit.get("evidence") or "").strip()
    source_text = re.sub(r"\s+", " ", str(primary_source.get("content") or "")).strip()
    if (audit.get("central_claim_supported") is True and len(evidence) >= 24
            and " ".join(evidence.casefold().split()) in " ".join(source_text.casefold().split())):
        return ai_result
    headline = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
    if (len(headline) >= 24
            and " ".join(headline.casefold().split()) in " ".join(source_text.casefold().split())
            and audit.get("attribution_preserved") is True
            and ai_result.get("facts")
            and all(f.get("claim_type") in {"CLAIM", "REPORT", "OPINION"}
                    for f in ai_result["facts"])):
        ai_result["original_reporting_check"] = {
            **audit, "central_claim_supported": True, "evidence": headline,
        }
        ai_result["_evidence_repair"] = "EXACT_READ_SOCIAL_HEADLINE"
    return ai_result


def require_primary_source_review(ai_result: dict | None, source_status: str, primary_source: dict | None = None,
                                  publisher_report: dict | None = None) -> dict | None:
    if ai_result is None or ai_result.get("action") == "DUPLICATE" or ai_result.get("publication_recommendation") == "DO_NOT_PUBLISH":
        return ai_result
    result = dict(ai_result)
    if publisher_report:
        supported = attributed_report_supported(publisher_report, result)
        if not supported or result.get("publication_recommendation") == "WAIT_FOR_AUTOMATION":
            result["publication_recommendation"] = "WAIT_FOR_AUTOMATION"
            result["source_review_required"] = True
            return result
    elif source_status != "READ" or not primary_source or not primary_source.get("url") or not primary_source.get("content"):
        result["publication_recommendation"] = "WAIT_FOR_AUTOMATION"
        result["source_review_required"] = True
        result["primary_source_missing"] = True
        return result
    if primary_source and primary_source.get("type", "").startswith(("ORIGINAL_MEDIA_", "ORIGINAL_SOCIAL_")):
        # Require an explicit editorial decision about the central claim, with a
        # verbatim supporting excerpt; a stray interview marker alone is insufficient.
        audit = result.get("original_reporting_check") or {}
        quote = audit.get("evidence", "")
        supported = (audit.get("central_claim_supported") is True
                     and len(quote.strip()) >= 24
                     and " ".join(quote.casefold().split()) in " ".join(primary_source["content"].casefold().split())
                     and audit.get("attribution_preserved") is True
                     and result.get("facts")
                     and all(f.get("claim_type") in {"CLAIM", "REPORT", "OPINION"} for f in result["facts"]))
        if not supported or result.get("publication_recommendation") == "WAIT_FOR_AUTOMATION":
            result["publication_recommendation"] = "WAIT_FOR_AUTOMATION"
            result["source_review_required"] = True
            return result
    independent_check = result.get("independent_check", "NOT_ASSESSED")
    # A second publisher is useful corroboration, but waiting for it can make a breaking story stale.
    # Keep material conflicts for an editor; absence of corroboration alone is advisory.
    if independent_check == "CONFLICT":
        result["independent_check_required"] = True
        result["source_review_required"] = True
        result["publication_recommendation"] = "DO_NOT_PUBLISH"
    else:
        result["independent_check_required"] = False

    return result


def _likely_local(item):
    return bool(re.search(r"росси|\bрф\b|совфед|минфин|госдум|банк\s+россии|шейкин|аксаков|набиуллин|сбер(?:банк)?|втб|газпромбанк|мособлбанк|москва|снг|беларус|казахстан", (item.get("title", "") + " " + item.get("description", "")), re.I))


class _WebSearchQuota:
    """Persistent per-purpose cooldown for feeds, source recovery, and story watch."""

    def __init__(self, db, interval_minutes: int, allowed_category: str | None = None):
        self.db = db
        self.interval_minutes = min(3, max(1, int(interval_minutes)))
        self.allowed_category = allowed_category

    def available(self, category: str = "feeds") -> bool:
        category_key = "web_search_last_call_at:" + category
        row = self.db.execute("SELECT value FROM app_state WHERE key=?", (category_key,)).fetchone()
        # Respect the old shared cooldown during the first interval after upgrade.
        if row is None:
            migrated = self.db.execute("SELECT 1 FROM app_state WHERE key GLOB 'web_search_last_call_at:*' LIMIT 1").fetchone()
            if not migrated:
                row = self.db.execute("SELECT value FROM app_state WHERE key='web_search_last_call_at'").fetchone()
        if row:
            try:
                checked = datetime.fromisoformat(row["value"].replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - checked).total_seconds() < self.interval_minutes * 60:
                    return False
            except ValueError:
                pass
        return True

    def reserve(self, source_url: str | None = None, *, category: str = "feeds") -> bool:
        if (self.allowed_category is not None and category != self.allowed_category) or not self.available(category):
            return False
        now = NOW()
        self.db.execute("INSERT INTO app_state(key,value) VALUES('web_search_last_call_at',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
        category_key = "web_search_last_call_at:" + category
        self.db.execute("INSERT INTO app_state(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (category_key, now))
        self.db.execute("INSERT INTO app_state(key,value) VALUES('web_search_last_category',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (category,))
        if source_url:
            self.db.execute("INSERT INTO app_state(key,value) VALUES('web_search_last_source_url',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (source_url,))
        self.db.commit()
        _log_timing("web_search_slot_reserved", category=category,
                    source_url=source_url, interval_minutes=self.interval_minutes)
        return True

    def reserve_primary_recovery(self) -> bool:
        return self.reserve(category="primary_recovery")

    def reserve_story_watch(self) -> bool:
        return self.reserve(category="story_watch")

    def __call__(self, query: str, ai_settings: dict) -> list[dict]:
        return fetch_web_search(query, ai_settings)


def _recover_primary(db, item, settings, *, steps=False):
    """Source recovery candidates still pass the normal factual/editorial gates."""
    from .source_search import recover
    return recover(db, item, settings, fetch_google_news,
                   settings.get("_web_search_quota") or fetch_web_search, terms, similarity, steps=steps)


def _agent_recover_primary(db, item, settings, source, item_id=None, *, steps=False):
    from .workflow import drive
    generator = _agent_recover_steps(db, item, settings, source, item_id)
    return generator if steps else drive(generator, settings.get('_runtime'))


def _agent_recover_steps(db, item, settings, source, item_id=None):
    """Let the bounded research agent choose a recovery step for unreadable news.

    Search results are not evidence until their publisher page is read. The
    agent can only read URLs returned by its own allowlisted search tool; the
    ordinary editor and publication gates remain authoritative.
    """
    from .agent import run_research_agent
    from .workflow import Work, resolve_steps
    from .runtime import BudgetDeferred

    quota = settings.get("_web_search_quota")
    searched: dict[str, dict] = {}
    read: dict[str, dict] = {}
    history: list[dict] = []

    def search(args):
        revision_hash = digest(str(item.get("title", "")) + "\n" + str(item.get("content", "")))
        prior = db.execute("SELECT MAX(attempt) FROM source_search_log WHERE item_url=? AND revision_hash=? AND outcome NOT IN ('STARTED','DEFERRED')",
                           (item["url"], revision_hash)).fetchone()[0] or 0
        if prior >= 3:
            return {"status": "BUDGET_LIMIT", "results": []}
        if settings.get("_recovery_search_budget", 0) <= 0:
            item["_source_search_deferred"] = True
            return {"status": "DEFERRED", "results": []}
        if not quota or not quota.reserve_primary_recovery():
            return {"status": "DEFERRED", "results": []}
        settings["_recovery_search_budget"] -= 1
        attempt = prior + 1
        from .source_search import log as log_source_search
        try:
            db.commit()
            results = yield Work('collector', fetch_web_search,
                (args["query"], {**settings, "timeout_seconds": min(25, int(settings.get("timeout_seconds", 45)))}),
                {"max_results": 3, "page_timeout": 8})
        except BudgetDeferred:
            item['_source_search_deferred'] = True
            item['_budget_deferred'] = True
            log_source_search(db, item['url'], attempt, 'AGENT_SEARCH_WEB', args['query'], 'DEFERRED', [], revision_hash)
            db.commit()
            raise
        except Exception as exc:
            log_source_search(db, item["url"], attempt, "AGENT_SEARCH_WEB", args["query"],
                              "ERROR", [{"error_code": type(exc).__name__}], revision_hash)
            db.commit()
            raise
        rows = []
        checked = []
        for result in results[:6]:
            url = result.get("material_url") or result.get("url")
            if not url or not str(url).startswith(("https://", "http://")):
                continue
            searched[url] = result
            checked.append({key: result.get(key) for key in ("url", "title", "content", "publisher_name",
                              "published_at", "material_read", "primary_source_url",
                              "primary_source_content", "primary_source_status", "primary_source_type")})
            rows.append({"title": result.get("title", "")[:240], "url": url,
                         "publisher": result.get("publisher_name", "")[:120],
                         "published_at": result.get("published_at")})
        log_source_search(db, item["url"], attempt, "AGENT_SEARCH_WEB", args["query"],
                          "FOUND_CANDIDATE" if rows else "NOT_FOUND", checked, revision_hash)
        db.commit()
        return {"status": "OK" if rows else "NO_RESULTS", "results": rows}

    def read_url(args):
        url = args["url"]
        result = searched.get(url)
        if result is None:
            return {"status": "REJECTED", "reason": "URL_NOT_FROM_SEARCH"}
        article = result
        content = str(article.get("content") or "")
        if result.get("material_read") is not True or len(content.strip()) < 100:
            parsed = urllib.parse.urlsplit(url)
            try:
                db.commit()
                article = yield Work('collector', fetch_publisher_article,
                    (url, result.get("publisher_name") or parsed.hostname or "Издание", result.get("published_at")),
                    {'discover_primary': True, 'timeout': 8, 'public_only': True})
                content = str(article.get("content") or "")
            except Exception as exc:
                code = _safe_source_error(exc)
                article = {"url": url, "title": result.get("title", ""),
                           "content": "", "publisher_name": result.get("publisher_name", ""),
                           "material_read": False, "read_error": code}
                content = ""

        revision_hash = digest(str(item.get("title", "")) + "\n" + str(item.get("content", "")))
        prior_search = db.execute(
            "SELECT attempt,query FROM source_search_log WHERE item_url=? AND revision_hash=? "
            "ORDER BY search_id DESC LIMIT 1", (item["url"], revision_hash),
        ).fetchone()
        attempt = int(prior_search["attempt"] or 1) if prior_search else 1
        from .source_search import log as log_source_search
        primary_content = str(article.get("primary_source_content") or "")
        article_readable = article.get("material_read") is True and len(content.strip()) >= 100
        primary_readable = (article.get("primary_source_status") == "READ"
                            and len(primary_content.strip()) >= 100)
        readable = article_readable or primary_readable
        checked = [{"url": article.get("url") or url, "title": article.get("title", ""),
                    "publisher_name": article.get("publisher_name", ""),
                    "published_at": article.get("published_at"),
                    "content": content[:12000], "material_read": article_readable,
                    "primary_source_url": article.get("primary_source_url"),
                    "primary_source_title": article.get("primary_source_title"),
                    "primary_source_content": primary_content[:12000],
                    "primary_source_status": article.get("primary_source_status", "NOT_CHECKED"),
                    "primary_source_type": article.get("primary_source_type"),
                    **({"error_code": article.get("read_error")} if article.get("read_error") else {})}]
        log_source_search(db, item["url"], attempt, "AGENT_READ_URL", url,
                          "READ" if readable else "UNREADABLE", checked, revision_hash)
        db.commit()
        if not readable:
            return {"status": "UNREADABLE", "url": url,
                    "error_code": article.get("read_error", "ARTICLE_TEXT_TOO_SHORT")}

        article = {**article, "url": article.get("url") or url, "content": content,
                   "publisher_name": article.get("publisher_name") or result.get("publisher_name"),
                   "material_read": article_readable}
        read[url] = article
        read[article["url"]] = article
        primary_url = article.get("primary_source_url")
        if (primary_url and article.get("primary_source_status") == "READ"
                and len(primary_content.strip()) >= 100):
            read[primary_url] = {
                "url": primary_url, "title": article.get("primary_source_title") or "",
                "publisher_name": article.get("primary_source_publisher") or "",
                "content": primary_content[:12000], "material_read": True,
                "primary_source_status": "READ",
                "primary_source_type": article.get("primary_source_type"),
            }
        return {"status": "READ" if article_readable else "PRIMARY_SOURCE_READ",
                "title": article.get("title", "")[:240],
                "url": article["url"], "publisher": article.get("publisher_name", "")[:120],
                "published_at": article.get("published_at"),
                "content": content[:8000],
                "primary_source_url": primary_url or "",
                "primary_source_title": article.get("primary_source_title", "")[:240],
                "primary_source_status": article.get("primary_source_status", "NOT_CHECKED"),
                "primary_source_content_excerpt": primary_content[:3000]}

    def check_history(args):
        query = args["query"]
        rows = db.execute("SELECT story_id,headline,canonical_topic,latest_information,last_published_at "
                          "FROM stories ORDER BY last_updated_at DESC LIMIT 1000").fetchall()
        matches = sorted(((similarity(query, " ".join(str(row[k] or "") for k in
                          ("headline", "canonical_topic", "latest_information"))), row) for row in rows),
                         key=lambda pair: pair[0], reverse=True)[:5]
        history[:] = [{"story_id": row["story_id"], "headline": row["headline"],
                       "latest_information": row["latest_information"][:800],
                       "last_published_at": row["last_published_at"], "similarity": round(score, 3)}
                      for score, row in matches if score >= 0.2]
        return {"status": "OK", "matches": history}

    def record_agent_event(event):
        _trace_item(item, "Исследователь", event.get("status", "ШАГ"),
                    event.get("rationale") or event.get("tool", ""),
                    tool=event.get("tool"), step=event.get("step"))
        _log_timing("research_agent_action_timing", item_id=item_id,
                    tool=event.get("tool"), outcome=event.get("status"),
                    model_seconds=event.get("model_seconds"),
                    tool_seconds=event.get("tool_seconds"))

    try:
        result = yield from resolve_steps(run_research_agent(
            {"title": item.get("title", ""), "description": item.get("description", "")[:1500],
             "source": source["name"], "source_url": item.get("url", ""),
             "published_at": item.get("published_at"), "read_text": str(item.get("content") or "")[:3000],
             "goal": "Найти прочитанный первоисточник или пригодный материал СМИ для проверки этой свежей новости."},
            {**settings, "timeout_seconds": min(25, int(settings.get("timeout_seconds", 45)))},
            {"search_web": search, "read_url": read_url, "check_story_history": check_history},
            max_steps=int(settings.get("research_agent_max_steps", 4)), execute_tools=True,
            on_event=record_agent_event, steps=True))
    except BudgetDeferred:
        item['_source_search_deferred'] = True
        item['_budget_deferred'] = True
        return None
    except Exception as exc:
        _trace_item(item, "Исследователь", "Ошибка", type(exc).__name__)
        return None

    selected = result.get("selected_primary_url") or result.get("selected_report_url")
    material = read.get(selected)
    if not material:
        return None
    if (len(terms(item.get("title", "")) & terms(material.get("title", ""))) < 3
            or similarity(item.get("title", ""), material.get("title", "")) < 0.2):
        _trace_item(item, "Исследователь", "Несовпадение", "Найденная страница не совпала с заголовком исходной новости.")
        return None
    host = urllib.parse.urlsplit(selected).hostname or ""
    article_text = str(material.get("content") or "")[:12000]
    if len(article_text.strip()) < 100:
        _trace_item(item, "Исследователь", "Непрочитанный результат",
                    "Не передаю поисковую страницу в редакторский этап без читаемого текста.")
        return None
    publisher = material.get("publisher_name") or host
    if _is_official_source_host(host):
        return {"primary_source_url": selected, "primary_source_title": material.get("title", ""),
                "primary_source_content": article_text, "primary_source_type": "OFFICIAL",
                "primary_source_publisher": publisher, "primary_source_status": "READ",
                "primary_source_published_at": material.get("published_at"),
                "material_read": True, "content": article_text, "url": selected,
                "title": material.get("title", ""), "publisher_name": publisher,
                "published_at": material.get("published_at")}
    return {"material_read": True, "content": article_text, "url": selected,
            "title": material.get("title", ""), "publisher_name": publisher,
            "published_at": material.get("published_at")}


def _shadow_research_agent(item, settings, source, *, steps=False):
    from .workflow import drive
    generator = _shadow_research_steps(item, settings, source)
    return generator if steps else drive(generator, settings.get('_runtime'))


def _shadow_research_steps(item, settings, source):
    """Record one proposed recovery action without invoking its tool."""
    from .agent import run_research_agent
    from .workflow import resolve_steps
    try:
        return (yield from resolve_steps(run_research_agent(
            {"title": item.get("title", ""), "description": item.get("description", "")[:1500],
             "source": source["name"], "source_url": item.get("url", ""),
             "published_at": item.get("published_at"), "read_text": str(item.get("content") or "")[:3000],
             "goal": "Выбрать следующий полезный шаг для восстановления читаемого источника."},
            {**settings, "timeout_seconds": min(25, int(settings.get("timeout_seconds", 45)))},
            {}, max_steps=1, execute_tools=False,
            on_event=lambda event: _trace_item(item, "Исследователь · тень", event.get("status", "ШАГ"),
                                                event.get("tool", ""), tool=event.get("tool"),
                                                step=event.get("step")), steps=True)))
    except Exception as exc:
        _trace_item(item, "Исследователь · тень", "Ошибка", type(exc).__name__)
        return None


def _history_context(db, item):
    # Archive scope is explicit: lack of a match must not be called a web investigation.
    names = set(re.findall(r"\b[А-ЯЁ][а-яё]{4,}\b", item.get("title", "")))
    rows = db.execute("SELECT title,url,published_at,content FROM items WHERE published_at<? AND disposition IN ('NEW_STORY','UPDATE_CANDIDATE') ORDER BY published_at DESC LIMIT 300", (item.get("published_at") or NOW(),)).fetchall()
    matches = [dict(r) for r in rows if any(name in r["title"] for name in names)]
    return [{"scope":"local_archive","title":r["title"],"url":r["url"],"published_at":r["published_at"],"content":r["content"][:1200]} for r in matches[:4]]


def _editorial_examples(db, item: dict, item_id: int, limit: int = 12,
                        story_id: int | None = None) -> list[dict]:
    """Prefer feedback tied to this story or similar coverage over merely recent notes."""
    current = " ".join(str(item.get(key) or "") for key in ("title", "description"))
    if story_id is None:
        story = db.execute("SELECT story_id FROM items WHERE item_id=?", (item_id,)).fetchone()
        story_id = story["story_id"] if story else None
    columns = ("feedback_id,feedback_type,reason,item_title,substr(post_text,1,900) AS post_text,"
               "item_id,story_id,created_at")
    related = db.execute(
        f"SELECT {columns} FROM editorial_feedback WHERE item_id=? OR (? IS NOT NULL AND story_id=?) "
        "ORDER BY created_at DESC LIMIT 200", (item_id, story_id, story_id),
    ).fetchall()
    recent = db.execute(
        f"SELECT {columns} FROM editorial_feedback ORDER BY created_at DESC LIMIT 400"
    ).fetchall()
    rows_by_id = {row["feedback_id"]: row for row in (*related, *recent)}
    rows = list(rows_by_id.values())
    scored = []
    general = []
    for row in rows:
        exact_item = row["item_id"] == item_id
        same_story = bool(story_id and row["story_id"] == story_id)
        topic_score = max(similarity(current, row["item_title"] or ""),
                          0.65 * similarity(current, row["reason"] or ""))
        # Owner-authored notes and confirmed/refined lessons can express style
        # preferences across topics; the editor prompt still forbids using them
        # as evidence for facts in the current story.
        is_general = row["feedback_type"] in {
            "OTHER", "TELEGRAM_LINK_FEEDBACK", "TELEGRAM_EDIT_CONFIRMATION",
            "TELEGRAM_EDIT_REFINEMENT",
        }
        score = (2.0 if exact_item else 0.0) + (1.4 if same_story else 0.0) + topic_score
        if is_general:
            # A direct owner note about a post or a refinement of the agent's
            # inferred lesson is a stronger editorial signal than a generic
            # recent comment. Keep that signal in the small cross-topic slice.
            owner_priority = {
                "TELEGRAM_EDIT_REFINEMENT": 3,
                "TELEGRAM_LINK_FEEDBACK": 2,
                "TELEGRAM_EDIT_CONFIRMATION": 1,
            }.get(row["feedback_type"], 0)
            general.append((owner_priority, score, row))
        if score > 0.05 and not is_general:
            scored.append((score, row))
    scored.sort(key=lambda pair: (pair[0], pair[1]["created_at"] or ""), reverse=True)
    general.sort(key=lambda pair: (pair[0], pair[1], pair[2]["created_at"] or ""), reverse=True)
    general_limit = min(3, limit)
    selected = [row for _, row in scored[:max(0, limit - general_limit)]]
    selected_ids = {row["feedback_id"] for row in selected}
    for _, _, row in general:
        if sum(candidate["feedback_type"] in {
                "OTHER", "TELEGRAM_LINK_FEEDBACK", "TELEGRAM_EDIT_CONFIRMATION",
                "TELEGRAM_EDIT_REFINEMENT",
        } for candidate in selected) >= general_limit:
            break
        if row["feedback_id"] not in selected_ids:
            selected.append(row)
            selected_ids.add(row["feedback_id"])
    for _, row in scored:
        if len(selected) >= limit:
            break
        if row["feedback_id"] not in selected_ids:
            selected.append(row)
            selected_ids.add(row["feedback_id"])
    return [{key: row[key] for key in ("feedback_type", "reason", "item_title", "post_text")}
            for row in selected[:limit]]


def _archive_item_revision(db, item_id: int, prior_row) -> None:
    analysis = db.execute("SELECT model,created_at,result_json FROM item_analysis WHERE item_id=?", (item_id,)).fetchone()
    retry_keys = (f"triage:{item_id}", f"selection_retry:{item_id}", f"editor_retry:{item_id}")
    retry_state = {}
    for key in retry_keys:
        state = db.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        if state:
            retry_state[key] = state["value"]
    source_snapshot = dict(prior_row)
    revision_hash = digest("\n".join((source_snapshot.get("title") or "", source_snapshot.get("content") or "",
                                       source_snapshot.get("description") or "")))
    db.execute("INSERT INTO item_revisions(item_id,observed_at,revision_hash,source_snapshot_json,decision_snapshot_json) VALUES(?,?,?,?,?)",
               (item_id, NOW(), revision_hash, json.dumps(source_snapshot, ensure_ascii=False),
                json.dumps({"analysis": dict(analysis) if analysis else None,
                            "retry_state": retry_state,
                            "disposition": source_snapshot.get("disposition"),
                            "processed_at": source_snapshot.get("processed_at")}, ensure_ascii=False)))


def _trace_item(item: dict, stage: str, outcome: str, reason: str, **details) -> None:
    """Keep a compact, user-readable trace alongside the immutable decision snapshot."""
    trace = item.setdefault("_audit_trace", [])
    event = {"stage": stage[:80], "outcome": outcome[:60], "reason": str(reason or "")[:400]}
    for key, value in details.items():
        if value is None or value == "":
            continue
        event[key] = value[:400] if isinstance(value, str) else value
    trace.append(event)


def _read_material_work(item, source, publisher_name):
    import copy
    local = copy.deepcopy(item)
    body = (_read_telegram_primary(local, source) if source["type"] == "telegram"
            else _read_feed_article(local, publisher_name))
    return {"body": body, "item": local}


def _editor_history_revision(db, item):
    from .runtime import cache_key
    # Include feedback and publication changes, including edits, which can
    # invalidate a decision even when no new story has appeared.
    candidate = " ".join(str(item.get(key) or "") for key in ("title", "description", "content"))
    related = [dict(row) for row in db.execute("SELECT * FROM stories")
               if similarity(candidate, row['canonical_topic'] + ' ' + row['headline'] + ' ' + row['latest_information']) >= .12]
    ids = [row['story_id'] for row in related]
    snapshots = {"stories": related,
                 'story_registry': [tuple(row) for row in db.execute(
                     'SELECT story_id,version,publication_count,last_updated_at,last_published_at FROM stories ORDER BY story_id')],
                 'publication_registry': [tuple(row) for row in db.execute(
                     'SELECT post_id,status,post_hash FROM posts ORDER BY post_id')]}
    for table in ("posts", "story_facts", "publication_coverage"):
        marks = ','.join('?' for _ in ids) or 'NULL'
        snapshots[table] = [tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE story_id IN ({marks})", ids)]
    snapshots['feedback'] = [tuple(row) for row in db.execute(
        "SELECT feedback_id,feedback_type,reason,item_title,post_text,item_id,story_id FROM editorial_feedback ORDER BY feedback_id DESC LIMIT 400")]
    return cache_key("editor-history", snapshots)


def _save_item(db, source, item, existing_item_id=None):
    """Persist discovery before any costly work; keep feed and read text distinct."""
    now = NOW()
    from .runtime import cache_key
    ingest_revision = cache_key('material-version', {key: item.get(key) for key in (
        'url', 'title', 'description', 'content', 'author', 'published_at', 'updated_at')})
    canonical = canonicalize(item["url"])
    body = item.get("content") or item.get("description") or item["title"]
    content_hash, title_hash = digest(body), digest(item["title"].lower().strip())
    feed_content_hash = digest(" ".join(str(body).split()))
    try:
        source_status = item.get("primary_source_status") or "NOT_CHECKED"
        primary_source = _primary_source_from_item(item, source_status)
        if existing_item_id is None:
            try:
                cur = db.execute("""INSERT INTO items(source_id,url,canonical_url,title,description,content,author,published_at,updated_at,
                  discovered_at,content_hash,title_hash,feed_content_hash,primary_source_json,ingest_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (source["source_id"], item["url"], canonical, item["title"], item.get("description", ""),
                   item.get("content", ""), item.get("author"), item.get("published_at"), item.get("updated_at"),
                   now, content_hash, title_hash, feed_content_hash,
                   _stored_primary(item, primary_source, source_status), ingest_revision))
                item_id = cur.lastrowid
            except Exception as exc:
                if "UNIQUE" not in str(exc):
                    raise
                prior = db.execute("SELECT * FROM items WHERE source_id=? AND canonical_url=?",
                                   (source["source_id"], canonical)).fetchone()
                if prior is None:
                    return None
                if db.execute("SELECT 1 FROM items WHERE source_id=? AND content_hash=? AND item_id<>?",
                              (source["source_id"], content_hash, prior["item_id"])).fetchone():
                    return None
                same_metadata = (prior["title_hash"] == title_hash
                                 and prior["description"] == item.get("description", "")
                                 and prior["author"] == item.get("author")
                                 and prior["published_at"] == item.get("published_at")
                                 and prior["updated_at"] == item.get("updated_at"))
                stored_feed_hash = prior["feed_content_hash"] if "feed_content_hash" in prior.keys() else ""
                # `items.content` is enriched with fetched article text. Compare a
                # feed poll to its own last-seen body so enrichment cannot reset retries.
                unchanged = same_metadata and (not stored_feed_hash or stored_feed_hash == feed_content_hash)
                if unchanged:
                    if not stored_feed_hash:
                        db.execute("UPDATE items SET feed_content_hash=? WHERE item_id=?",
                                   (feed_content_hash, prior["item_id"]))
                        db.commit()
                    return None
                item_id = prior["item_id"]
                _archive_item_revision(db, item_id, prior)
                db.execute("""UPDATE items SET url=?,title=?,description=?,content=?,author=?,published_at=?,updated_at=?,
                              content_hash=?,title_hash=?,feed_content_hash=?,primary_source_json=?,disposition='PENDING',processed_at=NULL
                              ,ingest_revision=? WHERE item_id=?""",
                           (item["url"], item["title"], item.get("description", ""), body, item.get("author"),
                            item.get("published_at"), item.get("updated_at"), content_hash, title_hash,
                            feed_content_hash, _stored_primary(item, primary_source, source_status), ingest_revision, item_id))
                db.execute("DELETE FROM item_analysis WHERE item_id=?", (item_id,))
                db.executemany("DELETE FROM app_state WHERE key=?", [(f"triage:{item_id}",),
                                 (f"selection_retry:{item_id}",), (f"editor_retry:{item_id}",)])
        else:
            item_id = existing_item_id
            prior = db.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
            if prior and (prior["content_hash"] != content_hash or prior["title_hash"] != title_hash
                          or prior["published_at"] != item.get("published_at")
                          or prior["updated_at"] != item.get("updated_at")):
                _archive_item_revision(db, item_id, prior)
            db.execute("""UPDATE items SET title=?,description=?,content=?,author=?,published_at=?,updated_at=?,
                          content_hash=?,title_hash=?,primary_source_json=?
                          WHERE item_id=?""",
                       (item['title'], item.get('description', ''), body, item.get('author'),
                        item.get('published_at'), item.get('updated_at'), content_hash, title_hash,
                        _stored_primary(item, primary_source, source_status), item_id))
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return None
        raise
    return item_id


def process_item(db, source, item: dict, threshold: float, max_length: int, freshness_hours: int,
                 initial_backfill_minutes: int | None = None, relevance_terms: list[str] | None = None,
                 ai_settings: dict | None = None, existing_item_id: int | None = None,
                 post_ready_callback=None) -> str:
    from .workflow import drive
    return drive(process_item_steps(db, source, item, threshold, max_length, freshness_hours,
        initial_backfill_minutes, relevance_terms, ai_settings, existing_item_id, post_ready_callback),
        (ai_settings or {}).get("_runtime"), {"item_id": existing_item_id,
        "category": (ai_settings or {}).get("_work_category", "fresh")})


def process_item_steps(db, source, item: dict, threshold: float, max_length: int, freshness_hours: int,
                 initial_backfill_minutes: int | None = None, relevance_terms: list[str] | None = None,
                 ai_settings: dict | None = None, existing_item_id: int | None = None,
                 post_ready_callback=None) -> str:
    timings = {"primary_source_read_seconds": 0.0, "ai_seconds": 0.0,
               "research_agent_seconds": 0.0}
    started = time.perf_counter()
    post_ids_ready = []
    post_ready_item_id = None
    outcome = "INTERRUPTED"
    try:
        outcome = yield from _process_item_steps(db, source, item, threshold, max_length, freshness_hours,
                                initial_backfill_minutes, relevance_terms, ai_settings, timings, existing_item_id)
        row = db.execute("SELECT item_id,disposition FROM items WHERE source_id=? AND canonical_url=?",
                         (source["source_id"], canonicalize(item["url"]))).fetchone()
        if row and row["disposition"] == outcome:
            if outcome in {"PRIMARY_RETRY", "AI_RETRY", "WAITING_CONFIRMATION"}:
                retry_without_count = bool(item.get("_retry_without_count") or item.get("_source_search_deferred"))
                attempts = schedule_retry(
                    db, row["item_id"], outcome,
                    retry=existing_item_id is not None and not item.get('_workflow_first_attempt') and not retry_without_count,
                    reason=item.get("_retry_reason"),
                    delay_seconds=item.get('_retry_delay_seconds', (ai_settings or {}).get("_retry_cycle_delay_seconds")) if retry_without_count else None,
                )
                if attempts >= MAX_AUTOMATIC_RETRIES:
                    exhausted_reason = (f"Исчерпан лимит: {attempts} автоматических повторных проверок; "
                                        "эта версия закрыта без публикации.")
                    item["_retry_reason"] = exhausted_reason
                    _trace_item(item, "Лимит повторов", "Закрыт", exhausted_reason,
                                attempts=attempts, limit=MAX_AUTOMATIC_RETRIES)
                    retry_key = f"selection_retry:{row['item_id']}"
                    retry_row = db.execute("SELECT value FROM app_state WHERE key=?", (retry_key,)).fetchone()
                    if retry_row:
                        try:
                            retry_state = json.loads(retry_row["value"] or "{}")
                        except (TypeError, json.JSONDecodeError):
                            retry_state = {}
                        retry_state.update({"reason": exhausted_reason, "outcome": "REJECTED", "next_at": NOW()})
                        db.execute("UPDATE app_state SET value=? WHERE key=?",
                                   (json.dumps(retry_state, ensure_ascii=False), retry_key))
                    db.execute("UPDATE items SET disposition='REJECTED',processed_at=? WHERE item_id=?",
                               (NOW(), row["item_id"]))
                    outcome = "REJECTED"
            else:
                schedule_retry(db, row["item_id"], outcome)
            trace = item.get("_audit_trace", [])
            last_reason = trace[-1].get("reason") if trace else ""
            summary = (item.get("_retry_reason") or last_reason or {
                "NEW_STORY": "Создан новый сюжет; черновик передан на автоматический допуск.",
                "UPDATE_CANDIDATE": "Добавлено новое сообщение к известному сюжету; черновик передан на автоматический допуск.",
                "DUPLICATE": "Новое существенное сведение не найдено.",
                "NOISE": "Материал отсеян до публикации.",
                "REJECTED": "Автоматическая обработка завершена по лимиту повторных проверок.",
                "STORE_ONLY": "Свидетельство сохранено в памяти; нового повода для поста нет.",
            }.get(outcome, "Обработка завершена."))
            _trace_item(item, "Итог обработки", outcome, summary)
            audit = {}
            if item.get("_audit_trace"):
                audit["audit_trace"] = item["_audit_trace"]
            if item.get("_audit_triage"):
                audit["audit_triage"] = item["_audit_triage"]
            from .decisions import record
            record(db, row["item_id"], outcome, (ai_settings or {}).get("model"), extra=audit)
            db.commit()
        if outcome in {"NEW_STORY", "UPDATE_CANDIDATE"} and post_ready_callback:
            item_row = db.execute("SELECT item_id FROM items WHERE source_id=? AND canonical_url=?",
                                  (source["source_id"], canonicalize(item["url"]))).fetchone()
            if item_row:
                post_ready_item_id = item_row["item_id"]
                post_ids_ready = [entry["post_id"] for entry in db.execute(
                    "SELECT post_id FROM posts WHERE origin_item_id=? AND status='PENDING' ORDER BY post_id",
                    (item_row["item_id"],)).fetchall()]
                if post_ids_ready:
                    db.commit()
    except Exception as exc:
        outcome = "ERROR"
        db.rollback()
        from .decisions import record_failure
        record_failure(db, item, source['source_id'], (ai_settings or {}).get('model'), type(exc).__name__)
        db.commit()
        raise
    finally:
        try:
            item_row = db.execute("SELECT item_id,discovered_at FROM items WHERE source_id=? AND canonical_url=?",
                                  (source["source_id"], canonicalize(item["url"]))).fetchone()
        except Exception:
            item_row = None
        _log_timing("item_processing_timing", item_id=item_row["item_id"] if item_row else None,
                    source=source["name"], source_type=source["type"], outcome=outcome,
                    source_published_at=(item.get("updated_at") or item.get("published_at")),
                    discovered_at=item_row["discovered_at"] if item_row else None,
                    processed_at=NOW(), total_seconds=round(time.perf_counter() - started, 3),
                    primary_source_read_seconds=round(timings["primary_source_read_seconds"], 3),
                    ai_seconds=round(timings["ai_seconds"], 3),
                    research_agent_seconds=round(timings["research_agent_seconds"], 3))
        if (timings["primary_source_read_seconds"] > 0 or timings["ai_seconds"] > 0
                or timings["research_agent_seconds"] > 0
                or outcome not in {"DUPLICATE", "NOISE", "STALE", "UNDATED", "BASELINE_SKIPPED"}):
            _log_timing("news_processing_timing", source=source["name"],
                        title=item.get("title", "")[:160], outcome=outcome,
                        primary_source_read_seconds=round(timings["primary_source_read_seconds"], 3),
                        ai_seconds=round(timings["ai_seconds"], 3),
                        research_agent_seconds=round(timings["research_agent_seconds"], 3),
                        total_seconds=round(time.perf_counter() - started, 3))
    if post_ids_ready and post_ready_callback:
        try:
            post_ready_callback(post_ids_ready)
        except Exception as callback_error:
            _log_timing("post_ready_callback_error", item_id=post_ready_item_id,
                        error_type=type(callback_error).__name__)
    return outcome


def _read_material_report(source, item: dict, body: str) -> dict | None:
    """A read secondary account is evidence of a report, never proof of its claims.

    Read provenance comes from the page/post reader, not source rank or an AI flag.
    Metadata, RSS summaries and search snippets cannot take this path.
    """
    role = source["source_role"] if "source_role" in source.keys() else "aggregator"
    url = item.get("material_url") or item.get("url", "")
    if (item.get("material_read") is not True or len((body or "").strip()) < 24
            or not str(url).startswith(("https://", "http://"))):
        return None
    return {"publisher": item.get("publisher_name") or source["name"], "url": url,
            "title": item.get("title", ""), "content": body, "type": "ATTRIBUTED_REPORT",
            "material_read": True, "source_role": role,
            "published_at": item.get("published_at") or item.get("updated_at"),
            "forwarded": bool(item.get("telegram_forwarded")),
            "priority": int(source["priority"]), "reputation": source["reputation"]}


def _process_item_steps(db, source, item: dict, threshold: float, max_length: int, freshness_hours: int,
                  initial_backfill_minutes: int | None, relevance_terms: list[str] | None,
                  ai_settings: dict | None, timings: dict[str, float], existing_item_id: int | None = None) -> str:
    now = NOW()
    from .workflow import Work, resolve_steps
    from .runtime import BudgetDeferred, cache_key
    memory_mode = (ai_settings or {}).get("memory_mode", "off")
    memory_enforced = memory_mode == "enforce"
    publisher_name = item.get("publisher_name") or source["name"]
    editor_source = dict(source)
    editor_source["name"] = publisher_name
    if item.get("publisher_name") and not (int(source["priority"]) == 3
            and source["reputation"] == "reputable_media"
            and (source["source_role"] if "source_role" in source.keys() else "aggregator") == "publisher"):
        editor_source["reputation"] = "unknown"
    body = item.get("content") or item.get("description") or item["title"]
    content_hash, title_hash = digest(body), digest(item["title"].lower().strip())
    source_status = item.get("primary_source_status") or "NOT_CHECKED"
    primary_source = _primary_source_from_item(item, source_status)
    item_id = _save_item(db, source, item, existing_item_id)
    if item_id is None:
        return "DUPLICATE"
    db.commit()
    if is_non_news_telegram_format(source, item):
        _trace_item(item, "Формат материала", "Отсеян", "Сообщение не является новостной публикацией.")
        db.execute("UPDATE items SET disposition='NOISE',processed_at=? WHERE item_id=?", (now, item_id))
        db.commit()
        return "NOISE"
    event_time = item.get("updated_at") or item.get("published_at")
    if not event_time:
        _trace_item(item, "Дата публикации", "Отсеян", "У материала не указана дата публикации.")
        db.execute("UPDATE items SET disposition='UNDATED',processed_at=? WHERE item_id=?", (now, item_id))
        db.commit()
        return "UNDATED"
    try:
        age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(event_time.replace("Z", "+00:00"))).total_seconds() / 3600
    except ValueError:
        age_hours = 0
    if initial_backfill_minutes is not None and age_hours * 60 > initial_backfill_minutes:
        _trace_item(item, "Первичная загрузка", "Отсеян", "Материал старше окна первичной загрузки.")
        db.execute("UPDATE items SET disposition='BASELINE_SKIPPED',processed_at=? WHERE item_id=?", (now, item_id))
        db.commit()
        return "BASELINE_SKIPPED"
    if age_hours > freshness_hours:
        _trace_item(item, "Свежесть", "Отсеян", f"Материал старше окна свежести ({freshness_hours} ч).")
        db.execute("UPDATE items SET disposition='STALE',processed_at=? WHERE item_id=?", (now, item_id))
        db.commit()
        return "STALE"
    # Use the headline and publisher's summary for topic screening; long article bodies can mention unrelated keywords.
    relevance_text = f"{item['title']} {item.get('description', '')}"
    if source["type"] == "manual" and item.get("material_read") is True:
        # A manually submitted page has no RSS summary. Use only its opening excerpt
        # for the cheap topic prefilter; the normal AI triage and full checks still decide relevance.
        excerpt_limit = 12000 if item.get("screening_primary_source") is True else 2200
        relevance_text += " " + str(item.get("screening_excerpt") or "")[:excerpt_limit]
    if relevance_terms and not is_relevant(relevance_text, relevance_terms):
        _trace_item(item, "Тематический фильтр", "Отсеян",
                    "В заголовке и описании не найдено совпадений с темами мониторинга.")
        db.execute("UPDATE items SET disposition='NOISE',processed_at=? WHERE item_id=?", (now, item_id))
        db.commit()
        return "NOISE"
    selection = None
    if ai_settings and ai_settings.get("triage_enabled"):
        stage_started = time.perf_counter()
        selection = yield from resolve_steps(screen_item(db, item_id, item, ai_settings, steps=True))
        timings["ai_seconds"] += time.perf_counter() - stage_started
        decision = selection["decision"]
        item["_audit_triage"] = {key: selection.get(key) for key in
                                 ("decision", "reason", "evidence", "confidence", "origin") if selection.get(key) is not None}
        _trace_item(item, "Предварительный ИИ-отбор", decision,
                    selection.get("reason") or "Предварительный отбор завершён.",
                    evidence=selection.get("evidence"), confidence=selection.get("confidence"))
        if decision in {"NOISE", "DEFER"} or (decision == "DUPLICATE" and not memory_enforced):
            outcome = "AI_RETRY" if decision == "DEFER" else decision
            if decision == "DEFER":
                item["_retry_without_count"] = bool(selection.get("retry_without_count"))
                item['_budget_deferred'] = bool(selection.get('budget_deferred'))
                item['_history_changed'] = bool(selection.get('history_changed'))
                if selection.get('retry_delay_seconds'):
                    item['_retry_delay_seconds'] = selection['retry_delay_seconds']
                item["_retry_reason"] = selection.get("reason")
            story_id = int(selection["story_id"]) if decision == "DUPLICATE" else None
            db.execute("UPDATE items SET disposition=?,story_id=?,processed_at=? WHERE item_id=?",
                       (outcome, story_id, now, item_id))
            db.commit()
            return outcome
    if source["type"] in {"rss", "web", "google_news", "web_search", "x", "telegram"} and item.get("primary_source_status") is None:
        stage_started = time.perf_counter()
        db.commit()
        read_key = cache_key("read", {"item": {key: value for key, value in item.items() if not key.startswith('_')},
                                     "source": {key: dict(source).get(key) for key in ('name','type','source_role','priority','reputation')},
                                     "reader_version": 1})
        read = yield Work("collector", _read_material_work, (dict(item), dict(source), publisher_name),
                          key=read_key, ttl=180)
        body = read["body"]
        item.update(read["item"])
        timings["primary_source_read_seconds"] += time.perf_counter() - stage_started
        source_status = item.get("primary_source_status", "ARTICLE_UNREADABLE")
        primary_source = _primary_source_from_item(item, source_status)
        read_reason = ("Материал прочитан; текст сохранён для проверки." if source_status == "READ"
                       else f"Не удалось прочитать материал ({source_status}).")
        _trace_item(item, "Чтение материала", source_status, read_reason)
        content_hash = digest(body)
        title_hash = digest(item["title"].lower().strip())
        db.execute("""UPDATE items SET title=?,description=?,content=?,content_hash=?,title_hash=?,primary_source_json=?
                      WHERE item_id=?""",
                   (item["title"], item.get("description", ""), body, content_hash, title_hash,
                    _stored_primary(item, primary_source, source_status),
                    item_id))

    publisher_report = (_read_material_report(source, item, body)
                        if not primary_source or source_status != "READ" else None)
    # Linked originals have already been attempted. Search recovery must not hold
    # an otherwise readable account hostage to an unavailable original.
    if not publisher_report and (not primary_source or source_status != "READ") and ai_settings and _likely_local(item) and (selection is None or selection["decision"] == "KEEP"):
        agent_mode = ai_settings.get("research_agent_mode", "active")
        if (agent_mode in {"active", "shadow"}
                and int(ai_settings.get("_research_agent_budget", 0)) > 0):
            ai_settings["_research_agent_budget"] -= 1
            agent_started = time.perf_counter()
            if agent_mode == "active":
                recovered = yield from resolve_steps(_agent_recover_primary(db, item, ai_settings, source, item_id, steps=True))
            else:
                yield from resolve_steps(_shadow_research_agent(item, ai_settings, source, steps=True))
                recovered = yield from resolve_steps(_recover_primary(db, item, ai_settings, steps=True))
            timings["research_agent_seconds"] += time.perf_counter() - agent_started
        else:
            recovered = yield from resolve_steps(_recover_primary(db, item, ai_settings, steps=True))
        if recovered:
            recovered_status = recovered.get("primary_source_status") or "NOT_CHECKED"
            if (recovered_status == "READ" and recovered.get("primary_source_url")
                    and recovered.get("primary_source_content")):
                item.update({key: value for key, value in recovered.items() if key.startswith("primary_source_")})
                source_status = recovered_status
                primary_source = _primary_source_from_item(item, source_status)
                db.execute("UPDATE items SET primary_source_json=? WHERE item_id=?",
                           (_stored_primary(item, primary_source, source_status), item_id))
            elif (recovered.get("material_read") is True
                  and len(str(recovered.get("content") or "").strip()) >= 100):
                publisher_report = {
                    "publisher": recovered.get("publisher_name") or "Издание",
                    "url": recovered["url"], "title": recovered.get("title", ""),
                    "content": recovered["content"], "type": "ATTRIBUTED_REPORT",
                    "material_read": True,
                    "published_at": recovered.get("published_at"),
                    "source_role": source["source_role"] if "source_role" in source.keys() else "aggregator",
                    "forwarded": False,
                }
                # Preserve the actual recovered account for a resumed editor;
                # another retry must not discard already-read evidence.
                body = recovered['content']
                item.update(content=body, material_read=True, material_url=recovered['url'],
                            publisher_name=publisher_report['publisher'])
                db.execute('UPDATE items SET content=?,content_hash=?,primary_source_json=? WHERE item_id=?',
                           (body, digest(body), _stored_primary(item, primary_source, source_status), item_id))
            _trace_item(item, "Резервный поиск источника",
                        "Первоисточник прочитан" if primary_source and source_status == "READ" else "Прочитан материал другого СМИ",
                        "Найденный полный текст передан на обычную проверку с сохранением ссылки и атрибуции.",
                        recovered_url=recovered.get("primary_source_url") or recovered.get("url"),
                        recovered_publisher=recovered.get("primary_source_publisher") or recovered.get("publisher_name"))

    if publisher_report:
        # Retain the original's actual status in items; use only the read report
        # for evidence, memory and the citation (never unread/OCR document text).
        primary_source = None
    if selection and (not primary_source or source_status != "READ") and not publisher_report:
        held = "PRIMARY_RETRY" if selection["decision"] == "KEEP" else "WAITING_CONFIRMATION"
        _trace_item(item, "Проверка источника", held,
                    "Для следующего этапа пока нет прочитанного пригодного материала.",
                    source_status=source_status)
        db.execute("UPDATE items SET disposition=?,processed_at=? WHERE item_id=?", (held, now, item_id))
        db.commit()
        return held

    story_rows = db.execute("SELECT * FROM stories ORDER BY last_updated_at DESC LIMIT 1000").fetchall()
    candidate = f"{item['title']} {item.get('description','')} {body}"
    best, score = None, 0.0
    ranked = []
    for story in story_rows:
        s = similarity(candidate, story["canonical_topic"] + " " + story["headline"] + " " + story["latest_information"])
        ranked.append((s, story))
        if s > score:
            best, score = story, s
    ai_result = None
    ai_settings = ai_settings or {}
    if not ai_settings.get("_disabled_for_cycle") and ai_settings.get("_analysis_budget", 1) > 0 and get_api_key(ai_settings):
        if "_analysis_budget" in ai_settings:
            ai_settings["_analysis_budget"] -= 1
        shortlist = [row for _, row in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:8]]
        if memory_mode in {"shadow", "enforce"}:
            from .knowledge import related_story_ids
            related = related_story_ids(db, candidate + ' ' + (primary_source or publisher_report or {}).get('content',''))
            structured = [db.execute('SELECT * FROM stories WHERE story_id=?',(sid,)).fetchone() for sid in related]
            shortlist = (structured + [row for row in shortlist if row['story_id'] not in related])[:12]
            story_rows = list(story_rows) + [row for row in structured if row['story_id'] not in {s['story_id'] for s in story_rows}]
        ai_candidates = [{"story_id": str(s["story_id"]), "headline": s["headline"],
                          "latest_information": s["latest_information"][:1200], "version": s["version"],
                          "publication_count": s["publication_count"],
                          "last_published_at": s["last_published_at"]} for s in shortlist]
        ai_options = dict(ai_settings)
        ai_options["max_post_length"] = max_length
        try:
            ai_input = {key: value for key, value in item.items()
                        if key not in {"_audit_trace", "_audit_triage"}}
            ai_input["primary_source"] = primary_source
            ai_input["primary_source_status"] = source_status
            ai_input["publisher_report_exception"] = bool(publisher_report)
            ai_input["publisher_report"] = publisher_report
            ai_input["independent_sources"] = item.get("independent_sources", [])
            ai_input["history_context"] = _history_context(db, item)
            from .interests import learning_context
            ai_input["interest_profile"] = learning_context(db)
            if memory_mode in {"shadow", "enforce"}:
                from .knowledge import context
                ai_input["knowledge_context"] = context(db, [s['story_id'] for s in shortlist])
            ai_input["editorial_examples"] = _editorial_examples(
                db, item, item_id, story_id=best["story_id"] if best else None)
            previous_analysis = db.execute("SELECT result_json FROM item_analysis WHERE item_id=?", (item_id,)).fetchone()
            ai_input['editorial_feedback'] = []
            if previous_analysis:
                previous_result = json.loads(previous_analysis["result_json"])
                ai_input["editorial_feedback"] = [
                    issue for key in ("editorial_issues", "memory_issues")
                    for issue in (previous_result.get(key) if isinstance(previous_result.get(key), list) else [])
                    if isinstance(issue, str)
                ]
            stage_started = time.perf_counter()
            history_revision = _editor_history_revision(db, item)
            rules = (Path(__file__).resolve().parent.parent / "EDITORIAL_RULES.md").read_text()
            logic = (Path(__file__).resolve().parent.parent / "AGENT_LOGIC.md").read_text()
            semantic_settings = {key: value for key, value in ai_options.items()
                                 if key in {"model", "max_output_tokens", "max_post_length", "memory_mode"}}
            from .ai import analysis_input
            analysis_key = cache_key("editor", {"input": analysis_input(ai_input, editor_source, ai_candidates, ai_options),
                "settings": semantic_settings,
                "rules": digest(rules), "logic": digest(logic), "filter_version": FILTER_VERSION,
                "prompt_code": digest((Path(__file__).resolve().parent / 'ai.py').read_text()
                                      + (Path(__file__).resolve().parent / 'knowledge.py').read_text()),
                "date": now[:10]})
            db.commit()
            try:
                try:
                    ai_result = yield Work("editor", analyze_with_ai, (ai_input, editor_source, ai_candidates, ai_options),
                                           key=analysis_key, ttl=21600)
                except AIResponseError as exc:
                    if exc.code != "OUTPUT_TOKEN_LIMIT":
                        raise
                    retry_options = dict(ai_options)
                    retry_options["max_output_tokens"] = max(8000, int(ai_options.get("max_output_tokens", 1800)) * 3)
                    ai_result = yield Work("editor", analyze_with_ai, (ai_input, editor_source, ai_candidates, retry_options),
                                           key=analysis_key, ttl=21600)
            finally:
                timings["ai_seconds"] += time.perf_counter() - stage_started
            if (ai_result is not None and ai_result.get('action') != 'NOISE'
                    and _editor_history_revision(db, item) != history_revision):
                item["_retry_without_count"] = True
                item['_history_changed'] = True
                item["_retry_reason"] = "Разбор отложен: история сюжета или публикаций изменилась во время анализа"
                db.execute("UPDATE items SET disposition='AI_RETRY',processed_at=? WHERE item_id=?", (NOW(), item_id))
                db.commit()
                return "AI_RETRY"
        except BudgetDeferred as exc:
            item["_retry_without_count"] = True
            item['_budget_deferred'] = exc.reason != 'concurrency'
            item['_retry_delay_seconds'] = exc.delay_seconds
            item["_retry_reason"] = "ИИ-разбор отложен: исчерпан общий бюджет запросов"
            ai_result = None
        except Exception as exc:
            http_status = re.search(r"HTTP (\d{3})", str(exc))
            if http_status:
                reason = f"HTTP {http_status.group(1)}"
            elif isinstance(exc, AIResponseError):
                reason = exc.code
            else:
                reason = _safe_source_error(exc.__cause__ or exc)
            db.execute("INSERT INTO errors(source_id,timestamp,message) VALUES(?,?,?)",
                       (source["source_id"], NOW(), f"AI editor unavailable ({reason}); item held for automatic retry."))
            db.commit()
            from .diagnostics import error_location
            db.execute("INSERT INTO app_state(key,value) VALUES('ai_last_error',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps({"at":NOW(),"code":reason,"location":error_location(exc)}),))
            db.commit()
            ai_settings["_disabled_for_cycle"] = True
            ai_result = None
            item["_retry_reason"] = f"ИИ-разбор не завершён: {reason}"
            item["_retry_without_count"] = False
            _trace_item(item, "Редакторский ИИ-разбор", "Ошибка", item["_retry_reason"])
        if ai_result is not None:
            _trace_item(item, "Редакторский ИИ-разбор", "Завершён",
                        "ИИ подготовил результат редакторского разбора.",
                        action=ai_result.get("action"), recommendation=ai_result.get("publication_recommendation"))
            db.execute("INSERT INTO app_state(key,value) VALUES('ai_last_success',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (NOW(),))
            # Missing evidence is not evidence of irrelevance. Keep plausible local
            # stories for source recovery; duplicates need no new publication evidence.
            missing_local_source = (selection is None and not publisher_report and (source_status != "READ" or not primary_source)
                and ai_result.get("action") != "DUPLICATE"
                and (_likely_local(item) or (ai_result.get("is_relevant") and ai_result.get("geographic_scope") in {"RUSSIA","CIS","RUSSIA_CIS"})))
            if missing_local_source:
                ai_result["action"] = "NEW_STORY"
                ai_result["is_relevant"] = True
                ai_result["publication_recommendation"] = "WAIT_FOR_AUTOMATION"
            # Reject irrelevant entries even when their source cannot be read.
            if not missing_local_source and ai_result.get("action") != "DUPLICATE" and (ai_result.get("is_relevant") is False or ai_result.get("action") == "NOISE"
                    or (ai_result.get("geographic_scope") in {"OTHER", "GLOBAL"}
                        and ai_result.get("russia_cis_impact") in {"NONE", "INDIRECT"})):
                ai_result["action"] = "NOISE"
                ai_result["publication_recommendation"] = "DO_NOT_PUBLISH"
            if memory_enforced and ai_result.get('action') == 'DUPLICATE':
                ai_result['proposed_action'] = 'DUPLICATE'
                ai_result['action'] = 'UPDATE' if ai_result.get('story_id') else 'NEW_STORY'
            ai_result = _restore_exact_social_headline_evidence(ai_result, item, primary_source)
            ai_result = require_primary_source_review(ai_result, source_status, primary_source,
                                                       publisher_report=publisher_report)
            confidence = ai_result.get("confidence")
            if (ai_result.get("publication_recommendation") == "AUTO_PUBLISH"
                    and (not isinstance(confidence, (int, float)) or confidence < 0.72)):
                ai_result["publication_recommendation"] = "WAIT_FOR_AUTOMATION"
                ai_result["source_review_required"] = True
            independent_audit = [
                {"publisher": candidate.get("publisher"), "title": candidate.get("title"),
                 "url": candidate.get("url"), "published_at": candidate.get("published_at"),
                 "primary_source_url": candidate.get("primary_source_url"),
                 "content_sha256": digest(candidate.get("content", ""))}
                for candidate in item.get("independent_sources", [])
            ]
            db.execute("INSERT INTO item_analysis(item_id,model,created_at,result_json) VALUES(?,?,?,?) "
                       "ON CONFLICT(item_id) DO UPDATE SET model=excluded.model,created_at=excluded.created_at,result_json=excluded.result_json",
                       (item_id, ai_options.get("model", "gpt-6-luna"), now,
                        json.dumps({**ai_result, "_filter_version": FILTER_VERSION,
                                    "_independent_sources": independent_audit}, ensure_ascii=False)))
            date_issue = _development_date_issue(ai_result, primary_source or publisher_report,
                                                 freshness_hours)
            if date_issue:
                severity, reason = date_issue
                ai_result["development_date_check"] = {"status": severity.upper(), "reason": reason}
                if severity == "stale":
                    ai_result["publication_recommendation"] = "DO_NOT_PUBLISH"
                    disposition = "REJECTED"
                else:
                    ai_result["publication_recommendation"] = "WAIT_FOR_AUTOMATION"
                    ai_result["source_review_required"] = True
                    disposition = "WAITING_CONFIRMATION"
                _trace_item(item, "Проверка даты события", disposition, reason,
                            development_date=ai_result.get("development_date"))
                db.execute('UPDATE item_analysis SET result_json=? WHERE item_id=?',
                           (json.dumps({**ai_result, "_filter_version": FILTER_VERSION,
                                        "_independent_sources": independent_audit}, ensure_ascii=False), item_id))
                db.execute("UPDATE items SET disposition=?,processed_at=? WHERE item_id=?",
                           (disposition, now, item_id))
                db.commit()
                return disposition

    if ai_result is None:
        held = "AI_RETRY" if (source_status == "READ" and primary_source) or publisher_report else "PRIMARY_RETRY"
        if not item.get("_retry_reason"):
            if ai_settings.get("_disabled_for_cycle"):
                item["_retry_reason"] = "ИИ-разбор отложен: модель временно недоступна в этом цикле"
                item["_retry_without_count"] = True
            elif ai_settings.get("_analysis_budget", 1) <= 0:
                item["_retry_reason"] = "ИИ-разбор отложен: исчерпан лимит проверок текущего цикла"
                item["_retry_without_count"] = True
                item['_budget_deferred'] = True
            else:
                item["_retry_reason"] = "ИИ-разбор отложен: проверьте настройки доступа модели"
                item["_retry_without_count"] = True
        _trace_item(item, "Редакторский ИИ-разбор", "Ожидает повтора", item["_retry_reason"])
        db.execute("UPDATE items SET disposition=?,processed_at=? WHERE item_id=?", (held, now, item_id))
        db.commit()
        return held

    if (ai_result and ai_result.get("publication_recommendation") == "WAIT_FOR_AUTOMATION"
            and (source_status != "READ" or not primary_source) and not publisher_report):
        _trace_item(item, "Проверка источника", "Ожидает повтора",
                    "Разбор завершён, но нет прочитанного материала, на котором можно обосновать публикацию.",
                    source_status=source_status)
        db.execute("UPDATE items SET disposition='PRIMARY_RETRY',processed_at=? WHERE item_id=?", (now, item_id))
        db.commit()
        return "PRIMARY_RETRY"

    ai_story = None
    if ai_result:
        chosen_id = ai_result.get("story_id")
        ai_story = next((s for s in story_rows if str(s["story_id"]) == chosen_id), None)
        action = ai_result.get("action")
        if action == "DUPLICATE" and not memory_enforced:
            _trace_item(item, "Сверка с опубликованными сюжетами", "Дубликат",
                        "ИИ сопоставил материал с уже опубликованным сюжетом.")
            if ai_story:
                db.execute("UPDATE items SET story_id=?,disposition='DUPLICATE',processed_at=? WHERE item_id=?",
                           (ai_story["story_id"], now, item_id))
            else:
                db.execute("UPDATE items SET disposition='DUPLICATE',processed_at=? WHERE item_id=?", (now, item_id))
            db.commit()
            return "DUPLICATE"
        category = ai_result.get("topic_category")
        concrete_tech = category in {"PRODUCT_FEATURE", "TECHNICAL_DEVELOPMENT"}
        geographic_scope = ai_result.get("geographic_scope")
        implementation_stage = ai_result.get("implementation_stage")
        allowed_tech_stages = {"OPERATIONAL", "RELEASED", "PILOT", "DETAILED_PLAN"}
        tech_not_in_scope = concrete_tech and (
            not ai_result.get("is_concrete") or geographic_scope not in {"RUSSIA", "CIS", "RUSSIA_CIS"}
            or implementation_stage not in allowed_tech_stages
        )
        outside_target_market = geographic_scope not in {"RUSSIA", "CIS", "RUSSIA_CIS"}
        evidence_source = primary_source or publisher_report
        direct_impact = ai_result.get("russia_cis_impact") == "DIRECT" and _impact_evidence_is_grounded(ai_result, evidence_source)
        if (not ai_result.get("is_relevant") or not direct_impact or action == "NOISE" or category == "PRICE_FORECAST"
                or tech_not_in_scope or outside_target_market):
            filter_reasons = []
            if not ai_result.get("is_relevant") or action == "NOISE":
                filter_reasons.append("ИИ не подтвердил тематическую значимость")
            if not direct_impact:
                filter_reasons.append("не подтверждено прямое влияние на Россию/СНГ")
            if category == "PRICE_FORECAST":
                filter_reasons.append("ценовой прогноз исключён редакционными правилами")
            if tech_not_in_scope:
                filter_reasons.append("техническая новость не прошла требования к конкретности, географии или стадии")
            if outside_target_market:
                filter_reasons.append("событие вне целевой географии")
            _trace_item(item, "Тематический и географический допуск", "Отсеян",
                        "; ".join(filter_reasons) or "Не выполнены условия допуска.",
                        geographic_scope=geographic_scope, category=category,
                        impact=ai_result.get("russia_cis_impact"), evidence=ai_result.get("impact_evidence"))
            db.execute("UPDATE items SET disposition='NOISE',processed_at=? WHERE item_id=?", (now, item_id))
            db.commit()
            return "NOISE"
        if memory_enforced:
            from .knowledge import exact_story, MemoryInvalid
            try:
                resolved_story_id = exact_story(db, ai_result.get('memory'))
            except MemoryInvalid as exc:
                ai_result['memory_issues'] = [str(exc)]
                _trace_item(item, "Проверка памяти сюжетов", "Нужна повторная проверка", str(exc))
                db.execute('UPDATE item_analysis SET result_json=? WHERE item_id=?',
                           (json.dumps({**ai_result, '_filter_version':FILTER_VERSION},ensure_ascii=False),item_id))
                db.execute("UPDATE items SET disposition='WAITING_CONFIRMATION',processed_at=? WHERE item_id=?",(now,item_id))
                db.commit()
                return 'WAITING_CONFIRMATION'
            if resolved_story_id:
                ai_story = db.execute('SELECT * FROM stories WHERE story_id=?',(resolved_story_id,)).fetchone()
                action = ai_result['action'] = 'UPDATE'
                ai_result['story_id'] = str(resolved_story_id)
        if (action == "UPDATE" or (memory_enforced and action == "DUPLICATE")) and ai_story:
            best = ai_story
        elif action == "NEW_STORY":
            best = None
        elif action == "UPDATE":
            # Reject a model reference to an unavailable story; create a distinct story instead.
            best = None

    if best and (ai_result is not None or score >= threshold):
        has_previous_publication = int(best["publication_count"] or 0) > 0
        story_id = best["story_id"]
        # The AI's explicit UPDATE decision may share most of the old wording while adding one crucial fact.
        # Use similarity as a repeat filter only when there is no explicit material-update decision.
        if (not memory_enforced and (ai_result is None or ai_result.get("action") != "UPDATE")
                and similarity(candidate, best["latest_information"]) > 0.8):
            _trace_item(item, "Сверка с известным сюжетом", "Дубликат",
                        "Существенное новое сведение не найдено; содержание совпадает с уже известным сюжетом.")
            db.execute("UPDATE items SET story_id=?,disposition='DUPLICATE',processed_at=? WHERE item_id=?",
                       (story_id, now, item_id))
            db.commit()
            return "DUPLICATE"
        db.execute("UPDATE items SET story_id=?,disposition='UPDATE_CANDIDATE',processed_at=? WHERE item_id=?", (story_id, now, item_id))
        db.execute("UPDATE stories SET last_updated_at=?, last_source_published_at=COALESCE(?,last_source_published_at), latest_information=?, source_count=(SELECT COUNT(DISTINCT source_id) FROM items WHERE story_id=?) WHERE story_id=?",
                   (now, item.get("published_at"), body[:2000], story_id, story_id))
        db.execute("INSERT OR IGNORE INTO story_timeline(story_id,item_id,timestamp,source_name,new_information,confidence) VALUES(?,?,?,?,?,?)",
                   (story_id, item_id, item.get("published_at") or now, publisher_name, body[:2000], min(0.99, score + 0.45)))
        status = "UPDATE_CANDIDATE"
        headline = (ai_result.get("headline_ru") or best["headline"]) if ai_result else best["headline"]
    else:
        cur = db.execute("INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at,last_source_published_at,latest_information,keywords) VALUES(?,?,?,?,?,?,?)",
                         (item["title"], item["title"], now, now, item.get("published_at"), body[:2000], json.dumps(sorted(terms(candidate)), ensure_ascii=False)))
        story_id = cur.lastrowid
        db.execute("UPDATE items SET story_id=?,disposition='NEW_STORY',processed_at=? WHERE item_id=?", (story_id, now, item_id))
        db.execute("INSERT INTO story_timeline(story_id,item_id,timestamp,source_name,new_information,confidence) VALUES(?,?,?,?,?,?)",
                   (story_id, item_id, item.get("published_at") or now, publisher_name, body[:2000], 0.6))
        status = "NEW_STORY"
        headline = ai_result.get("headline_ru") or item["title"] if ai_result else item["title"]
    _trace_item(item, "Сюжет и черновик", status,
                "Новое событие передано на автоматический допуск." if status == "NEW_STORY"
                else "Новые сведения добавлены к известному сюжету и переданы на автоматический допуск.",
                what_is_new=(ai_result or {}).get("what_is_new"))
    memory_diff = None
    if memory_mode in {"shadow", "enforce"}:
        from .knowledge import ingest, MemoryInvalid
        try:
            memory_diff = ingest(db, item_id, story_id, ai_result, primary_source or publisher_report,
                                 publisher_report=bool(publisher_report))
        except MemoryInvalid as exc:
            ai_result['memory_issues'] = [str(exc)]
            _trace_item(item, "Проверка памяти сюжетов", "Нужна повторная проверка", str(exc))
            if memory_enforced:
                ai_result['publication_recommendation'] = 'WAIT_FOR_AUTOMATION'
        else:
            ai_result['story_diff'] = memory_diff
            if memory_enforced and memory_diff['conflict_state'] != 'NONE':
                ai_result['publication_recommendation'] = 'WAIT_FOR_AUTOMATION'
                queued_corrections = []
                correction_owner = str(ai_settings.get("_correction_owner_chat_id") or "")
                changed_fact_ids = list(dict.fromkeys(
                    memory_diff.get("contradicted_facts", []) + memory_diff.get("changed_facts", [])))
                if correction_owner and primary_source and source_status == "READ" and changed_fact_ids:
                    old_claims = db.execute(
                        "SELECT DISTINCT p.post_id FROM fact_relations r "
                        "JOIN post_facts pf ON pf.fact_id=r.old_fact_id "
                        "JOIN posts p ON p.post_id=pf.post_id "
                        "WHERE r.new_fact_id IN (" + ",".join("?" for _ in changed_fact_ids) + ") "
                        "AND r.relation IN ('CONTRADICTS','SUPERSEDES','RETRACTS') "
                        "AND p.story_id=? AND p.status='PUBLISHED' "
                        "ORDER BY p.published_at DESC LIMIT 3",
                        (*changed_fact_ids, story_id),
                    ).fetchall()
                    if old_claims:
                        from .review import enqueue_agent_fact_correction
                        for claim in old_claims:
                            correction_id = enqueue_agent_fact_correction(
                                db, item_id=item_id, story_id=story_id,
                                post_id=claim["post_id"], owner_chat_id=correction_owner)
                            if correction_id:
                                queued_corrections.append(correction_id)
                if queued_corrections:
                    disposition = "AGENT_CORRECTION_QUEUED"
                    _trace_item(item, "Самостоятельная проверка опубликованного поста", disposition,
                                "Новый прочитанный источник противоречит факту в опубликованном посте; правка поставлена в обычную редакторскую очередь.",
                                correction_ids=queued_corrections)
                    db.execute("UPDATE items SET disposition=?,processed_at=? WHERE item_id=?",
                               (disposition, now, item_id))
                    db.execute('UPDATE item_analysis SET result_json=? WHERE item_id=?',
                               (json.dumps({**ai_result, '_filter_version':FILTER_VERSION},ensure_ascii=False),item_id))
                    db.commit()
                    return disposition
                _trace_item(item, "Проверка памяти сюжетов", "Нужна повторная проверка",
                            "Найден конфликт между новым материалом и сохранёнными сведениями.",
                            conflict_state=memory_diff.get('conflict_state'))
            elif (memory_enforced and memory_diff['significant_update']
                  and memory_diff.get('material_unpublished_facts')
                  and (primary_source or publisher_report)):
                correction_owner = str(ai_settings.get("_correction_owner_chat_id") or "")
                prior_post = db.execute(
                    "SELECT post_id FROM posts WHERE story_id=? AND status='PUBLISHED' "
                    "AND julianday(published_at)>=julianday('now',?) "
                    "ORDER BY published_at DESC,post_id DESC LIMIT 1",
                    (story_id, f"-{int(freshness_hours)} hours"),
                ).fetchone()
                if correction_owner and prior_post:
                    from .review import enqueue_agent_fact_correction
                    correction_id = enqueue_agent_fact_correction(
                        db, item_id=item_id, story_id=story_id, post_id=prior_post["post_id"],
                        owner_chat_id=correction_owner, supplement=True)
                    if correction_id:
                        _trace_item(item, "Самостоятельное дополнение опубликованного поста",
                                    "AGENT_CORRECTION_QUEUED",
                                    "Найдено новое существенное подтверждённое сведение в свежем опубликованном сюжете; редактор проверит, стоит ли дополнить тот же пост.",
                                    correction_id=correction_id)
                        db.execute("UPDATE items SET disposition='AGENT_CORRECTION_QUEUED',processed_at=? WHERE item_id=?",
                                   (now, item_id))
                        db.execute('UPDATE item_analysis SET result_json=? WHERE item_id=?',
                                   (json.dumps({**ai_result, '_filter_version': FILTER_VERSION}, ensure_ascii=False), item_id))
                        db.commit()
                        return "AGENT_CORRECTION_QUEUED"
            elif memory_enforced and not memory_diff['significant_update']:
                disposition = 'STORE_ONLY' if memory_diff['unpublished_facts'] else 'DUPLICATE'
                _trace_item(item, "Проверка новизны сюжета", disposition,
                            "Нового существенного повода для отдельной публикации не найдено.")
                if memory_diff['unpublished_facts'] and ai_result.get('publication_recommendation') == 'AUTO_PUBLISH':
                    ai_result['memory_issues'] = ['PUBLICATION_RECOMMENDATION_WITHOUT_MATERIAL_FACT: оцените существенность для читателя, а не новизну для памяти; REPEAT неопубликованного факта допускает material=true']
                    disposition = 'WAITING_CONFIRMATION'
                    _trace_item(item, "Проверка допуска", disposition,
                                "Рекомендация к публикации не подтверждена существенным новым фактом.")
                db.execute("UPDATE items SET disposition=?,processed_at=? WHERE item_id=?", (disposition,now,item_id))
                db.execute('UPDATE item_analysis SET result_json=? WHERE item_id=?',
                           (json.dumps({**ai_result, '_filter_version':FILTER_VERSION},ensure_ascii=False),item_id))
                db.commit()
                return disposition
        db.execute('UPDATE item_analysis SET result_json=? WHERE item_id=?',
                   (json.dumps({**ai_result, '_filter_version':FILTER_VERSION},ensure_ascii=False),item_id))
    if ai_result:
        recommendation = ai_result.get("publication_recommendation")
        if recommendation != "AUTO_PUBLISH" or ai_result.get("source_review_required") is True:
            if recommendation == "WAIT_FOR_AUTOMATION":
                if (source_status != "READ" or not primary_source) and not publisher_report:
                    disposition = "PRIMARY_RETRY"
                else:
                    retries = db.execute("SELECT CAST(value AS INTEGER) FROM app_state WHERE key=?", ("editor_retry:"+str(item_id),)).fetchone()
                    disposition = "REJECTED" if retries and int(retries[0]) >= 3 else "WAITING_CONFIRMATION"
            else:
                disposition = "REJECTED"
            reasons = []
            if ai_result.get("memory_issues"):
                reasons.extend(str(value) for value in ai_result["memory_issues"][:3])
            if ai_result.get("source_review_required"):
                reasons.append("нужна дополнительная проверка источника или доказательства")
            if ai_result.get("independent_check_required"):
                reasons.append("нужна сверка выявленного расхождения")
            if not reasons:
                reasons.append(f"редакторская рекомендация: {recommendation or 'не задана'}")
            _trace_item(item, "Автоматический редакторский допуск", disposition,
                        "; ".join(reasons))
            db.execute("UPDATE items SET disposition=?,processed_at=? WHERE item_id=?", (disposition, now, item_id))
            db.commit()
            return disposition
        quality_body = ai_result.get("what_is_new") if status == "UPDATE_CANDIDATE" and has_previous_publication else ai_result.get("summary_ru", "")
        citation_url = primary_source["url"] if primary_source else (publisher_report or {}).get("url", item["url"])
        citation_name = (primary_source.get("publisher") or "Первоисточник") if primary_source else (publisher_report or {}).get("publisher", publisher_name)
        source_is_report = (bool(primary_source and str(primary_source.get("type") or "").startswith(("ORIGINAL_MEDIA_", "ORIGINAL_SOCIAL_")))
                            if primary_source else bool(publisher_report))
        issues = editorial_issues(headline, quality_body or "", ai_result,
                                 source_name=citation_name, source_is_report=source_is_report)
        if issues:
            ai_result["editorial_issues"] = issues
            _trace_item(item, "Автоматическая проверка текста", "Нужна повторная проверка",
                        "; ".join(issues), issues=issues)
            db.execute("UPDATE item_analysis SET result_json=? WHERE item_id=?",
                       (json.dumps({**ai_result, "_filter_version": FILTER_VERSION}, ensure_ascii=False), item_id))
            retries = db.execute("SELECT CAST(value AS INTEGER) FROM app_state WHERE key=?", ("editor_retry:"+str(item_id),)).fetchone()
            disposition = "REJECTED" if retries and int(retries[0]) >= 3 else "WAITING_CONFIRMATION"
            db.execute("UPDATE items SET disposition=?,processed_at=? WHERE item_id=?", (disposition,now,item_id))
            db.commit()
            return disposition
        description = ai_result.get("summary_ru", "")
        if status == "UPDATE_CANDIDATE" and has_previous_publication:
            description = ai_result.get("what_is_new") or description
        post = make_post(headline, description, citation_name, citation_url, max_length)
        primary_source_record = None
        if primary_source:
            primary_source_record = {key: value for key, value in primary_source.items() if key != "content"}
            primary_source_record["content_sha256"] = digest(primary_source["content"])
        facts_json = json.dumps({"mode": "AI", "_filter_version": FILTER_VERSION,
                                 "memory_mode": memory_mode, "story_diff": memory_diff,
                                 "primary_source": primary_source_record,
                                 "primary_source_status": source_status,
                                 "citation_is_report": source_is_report,
                                 "publisher_report_exception": bool(publisher_report),
                                 "publisher_report": (dict(publisher_report) | {"content_sha256": digest(publisher_report["content"]), "evidence": (ai_result.get("original_reporting_check") or {}).get("evidence", "")}) if publisher_report else None,
                                 "source_review_required": bool(ai_result.get("source_review_required")),
                                 "independent_check_required": bool(ai_result.get("independent_check_required")),
                                 "confidence": ai_result.get("confidence"),
                                 "importance": ai_result.get("importance"),
                                 "event_status": ai_result.get("event_status"), "freshness": ai_result.get("freshness"),
                                 "topic_category": ai_result.get("topic_category"), "is_concrete": ai_result.get("is_concrete"),
                                 "implementation_stage": ai_result.get("implementation_stage"), "geographic_scope": ai_result.get("geographic_scope"),
                                 "russia_cis_impact": ai_result.get("russia_cis_impact"), "impact_evidence": ai_result.get("impact_evidence", ""),
                                 "original_reporting_check": ai_result.get("original_reporting_check"),
                                 "editorial_check": ai_result.get("editorial_check"),
                                 "facts": ai_result.get("facts", []), "what_is_new": ai_result.get("what_is_new", ""),
                                 "independent_check": ai_result.get("independent_check", "NOT_ASSESSED"),
                                 "independent_check_note": ai_result.get("independent_check_note", ""),
                                 "independent_sources": [{key: source.get(key) for key in ("publisher", "title", "url", "published_at", "primary_source_url")}
                                                         for source in item.get("independent_sources", [])]}, ensure_ascii=False)
    else:
        citation_url = primary_source["url"] if primary_source else item["url"]
        citation_name = (primary_source.get("publisher") or "Первоисточник") if primary_source else publisher_name
        post = make_post(headline, body, citation_name, citation_url, max_length)
        primary_source_record = None
        if primary_source:
            primary_source_record = {key: value for key, value in primary_source.items() if key != "content"}
            primary_source_record["content_sha256"] = digest(primary_source["content"])
        facts_json = json.dumps({"mode": "RULE_BASED", "primary_source": primary_source_record,
                                 "primary_source_status": source_status,
                                 "source_review_required": not bool(primary_source and source_status == "READ")}, ensure_ascii=False)
    post_hash = digest(post)
    db.execute('SAVEPOINT memory_post')
    cur = db.execute("INSERT OR IGNORE INTO posts(story_id,origin_item_id,text,status,created_at,version,source_ids,post_hash,fact_check_result) VALUES(?,?,?, 'PENDING', ?,?,?,?,?)",
               (story_id, item_id, post, now, (best["version"] + 1 if best else 1), json.dumps([source["source_id"]]), post_hash, facts_json))
    if memory_diff and memory_enforced:
        from .knowledge import bind_post, MemoryInvalid
        try:
            bind_post(db, cur.lastrowid, item_id, memory_diff, post)
        except MemoryInvalid as exc:
            db.execute('ROLLBACK TO memory_post')
            db.execute('RELEASE memory_post')
            ai_result['memory_issues'] = [str(exc)]
            db.execute('UPDATE item_analysis SET result_json=? WHERE item_id=?',
                       (json.dumps({**ai_result, '_filter_version':FILTER_VERSION},ensure_ascii=False),item_id))
            db.execute("UPDATE items SET disposition='WAITING_CONFIRMATION',processed_at=? WHERE item_id=?",(now,item_id))
            db.commit()
            return 'WAITING_CONFIRMATION'
    db.execute('RELEASE memory_post')
    db.commit()
    discovered_row = db.execute("SELECT discovered_at FROM items WHERE item_id=?", (item_id,)).fetchone()
    ready_post = db.execute("SELECT post_id FROM posts WHERE origin_item_id=? AND post_hash=?", (item_id, post_hash)).fetchone()
    used_source_published_at = (item.get("published_at")
                                or (primary_source or {}).get("published_at")
                                or (publisher_report or {}).get("published_at")
                                or item.get("updated_at"))
    _log_timing("post_ready", post_id=ready_post["post_id"] if ready_post else None, item_id=item_id,
                source_published_at=used_source_published_at,
                item_discovered_at=discovered_row["discovered_at"] if discovered_row else None)
    return status


def _saved_material(row, source):
    try:
        primary = json.loads(row["primary_source_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        primary = {}
    item = {key: row[key] for key in ("url", "title", "description", "content", "author", "published_at", "updated_at")}
    item["material_read"] = primary.get("_material_read") is True
    item["material_url"] = primary.get("_material_url", row["url"])
    if primary.get("_material_publisher"):
        item["publisher_name"] = primary["_material_publisher"]
    if source["type"] == "manual" and item["material_read"]:
        item["screening_excerpt"] = str(item.get("content") or "")[:2200]
    if "_discovery_links" in primary:
        item["discovery_links"] = primary["_discovery_links"]
        item["telegram_forwarded"] = primary.get("_telegram_forwarded", False)
    retry_primary = primary.get("status") != "READ" and not item["material_read"]
    item.update({
        "primary_source_url": None if retry_primary else primary.get("url"),
        "primary_source_title": None if retry_primary else primary.get("title"),
        "primary_source_content": "" if retry_primary else primary.get("content", ""),
        "primary_source_type": None if retry_primary else primary.get("type"),
        "primary_source_publisher": None if retry_primary else primary.get("publisher"),
        "primary_source_status": None if retry_primary else primary.get("status", "NOT_CHECKED"),
    })
    return item


def _retry_ai_held_items(db, source_by_id: dict[int, object], config: dict, limit: int = 20, coordinator=None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not source_by_id or limit <= 0:
        return counts
    # Retries share model budgets with fresh stories. Keep a small retry slice so
    # held items cannot consume the entire cycle before current feeds arrive.
    limit = min(limit, max(0, int(config.get("newsroom", {}).get("retry_items_per_cycle", 2))))
    ai_settings = config.get("ai", {})
    if ai_settings.get("triage_enabled"):
        limit = min(limit, max(0, int(ai_settings.get("_triage_budget",
                    config.get("newsroom", {}).get("triage_per_cycle", 12)))))
    if "_analysis_budget" in ai_settings:
        limit = min(limit, max(0, int(ai_settings.get("_analysis_budget", 0))))
    if limit <= 0:
        return counts
    placeholders = ",".join("?" for _ in source_by_id)
    # Apply due times before LIMIT, so sleeping retries cannot starve fresh work.
    due_filter = (" AND COALESCE(julianday((SELECT json_extract(value,'$.next_at') FROM app_state WHERE key='selection_retry:'||items.item_id)),0)<=julianday('now') "
                  if config.get("ai", {}).get("triage_enabled") else "")
    # Exclude inactive sources before LIMIT and rotate by last attempt.
    rows = db.execute(
        f"SELECT * FROM items WHERE (disposition IN ('AI_RETRY','PRIMARY_RETRY') OR "
        "(disposition='WAITING_CONFIRMATION' "
        "AND julianday(processed_at)<julianday('now','-5 minutes') "
        "AND COALESCE((SELECT CAST(value AS INTEGER) FROM app_state WHERE key='editor_retry:'||items.item_id),0)<3)) "
        f"AND source_id IN ({placeholders}) {due_filter} "
        "AND NOT EXISTS(SELECT 1 FROM processing_jobs j WHERE j.item_id=items.item_id AND j.status IN ('PENDING','WAITING','RUNNING')) "
        "ORDER BY COALESCE(processed_at, discovered_at), discovered_at, item_id LIMIT ?",
        (*source_by_id, limit),
    ).fetchall()
    for row in rows:
        source = source_by_id.get(row["source_id"])
        if not source:
            continue
        item = _saved_material(row, source)
        if row["disposition"] == "WAITING_CONFIRMATION" and coordinator is None:
            db.execute("INSERT INTO app_state(key,value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1", ("editor_retry:"+str(row["item_id"]),))
        if coordinator is not None:
            from .workflow import enqueue
            enqueue(db, row['item_id'], item, source, {
                'threshold': config['newsroom'].get('similarity_threshold', .35),
                'max_length': config['newsroom'].get('max_post_length', 3500),
                'freshness_hours': config['newsroom'].get('freshness_window_hours', 24),
                'initial_backfill_minutes': None, 'relevance_terms': config['newsroom'].get('relevance_terms', []),
            }, category='retry')
            continue
        try:
            outcome = process_item(db, source, item, config["newsroom"].get("similarity_threshold", 0.35),
                                   config["newsroom"].get("max_post_length", 3500),
                                   config["newsroom"].get("freshness_window_hours", 24), None,
                                   config["newsroom"].get("relevance_terms", []), config.get("ai", {}),
                                   existing_item_id=row["item_id"],
                                   post_ready_callback=config.get("_publish_ready_callback"))
            counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as exc:
            db.rollback()
            # A failed attempt must also move to the back of the retry queue.
            db.execute("UPDATE items SET processed_at=? WHERE item_id=?", (NOW(), row["item_id"]))
            counts["ERROR"] = counts.get("ERROR", 0) + 1
            attempts = schedule_retry(db, row["item_id"], "ERROR")
            if attempts >= MAX_AUTOMATIC_RETRIES:
                db.execute("UPDATE items SET disposition='REJECTED',processed_at=? WHERE item_id=?",
                           (NOW(), row["item_id"]))
            db.execute("INSERT INTO errors(source_id,timestamp,message) VALUES(?,?,?)",
                       (source["source_id"], NOW(), _safe_source_error(exc)))
            db.commit()
    return counts


def _close_exhausted_retries(db) -> int:
    """Move legacy or newly exhausted held items out of visible retry stages."""
    db.execute("""UPDATE items SET disposition='REJECTED',processed_at=?
        WHERE disposition IN ('AI_RETRY','PRIMARY_RETRY','WAITING_CONFIRMATION')
          AND (
            COALESCE((SELECT CASE WHEN json_valid(value)
                THEN CAST(json_extract(value,'$.attempts') AS INTEGER) ELSE 0 END
                FROM app_state WHERE key='selection_retry:'||items.item_id),0)>=?
            OR (disposition='WAITING_CONFIRMATION' AND
                COALESCE((SELECT CAST(value AS INTEGER) FROM app_state
                    WHERE key='editor_retry:'||items.item_id),0)>=?)
          )""", (NOW(), MAX_AUTOMATIC_RETRIES, MAX_AUTOMATIC_RETRIES))
    count = db.execute("SELECT changes()").fetchone()[0]
    db.commit()
    return count


def _reconcile_legacy_retry_loops(db) -> int:
    """Close unchanged items already analyzed past the automatic retry limit.

    Older builds confused an enriched article body with a changed feed entry,
    which reset retry state on every poll. Use the append-only snapshots once
    to enforce the existing retry limit for those same unchanged versions.
    """
    marker = "retry_enrichment_reconciled_v2"
    if db.execute("SELECT 1 FROM app_state WHERE key=?", (marker,)).fetchone():
        return 0

    rows = db.execute("""SELECT item_id,title,description,author,published_at,updated_at,content,disposition
        FROM items WHERE disposition IN ('AI_RETRY','PRIMARY_RETRY','WAITING_CONFIRMATION')""").fetchall()
    closed = 0
    for item in rows:
        revisions = db.execute("""SELECT source_snapshot_json,decision_snapshot_json
            FROM item_revisions WHERE item_id=? ORDER BY revision_id""", (item["item_id"],)).fetchall()
        completed = 0
        for revision in revisions:
            try:
                source = json.loads(revision["source_snapshot_json"])
                decision = json.loads(revision["decision_snapshot_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            same_version = all(source.get(key) == item[key]
                               for key in ("title", "description", "author", "published_at", "updated_at"))
            if same_version and decision.get("analysis"):
                completed += 1
        current_analysis = db.execute("SELECT 1 FROM item_analysis WHERE item_id=?", (item["item_id"],)).fetchone()
        if current_analysis:
            completed += 1
        if completed <= 0:
            continue

        prior = db.execute("SELECT value FROM app_state WHERE key=?",
                           (f"selection_retry:{item['item_id']}",)).fetchone()
        try:
            retry = json.loads(prior[0]) if prior else {}
        except (TypeError, json.JSONDecodeError):
            retry = {}
        prior_attempts = int(retry.get("attempts") or 0)
        attempts = max(prior_attempts, min(MAX_AUTOMATIC_RETRIES, completed - 1))
        attempts_updated = attempts > prior_attempts
        if attempts_updated:
            retry["attempts"] = attempts
            retry["reason"] = f"По журналу версий учтено {completed} завершённых разборов этой версии."
        if attempts < MAX_AUTOMATIC_RETRIES:
            if attempts_updated:
                db.execute("INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (f"selection_retry:{item['item_id']}", json.dumps(retry, ensure_ascii=False)))
            continue

        reason = (f"Эта версия уже прошла {MAX_AUTOMATIC_RETRIES} повторных проверок; "
                  "дальнейшие автоматические попытки остановлены.")
        retry.update({"attempts": MAX_AUTOMATIC_RETRIES, "outcome": item["disposition"], "reason": reason})
        retry["next_at"] = NOW()
        db.execute("INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (f"selection_retry:{item['item_id']}", json.dumps(retry, ensure_ascii=False)))
        db.execute("UPDATE items SET disposition='REJECTED',processed_at=? WHERE item_id=?",
                   (NOW(), item["item_id"]))
        from .decisions import record
        record(db, item["item_id"], "REJECTED", extra={
            "reason": reason,
            "reason_code": "RETRY_HISTORY_RECONCILIATION",
            "completed_analyses_for_unchanged_version": completed,
        })
        closed += 1

    db.execute("INSERT INTO app_state(key,value) VALUES(?,?)",
               (marker, json.dumps({"at": NOW(), "closed": closed}, ensure_ascii=False)))
    db.commit()
    return closed


def _requeue_social_quote_repairs(db, ai_settings, freshness_hours: int) -> int:
    """Re-evaluate fresh rejected exact social posts once under the current filter."""
    if not get_api_key(ai_settings or {}):
        return 0
    rows = db.execute("SELECT i.*,a.result_json FROM items i JOIN item_analysis a USING(item_id) "
                      "WHERE i.disposition='REJECTED' AND "
                      "julianday(COALESCE(i.updated_at,i.published_at))>=julianday('now',?)",
                      ("-" + str(int(freshness_hours)) + " hours",)).fetchall()
    queued = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            analysis = json.loads(row["result_json"] or "{}")
            primary = json.loads(row["primary_source_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if int(analysis.get("_filter_version", 0) or 0) >= FILTER_VERSION:
            continue
        if (not str(primary.get("type", "")).startswith("ORIGINAL_SOCIAL_")
                or primary.get("status") != "READ"):
            continue
        audit = analysis.get("original_reporting_check") or {}
        if (audit.get("central_claim_supported") is not False
                or audit.get("attribution_preserved") is not True
                or len(str(audit.get("evidence") or "").strip()) >= 24
                or not analysis.get("is_relevant")
                or analysis.get("geographic_scope") not in {"RUSSIA", "CIS", "RUSSIA_CIS"}
                or analysis.get("russia_cis_impact") != "DIRECT"
                or analysis.get("independent_check") == "CONFLICT"
                or not _impact_evidence_is_grounded(analysis, primary)):
            continue
        title = " ".join(str(row["title"] or "").casefold().split())
        source_text = " ".join(str(primary.get("content") or "").casefold().split())
        if len(title) < 24 or title not in source_text:
            continue
        event_time = row["updated_at"] or row["published_at"]
        if not event_time:
            continue
        try:
            event_stamp = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
            if event_stamp.tzinfo is None:
                event_stamp = event_stamp.replace(tzinfo=timezone.utc)
            age_hours = (now - event_stamp).total_seconds() / 3600
        except ValueError:
            continue
        if age_hours > freshness_hours:
            continue
        marker = f"filter_reprocess:{FILTER_VERSION}:{row['item_id']}"
        if db.execute("SELECT 1 FROM app_state WHERE key=?", (marker,)).fetchone():
            continue
        db.execute("INSERT INTO app_state(key,value) VALUES(?,?)", (marker, NOW()))
        db.executemany("DELETE FROM app_state WHERE key=?", [(f"triage:{row['item_id']}",),
                         (f"selection_retry:{row['item_id']}",), (f"editor_retry:{row['item_id']}",)])
        db.execute("UPDATE items SET disposition='AI_RETRY',processed_at=NULL WHERE item_id=?", (row["item_id"],))
        queued += 1
    db.commit()
    return queued


def run_cycle(config: dict) -> dict[str, int]:
    from contextlib import ExitStack
    with ExitStack() as cleanup:
        db = connect(config["newsroom"]["database"])
        cleanup.callback(db.close)
        return _run_cycle(config, db, cleanup)


def _run_cycle(config, db, cleanup):
    config = {**config, "ai": {k: v for k, v in config.get("ai", {}).items() if k not in {"_disabled_for_cycle", "_triage_disabled"}}}
    owner_ids = config.get("telegram", {}).get("interest_owner_user_ids") or []
    if owner_ids:
        config["ai"]["_correction_owner_chat_id"] = str(owner_ids[0])
    config["ai"]["_analysis_budget"] = int(config["newsroom"].get("analysis_per_cycle", 25))
    config["ai"]["_research_agent_budget"] = max(
        0, min(3, int(config["ai"].get("research_agent_per_cycle", 1))))
    config["ai"]["_retry_cycle_delay_seconds"] = min(
        180, max(30, int(config["newsroom"].get("poll_interval_seconds", 180))))
    config["ai"]["_recovery_search_budget"] = 2
    config["ai"]["_triage_budget"] = int(config["newsroom"].get("triage_per_cycle", 12))
    watch_reserve = min(3, max(0, config['ai']['_analysis_budget']), max(0, config['ai']['_triage_budget'])) if config['newsroom'].get('story_watch_enabled') else 0
    config['ai']['_analysis_budget'] -= watch_reserve
    config['ai']['_triage_budget'] -= watch_reserve
    retry_reserve = min(
        max(0, int(config["newsroom"].get("retry_items_per_cycle", 2))),
        max(0, config["ai"]["_analysis_budget"]),
        max(0, config["ai"]["_triage_budget"])
        if config["ai"].get("triage_enabled") else max(0, config["ai"]["_analysis_budget"]),
    )
    config["ai"]["_analysis_budget"] -= retry_reserve
    if config["ai"].get("triage_enabled"):
        config["ai"]["_triage_budget"] -= retry_reserve
    run_started = time.perf_counter()
    stage_times = {"retry_seconds": 0.0, "fetch_wait_seconds": 0.0,
                   "matching_seconds": 0.0, "processing_seconds": 0.0}
    from .runtime import attach
    attach(config)
    counts: dict[str, int] = {}
    reconciled = _reconcile_legacy_retry_loops(db)
    if reconciled:
        counts["RETRY_HISTORY_RECONCILED"] = reconciled
    sources = []
    active_configs = [
        (source_cfg, source_cfg.get("type", "rss"))
        for source_cfg in config.get("sources", [])
        if source_cfg.get("type", "rss") in {"rss", "web", "telegram", "google_news", "web_search", "x"}
        and source_cfg.get("active", True)
    ]
    active_configs.sort(key=lambda entry: (
        0 if entry[1] == "google_news" else
        1 if entry[1] == "web_search" else 2,
        -int(entry[0].get("priority", 1))))
    active_urls = [cfg["url"] for cfg, _ in active_configs]
    if active_urls:
        placeholders = ",".join("?" for _ in active_urls)
        db.execute(f"UPDATE sources SET active=0 WHERE url NOT IN ({placeholders})", active_urls)
    else:
        db.execute("UPDATE sources SET active=0")
    for source_cfg, source_type in active_configs:
        db.execute("""INSERT INTO sources(name,type,url,active,priority,reputation) VALUES(?,?,?,?,?,?)
                      ON CONFLICT(url) DO UPDATE SET name=excluded.name,type=excluded.type,active=1,
                      priority=excluded.priority,reputation=excluded.reputation""",
                   (source_cfg["name"], source_type, source_cfg["url"], 1, source_cfg.get("priority", 1), source_cfg.get("reputation", "unknown")))
        role = source_cfg.get("source_role", "aggregator")
        if role not in {"participant", "publisher", "expert", "aggregator", "discovery"}:
            raise ValueError("INVALID_SOURCE_ROLE")
        db.execute("UPDATE sources SET source_role=? WHERE url=?", (role, source_cfg["url"]))
        source = db.execute("SELECT * FROM sources WHERE url=?", (source_cfg["url"],)).fetchone()
        first_check = source["last_seen_published_at"] is None and source["last_success_at"] is None
        sources.append((source_cfg, source, first_check, source_type))
    db.commit()
    source_by_id = {source["source_id"]: source for _, source, _, _ in sources}
    manual_retry_sources = db.execute(
        "SELECT DISTINCT s.* FROM sources s JOIN items i USING(source_id) "
        "WHERE s.type='manual' AND i.disposition IN ('AI_RETRY','PRIMARY_RETRY','WAITING_CONFIRMATION')"
    ).fetchall()
    source_by_id.update({source["source_id"]: source for source in manual_retry_sources})
    if config.get('newsroom',{}).get('story_watch_enabled'):
        source_by_id.update({r['source_id']:r for r in db.execute("SELECT * FROM sources WHERE url LIKE 'story-watch://%'")})
    # Breaking-news discovery shares one persistent search slot across source
    # feeds, source recovery, and story watch. Keep the effective cooldown at
    # or below the three-minute collection cadence so stale config cannot
    # silently defeat the publication-latency target.
    web_search_interval = min(3, max(1, int(config.get("web_search", {}).get("min_interval_minutes", 3))))
    web_search_infos = [info for info in sources if info[3] == "web_search"]
    web_search_queries = [
        {"query": str(info[0].get("query", "")).strip(),
         "interest_exclusions": info[0].get("interest_exclusions", [])}
        for info in web_search_infos if str(info[0].get("query", "")).strip()]
    quota_probe = _WebSearchQuota(db, web_search_interval)
    # The persisted per-category reservation timestamp is the scheduler clock.
    # Source health timestamps are written after result processing and can lag
    # by most of a cycle, which would otherwise skip every other three-minute run.
    eligible_web_search = list(web_search_infos) if quota_probe.available("feeds") else []
    web_search_quota = quota_probe
    config["ai"]["_web_search_quota"] = web_search_quota
    scheduled_web_search_url = None
    if eligible_web_search and quota_probe.available("feeds"):
        last_source = db.execute("SELECT value FROM app_state WHERE key='web_search_last_source_url'").fetchone()
        info_by_url = {info[0]["url"]: info for info in web_search_infos}
        ordered_urls = [info[0]["url"] for info in web_search_infos]
        last_url = last_source["value"] if last_source else None
        if last_url in ordered_urls:
            start = (ordered_urls.index(last_url) + 1) % len(ordered_urls)
            ordered_urls = ordered_urls[start:] + ordered_urls[:start]
        selected = next((info_by_url[url] for url in ordered_urls
                         if info_by_url[url] in eligible_web_search), None)
        if selected and web_search_quota.reserve(selected[0]["url"], category="feeds"):
            scheduled_web_search_url = selected[0]["url"]
            _log_timing("web_search_scheduled", category="feeds", eligible_count=len(eligible_web_search),
                        query_count=len(web_search_queries),
                        interval_minutes=web_search_interval)
    if web_search_infos and scheduled_web_search_url is None:
        counts["WEB_SEARCH_DEFERRED"] = len(web_search_infos)
        _log_timing("web_search_deferred", category="cooldown_or_not_due",
                    eligible_count=len(eligible_web_search), configured_sources=len(web_search_infos),
                    interval_minutes=web_search_interval)
    for source_cfg, source_type in active_configs:
        host = (urllib.parse.urlsplit(source_cfg["url"]).hostname or "").lower()
        if source_type in {"rss", "web"} and source_cfg.get("reputation") == "reputable_media" and host:
            ORIGINAL_REPORTING_PUBLISHERS.setdefault(_registrable_domain(host), re.escape(source_cfg["name"]))

    db.execute("UPDATE items SET disposition='STALE',processed_at=? WHERE disposition IN ('PENDING','AI_RETRY','PRIMARY_RETRY','WAITING_CONFIRMATION') AND julianday(COALESCE(updated_at,published_at))<julianday('now',?)",
               (NOW(), "-"+str(int(config["newsroom"].get("freshness_window_hours",24)))+" hours"))
    db.commit()
    requeued = _requeue_social_quote_repairs(
        db, config.get("ai", {}), int(config["newsroom"].get("freshness_window_hours", 24)))
    if requeued:
        counts["FILTER_REPROCESS_QUEUED"] = requeued
    _close_exhausted_retries(db)
    def fetch_one(source_info):
        source_cfg, source, first_check, source_type = source_info
        if source_type == "web_search":
            if source_cfg["url"] != scheduled_web_search_url:
                return None
        if source_type == "x":
            interval = int(config.get("x", {}).get("min_interval_minutes", 15))
            if source["last_checked_at"]:
                checked = datetime.fromisoformat(source["last_checked_at"].replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - checked).total_seconds() < interval * 60:
                    return None
        if source_type == "rss":
            return fetch_rss(source["url"])
        if source_type == "web":
            return fetch_web(source["url"])
        if source_type == "google_news":
            return fetch_google_news(source["url"])
        if source_type == "web_search":
            return fetch_web_search(web_search_queries, config.get("ai", {}))
        if source_type == "x":
            return fetch_x_recent(source_cfg.get("query", ""), config.get("x", {}))
        since = source["recovery_since"] or source["last_seen_published_at"] or (
            datetime.now(timezone.utc) - timedelta(hours=config["newsroom"].get("freshness_window_hours", 24))
        ).isoformat(timespec="seconds")
        return fetch_telegram(source["url"], since=since, before=source["recovery_before"],
                              max_pages=config["newsroom"].get("telegram_recovery_pages", 5))

    def timed_fetch(source_info):
        started = time.perf_counter()
        try:
            return fetch_one(source_info), time.perf_counter() - started, None
        except Exception as exc:
            return None, time.perf_counter() - started, exc

    # Reuse only already-read pages from configured reputable publishers as cross-check material.
    independent_reference_items: list[dict] = []
    trusted_independent_domains = {
        (urllib.parse.urlsplit(source_cfg["url"]).hostname or "").lower()
        for source_cfg, source_type in active_configs
        if source_type in {"rss", "web"} and source_cfg.get("reputation") == "reputable_media"
        and urllib.parse.urlsplit(source_cfg["url"]).hostname
    }

    # Fetch concurrently and begin processing each source as soon as it returns;
    # waiting in configuration order would let one slow publisher hold up every
    # already available news item and consume the source-to-publication budget.
    from .workflow import Coordinator, enqueue
    coordinator = Coordinator(db, config, counts)
    cleanup.callback(coordinator.abort)
    def completed_sources(pending):
        from concurrent.futures import wait, FIRST_COMPLETED
        while pending:
            coordinator.tick()
            done, _ = wait(list(pending) + list(coordinator.running), timeout=1, return_when=FIRST_COMPLETED)
            for future in done:
                if future in pending:
                    yield future
            coordinator.tick()

    with ThreadPoolExecutor(max_workers=min(12, max(1, len(sources)))) as pool:
        pending = {pool.submit(timed_fetch, info): info for info in sources}
        for future in completed_sources(pending):
            info = pending.pop(future)
            source_cfg, source, first_check, source_type = info
            items, fetch_seconds, fetch_error = future.result()
            # Futures are already complete when as_completed yields them; time
            # spent calling result() is effectively zero. Track the slowest
            # concurrent fetch so the cycle report reflects the actual wait
            # imposed by its least responsive source without summing overlaps.
            stage_times["fetch_wait_seconds"] = max(
                stage_times["fetch_wait_seconds"], fetch_seconds)
            if items is None and fetch_error is None:
                continue
            if fetch_error or fetch_seconds >= 2:
                _log_timing("source_fetch_timing", source=source["name"],
                            seconds=round(fetch_seconds, 3),
                            result="ERROR" if fetch_error else "OK")
            try:
                if fetch_error:
                    raise fetch_error
                article_diagnostics = getattr(items, "diagnostics", [])
                if article_diagnostics:
                    for error_code in article_diagnostics:
                        counts["ARTICLE_SOURCE_ERROR"] = counts.get("ARTICLE_SOURCE_ERROR", 0) + 1
                        db.execute("INSERT INTO errors(source_id,timestamp,message) VALUES(?,?,?)",
                                   (source["source_id"], NOW(), error_code))
                    db.commit()
                # Use the already-read Google News publisher pages to cross-check stories found in other feeds.
                if source_type == "google_news":
                    independent_reference_items = list(items)
                # Process each discovered entry individually in event-time order.
                items.sort(key=lambda x: (x.get("updated_at") or x.get("published_at") or ""), reverse=True)
                for item in items:
                    matching_started = time.perf_counter()
                    peers = [candidate for candidate in independent_reference_items if candidate is not item]
                    item["independent_sources"] = _independent_candidates(item, peers, trusted_independent_domains)
                    stage_times["matching_seconds"] += time.perf_counter() - matching_started
                    try:
                        queued = enqueue(db, None, item, source, {
                            "threshold": config["newsroom"].get("similarity_threshold", .35),
                            "max_length": config["newsroom"].get("max_post_length", 3500),
                            "freshness_hours": config["newsroom"].get("freshness_window_hours", 24),
                            "initial_backfill_minutes": config["newsroom"].get("initial_backfill_minutes",
                                config["newsroom"].get("freshness_window_hours", 24) * 60) if first_check else None,
                            "relevance_terms": config["newsroom"].get("relevance_terms", []),
                        })
                        if queued is None:
                            counts["DUPLICATE"] = counts.get("DUPLICATE", 0) + 1
                    except Exception as exc:
                        db.rollback()
                        counts["ERROR"] = counts.get("ERROR", 0) + 1
                        db.execute("INSERT INTO errors(source_id,timestamp,message) VALUES(?,?,?)",
                                   (source["source_id"], NOW(), _safe_source_error(exc)))
                        db.commit()
                latest = next((x["published_at"] for x in items if x.get("published_at")), None)
                recovery_latest = source["recovery_latest"] or latest
                if source["recovery_before"] is not None:
                    latest = source["recovery_latest"] or latest
                recovery_before = getattr(items, "recovery_before", None)
                recovery_error = getattr(items, "recovery_error", None)
                if recovery_before is not None:
                    since = source["recovery_since"] or source["last_seen_published_at"] or (
                        datetime.now(timezone.utc) - timedelta(hours=config["newsroom"].get("freshness_window_hours", 24))
                    ).isoformat(timespec="seconds")
                    latest = None  # Do not advance the coverage checkpoint past a gap.
                    counts["RECOVERY_PENDING"] = counts.get("RECOVERY_PENDING", 0) + 1
                else:
                    since = None
                # A feed retrieval succeeds even when an individual publisher page
                # is unreadable; keep that diagnostic separate from feed health.
                error = recovery_error
                now = NOW()
                db.execute("""UPDATE sources SET last_checked_at=?,
                    last_success_at=CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
                    consecutive_failures=CASE WHEN ? IS NULL THEN 0 ELSE consecutive_failures+1 END,
                    last_seen_published_at=CASE WHEN ? IS NULL THEN last_seen_published_at
                        WHEN last_seen_published_at IS NULL OR last_seen_published_at < ? THEN ? ELSE last_seen_published_at END,
                    last_error=?,recovery_before=?,recovery_since=?,recovery_latest=? WHERE source_id=?""",
                    (now, error, now, error, latest, latest, latest, error, recovery_before, since, recovery_latest if recovery_before is not None else None, source["source_id"]))
                if recovery_error:
                    db.execute("INSERT INTO errors(source_id,timestamp,message) VALUES(?,?,?)",
                               (source["source_id"], now, recovery_error))
                if not error and not recovery_before and (source["consecutive_failures"] or source["last_error"] or source["recovery_before"]):
                    counts["SOURCE_RECOVERED"] = counts.get("SOURCE_RECOVERED", 0) + 1
                    _log_timing("source_recovered", source=source["name"])
                db.commit()
            except Exception as exc:
                from .runtime import BudgetDeferred
                if isinstance(exc, BudgetDeferred):
                    scheduled_web_search_url = None
                    counts["WEB_SEARCH_DEFERRED"] = counts.get("WEB_SEARCH_DEFERRED", 0) + 1
                    _log_timing("web_search_deferred", category=exc.reason,
                                delay_seconds=exc.delay_seconds)
                    continue
                counts["SOURCE_ERROR"] = counts.get("SOURCE_ERROR", 0) + 1
                db.execute("UPDATE sources SET last_checked_at=?,last_error=?,consecutive_failures=consecutive_failures+1 WHERE source_id=?", (NOW(), _safe_source_error(exc), source["source_id"]))
                db.execute("INSERT INTO errors(source_id,timestamp,message) VALUES(?,?,?)", (source["source_id"], NOW(), _safe_source_error(exc)))
                db.commit()
    processing_started = time.perf_counter()
    coordinator.close()
    stage_times["processing_seconds"] = time.perf_counter() - processing_started
    if scheduled_web_search_url:
        # One request contains every active configured search scope, so they
        # share the same freshness timestamp even though results use one source
        # row for provenance and downstream processing.
        db.execute("UPDATE sources SET last_checked_at=? WHERE type='web_search' AND active=1 AND url!=?",
                   (NOW(), scheduled_web_search_url))
        db.commit()
    from .watch import run as run_story_watch
    config['ai']['_analysis_budget'] += watch_reserve
    config['ai']['_triage_budget'] += watch_reserve
    story_watch_started = time.perf_counter()
    def process_story_watch_item(db_, source_, item_, threshold_, max_length_, freshness_hours_, baseline_, relevance_, ai_):
        queued = enqueue(db_, None, item_, source_, {'threshold': threshold_, 'max_length': max_length_,
            'freshness_hours': freshness_hours_, 'initial_backfill_minutes': baseline_, 'relevance_terms': relevance_},
            category='watch')
        return 'QUEUED' if queued else 'DUPLICATE'
    counts.update(run_story_watch(db, config, web_search_quota, process_story_watch_item))
    watch_coordinator = Coordinator(db, config, counts, categories=('watch',), max_jobs=watch_reserve)
    cleanup.callback(watch_coordinator.abort)
    watch_coordinator.close()
    stage_times["story_watch_seconds"] = time.perf_counter() - story_watch_started
    # Give newly fetched material and due story watches first access to the
    # shared model budgets. Held-item retries are bounded and run afterward;
    # their reserved slots remain available even when fresh work is heavy.
    config['ai']['_analysis_budget'] += retry_reserve
    if config['ai'].get("triage_enabled"):
        config['ai']['_triage_budget'] += retry_reserve
    retry_started = time.perf_counter()
    retry_counts = {}
    retry_coordinator = Coordinator(db, config, retry_counts, categories=('retry',), max_jobs=retry_reserve)
    cleanup.callback(retry_coordinator.abort)
    for outcome, count in _retry_ai_held_items(db, source_by_id, config, coordinator=retry_coordinator).items():
        counts[outcome] = counts.get(outcome, 0) + count
    retry_coordinator.close()
    for outcome, count in retry_counts.items():
        counts[outcome] = counts.get(outcome, 0) + count
    stage_times["retry_seconds"] = time.perf_counter() - retry_started
    total_seconds = time.perf_counter() - run_started
    stage_times["other_seconds"] = max(0.0, total_seconds - sum(stage_times.values()))
    _log_timing("collection_stage_timing", total_seconds=round(total_seconds, 3),
                **{key: round(value, 3) for key, value in stage_times.items()})
    return counts
