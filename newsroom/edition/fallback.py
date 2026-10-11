"""Finite release paths: editorial warnings never strand an admitted material."""
from __future__ import annotations
import copy
import uuid
from . import model,store
from .formatting import render,validate
from .source_text import body,text,pieces,canonical,formatted,editorial_source

FACT_ERRORS={'language','facts','evidence','quotes','numbers','status','conflicts','main_conflict','unsupported_fact','new_clarification','scope','structure','telegram_length','sources','headline_length','headline_emoji','length'}


def can_release(issues):
    from .short_release import enabled
    if enabled():return not any(i.get('code')!='paragraph' for i in issues)
    return not any(i.get('code') in FACT_ERRORS for i in issues)


def prefix(text,limit):
    text=str(text or '').strip()
    if len(text)<=limit:return text
    part=text[:max(1,limit-1)]
    if ' ' in part:part=part.rsplit(' ',1)[0]
    return part.rstrip()+'…'


def source_group(db,group,materials):
    from .source_text import expand_link_preview
    group=expand_link_preview(group,materials)
    """Retain topical digest sections on emergency release using the saved authority."""
    if any(f.get('scope') for f in group.get('focus',[])):return group
    semantic=[]
    for m in materials:
        if m['material_id'] not in group['material_ids']:continue
        row=db.execute('SELECT result_json FROM edition_semantic_decisions WHERE material_id=? AND bundle_hash=?',(m['material_id'],model.bundle()[2])).fetchone()
        decision=store.read(row[0],{}) if row else {}
        if decision.get('decision')=='allow':
            semantic.extend({'material_id':m['material_id'],'excerpt':e['quote'],'scope':e['quote'],
                             'summary':'Допустимое событие первого смыслового отбора.'} for e in decision.get('evidence',[]))
    if semantic and {f['material_id'] for f in semantic}==set(group['material_ids']):return {**group,'focus':semantic}
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
    scoped=any(f.get('scope') for f in group.get('focus',[]))
    focus=next((f for f in group.get('focus',[]) if f.get('material_id')==first['material_id'] and f.get('scope')),None)
    # The emergency headline comes from the source, never a generated subject.
    title=text((editorial_source(focus['scope']) if scoped and focus else first.get('title')) or first.get('title') or 'Сообщение о крипторынке').splitlines()[0]
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
        original=editorial_source(original)
        retained=[]
        for excerpt in pieces(original,209):
            key=canonical(excerpt)
            if key in seen or words+len(excerpt.split())>220:continue
            if key==canonical(title) and not scopes:continue
            seen.add(key);words+=len(excerpt.split())
            retained.append({'text':excerpt,'kind':'paragraph' if not blocks and not retained else 'details',
                'evidence':[{'material_id':m['material_id'],'quote':excerpt}],
                'quote_text':'','quote_author':''})
            if len(retained)>=6:break
        if not retained:
            cleaned=editorial_source(body(m))
            excerpt=pieces(cleaned or text(m.get('title')) or group.get('subject') or 'Сообщение источника.',209)[0]
            retained=[{'text':excerpt,'kind':'paragraph','evidence':[{'material_id':m['material_id'],'quote':excerpt}],
                       'quote_text':'','quote_author':''}]
        blocks.extend(retained)
    # A shortened source release is visibly an excerpt; proof remains verbatim.
    used=' '.join(b['evidence'][0]['quote'] for b in blocks)
    original=' '.join(body(m) for m in sources)
    if canonical(used)!=canonical(original):blocks[-1]['text']+='…'
    lead=blocks.pop(0)
    return {'headline':headline,'headline_evidence':copy.deepcopy(lead['evidence']),
            'lead':lead['text'],'lead_evidence':lead['evidence'],'blocks':blocks}


def release(db,document,*,source=False,reason=None,settings=None):
    from .freshness import protect_document
    if not protect_document(db,document):return
    from .short_release import enabled,prepare
    if source and enabled():
        return prepare(db,document,settings or {},reason)
    current=db.execute('SELECT state FROM edition_documents WHERE document_id=?',(document['document_id'],)).fetchone()
    if current and current[0] in ('DUPLICATE','FILTERED','PUBLISHED','UNKNOWN','COVERED'):return
    group=store.read(document['group_json'],{})
    materials=store.read(document['materials_json'],[])
    if source:
        group=source_group(db,group,materials)
        db.execute('UPDATE edition_documents SET group_json=? WHERE document_id=?',(store.encoded(group),document['document_id']))
    prior=db.execute('SELECT * FROM edition_checks WHERE document_id=? ORDER BY check_id DESC LIMIT 1',(document['document_id'],)).fetchone()
    draft=source_draft(group,materials) if source else formatted(store.read(document['draft_json'],{}),sentences_only=enabled())
    if not source and enabled() and validate(draft,materials,group):
        # Never turn a long sentence into disconnected paragraph fragments.
        return prepare(db,document,settings or {},reason or {'code':'paragraph','reason':'Длинное предложение требует короткой редакторской версии.'})
    if source:
        _,original_plain=render(draft,materials)
        if len(original_plain.encode('utf-16-le'))//2>4096 or len(original_plain.split())>model.bundle()[0]['limits']['word_max']:
            raise ValueError('EDITION_FALLBACK_NEEDS_SPLIT')
    translation_receipt=None
    from .language import issues as language_issues
    if language_issues(draft):
        from .source_translation import translate
        # Failed writer prose is replaced by evidence before translation.
        if not source:
            source=True;group=source_group(db,group,materials);draft=source_draft(group,materials)
            db.execute('UPDATE edition_documents SET group_json=? WHERE document_id=?',(store.encoded(group),document['document_id']))
        draft,translation_receipt=translate(db,document,draft,materials,group,settings)
        if draft is None:return
    rendered,plain=render(draft,materials)
    if len(plain.encode('utf-16-le'))//2>4096:
        # A large bundle's sources alone may exceed Telegram. Keep every material in its own release.
        raise ValueError('EDITION_FALLBACK_NEEDS_SPLIT')
    code=validate(draft,materials,group)
    if source and any(i['code'] in ('length','telegram_length') for i in code):
        raise ValueError('EDITION_FALLBACK_NEEDS_SPLIT')
    review=store.read(prior['review_json'],{}) if prior and not source else {'approved':False,'issues':[]}
    review={**review,'release_mode':'TRANSLATED_SOURCE' if translation_receipt else 'SOURCE' if source else 'ADVISORY'}
    receipt=translation_receipt or (store.read(prior['response_json'],{}) if prior and not source else {'kind':'source_fallback'})
    reasons=store.read(document['reasons_json'],[])
    if reason:reasons.append(reason)
    reasons.append({'code':'source_fallback' if source else 'released_with_warnings',
        'reason':'Выпущен фрагмент исходного материала; редакторские замечания сохранены.' if source else 'Выпущено после одной доработки с сохранёнными замечаниями.'})
    db.execute('INSERT INTO edition_checks(document_id,draft_hash,rendered_hash,bundle_hash,evidence_hash,code_json,review_json,response_json,passed,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)',
        (document['document_id'],store.sha(draft),store.sha(rendered),model.bundle()[2],store.sha(materials),store.encoded(code),store.encoded(review),store.encoded(receipt),store.stamp()))
    db.execute("UPDATE edition_documents SET draft_json=?,rendered_text=?,plain_text=?,state='READY',reasons_json=?,updated_at=? WHERE document_id=?",
        (store.encoded(draft),rendered,plain,store.encoded(reasons),store.stamp(),document['document_id']));db.commit()
    store.event(db,document['job_id'],'release',{'mode':review['release_mode'],'reasons':reasons},document['document_id'])


def ensure_job(db,job_id,reason,settings=None,*,document_ids=None):
    job=db.execute('SELECT * FROM edition_jobs WHERE job_id=?',(job_id,)).fetchone()
    inputs=store.materials(db,store.read(job['material_ids_json'],[]))
    from .vacancies import protect_recovery
    inputs=[protect_recovery(db,m) for m in inputs]
    inputs=[m for m in inputs if m is not None]
    docs=db.execute('SELECT * FROM edition_documents WHERE job_id=?',(job_id,)).fetchall()
    covered={i for d in docs for i in store.read(d['group_json'],{}).get('material_ids',[])}
    from .freshness import admit
    inputs,date_excluded=admit(db,inputs)
    excluded=store.read(job['reasons_json'],[])+date_excluded
    filtered={r['material_id'] for r in excluded if r.get('kind')=='out_of_scope' and r.get('material_id')}
    for m in inputs:
        decision=db.execute('SELECT result_json FROM edition_semantic_decisions WHERE material_id=? AND bundle_hash=?',(m['material_id'],job['bundle_hash'])).fetchone()
        entry=store.read(decision[0],{}) if decision else {}
        if entry.get('decision')=='reject':
            filtered.add(m['material_id'])
            if not any(e.get('material_id')==m['material_id'] and e.get('kind')=='out_of_scope' for e in excluded):
                excluded.append({'material_id':m['material_id'],'kind':'out_of_scope','code':entry.get('rule_code','onchain_noise'),'reason':entry['reason'],'evidence':entry.get('evidence',[])})
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
        if document_ids is not None and doc['document_id'] not in document_ids:continue
        ids=set(store.read(doc['group_json'],{}).get('material_ids',[]))
        if ids and ids<=filtered:
            db.execute("UPDATE edition_documents SET state='FILTERED',reasons_json=?,updated_at=? WHERE document_id=?",(store.encoded([e for e in excluded if e.get('material_id') in ids]),store.stamp(),doc['document_id']));db.commit()
            continue
        try:release(db,doc,source=True,reason=reason,settings=settings)
        except ValueError as exc:
            if str(exc)!='EDITION_FALLBACK_NEEDS_SPLIT':raise
            group=store.read(doc['group_json'],{});selected=[m for m in store.read(doc['materials_json'],[]) if m['material_id'] in group['material_ids']]
            # Reuse the first document; additional documents have independent delivery identities.
            for n,m in enumerate(selected):
                sub={**group,'material_ids':[m['material_id']],
                     'focus':[f for f in group.get('focus',[]) if f.get('material_id')==m['material_id']],
                     'topic_evidence':[e for e in group.get('topic_evidence',[]) if e.get('material_id')==m['material_id']]}
                now=store.stamp();docid=doc['document_id'] if n==0 else uuid.uuid4().hex
                if n==0:db.execute('UPDATE edition_documents SET group_json=?,materials_json=? WHERE document_id=?',(store.encoded(sub),store.encoded([m]),docid))
                else:db.execute('INSERT INTO edition_documents(document_id,job_id,group_json,materials_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',(docid,job_id,store.encoded(sub),store.encoded([m]),now,now))
                db.commit();release(db,db.execute('SELECT * FROM edition_documents WHERE document_id=?',(docid,)).fetchone(),source=True,reason=reason,settings=settings)
    states=store.document_states(db,job_id)
    state='FILTERED' if (not states and filtered) or states and all(s=='FILTERED' for s in states) else 'PUBLISHED' if states and all(s in ('PUBLISHED','FILTERED') for s in states) else 'READY' if all(s in ('READY','PUBLISHED','FILTERED') for s in states) else 'INCOMPLETE'
    store.finish(db,job_id,state,[*excluded,reason])
    db.executemany("UPDATE edition_materials SET queue_state='DONE' WHERE material_id=? AND queue_state<>'QUEUED'",[(m['material_id'],) for m in inputs]);db.commit()
