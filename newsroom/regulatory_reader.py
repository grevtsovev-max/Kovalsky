"""Read every PDF page and preserve attachments and evidence locations."""
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .core import _request_with_url


class DocumentHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = []
        self.parts, self.links = [], []
        self.anchor = None
        self.heading = ''
        self.heading_parts = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.skip:
            if tag == self.skip[-1]: self.skip.append(tag)
            return
        if tag in {'script','style','nav','header','footer','noscript','svg','form'}:
            self.skip.append(tag); return
        if tag in {'h1','h2','h3','h4','h5'}:
            self.heading_parts = []
        if tag in {'p','div','li','tr','br','h1','h2','h3','h4','h5'}:
            self.parts.append('\n')
        if tag == 'a' and attrs.get('href'):
            self.anchor = {'href': attrs['href'], 'parts': [], 'heading': self.heading}
        if tag in {'iframe','object','embed'} and (attrs.get('src') or attrs.get('data')):
            self.links.append({'href': attrs.get('src') or attrs['data'], 'text': attrs.get('title','Документ'), 'heading': self.heading})

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]: self.skip.pop()
            return
        if tag in {'h1','h2','h3','h4','h5'} and self.heading_parts is not None:
            self.heading = ' '.join(self.heading_parts).strip(); self.heading_parts = None
        if tag == 'a' and self.anchor:
            self.anchor['text'] = ' '.join(self.anchor.pop('parts')).strip()
            self.links.append(self.anchor); self.anchor = None
        if tag in {'p','div','li','tr'}: self.parts.append('\n')

    def handle_data(self, data):
        if self.skip: return
        self.parts.append(data)
        if self.heading_parts is not None: self.heading_parts.append(data)
        if self.anchor is not None: self.anchor['parts'].append(data)

    @property
    def text(self):
        return '\n'.join(re.sub(r'\s+', ' ', line).strip() for line in ''.join(self.parts).splitlines() if line.strip())


def attachment_url(url):
    path = urlsplit(url).path.lower()
    return any(part in path for part in ('.pdf','/file/','/document/','/bill/','/documents/'))


def pdf_pages(payload):
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader:
        reader = PdfReader(io.BytesIO(payload))
        if not 0 < len(reader.pages) <= 500: raise ValueError('PDF_PAGE_LIMIT')
        pages = [(page.extract_text() or '') for page in reader.pages]
        if all(len(page.strip()) >= 30 for page in pages):
            return pages, []
    script = Path(__file__).with_name('regulatory_pdf.swift')
    env = os.environ.copy()
    cache = str(Path(tempfile.gettempdir())/'kovalsky-regulatory-swift')
    Path(cache).mkdir(exist_ok=True)
    env.update(SWIFT_MODULECACHE_PATH=cache, CLANG_MODULE_CACHE_PATH=cache)
    with tempfile.NamedTemporaryFile(suffix='.pdf') as handle:
        handle.write(payload); handle.flush()
        result = subprocess.run(['/usr/bin/swift', str(script), handle.name], capture_output=True,
                                timeout=180, env=env)
    if result.returncode: raise ValueError('PDF_EXTRACTION_FAILED')
    result = json.loads(result.stdout)
    return result['pages'], result['ocr_pages']


def read_source(url, archive=None):
    from .regulatory import official, now
    if not official(url): raise ValueError('UNTRUSTED_URL')
    payload, final_url, mime = _request_with_url(url, timeout=20)
    if not official(final_url): raise ValueError('UNTRUSTED_REDIRECT')
    digest = hashlib.sha256(payload).hexdigest()
    is_pdf = payload.startswith(b'%PDF-')
    if archive:
        directory = Path(archive); directory.mkdir(parents=True, exist_ok=True)
        (directory/(digest + ('.pdf' if is_pdf else '.html'))).write_bytes(payload)
    links = []
    if is_pdf:
        pages, ocr = pdf_pages(payload)
    elif 'html' in mime.lower():
        page = payload.decode('utf-8', errors='replace')
        if any(s in page.lower() for s in ('<js-challenge-loader','verify you are human','captcha-container')):
            raise ValueError('BROWSER_CHALLENGE')
        parser = DocumentHTML(); parser.feed(page)
        pages, ocr = [parser.text], []
        for link in parser.links:
            target = urljoin(final_url, link['href'])
            if official(target) and attachment_url(target):
                links.append({'url': target, 'title': (link.get('heading','') + ' — ' + link['text']).strip(' —')})
    else:
        raise ValueError('UNSUPPORTED_DOCUMENT_FORMAT')
    if len(''.join(pages).strip()) < 150: raise ValueError('DOCUMENT_NOT_READ')
    result = dict(url=url, final_url=final_url, read_at=now(), raw_hash=digest,
                  pages=pages, ocr_pages=ocr, empty_pages=[i+1 for i,p in enumerate(pages) if len(p.strip())<30],
                  links=links, format='pdf' if is_pdf else 'html')
    if archive:
        (Path(archive)/(digest+'.json')).write_text(json.dumps(result,ensure_ascii=False))
    return result


def full_text(source):
    return '\n\n'.join('[Страница %d]\n%s' % (i+1, text) for i,text in enumerate(source['pages']))
