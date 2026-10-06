"""Bounded fallback to a separately deployed, private Jina Reader service."""
from __future__ import annotations

import json
import html
import re
import urllib.parse
import urllib.request
import urllib.error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('JINA_ENDPOINT_REDIRECT')


def read_article(url, publisher, published_at, settings, timeout, *, discover_primary=True):
    if (settings.get('jina_cloud_enabled') is True and
            urllib.parse.urlsplit(url).hostname in settings.get('jina_cloud_hosts', [])):
        return read_cloud_article(url, publisher, published_at, settings, timeout,
                                  discover_primary=discover_primary)
    try:
        return _read_article(url, publisher, published_at, settings, timeout,
                             discover_primary=discover_primary)
    except (TimeoutError, ValueError) as exc:
        retryable = isinstance(exc, ValueError) and str(exc) in {
            'JINA_BROWSER_CHALLENGE', 'JINA_ARTICLE_NOT_READ', 'JINA_HTML_MISSING', 'JINA_ACCESS_DENIED'}
        if not retryable or settings.get('jina_cloud_enabled') is not True:
            raise
    except urllib.error.HTTPError as exc:
        if exc.code not in {403, 406, 429, 503} or settings.get('jina_cloud_enabled') is not True:
            raise
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError) or settings.get('jina_cloud_enabled') is not True:
            raise
    return read_cloud_article(url, publisher, published_at, settings, timeout,
                              discover_primary=discover_primary)


def read_cloud_article(url, publisher, published_at, settings, timeout, *, discover_primary=True):
    if settings.get('jina_cloud_enabled') is not True:
        raise ValueError('JINA_CLOUD_DISABLED')
    from .core import _validate_public_http_url
    _validate_public_http_url(url)
    from .jina_cloud import api_key, reserve, finish
    key = api_key(settings)
    attempt, budget = reserve(settings)
    try:
        article = _read_article(url, publisher, published_at, settings, timeout,
            discover_primary=discover_primary, cloud_key=key, token_budget=budget)
    except Exception:
        finish(settings, attempt, 'FAILED')
        raise
    finish(settings, attempt, 'READ', article.pop('_jina_tokens', None))
    return article


def _read_article(url, publisher, published_at, settings, timeout, *, discover_primary=True,
                  cloud_key=None, token_budget=None):
    from .core import _validate_public_http_url
    _validate_public_http_url(url)
    endpoint = str(settings.get('jina_endpoint', '')).rstrip('/')
    parsed = urllib.parse.urlsplit(endpoint)
    if cloud_key is not None:
        endpoint = 'https://r.jina.ai'
    elif (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise ValueError('JINA_ENDPOINT_NOT_LOOPBACK')
    headers = {
        'Accept': 'application/json', 'X-Respond-With': 'html',
        'X-Engine': 'browser', 'X-No-Cache': 'true',
        'X-Timeout': str(max(1, min(15, int(timeout)))),
    }
    if cloud_key is not None:
        headers.update({'Authorization': 'Bearer ' + cloud_key, 'X-Token-Budget': str(token_budget), 'DNT': '1', 'X-Respond-With': 'content'})
        formats = settings.get('jina_cloud_formats', {})
        host = urllib.parse.urlsplit(url).hostname
        if isinstance(formats, dict) and formats.get(host) == 'markdown':
            headers['X-Respond-With'] = 'markdown'
        if settings.get('jina_cloud_proxy') is True:
            headers['X-Proxy'] = 'auto'
    request = urllib.request.Request(endpoint + '/' + url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=max(1, min(20, int(timeout) + 2))) as response:
        payload = response.read(10_000_001)
    if len(payload) > 10_000_000:
        raise ValueError('JINA_RESPONSE_TOO_LARGE')
    result = json.loads(payload)
    if not isinstance(result, dict) or result.get('code') != 200 or not isinstance(result.get('data'), dict):
        raise ValueError('JINA_INVALID_RESPONSE')
    data = result['data']
    final_url = data.get('url') or url
    _validate_public_http_url(final_url)
    markup = data.get('html') or data.get('content') or ''
    cloud_content = None
    if cloud_key is not None and not data.get('html'):
        if not isinstance(markup, str) or len(markup.strip()) < 100:
            raise ValueError('JINA_ARTICLE_NOT_READ')
        if (urllib.parse.urlsplit(final_url).hostname or '').removeprefix('www.') == 'tass.ru':
            title_words = {word[:5] for word in re.findall(r'[а-яёa-z]+', str(data.get('title') or '').lower()) if len(word) >= 4}
            body_words = {word[:5] for word in re.findall(r'[а-яёa-z]+', markup[:3000].lower()) if len(word) >= 4}
            if not title_words or len(title_words & body_words) < max(2, (len(title_words) + 1) // 2):
                raise ValueError('JINA_TITLE_BODY_MISMATCH')
        cloud_content = markup
        # Keep all extracted Markdown, including tables and footnotes, and expose
        # its links to the existing primary-source discovery parser.
        escaped = html.escape(markup)
        escaped = re.sub(r'\[([^\]\n]+)\]\((https://[^\s)]+)\)',
                         lambda match: '<a href="' + match[2] + '">' + match[1] + '</a>', escaped)
        markup = '<html><title>' + html.escape(str(data.get('title') or publisher)) + '</title><article>' + ''.join(
            '<p>' + line + '</p>' for line in escaped.splitlines() if line.strip()) + '</article></html>'
    if not isinstance(markup, str) or '<' not in markup:
        raise ValueError('JINA_HTML_MISSING')
    if any(value in markup.lower() for value in (
            'verify you are human', 'captcha-container', '<js-challenge-loader')):
        raise ValueError('JINA_BROWSER_CHALLENGE')
    # Reader may return a successful envelope for a publisher's error page.
    status = data.get('status')
    if status in (401, 403):
        raise ValueError('JINA_ACCESS_DENIED')
    if isinstance(status, int) and status >= 400:
        raise ValueError('JINA_PUBLISHER_ERROR')
    from .core import PublisherArticleParser
    parser = PublisherArticleParser()
    parser.feed(markup)
    title = (parser.meta_title or ' '.join(parser.title_parts) or data.get('title') or '').lower()
    if title.strip() in {'forbidden', '403', '403 forbidden', 'unauthorized', 'access denied'}:
        raise ValueError('JINA_ACCESS_DENIED')
    if any(value in title for value in ('404', '403 forbidden', 'page not found', 'страница не найдена', 'access denied')):
        raise ValueError('JINA_PUBLISHER_ERROR')
    from .core import _fetch_publisher_article_native
    article = _fetch_publisher_article_native(final_url, publisher, published_at,
        discover_primary=discover_primary, timeout=timeout, public_only=True,
        _rendered_response=(markup.encode('utf-8'), final_url, 'text/html'))
    if article.get('material_read') is not True:
        raise ValueError('JINA_ARTICLE_NOT_READ')
    article['reading_method'] = 'jina_cloud' if cloud_key is not None else 'jina'
    if cloud_key is not None:
        article['reader_content'] = cloud_content
        tokens = (data.get('usage') or {}).get('tokens') if isinstance(data.get('usage'), dict) else None
        article['_jina_tokens'] = tokens if isinstance(tokens, int) and tokens >= 0 else None
    return article
