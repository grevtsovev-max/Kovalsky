"""Conservative presentation fixes before the independent factual review."""
import copy
import re
import time
import unicodedata
from . import store
from .formatting import normalize, validate

UPDATE='layout-review-v1'


def enabled(settings):
    return settings.get('_edition_config',{}).get('editorial',{}).get('layout_review_enabled',True) is not False


def parts(value,limit):
    # Never split a token, rewrite punctuation, or interpret markup from a source.
    result=[]
    for paragraph in value.splitlines() or [value]:
        paragraph=paragraph.strip()
        while len(paragraph)>limit:
            sentences=[m.start() for m in re.finditer(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z«])',paragraph) if 0<m.start()<=limit]
            # A word boundary is not a complete thought. Leave a long sentence
            # to the finite editorial repair rather than publishing its tail.
            if not sentences:return None
            cut=sentences[-1]
            result.append(paragraph[:cut]);paragraph=paragraph[cut:].lstrip()
        if paragraph:result.append(paragraph)
    return result


def apply(draft):
    result=normalize(draft)
    words=result.get('headline','').split()
    def emoji(value):
        chars=[c for c in value if ord(c) not in (0xfe0f,0x200d) and not 0x1f3fb<=ord(c)<=0x1f3ff]
        return bool(chars and all(unicodedata.category(c)=='So' for c in chars))
    # Only relocate an existing unique emoji. Do not invent a topic marker.
    if words and sum(emoji(w) for w in words)==1 and emoji(words[-1]) and not emoji(words[0]):
        result['headline']=' '.join([words[-1],*words[:-1]])
    if '\n' in result.get('headline',''):
        result['headline']=' '.join(result['headline'].split())
    extra=[]
    lead=result.get('lead','')
    # Preserve the complete first sentence; an overlong single sentence still
    # needs the editor. Quotes in a lead are never touched.
    if len(lead)>210 and not re.search('[«»“”"]',lead):
        sentences=re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z«])',lead)
        if len(sentences)>1 and len(sentences[0])<=210:
            remaining=parts(' '.join(sentences[1:]),210)
            if remaining is not None:
                result['lead']=sentences[0]
                extra=[{'text':p,'kind':'paragraph','evidence':copy.deepcopy(result.get('lead_evidence',[])),
                        'quote_text':'','quote_author':''} for p in remaining]
    blocks=[]
    for original in result.get('blocks',[]):
        block=copy.deepcopy(original)
        # Retain direct quotes and author metadata byte for byte.
        if block.get('quote_text') or block.get('kind')=='quote' or re.search('[«»“”"]',block.get('text','')):
            blocks.append(block);continue
        if block.get('kind')=='bullet':block['text']=re.sub(r'^(?:➤\s*)+','',block['text'])
        split=parts(block.get('text',''),208 if block.get('kind')=='bullet' else 210)
        if split is None:blocks.append(block)
        else:blocks.extend({**copy.deepcopy(block),'text':p} for p in split)
    result['blocks']=extra+blocks
    return result


def prepare(db,document,draft,materials,group,settings):
    started=time.perf_counter();before=validate(draft,materials,group)
    fixed=apply(draft) if enabled(settings) else copy.deepcopy(draft)
    after=validate(fixed,materials,group)
    # Pure layout may not introduce a new admission failure.
    if {i['code'] for i in after}-{i['code'] for i in before}:fixed=copy.deepcopy(draft);after=before
    payload={'update':UPDATE,'enabled':enabled(settings),'changed':fixed!=draft,
             'before_hash':store.sha(draft),'after_hash':store.sha(fixed),
             'before_codes':[i['code'] for i in before],'after_codes':[i['code'] for i in after],
             'removed_issues':len(before)-len(after),'layout_seconds':time.perf_counter()-started}
    if fixed!=draft:
        from .formatting import render
        rendered,plain=render(fixed,materials)
        db.execute('UPDATE edition_documents SET draft_json=?,rendered_text=?,plain_text=?,updated_at=? WHERE document_id=?',
                   (store.encoded(fixed),rendered,plain,store.stamp(),document['document_id']))
        db.commit()
        # The complete previous draft is already in the draft/repair event.
        payload['draft']=fixed
    store.event(db,document['job_id'],'layout_review',payload,document['document_id'])
    return fixed,after
