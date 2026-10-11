"""Verified, cached translations of source releases; failures never emit English."""
import copy
from datetime import datetime,timedelta,timezone
from . import model,store
from .formatting import validate
from .source_text import formatted


def translate(db,document,original,materials,group,settings):
    key=store.sha(original);digest=model.bundle()[2]
    cached=db.execute('SELECT draft_json,receipt_json FROM edition_source_translations WHERE source_hash=? AND bundle_hash=?',(key,digest)).fetchone()
    if cached:return store.read(cached[0],{}),store.read(cached[1],{})
    feedback=None
    try:
        if settings is None:raise model.AIResponseError('EDITION_TRANSLATION_SETTINGS_MISSING')
        for attempt in range(2):
            result,receipt=model.translate_source(original,settings,feedback=feedback)
            store.event(db,document['job_id'],'source_translation_response',{'result':result,'receipt':receipt,'source_hash':key,'attempt':attempt},document['document_id'])
            if not isinstance(result.get('headline'),str) or not isinstance(result.get('lead'),str) or not isinstance(result.get('blocks'),list) or len(result['blocks'])!=len(original['blocks']) or not all(isinstance(x,str) for x in result['blocks']):
                raise model.AIResponseError('EDITION_TRANSLATION_STRUCTURE')
            draft=copy.deepcopy(original);draft['headline']=result['headline'];draft['lead']=result['lead']
            for block,value in zip(draft['blocks'],result['blocks']):block['text']=value
            draft=formatted(draft)
            physical={'language','headline_length','headline_emoji','paragraph','length','telegram_length','facts','evidence','scope','sources','structure'}
            code=[i for i in validate(draft,materials,group) if i['code'] in physical]
            verdict,check_receipt=model.check_translation(original,draft,settings)
            store.event(db,document['job_id'],'source_translation_check',{'verdict':verdict,'receipt':check_receipt,'source_hash':key,'draft_hash':store.sha(draft)},document['document_id'])
            if not isinstance(verdict.get('approved'),bool) or not isinstance(verdict.get('issues'),list) or verdict['approved']!=(not verdict['issues']):
                raise model.AIResponseError('EDITION_INVALID_TRANSLATION_CHECK')
            if not code and verdict['approved']:
                proof={'kind':'translated_source','source_hash':key,'response_id':receipt['response_id'],
                       'translation_receipt':receipt,'check_receipt':check_receipt,'verdict':verdict}
                db.execute('INSERT OR IGNORE INTO edition_source_translations VALUES(?,?,?,?,?)',(key,digest,store.encoded(draft),store.encoded(proof),store.stamp()));db.commit()
                db.execute('DELETE FROM edition_translation_waits WHERE document_id=?',(document['document_id'],));db.commit()
                return draft,proof
            feedback=[*code,*verdict['issues']]
        raise model.AIResponseError('EDITION_TRANSLATION_UNVERIFIED')
    except Exception as exc:
        code=getattr(exc,'code',None) or type(exc).__name__
        # One repair for a translation, then owner-visible failure. Transport
        # outages retry only the translation, never the entire editor/planner.
        automatic=code!='EDITION_TRANSLATION_UNVERIFIED'
        next_at=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat(timespec='microseconds')
        reason={'code':code,'reason':'Ожидается проверенный русский перевод; иностранный текст не отправляется.'}
        reasons=store.read(document['reasons_json'],[])+[reason]
        db.execute("UPDATE edition_documents SET state='LANGUAGE_PENDING',reasons_json=?,updated_at=? WHERE document_id=?",(store.encoded(reasons),store.stamp(),document['document_id']))
        db.execute('INSERT OR REPLACE INTO edition_translation_waits VALUES(?,?,?)',(document['document_id'],next_at,int(automatic)));db.commit()
        store.event(db,document['job_id'],'translation_pending',reason,document['document_id'])
        return None,None
