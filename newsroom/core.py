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


from .ai import request_response, AIResponseError, get_api_key, web_search_enabled


_RUNTIME_LOG_PATH: Path | None = None

NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            return None
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return date.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                return None
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
                # Reserve part of the same request deadline for the other
                # checked addresses; one unreachable IP must not consume it all.
                connect_timeout = remaining / (len(addresses) - index)
                sock = socket.create_connection((address, self.port), connect_timeout, self.source_address)
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
        kwargs = {'context': self._context}
        # Python 3.12+ moved hostname verification entirely into SSLContext.
        if hasattr(self, '_check_hostname'):
            kwargs['check_hostname'] = self._check_hostname
        return self.do_open(_PublicHttpsConnection, req, **kwargs)



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
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
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
    return PDFText(text, ocr_used=ocr_used)



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
    CHROME_CLASSES = {"related", "related-posts", "related-articles", "recommendations",
                      "advertisement", "ad-banner", "ads", "view-count", "views-count",
                      "social-share", "share-buttons", "cookie-banner", "cookie-consent"}
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

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
        chrome = (bool(self.CHROME_CLASSES.intersection((values.get('class') or '').lower().split()))
                  or values.get('role', '').lower() in {'navigation', 'banner', 'contentinfo', 'complementary'}
                  or 'hidden' in values or values.get('aria-hidden', '').lower() == 'true')
        if tag == "time" and not self.skip_stack and not chrome and values.get("datetime"):
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
            elif key in {"article:modified_time", "datemodified", "zoom:last-modified"}:
                self.updated_at = parse_date(values.get("content"))
            if key in {"og:title", "twitter:title"} and values.get("content"):
                self.meta_title = values["content"]
            elif key in {"og:description", "description", "twitter:description"} and values.get("content") and not self.meta_description:
                self.meta_description = values["content"]
        elif tag == "link" and "canonical" in values.get("rel", "").lower():
            self.canonical_url = values.get("href", "")
        if (tag in self.SKIP or chrome
                or (self.skip_stack and tag == self.skip_stack[-1])):
            if tag not in self.VOID_TAGS:
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
                        if isinstance(body, str) and body.strip():
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
    from .material_store import save, settings
    options = settings()
    try:
        article = _fetch_publisher_article_native(url, publisher_name, published_at,
            discover_primary=discover_primary, timeout=timeout, public_only=public_only)
    except (TimeoutError, socket.timeout):
        # A timeout has already spent the source's network allowance.
        raise
    except Exception as exc:
        if not options.get('jina_enabled') or not _jina_retryable(exc):
            raise
        from .jina_reader import read_article
        article = read_article(url, publisher_name, published_at, options, timeout, discover_primary=discover_primary)
    else:
        if article.get('material_read') is False and options.get('jina_enabled'):
            from .jina_reader import read_article
            article = read_article(url, publisher_name, published_at, options, timeout, discover_primary=discover_primary)
    article.setdefault('reading_method', 'native')
    return save(article)



def _jina_retryable(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {403, 406, 429, 503}
    return isinstance(exc, ValueError) and str(exc) in {
        'PUBLISHER_BROWSER_CHALLENGE', 'Publisher page has no readable article text'}



def _fetch_publisher_article_native(url: str, publisher_name: str, published_at: str | None,
                            discover_primary: bool = True, timeout: int = 20,
                            public_only: bool = False, _rendered_response=None) -> dict:
    parsed_url = urllib.parse.urlsplit(url)
    if (not public_only and (parsed_url.hostname or "").lower().removeprefix("www.") == "bybit.com"
            and parsed_url.path.rstrip("/").lower() ==
            "/en/legal/additional-terms-and-disclosures/restricted-counterparty-list"):
        return _fetch_bybit_restricted_counterparty_pdf(url, publisher_name, published_at, timeout)
    payload, final_url, content_type = (_rendered_response if _rendered_response is not None else
                                      _request_with_url(url, timeout=timeout, public_only=public_only))
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
                   "material_read": True, "material_url": final_url,
                   "publisher_name": publisher_name or host,
                   "primary_source_url": final_url, "primary_source_title": title,
                   "primary_source_content": str(content),
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
    if structured_body.strip():
        content = re.sub(r"\s+", " ", html.unescape(structured_body)).strip()
    article_body_read = bool(structured_body.strip()) or len(content) >= 100
    if not article_body_read and len(content) < 100 and len(description) >= 100:
        content = description
    if not article_body_read and len(content) < 100 and not embedded_candidates:
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
               "content": content, "author": None, "published_at": parser.published_at or published_at,
               "updated_at": parser.updated_at, "publisher_name": publisher_name or canonical_host,
               "material_read": article_body_read, "material_url": canonical_url,
               "primary_source_url": None, "primary_source_title": None,
               "primary_source_content": "", "primary_source_type": None,
               "primary_source_publisher": None, "primary_source_status": "NOT_CHECKED"}
    if discover_primary and _is_official_source_host(canonical_host) and not embedded_candidates:
        article["primary_source_url"] = canonical_url
        article["primary_source_title"] = title
        article["primary_source_content"] = content
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
            article["primary_source_content"] = primary["content"]
            article["primary_source_type"] = candidate["kind"]
            article["primary_source_publisher"] = primary["publisher_name"]
            article["primary_source_status"] = ("OCR_REVIEW" if primary.get("primary_source_status") == "OCR_REVIEW" else "READ")
            if len(content) < 100 and primary.get("content"):
                article["content"] = primary["content"]
                article["title"] = primary.get("title") or title
            break
    # A document remains preferred. If unavailable, original reporting can support
    # attributed claims, never pretend that the underlying document was read.
    kind = _original_reporting_kind(final_url, content) if article_body_read else None
    if discover_primary and article["primary_source_status"] != "READ" and kind:
        article.update(primary_source_url=final_url, primary_source_title=title,
                       primary_source_content=content, primary_source_type=kind,
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



def fetch_google_news(url: str, *, read_articles=True) -> list[dict]:
    """Use Google News for discovery, then resolve and read each publisher article."""
    root = ET.fromstring(_request(url))
    entries = root.findall(".//item")[:6]
    if not read_articles:
        result = []
        for node in entries:
            link = (node.findtext('link') or '').strip()
            if not link:
                continue
            result.append({'url': link, 'title': node.findtext('title') or '',
                           'description': node.findtext('description') or '', 'content': '',
                           'publisher_name': node.findtext('source') or '',
                           'published_at': parse_date(node.findtext('pubDate'))})
        return FetchedItems(result, [])

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
                     max_results: int = 8, page_timeout: int = 20,
                     read_articles: bool = True) -> list[dict]:
    """Use Responses web_search for discovery, then read publisher pages."""
    if not web_search_enabled(ai_settings):
        return FetchedItems([], ['WEB_SEARCH_DISABLED'])
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
    if not scopes and "_topic_registry" in ai_settings:
        return FetchedItems([], ["TOPIC_SEARCH_SCOPE_EMPTY"])
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
            f"Respect this user's negative interest examples and avoid similar topics: {'; '.join(interest_exclusions or [])}"},
            {**ai_settings, '_work_stage': 'discovery_search' if not ai_settings.get('_work_stage') else ai_settings['_work_stage']})
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
                items = fetch_google_news(url) if read_articles else fetch_google_news(url, read_articles=False)
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
    if not read_articles:
        return FetchedItems([
            {'url': link['url'], 'title': link.get('title') or link['url'],
             'description': '', 'content': '', 'published_at': None, 'updated_at': None}
            for link in links[:max(1, min(8, int(max_results)))]
            if link['url'].startswith(('https://', 'http://'))
        ], [])
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
        if self.msg_depth and self.current and tag == 'br':
            self.current['parts'].append('\n')
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
                text = '\n'.join(' '.join(line.split()) for line in ' '.join(self.current['parts']).splitlines() if line.strip())
                post = self.current["post"]
                if text and post:
                    url = "https://t.me/" + post
                    self.items.append({"url": url, "title": text.splitlines()[0][:160], "description": text,
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
        article_url = item['url']
        if (urllib.parse.urlsplit(article_url).hostname or '').lower() == 'news.google.com':
            article_url = decode_google_news_url(article_url)
        article = fetch_publisher_article(article_url, publisher_name, item.get("published_at"), timeout=8)
    except Exception as exc:
        item["material_read"] = False
        item["primary_source_status"] = "ARTICLE_UNREADABLE"
        item["primary_source_error"] = type(exc).__name__
        return item.get("content") or item.get("description") or item["title"]
    item.update({
        "title": article["title"] or item["title"],
        "description": article.get("description") or item.get("description", ""),
        "content": article["content"],
        "published_at": item.get("published_at") or article.get("published_at"),
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
        return fetch_web_search(query, {**ai_settings, '_work_stage': 'recovery_search'})



def _archive_item_revision(db, item_id: int, prior_row) -> None:
    source_snapshot = dict(prior_row)
    revision_hash = digest("\n".join((source_snapshot.get("title") or "", source_snapshot.get("content") or "", source_snapshot.get("description") or "")))
    db.execute("INSERT INTO item_revisions(item_id,observed_at,revision_hash,source_snapshot_json,decision_snapshot_json) VALUES(?,?,?,?,?)",
               (item_id, NOW(), revision_hash, json.dumps(source_snapshot, ensure_ascii=False), '{}'))


def _save_item(db, source, item, existing_item_id=None):
    """Persist discovery before any costly work; keep feed and read text distinct."""
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    from .runtime import cache_key
    ingest_revision = cache_key('material-version', {key: item.get(key) for key in (
        'url', 'title', 'description', 'content', 'author', 'published_at')})
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
                                 and prior["published_at"] == item.get("published_at"))
                stored_feed_hash = prior["feed_content_hash"] if "feed_content_hash" in prior.keys() else ""
                # `items.content` is enriched with fetched article text. Compare a
                # feed poll to its own last-seen body so enrichment cannot reset retries.
                unchanged = (prior['ingest_revision'] == ingest_revision or
                             same_metadata and (not stored_feed_hash or stored_feed_hash == feed_content_hash))
                if unchanged:
                    # A changed modification clock alone is metadata, not a new
                    # article version or permission to reset bounded attempts.
                    if not stored_feed_hash or prior['updated_at'] != item.get('updated_at'):
                        db.execute("UPDATE items SET feed_content_hash=?,updated_at=? WHERE item_id=?",
                                   (stored_feed_hash or feed_content_hash, item.get('updated_at'), prior['item_id']))
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
        else:
            item_id = existing_item_id
            prior = db.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
            if prior and item.get('_expected_revision') and prior['ingest_revision'] != item['_expected_revision']:
                return None
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
    saved = db.execute('SELECT ingest_revision,discovered_at FROM items WHERE item_id=?', (item_id,)).fetchone()
    receipt = now if existing_item_id is None else saved['discovered_at']
    db.execute('INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING',
               (f"material_received:{item_id}:{saved['ingest_revision']}", json.dumps({'at': receipt})))
    return item_id


def run_cycle(config: dict) -> dict[str, int]:
    from .agent_control import require_enabled
    require_enabled(config)
    from contextlib import ExitStack
    try:
        with ExitStack() as cleanup:
            db = connect(config["newsroom"]["database"])
            cleanup.callback(db.close)
            return _run_cycle(config, db, cleanup)
    except Exception as exc:
        # Record after all coordinators have rolled back and released work.
        # Never retain the exception text: it may contain URLs or credentials.
        from contextlib import suppress
        from .diagnostics import error_location
        with suppress(Exception):
            evidence_db = connect(config["newsroom"]["database"])
            try:
                evidence_db.execute("INSERT INTO app_state(key,value) VALUES('diagnostic_last_error',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({'at': NOW(), 'code': type(exc).__name__, 'location': error_location(exc)}),))
                evidence_db.commit()
            finally:
                evidence_db.close()
        raise



def _run_cycle(config, db, cleanup):
    config = {**config, "ai": {k: v for k, v in config.get("ai", {}).items() if k not in {"_disabled_for_cycle", "_unused_legacy_flag"}}}
    from .source_registry import sync
    from .topic_registry import sync as sync_topics
    sync_topics(db, config)
    sync(db, config)
    sync_topics(db, config)
    config['_collection_only'] = True
    run_started = time.perf_counter()
    stage_times = {"retry_seconds": 0.0, "fetch_wait_seconds": 0.0,
                   "matching_seconds": 0.0, "processing_seconds": 0.0}
    from .runtime import attach
    attach(config)
    counts: dict[str, int] = {}
    sources = []
    active_configs = [
        (source_cfg, source_cfg.get("type", "rss"))
        for source_cfg in config.get("sources", [])
        if source_cfg.get("type", "rss") in {"rss", "web", "telegram", "google_news", "web_search", "x"}
        and source_cfg.get("active", True)
        and (source_cfg.get('type') != 'web_search' or web_search_enabled(config['ai']))
    ]
    active_configs.sort(key=lambda entry: (
        0 if entry[1] == "google_news" else
        1 if entry[1] == "web_search" else 2,
        -int(entry[0].get("priority", 1))))
    processing_urls = config.get('_registry_processing_urls', [])
    active_urls = list(dict.fromkeys([cfg["url"] for cfg, _ in active_configs] + processing_urls))
    if active_urls:
        placeholders = ",".join("?" for _ in active_urls)
        db.execute(f"UPDATE sources SET active=0 WHERE url NOT IN ({placeholders})", active_urls)
        db.execute(f"UPDATE sources SET active=1 WHERE url IN ({placeholders})", active_urls)
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
    if processing_urls:
        marks = ','.join('?' for _ in processing_urls)
        source_by_id.update({row['source_id']: row for row in db.execute(
            f'SELECT * FROM sources WHERE active=1 AND url IN ({marks})', processing_urls)})
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
            return fetch_google_news(source["url"], read_articles=False) if config.get("_collection_only") else fetch_google_news(source["url"])
        if source_type == "web_search":
            return fetch_web_search(web_search_queries, config.get("ai", {}), read_articles=False) if config.get('_collection_only') else fetch_web_search(web_search_queries, config.get("ai", {}))
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
            from .workflow import Work
            stage = 'discovery_search' if source_info[3] == 'web_search' else 'source_' + source_info[3]
            result = Work('collector', fetch_one, (source_info,), stage=stage).execute(
                config['ai'].get('_runtime'), {'category': 'feeds', 'source_id': source_info[1]['source_id']})
            return result, time.perf_counter() - started, None
        except Exception as exc:
            return None, time.perf_counter() - started, exc

    # Fetch concurrently and begin processing each source as soon as it returns;
    # waiting in configuration order would let one slow publisher hold up every
    # already available news item and consume the source-to-publication budget.
    from .workflow import CollectionCoordinator, enqueue
    coordinator = CollectionCoordinator()
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
                # Process each discovered entry individually in event-time order.
                items.sort(key=lambda x: (x.get("updated_at") or x.get("published_at") or ""), reverse=True)
                for item in items:
                    try:
                        queued = enqueue(db, None, item, source, {"settings": config.get("ai", {})})
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
    _log_timing('collection_stage_timing', total_seconds=round(time.perf_counter()-run_started, 3), **stage_times)
    return counts
