"""Send only the exact text bound to a successful independent and code check."""
from __future__ import annotations
import os
from . import model,store
from .formatting import render,validate
from ..delivery import DeliveryRejected,DeliveryUncertain,DeliveryRateLimited,TelegramReceipt,channel,deliver,confirm
from ..runtime import BudgetDeferred


def gate(db,document_id,text,post_id=None):
    doc=db.execute('SELECT * FROM edition_documents WHERE document_id=?',(document_id,)).fetchone()
    if not doc or doc['state'] not in ('READY','SENDING','PUBLISHED'):raise DeliveryRejected('EDITION_NOT_READY')
    if post_id is not None and doc['post_id']!=post_id:raise DeliveryRejected('EDITION_POST_MISMATCH')
    draft=store.read(doc['draft_json'],{});materials=store.read(doc['materials_json'],[])
    rendered,plain=render(draft,materials)
    if rendered!=text or rendered!=doc['rendered_text'] or plain!=doc['plain_text']:raise DeliveryRejected('EDITION_TEXT_CHANGED')
    policy_hash=model.bundle()[2]
    job=db.execute('SELECT * FROM edition_jobs WHERE job_id=?',(doc['job_id'],)).fetchone()
    if not job or job['bundle_hash']!=policy_hash:raise DeliveryRejected('EDITION_RULES_CHANGED')
    allowed=set(store.read(job['material_ids_json'],[]))
    for row in db.execute('SELECT material_ids_json FROM edition_lookups WHERE job_id=?',(doc['job_id'],)):allowed.update(store.read(row[0],[]))
    for material in materials:
        if material['material_id'] not in allowed:raise DeliveryRejected('EDITION_UNREGISTERED_EVIDENCE')
        source=db.execute('SELECT snapshot_json FROM edition_materials WHERE material_id=?',(material['material_id'],)).fetchone()
        snapshot=store.read(source[0],{}) if source else {}
        if not snapshot or any(material.get(k)!=value for k,value in snapshot.items()):raise DeliveryRejected('EDITION_EVIDENCE_CHANGED')
    proof=db.execute('SELECT * FROM edition_checks WHERE document_id=? AND passed=1 ORDER BY check_id DESC LIMIT 1',(document_id,)).fetchone()
    if not proof or proof['draft_hash']!=store.sha(draft) or proof['rendered_hash']!=store.sha(text) or proof['bundle_hash']!=policy_hash or proof['evidence_hash']!=store.sha(materials):
        raise DeliveryRejected('EDITION_PROOF_MISMATCH')
    review=store.read(proof['review_json'],{});mode=review.get('release_mode','CHECKED')
    if mode=='SOURCE':
        from .fallback import source_draft
        if draft!=source_draft(store.read(doc['group_json'],{}),materials):raise DeliveryRejected('EDITION_SOURCE_TEXT_CHANGED')
        if store.read(proof['response_json'],{}).get('kind')!='source_fallback':raise DeliveryRejected('EDITION_SOURCE_RECEIPT_MISSING')
    else:
        if not store.read(proof['response_json'],{}).get('response_id'):raise DeliveryRejected('EDITION_REVIEW_RECEIPT_MISSING')
        try:model.validate_review(review)
        except (ValueError,model.AIResponseError):raise DeliveryRejected('EDITION_REVIEW_COVERAGE_MISSING') from None
        code=validate(draft,materials,store.read(doc['group_json'],{}))
        if mode=='ADVISORY':
            from .fallback import can_release
            if not can_release([*code,*review.get('issues',[])]):raise DeliveryRejected('EDITION_SOURCE_RELEASE_REQUIRED')
        elif mode!='CHECKED' or code or review.get('approved') is not True or review.get('issues')!=[]:raise DeliveryRejected('EDITION_REVIEW_NOT_PASSED')
    if len(plain.encode('utf-16-le'))//2>4096:raise DeliveryRejected('EDITION_TELEGRAM_LENGTH')
    return doc,proof


def send_html(config,text):
    from ..cli import telegram_api
    target=channel(config)
    response=telegram_api(config,'sendMessage',{'chat_id':target,'text':text,'parse_mode':'HTML','disable_web_page_preview':True})
    return TelegramReceipt(response)


def published(db,doc,config,message_id):
    now=store.stamp()
    db.execute("UPDATE edition_documents SET state='PUBLISHED',updated_at=? WHERE document_id=?",(now,doc['document_id']))
    db.execute("UPDATE posts SET status='PUBLISHED',published_at=COALESCE(published_at,?),external_id=?,editor_decision='APPROVED' WHERE post_id=?",(now,str(message_id),doc['post_id']))
    confirm(db,config,'edition:'+doc['document_id']);db.commit()
    job=db.execute('SELECT received_at FROM edition_jobs WHERE job_id=?',(doc['job_id'],)).fetchone()
    from datetime import datetime
    ids=store.read(doc['group_json'],{}).get('material_ids',[])
    received=[m['received_at'] for m in store.read(doc['materials_json'],[]) if m['material_id'] in ids]
    seconds=(datetime.fromisoformat(now)-datetime.fromisoformat(min(received) if received else job[0])).total_seconds()
    store.event(db,doc['job_id'],'published',{'message_id':str(message_id),'received_to_channel_seconds':seconds},doc['document_id'])
    state=db.execute('SELECT state FROM edition_jobs WHERE job_id=?',(doc['job_id'],)).fetchone()[0]
    if state in ('READY','INCOMPLETE') and not db.execute("SELECT 1 FROM edition_documents WHERE job_id=? AND state!='PUBLISHED'",(doc['job_id'],)).fetchone():
        store.finish(db,doc['job_id'],'PUBLISHED',[])
    return str(message_id)


def publish_document(db,document_id,config,*,send=None):
    doc=db.execute('SELECT * FROM edition_documents WHERE document_id=?',(document_id,)).fetchone()
    if not doc:raise ValueError('EDITION_DOCUMENT_MISSING')
    if doc['state']=='UNKNOWN':
        return None
    if doc['state']=='PUBLISHED':
        return db.execute('SELECT external_id FROM posts WHERE post_id=?',(doc['post_id'],)).fetchone()[0]
    try:
        doc,proof=gate(db,document_id,doc['rendered_text'])
        if doc['post_id'] is None:
            now=store.stamp();draft=store.read(doc['draft_json'],{});group=store.read(doc['group_json'],{})
            sources=store.read(doc['materials_json'],[]);origin=next(m for m in sources if m['material_id'] in group['material_ids'])
            story=db.execute('INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at,entities) VALUES(?,?,?,?,?)',
                             (group['subject'],draft['headline'],now,now,store.encoded(group['entities']))).lastrowid
            post_id=db.execute('INSERT INTO posts(story_id,origin_item_id,text,status,created_at,version,source_ids,post_hash,editor_decision,fact_check_result) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (story,origin['item_id'],doc['rendered_text'],'PENDING',now,1,store.encoded(list({m['source_id'] for m in sources})),store.sha(doc['rendered_text']),'APPROVED',
                 store.encoded({'edition_v2':document_id,'check_id':proof['check_id'],'bundle_hash':proof['bundle_hash']}))).lastrowid
            db.execute('UPDATE edition_documents SET post_id=? WHERE document_id=?',(post_id,document_id));db.commit()
            doc=db.execute('SELECT * FROM edition_documents WHERE document_id=?',(document_id,)).fetchone()
        db.execute("UPDATE edition_documents SET state='SENDING',updated_at=? WHERE document_id=?",(store.stamp(),document_id));db.commit()
        def verified_send(settings,text):
            gate(db,document_id,text,doc['post_id'])
            return (send or send_html)(settings,text)
        receipt=deliver(db,config,'edition:'+document_id,doc['rendered_text'],verified_send,post_id=doc['post_id'])
        return published(db,doc,config,receipt)
    except (DeliveryRateLimited,BudgetDeferred) as exc:
        db.execute("UPDATE edition_documents SET state='READY',reasons_json=?,updated_at=? WHERE document_id=?",
            (store.encoded([{'code':'delivery_deferred','reason':'Доставка ожидает доступного лимита Telegram.'}]),store.stamp(),document_id));db.commit()
        return None
    except DeliveryUncertain:
        db.execute("UPDATE edition_documents SET state='UNKNOWN',reasons_json=?,updated_at=? WHERE document_id=?",
            (store.encoded([{'code':'delivery_unknown','reason':'Telegram мог принять пост. Повторная отправка заблокирована до подтверждения результата.'}]),store.stamp(),document_id));db.commit()
        return None
    except DeliveryRejected as exc:
        db.execute("UPDATE edition_documents SET state='INCOMPLETE',reasons_json=?,updated_at=? WHERE document_id=?",
            (store.encoded([{'code':str(exc),'reason':'Доставка остановлена; текст или допуск требуют проверки.'}]),store.stamp(),document_id));db.commit()
        return None
