"""One terminal short writer/checker; durable transport continuation, no re-planning."""
import copy
import re
import unicodedata
from datetime import datetime,timedelta,timezone
from . import model,store
from .formatting import validate,render,contains,normalized
from .language import russian_prose
from .source_text import text,editorial_source,canonical

SCHEMA="""
CREATE TABLE IF NOT EXISTS edition_short_work (
 document_id TEXT PRIMARY KEY REFERENCES edition_documents(document_id),
 bundle_hash TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 retry_after TEXT, candidate_json TEXT, writer_receipt_json TEXT, error TEXT, updated_at TEXT NOT NULL
);
"""


def enabled():return bool(model.bundle()[0].get('short_release'))


def align_evidence(draft,materials,group):
    """Restore source whitespace before punctuation; never replace words/signs."""
    result=copy.deepcopy(draft);known={m['material_id']:m for m in materials}
    arrays=[result.get('headline_evidence',[]),result.get('lead_evidence',[]),
            *(b.get('evidence',[]) for b in result.get('blocks',[]))]
    for proofs in arrays:
        for proof in proofs:
            material=known.get(proof.get('material_id'));quote=proof.get('quote')
            if not material or not isinstance(quote,str) or not quote:continue
            scopes=[f['scope'] for f in group.get('focus',[]) if f.get('material_id')==proof['material_id'] and f.get('scope')]
            from .source_text import saved_fields
            sources=scopes or list(saved_fields(material).values())
            if any(contains({'content':source},quote) for source in sources):continue
            value=re.sub(r'\s+([.,;:!?»])',r'\1',normalized(quote))
            pattern=''.join(r'\s*'+re.escape(c) if c in '.,;:!?»' else r'\s+' if c==' ' else re.escape(c) for c in value)
            match=next((match for source in sources if (match:=re.search(pattern,source,re.I))),None)
            if match:proof['quote']=match.group(0).strip()
    return result


def layout(draft):
    """Move only presentation markers/date position; retain numbers and wording."""
    from .source_text import formatted
    result=copy.deepcopy(draft)
    # Structured headlines are plain text; Telegram bold is added by render().
    head=result['headline'].strip()
    marked=re.fullmatch(r'(?P<emoji>[^\w\s]+\s+)?\*\*(?P<text>[^*]+)\*\*',head)
    if marked:result['headline']=(marked['emoji'] or '')+marked['text']
    words=result['headline'].strip().split()
    if len(words)>1 and words[0]=='₿' and words[1].casefold() in ('bitcoin','биткоин','биткойн','btc'):
        words[0]='🪙';result['headline']=' '.join(words)
    def emoji(value):
        letters=[c for c in value if ord(c) not in (0xfe0f,0x200d) and not 0x1f3fb<=ord(c)<=0x1f3ff]
        return bool(letters and all(unicodedata.category(c)=='So' for c in letters))
    if words and not emoji(words[0]) and emoji(words[-1]):
        result['headline']=' '.join([words[-1],*words[:-1]])
    for block in result['blocks']:
        if block.get('kind')=='bullet':block['text']=re.sub(r'^(?:➤\s*)+','',block['text'].lstrip())
    lead=result['lead'].strip()
    match=re.match(r'^(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+\d{4}(?:\s+года)?)?)\s+',lead,re.I)
    if match:
        date=match[1];rest=lead[match.end():]
        first=re.search(r'[.!?](?=\s+[А-ЯЁA-Z«]|$)',rest)
        position=first.start() if first else len(rest)
        result['lead']=rest[:1].upper()+rest[1:position]+' '+date+rest[position:]
    return formatted(result,sentences_only=True)


def selected(draft,verdict):
    verdict=copy.deepcopy(verdict)
    draft=verdict.get('final_draft',draft)
    flags=verdict.get('blocks_supported')
    if not isinstance(flags,list) or len(flags)!=len(draft['blocks']) or any(type(v)!=bool for v in flags):
        raise ValueError('EDITION_SHORT_CHECK_STRUCTURE')
    for issue in verdict.get('issues',[]):
        if not isinstance(issue,dict) or issue.get('field') not in ('headline','lead','block'):raise ValueError('EDITION_SHORT_CHECK_STRUCTURE')
        if issue['field']=='block':
            position=issue.get('block_index')
            if not isinstance(position,int) or not 0<=position<len(flags):raise ValueError('EDITION_SHORT_CHECK_STRUCTURE')
            flags[position]=False
        else:verdict[issue['field']+'_supported']=False
    if verdict.get('headline_supported') is not True or verdict.get('lead_supported') is not True:
        raise ValueError('EDITION_SHORT_MAIN_UNVERIFIED')
    result=copy.deepcopy(draft)
    result['blocks']=[b for b,passed in zip(result['blocks'],flags) if passed]
    return layout(result)


def finalized(candidate,verdict,materials,group):
    """Omit only nonessential optional prose with invalid source proof."""
    result=align_evidence(selected(candidate,verdict),materials,group)
    known={m['material_id']:m for m in materials};omitted=[]
    from .formatting import citations
    for block in list(result['blocks']):
        def valid(proof):
            material=known.get(proof.get('material_id'));quote=proof.get('quote')
            if not material or not contains(material,quote):return False
            scopes=[f['scope'] for f in group.get('focus',[]) if f.get('material_id')==material['material_id'] and f.get('scope')]
            return not scopes or any(contains({'content':scope},quote) for scope in scopes)
        if block.get('evidence') and all(valid(p) for p in block['evidence']):continue
        # Never silently remove conditions or qualifications from a release.
        if re.search(r'\b(?:не|без|но|лишь|только|если|пока|нельзя|при условии|ограничени\w*|лимит\w*|исключени\w*|запре\w*|доступ\w*|комисси\w*|срок\w*|требовани\w*|обязан\w*|квалифицирован\w*)\b',block['text'],re.I):continue
        remaining=[b for b in result['blocks'] if b is not block]
        candidate_result={**result,'blocks':remaining}
        covered={p.get('material_id') for p in citations(candidate_result)}
        if not set(group.get('material_ids',[]))<=covered:continue
        result=candidate_result;omitted.append({'code':'evidence','post_fragment':block['text'],
            'reason':'Необязательный пример исключён: его цитата не совпадает с выбранным исходным фрагментом.'})
    return result,omitted


def problems(draft,materials,group):
    code=validate(draft,materials,group)
    from .formatting import citations
    if not set(group.get('material_ids',[]))<={p.get('material_id') for p in citations(draft)}:
        code.append({'code':'coverage','reason':'Не отражён один из включённых материалов.'})
    # These are measurable requirements, including a finished headline and lead.
    if re.search(r'[…]|\.{3}|[:;,—-]\s*$',draft.get('headline','')):
        code.append({'code':'headline_meaning','reason':'Короткий заголовок должен быть законченным.'})
    for field in [draft.get('headline',''),draft.get('lead',''),*(b['text'] for b in draft.get('blocks',[]))]:
        if re.search(r'\b(?:по данным|по сообщению)\s+(?:издания|газеты|журнала|канала|одного из источников)\b',field,re.I):
            code.append({'code':'sources','reason':'Издание указывается в источнике внизу.'})
    return code


def reporting_fragment(value):
    """Offline prose must report an action, not a navigation or service label."""
    if len(value.split())<3:return False
    if re.search(r'\b(?:файлы? cookie|политик\w* приватности|пользуясь сайтом|подписывайтесь|все права защищены)\b',value,re.I):return False
    return bool(re.search(r'\b\w+(?:ил[аи]?|али|ет|ют|ут|ется|ятся|ены|лся|лась|л[аи]?|ёт)\b',value,re.I))


def extractive(group,materials):
    """Offline safety net: complete Russian source clauses, never a chopped article."""
    # A brand bundle can contain unrelated events. Verbatim snippets cannot
    # supply the editorial connections; resume the saved short checker instead.
    if len(set(group.get('_event_keys',[])))>1:return None
    from .fragments import index
    fragments=[]
    for m in materials:
        if m['material_id'] not in group['material_ids']:continue
        scopes=[f['scope'] for f in group.get('focus',[]) if f.get('material_id')==m['material_id'] and f.get('scope')]
        for fragment in index([m]):
            value=editorial_source(fragment['text'])
            if not value or value!=text(fragment['text']):continue
            value=re.sub(r'^[\s➤👉➡️🔸🔹⚡💲💰❗📌📲]+','',value)
            if not russian_prose(value):continue
            if scopes and not any(contains({'content':s},value) for s in scopes):continue
            if '\n' in value or len(value)>210 or re.search(r'https?://|\b(?:по данным|по сообщению)\b',value,re.I):continue
            # A clipped quotation can lose its author/modality. Offline release
            # uses reporting sentences; quoted opinions require the short editor.
            if any(mark in value for mark in ('«','»','“','”','"')):
                # A statutory document name is not an attributed opinion. Keep
                # its complete quoted name; reject every other quoted passage.
                named=re.match(r'^(?:Законопроект|Проект закона|Закон)\s+«[^«»]+»',value,re.I)
                if not named or any(mark in value[named.end():] for mark in ('«','»','“','”','"')):continue
            fragments.append((value,[{'material_id':m['material_id'],'quote':value}]))
    mixed_scope=any(len(re.findall(r'[➤🔸🔹]',f.get('scope','')))>=2 for f in group.get('focus',[]))
    def matching_entity(value):
        entities=group.get('entities',[]) if mixed_scope else []
        return not entities or any(canonical(entity) in canonical(value) for entity in entities if entity)
    for lead,proof in fragments:
        if not matching_entity(lead) or not reporting_fragment(lead):continue
        # Keep the actor, action, negation and modality. Drop only trailing detail
        # introduced after the event predicate; the full condition remains in lead.
        words=lead.split();head=lead
        if len(head)>100:
            # Only remove a document's explanatory relative clause from the
            # headline. The complete original stays in the lead and evidence;
            # the quoted title and the main predicate remain unchanged.
            head=re.sub(r'^(?P<name>(?:Законопроект|Проект закона|Закон)\s+«[^«»]+»),\s+который\s+[^,«»]+,\s+(?P<action>.+)$',
                        lambda m:m['name']+' '+m['action'],head,flags=re.I)
        if len(head)>100:
            matches=list(re.finditer(r'\s+(?:для|с комиссией|на сумму|стоимостью|в размере|среди|после|через|в (?:отраслевом )?обзоре|в интервью|в публикации|на конференции|на форуме)\b',head,re.I))
            head=next((head[:m.start()] for m in matches if 20<=m.start()<=100), '')
        if not head or len(head)>100 or len(head.split())<3:continue
        if re.match(r'^(?:\d|[➤👉➡])',head) or re.search(r'\b(?:если|при условии|пока не|только если)\b',lead,re.I):continue
        # A bare label or introductory passage is not a news headline.
        if not re.search(r'\b\w+(?:ил[аи]?|али|ет|ют|ут|ется|ятся|ены|лся|лась|л[аи]?|ёт)\b',head,re.I):continue
        head=re.sub(r'^[^\wА-ЯЁа-яё]+','',head).rstrip('.!?')
        if canonical(head)==canonical(lead.rstrip('.!?')):
            additional=next(((v,p) for v,p in fragments if canonical(v)!=canonical(lead) and matching_entity(v) and
                             not re.match(r'^\d',v) and len(v)<=190 and reporting_fragment(v)),None)
            if additional:lead,lead_proof=additional
            else:
                detail=re.search(r'\s+(?:для|на рынке)\b',head,re.I)
                if detail and 20<=detail.start()<=100 and len(head[:detail.start()].split())>=3:
                    head=head[:detail.start()];lead_proof=proof
                else:
                    # A source with one fact still gets a release: a complete
                    # actor/action headline, with the object/detail retained in lead.
                    # Only these intransitive predicates can safely stand alone.
                    predicate=re.search(r'\b(?:подорожал[аи]?|подешевел[аи]?|вырос(?:ла|ли)?|'
                                        r'упал[аи]?|снизил(?:ась|ись|ся)|опустил(?:ась|ись|ся)|'
                                        r'повысил(?:ась|ись|ся)|предупредил[аи]?|пояснил[аи]?|'
                                        r'рассказал[аи]?|высказал(?:ась|ись|ся))\b',head,re.I)
                    if not predicate or not 12<=predicate.end()<len(head):continue
                    head=head[:predicate.end()];lead_proof=proof
        else:lead_proof=proof
        result={'headline':'📰 '+head,'headline_evidence':proof,'lead':lead,'lead_evidence':lead_proof,'blocks':[]}
        covered={p['material_id'] for p in [*proof,*lead_proof]}
        for mid in group['material_ids']:
            if mid in covered:continue
            additional=next(((v,p) for v,p in fragments if p[0]['material_id']==mid and len(v)<=190 and reporting_fragment(v)),None)
            if not additional:break
            value,evidence=additional
            result['blocks'].append({'text':value,'kind':'bullet','evidence':evidence,'quote_text':'','quote_author':''})
            covered.add(mid)
        if covered!=set(group['material_ids']):continue
        retained={canonical(lead),canonical(head),*(canonical(b['text']) for b in result['blocks'])}
        for value,evidence in fragments:
            if canonical(value) in retained:continue
            if re.search(r'\b(?:только|если|при условии|лимит|ограничени|неквалифицирован|квалифицирован)\w*',value,re.I):
                result['blocks'].append({'text':value,'kind':'paragraph','evidence':evidence,'quote_text':'','quote_author':''})
                retained.add(canonical(value))
        if not problems(result,materials,group):return result
    return None


def save(db,document,draft,materials,receipt,review,reason):
    rendered,plain=render(draft,materials);digest=model.bundle()[2]
    reasons=store.read(document['reasons_json'],[])
    if reason and reason not in reasons:reasons.append(reason)
    reasons.append({'code':'short_release','reason':'Подготовлена короткая версия без нового полного редакторского цикла.'})
    db.execute('INSERT INTO edition_checks(document_id,draft_hash,rendered_hash,bundle_hash,evidence_hash,code_json,review_json,response_json,passed,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)',
        (document['document_id'],store.sha(draft),store.sha(rendered),digest,store.sha(materials),'[]',store.encoded(review),store.encoded(receipt),store.stamp()))
    db.execute("UPDATE edition_documents SET draft_json=?,rendered_text=?,plain_text=?,state='READY',reasons_json=?,updated_at=? WHERE document_id=?",
        (store.encoded(draft),rendered,plain,store.encoded(reasons),store.stamp(),document['document_id']))
    db.execute("UPDATE edition_short_work SET status='READY',candidate_json=?,error=NULL,updated_at=? WHERE document_id=?",(store.encoded(draft),store.stamp(),document['document_id']))
    db.execute('DELETE FROM edition_translation_waits WHERE document_id=?',(document['document_id'],));db.commit()
    store.event(db,document['job_id'],'release',{'mode':review['release_mode'],'reasons':reasons},document['document_id'])


def prepare(db,document,settings,reason=None):
    from .freshness import protect_document
    if not protect_document(db,document):return
    db.executescript(SCHEMA)
    digest=model.bundle()[2];docid=document['document_id']
    state=db.execute('SELECT state FROM edition_documents WHERE document_id=?',(docid,)).fetchone()
    if not state or state[0] in ('PUBLISHED','UNKNOWN','SENDING','FILTERED','DUPLICATE','COVERED'):return
    current=db.execute('SELECT * FROM edition_short_work WHERE document_id=?',(docid,)).fetchone()
    if current and current['bundle_hash']==digest and current['status']=='READY':return
    if current and current['bundle_hash']==digest and current['status']=='WAITING':
        # A completed negative decision cannot improve by replaying saved input.
        # NULL marks source recovery requiring an explicit change/resume.
        if current['retry_after'] is None or current['retry_after']>store.stamp():return
    group=store.read(document['group_json'],{});materials=store.read(document['materials_json'],[])
    from .fallback import source_group
    group=source_group(db,group,materials)
    db.execute('UPDATE edition_documents SET group_json=? WHERE document_id=?',(store.encoded(group),docid))
    db.execute("INSERT INTO edition_short_work(document_id,bundle_hash,status,attempts,updated_at) VALUES(?,?,'WRITING',1,?) ON CONFLICT(document_id) DO UPDATE SET bundle_hash=excluded.bundle_hash,status='WRITING',attempts=edition_short_work.attempts+1,updated_at=excluded.updated_at",(docid,digest,store.stamp()));db.commit()
    try:
        cached=current and current['bundle_hash']==digest and current['candidate_json'] and current['status'] in ('CHECKING','WAITING')
        if current and current['bundle_hash']==digest and current['error']=='SOURCE_ONLY' and not cached:
            raise ValueError('EDITION_SHORT_MAIN_UNVERIFIED')
        if cached:
            candidate=store.read(current['candidate_json'],{});writer=store.read(current['writer_receipt_json'],{})
        else:
            candidate,writer=model.short_draft(group,materials,settings)
            store.event(db,document['job_id'],'short_draft',{'draft':candidate,'receipt':writer},docid)
            db.execute("UPDATE edition_short_work SET status='CHECKING',candidate_json=?,writer_receipt_json=?,updated_at=? WHERE document_id=?",(store.encoded(candidate),store.encoded(writer),store.stamp(),docid));db.commit()
        from . import publication_history
        cached_check=None
        for row in db.execute("SELECT payload_json FROM edition_events WHERE document_id=? AND stage='short_check' ORDER BY event_id DESC",(docid,)):
            response=store.read(row[0],{})
            if response.get('candidate_hash')==store.sha(candidate) and response.get('receipt',{}).get('bundle_hash')==digest:
                cached_check=response;break
        if cached_check:
            verdict=cached_check['verdict'];checker=cached_check['receipt'];history=cached_check.get('history',[])
        else:
            if current and current['bundle_hash']==digest and current['error']=='SOURCE_ONLY':
                raise ValueError('EDITION_SHORT_MAIN_UNVERIFIED')
            history=publication_history.context(db,document,candidate,group)
            verdict,checker=model.short_check(candidate,materials,group,settings,publication_history=history)
            store.event(db,document['job_id'],'short_check',{'verdict':verdict,'receipt':checker,
                'candidate_hash':store.sha(candidate),'history':history},docid)
        duplicate=publication_history.confirmed(verdict,history,materials,group)
        if duplicate:
            publication_history.cover(db,document,duplicate)
            db.execute("UPDATE edition_short_work SET status='DUPLICATE',updated_at=? WHERE document_id=?",(store.stamp(),docid));db.commit()
            return
        draft,omitted=finalized(candidate,verdict,materials,group)
        update_issues=publication_history.update_issues(verdict,history,materials,group,draft)
        if update_issues:
            store.event(db,document['job_id'],'publication_update_invalid',{'issues':update_issues},docid)
            # A failed novelty proof is uncertainty, not a confirmed duplicate
            # or a reason to repeat the terminal checker. Preserve release.
            verdict={**verdict,'publication_history':{**verdict['publication_history'],
                'decision':'uncertain','updates':[]}}
        if omitted:store.event(db,document['job_id'],'short_optional_omitted',{'issues':omitted},docid)
        issues=problems(draft,materials,group)
        if any(i['code'] in ('length','telegram_length') for i in issues):raise ValueError('EDITION_FALLBACK_NEEDS_SPLIT')
        if issues:
            store.event(db,document['job_id'],'short_validation',{'issues':issues},docid)
            raise ValueError('EDITION_SHORT_FORMAT')
        proof={'kind':'short_release','candidate':candidate,'verdict':verdict,'writer_receipt':writer,'check_receipt':checker}
        save(db,document,draft,materials,proof,{'release_mode':'SHORT','approved':True,'issues':[]},reason)
        return
    except Exception as exc:
        code=getattr(exc,'code',None) or (str(exc) if str(exc).startswith('EDITION_') else type(exc).__name__)
        if code=='EDITION_FALLBACK_NEEDS_SPLIT':raise
        previous=store.read(document['reasons_json'],[])
        if not previous or previous[-1].get('code')!=code:
            store.event(db,document['job_id'],'short_error',{'code':code},docid)
        draft=extractive(group,materials)
        if draft:
            save(db,document,draft,materials,{'kind':'short_source','group_hash':store.sha(group)},
                 {'release_mode':'SHORT_SOURCE','approved':True,'issues':[]},reason or {'code':code,'reason':'Короткий AI-выпуск недоступен; использованы законченные подтверждённые фразы.'})
            return
        # Continue only the terminal stage. An unverified candidate is never
        # sent; transport outages resume the already saved short candidate.
        next_at=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat(timespec='microseconds')
        # A substantive verdict is final. Only source-based recovery remains;
        # the same rejected news never starts another writing/checking cycle.
        error='SOURCE_ONLY' if code in ('EDITION_SHORT_MAIN_UNVERIFIED','EDITION_SHORT_FORMAT') else code
        if error=='SOURCE_ONLY':next_at=None
        db.execute("UPDATE edition_short_work SET status='WAITING',retry_after=?,error=?,updated_at=? WHERE document_id=?",(next_at,error,store.stamp(),docid))
        reasons=previous
        waiting={'code':code,'reason':('Автоматический повтор остановлен: сохранённый короткий текст не прошёл проверку, резерв по исходнику недоступен.' if error=='SOURCE_ONLY' else 'Ожидается только короткий русский выпуск; полная редактура не перезапускается.')}
        if waiting not in reasons:reasons.append(waiting)
        db.execute("UPDATE edition_documents SET state='SHORT_PENDING',reasons_json=?,updated_at=? WHERE document_id=?",(store.encoded(reasons),store.stamp(),docid));db.commit()


def verify(draft,receipt,materials,group):
    if problems(draft,materials,group):raise ValueError('EDITION_SHORT_FORMAT')
    if receipt.get('kind')=='short_source':
        if receipt.get('group_hash')!=store.sha(group) or draft!=extractive(group,materials):raise ValueError('EDITION_SHORT_SOURCE_CHANGED')
    elif receipt.get('kind')=='short_release':
        if not receipt.get('writer_receipt',{}).get('response_id') or not receipt.get('check_receipt',{}).get('response_id'):
            raise ValueError('EDITION_SHORT_RECEIPT_MISSING')
        if any(receipt[key].get('bundle_hash')!=model.bundle()[2] for key in ('writer_receipt','check_receipt')):
            raise ValueError('EDITION_SHORT_RULES_CHANGED')
        if draft!=finalized(receipt.get('candidate',{}),receipt.get('verdict',{}),materials,group)[0]:raise ValueError('EDITION_SHORT_CHECK_CHANGED')
    else:raise ValueError('EDITION_SHORT_RECEIPT_MISSING')
