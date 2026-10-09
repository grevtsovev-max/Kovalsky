"""Finite release paths: editorial warnings never strand an admitted material."""
from __future__ import annotations
import copy
import uuid
from . import model,store
from .formatting import render,validate
from .source_text import body,text,pieces,canonical,formatted

FACT_ERRORS={'facts','evidence','quotes','numbers','status','conflicts','main_conflict','unsupported_fact','new_clarification','scope','structure','telegram_length','sources','headline_length','headline_emoji','length'}


def can_release(issues):
    return not any(i.get('code') in FACT_ERRORS for i in issues)


def prefix(text,limit):
    text=str(text or '').strip()
    if len(text)<=limit:return text
    part=text[:max(1,limit-1)]
    if ' ' in part:part=part.rsplit(' ',1)[0]
    return part.rstrip()+'…'


def source_group(db,group,materials):
    """Retain topical digest sections on emergency release using the saved authority."""
    if any(f.get('scope') for f in group.get('focus',[])):return group
    from ..topic_registry import attach_cached
    from ..keyword_filter import evaluate
    from .source_text import sections
    path=db.execute('PRAGMA database_list').fetchone()[2]
    config={'newsroom':{'database':path},'ai':{}}
    attach_cached(config)
    spec=config['ai'].get('_keyword_prefilter')
    if not spec:return group
    focus=[]
    for m in materials:
        if m['material_id'] not in group['material_ids']:continue
        selected=[s for s in sections(body(m)) if evaluate(s,spec,s)['passed']]
        if selected:
            scope='\n'.join(selected)
            focus.append({'material_id':m['material_id'],'excerpt':selected[0],
                          'summary':'Профильный фрагмент сохранённого источника.','scope':scope})
    return {**group,'focus':focus} if focus else group


def source_draft(group,materials):
    sources=[m for m in materials if m['material_id'] in group['material_ids']]
    if not sources:raise ValueError('EDITION_FALLBACK_SOURCE_MISSING')
    first=sources[0]
    title=text(first.get('title') or group.get('subject') or 'Сообщение о крипторынке').splitlines()[0]
    import re
    import unicodedata
    title=''.join(c for c in title if unicodedata.category(c)!='So' and ord(c) not in (0xfe0f,0x200d))
    title=re.sub(r'^[^\wА-Яа-яЁё]+','',title)
    title=re.split(r'(?<=[.!?])\s+',title)[0]
    headline='📰 '+prefix(title,108)
    blocks=[]
    seen=set();words=0
    for m in sources:
        scopes=[f['scope'] for f in group.get('focus',[]) if f.get('material_id')==m['material_id'] and f.get('scope')]
        original='\n'.join(scopes) if scopes else body(m)
        original=re.split(r'(?:#\w+\s*){2,}|Подписывайтесь|Подписаться|РОСКРИПТО\.РФ\s*\|',original,flags=re.I)[0].strip()
        retained=[]
        for excerpt in pieces(original):
            key=canonical(excerpt)
            if key in seen or words+len(excerpt.split())>220:continue
            if key==canonical(title):continue
            seen.add(key);words+=len(excerpt.split())
            retained.append({'text':excerpt,'kind':'paragraph' if not blocks and not retained else 'details',
                'evidence':[{'material_id':m['material_id'],'quote':excerpt}],
                'quote_text':'','quote_author':''})
            if len(retained)>=6:break
        if not retained:
            excerpt=pieces(body(m))[0]
            retained=[{'text':excerpt,'kind':'paragraph','evidence':[{'material_id':m['material_id'],'quote':excerpt}],
                       'quote_text':'','quote_author':''}]
        blocks.extend(retained)
    lead=blocks.pop(0)
    return {'headline':headline,'headline_evidence':copy.deepcopy(lead['evidence']),
            'lead':lead['text'],'lead_evidence':lead['evidence'],'blocks':blocks}


def release(db,document,*,source=False,reason=None):
    group=store.read(document['group_json'],{})
    materials=store.read(document['materials_json'],[])
    if source:
        group=source_group(db,group,materials)
        db.execute('UPDATE edition_documents SET group_json=? WHERE document_id=?',(store.encoded(group),document['document_id']))
    prior=db.execute('SELECT * FROM edition_checks WHERE document_id=? ORDER BY check_id DESC LIMIT 1',(document['document_id'],)).fetchone()
    draft=source_draft(group,materials) if source else formatted(store.read(document['draft_json'],{}))
    rendered,plain=render(draft,materials)
    if len(plain.encode('utf-16-le'))//2>4096:
        # A large bundle's sources alone may exceed Telegram. Keep every material in its own release.
        raise ValueError('EDITION_FALLBACK_NEEDS_SPLIT')
    code=validate(draft,materials,group)
    if source and any(i['code'] in ('length','telegram_length') for i in code):
        raise ValueError('EDITION_FALLBACK_NEEDS_SPLIT')
    review=store.read(prior['review_json'],{}) if prior and not source else {'approved':False,'issues':[]}
    review={**review,'release_mode':'SOURCE' if source else 'ADVISORY'}
    receipt=store.read(prior['response_json'],{}) if prior and not source else {'kind':'source_fallback'}
    reasons=store.read(document['reasons_json'],[])
    if reason:reasons.append(reason)
    reasons.append({'code':'source_fallback' if source else 'released_with_warnings',
        'reason':'Выпущен фрагмент исходного материала; редакторские замечания сохранены.' if source else 'Выпущено после одной доработки с сохранёнными замечаниями.'})
    db.execute('INSERT INTO edition_checks(document_id,draft_hash,rendered_hash,bundle_hash,evidence_hash,code_json,review_json,response_json,passed,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)',
        (document['document_id'],store.sha(draft),store.sha(rendered),model.bundle()[2],store.sha(materials),store.encoded(code),store.encoded(review),store.encoded(receipt),store.stamp()))
    db.execute("UPDATE edition_documents SET draft_json=?,rendered_text=?,plain_text=?,state='READY',reasons_json=?,updated_at=? WHERE document_id=?",
        (store.encoded(draft),rendered,plain,store.encoded(reasons),store.stamp(),document['document_id']));db.commit()
    store.event(db,document['job_id'],'release',{'mode':review['release_mode'],'reasons':reasons},document['document_id'])


def ensure_job(db,job_id,reason):
    job=db.execute('SELECT * FROM edition_jobs WHERE job_id=?',(job_id,)).fetchone()
    inputs=store.materials(db,store.read(job['material_ids_json'],[]))
    docs=db.execute('SELECT * FROM edition_documents WHERE job_id=?',(job_id,)).fetchall()
    covered={i for d in docs for i in store.read(d['group_json'],{}).get('material_ids',[])}
    excluded=store.read(job['reasons_json'],[])
    filtered={r['material_id'] for r in excluded if r.get('kind')=='out_of_scope' and r.get('material_id')}
    covered.update(filtered)
    groups=store.read(job['groups_json'],[])
    for m in inputs:
        if m['material_id'] in covered:continue
        group=next((copy.deepcopy(g) for g in groups if m['material_id'] in g['material_ids']),None)
        if group:group['material_ids']=[i for i in group['material_ids'] if i not in covered]
        else:group={'subject':m.get('title') or 'Сообщение','entities':[],'event_key':'','material_ids':[m['material_id']],'focus':[],'lookup_query':''}
        selected=[m for m in inputs if m['material_id'] in group['material_ids']]
        now=store.stamp();docid=uuid.uuid4().hex
        db.execute('INSERT INTO edition_documents(document_id,job_id,group_json,materials_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',
            (docid,job_id,store.encoded(group),store.encoded(selected),now,now));db.commit();covered.update(group['material_ids'])
    for doc in db.execute("SELECT * FROM edition_documents WHERE job_id=? AND state IN ('DRAFTING','CHECKING','INCOMPLETE')",(job_id,)).fetchall():
        try:release(db,doc,source=True,reason=reason)
        except ValueError as exc:
            if str(exc)!='EDITION_FALLBACK_NEEDS_SPLIT':raise
            group=store.read(doc['group_json'],{});selected=[m for m in store.read(doc['materials_json'],[]) if m['material_id'] in group['material_ids']]
            # Reuse the first document; additional documents have independent delivery identities.
            for n,m in enumerate(selected):
                sub={**group,'material_ids':[m['material_id']]};now=store.stamp();docid=doc['document_id'] if n==0 else uuid.uuid4().hex
                if n==0:db.execute('UPDATE edition_documents SET group_json=?,materials_json=? WHERE document_id=?',(store.encoded(sub),store.encoded([m]),docid))
                else:db.execute('INSERT INTO edition_documents(document_id,job_id,group_json,materials_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',(docid,job_id,store.encoded(sub),store.encoded([m]),now,now))
                db.commit();release(db,db.execute('SELECT * FROM edition_documents WHERE document_id=?',(docid,)).fetchone(),source=True,reason=reason)
    states=[r[0] for r in db.execute('SELECT state FROM edition_documents WHERE job_id=?',(job_id,))]
    state='FILTERED' if not states and filtered else 'PUBLISHED' if states and all(s=='PUBLISHED' for s in states) else 'READY' if all(s in ('READY','PUBLISHED') for s in states) else 'INCOMPLETE'
    store.finish(db,job_id,state,[*excluded,reason])
    db.executemany("UPDATE edition_materials SET queue_state='DONE' WHERE material_id=?",[(m['material_id'],) for m in inputs]);db.commit()
