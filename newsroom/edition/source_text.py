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


def saved_fields(material):
    """Original fields and a successfully read, immutable linked source."""
    import json
    from urllib.parse import urlsplit
    result={field:text(material.get(field,'')) for field in ('title','content','description')}
    primary=material.get('primary_source_json') or {}
    if isinstance(primary,str):
        try:primary=json.loads(primary)
        except (ValueError,TypeError):return result
    if not isinstance(primary,dict) or primary.get('status')!='READ' or primary.get('type') not in ('OFFICIAL','LINKED_DOCUMENT'):return result
    try:
        url=urlsplit(primary.get('url',''))
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:return result
    except (ValueError,TypeError):return result
    if not isinstance(primary.get('content'),str) or not primary['content'].strip():return result
    result['primary_title']=text(primary.get('title',''))
    result['primary_content']=text(primary['content'])
    return result


def expand_link_preview(group,materials):
    """A selected single-title link preview includes its already read article.

    A digest mentioning a title is insufficient: the selected scope must consist
    solely of that exact title followed by a publisher label and this source URL.
    """
    import json
    result=copy.deepcopy(group)
    for material in materials:
        fields=saved_fields(material)
        if not fields.get('primary_title') or not fields.get('primary_content'):continue
        primary=material.get('primary_source_json') or {}
        if isinstance(primary,str):primary=json.loads(primary)
        pattern=r'^'+re.escape(fields['primary_title'])+r'\s*\|\s*[^|\n]+\s+'+re.escape(primary['url'])+r'\s*$'
        selected=[f for f in result.get('focus',[]) if f.get('material_id')==material['material_id'] and f.get('scope')]
        if not any(re.fullmatch(pattern,text(f['scope']),re.I) for f in selected):continue
        if any(f['scope']==fields['primary_content'] for f in selected):continue
        result['focus'].append({'material_id':material['material_id'],'excerpt':fields['primary_title'],
                               'scope':fields['primary_content'],'summary':'Сохранённый прочитанный первоисточник выбранного анонса.'})
    return result


def canonical(value):return ' '.join(text(value).split()).casefold().replace('ё','е')


def body(material):
    return text(material.get('content') or material.get('description') or material.get('title'))


def sections(value):
    """Numbered digests remain separate even if an old collector flattened newlines."""
    value=text(value)
    return [s.strip() for s in re.split(r'\s+(?=\d{1,2}[.)]\s+[А-ЯЁA-Z])',value) if s.strip()]


def promotional_sentence(value):
    """Explicit reader solicitation; reported company actions remain facts."""
    value=re.sub(r'^[^\wА-ЯЁа-яё]+','',text(value))
    return bool(re.match(r'(?:Хотите\s+(?:е[щш][её]|получить)|Подпис(?:ывайтесь|аться)|'
        r'Читайте\s+(?:последние|свежие)|Будьте\s+на\s+шаг|'
        r'(?:Вступайте|Присоединяйтесь)\s+(?:в|к)|Обсуждайте\s+новости|'
        r'Подключ(?:ите|айтесь)\b|(?:Оставьте|Подайте|Заполните)\s+(?:заявку|форму)|'
        r'Если\s+оборудование\s+уже\s+подключено,\s*проверьте)',value,re.I))


def editorial_source(value):
    """Remove discrete publisher promotions, preserving subsequent reporting."""
    value=text(value)
    value=re.split(r'\bThe post\b|(?:#\w+\s*){2,}|РОСКРИПТО\.РФ\s*\|',value,flags=re.I)[0]
    sentences=re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z«\d])|\n+',value)
    return '\n'.join(s.strip() for s in sentences if s.strip() and not promotional_sentence(s))


def pieces(value,limit=210,*,sentences_only=False):
    """Split on sentence/word boundaries; never rewrite numbers or invent facts."""
    value=text(value)
    sentences=re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z«\d])|\n+',value)
    result=[]
    for sentence in sentences:
        if sentences_only and len(sentence)>limit:
            result.append(sentence);continue
        while len(sentence)>limit:
            cut=sentence.rfind(' ',0,limit+1)
            if cut<1:cut=limit
            result.append(sentence[:cut].strip());sentence=sentence[cut:].strip()
        if sentence:result.append(sentence)
    return result


def formatted(draft,*,sentences_only=False):
    """Mechanical paragraph repair preserves the wording and evidence."""
    result=copy.deepcopy(draft)
    lead=pieces(result.get('lead',''),sentences_only=sentences_only) if len(result.get('lead',''))>210 else [result.get('lead','')]
    result['lead']=lead[0] if lead else ''
    blocks=[{'text':p,'kind':'paragraph','evidence':copy.deepcopy(result.get('lead_evidence',[])),
             'quote_text':'','quote_author':''} for p in lead[1:]]
    for block in result.get('blocks',[]):
        limit=208 if block.get('kind')=='bullet' else 210
        parts=pieces(block.get('text',''),limit,sentences_only=sentences_only) if len(block.get('text',''))>limit or '\n' in block.get('text','') else [block.get('text','')]
        if block.get('kind')=='quote' and len(parts)>1:
            # Keep a direct quote intact in details, with the same author and evidence.
            for part in parts:
                blocks.append({**copy.deepcopy(block),'text':part,'kind':'details','quote_text':'','quote_author':''})
        else:
            blocks.extend({**copy.deepcopy(block),'text':part} for part in parts)
    result['blocks']=blocks
    return result


def layout_version(a,b):
    """A collector layout change cannot turn an indexed archive into fresh news."""
    compact=lambda value:re.sub(r'\s+','',canonical(value))
    left=compact(body(a));right=compact(body(b))
    if not left or left!=right:return False
    first=compact(a.get('title',''));second=compact(b.get('title',''))
    return first==second or bool(first and second and left.startswith(first) and right.startswith(second))


def same_version(a,b):
    """Ignore punctuation, markup and grammatical copy edits, never changed facts."""
    if layout_version(a,b):return True
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
