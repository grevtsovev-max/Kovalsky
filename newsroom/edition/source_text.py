"""Readable source excerpts and conservative identity checks, without network access."""
from __future__ import annotations
import copy
import html
import re
from html.parser import HTMLParser


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.hidden=0

    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
        if tag in ('br','p','div','li','blockquote'):self.parts.append('\n')

    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
        if tag in ('p','div','li','blockquote'):self.parts.append('\n')

    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def text(value):
    value=str(value or '')
    if re.search(r'</?(?:a|p|div|br|font|span|b|i|script|style)\b',value,re.I):
        parser=TextParser();parser.feed(value);value=''.join(parser.parts)
    value=html.unescape(value).replace('\xa0',' ')
    return '\n'.join(' '.join(line.split()) for line in value.splitlines() if line.strip()).strip()


def canonical(value):return ' '.join(text(value).split()).casefold().replace('ё','е')


def body(material):
    return text(material.get('content') or material.get('description') or material.get('title'))


def sections(value):
    """Numbered digests remain separate even if an old collector flattened newlines."""
    value=text(value)
    return [s.strip() for s in re.split(r'\s+(?=\d{1,2}[.)]\s+[А-ЯЁA-Z])',value) if s.strip()]


def pieces(value,limit=210):
    """Split on sentence/word boundaries; never rewrite numbers or invent facts."""
    value=text(value)
    sentences=re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z«\d])|\n+',value)
    result=[]
    for sentence in sentences:
        while len(sentence)>limit:
            cut=sentence.rfind(' ',0,limit+1)
            if cut<1:cut=limit
            result.append(sentence[:cut].strip());sentence=sentence[cut:].strip()
        if sentence:result.append(sentence)
    return result


def formatted(draft):
    """Mechanical paragraph repair preserves the wording and evidence."""
    result=copy.deepcopy(draft)
    lead=pieces(result.get('lead','')) if len(result.get('lead',''))>210 else [result.get('lead','')]
    result['lead']=lead[0] if lead else ''
    blocks=[{'text':p,'kind':'paragraph','evidence':copy.deepcopy(result.get('lead_evidence',[])),
             'quote_text':'','quote_author':''} for p in lead[1:]]
    for block in result.get('blocks',[]):
        limit=208 if block.get('kind')=='bullet' else 210
        parts=pieces(block.get('text',''),limit) if len(block.get('text',''))>limit or '\n' in block.get('text','') else [block.get('text','')]
        if block.get('kind')=='quote' and len(parts)>1:
            # Keep a direct quote intact in details, with the same author and evidence.
            for part in parts:
                blocks.append({**copy.deepcopy(block),'text':part,'kind':'details','quote_text':'','quote_author':''})
        else:
            blocks.extend({**copy.deepcopy(block),'text':part} for part in parts)
    result['blocks']=blocks
    return result


def same_version(a,b):
    """Ignore punctuation, markup and grammatical copy edits, never changed facts."""
    left=re.findall(r'\w+',canonical(body(a)));right=re.findall(r'\w+',canonical(body(b)))
    left_title=re.findall(r'\w+',canonical(a.get('title','')))
    right_title=re.findall(r'\w+',canonical(b.get('title','')))
    # A headline correction is substantive too. A collector shortening the same
    # unchanged text to its first line is only a presentation change.
    titles_equal=left_title==right_title or (left[:len(left_title)]==left_title and right[:len(right_title)]==right_title)
    if not titles_equal:return False
    if left==right:return True
    if len(left)<40 or len(left)!=len(right):return False
    differences=[(x,y) for x,y in zip(left,right) if x!=y]
    if len(differences)>max(2,len(left)//100):return False
    from ..keyword_filter import forms
    connective={'что','как'}
    return all(not any(c.isdigit() for c in x+y) and
               (bool(forms(x)&forms(y)) or x in connective and y in connective)
               for x,y in differences)


def article_identity(material):
    """Only identical event titles AND available factual text collapse across feeds."""
    title=text(material.get('title',''))
    metadata=material.get('primary_source_json')
    import json
    try:metadata=json.loads(metadata) if isinstance(metadata,str) else metadata or {}
    except (TypeError,ValueError):metadata={}
    publisher=metadata.get('_material_publisher') if isinstance(metadata,dict) else None
    if publisher:
        title=re.sub(r'\s*[-–—|]\s*'+re.escape(publisher)+r'\s*$','',title,flags=re.I)
    content=body(material)
    if publisher:content=re.sub(r'\s*'+re.escape(publisher)+r'\s*$','',content,flags=re.I)
    return canonical(title),canonical(content)
