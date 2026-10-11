"""Explicit owner corrections, editing the original message with durable receipts."""
import argparse
import json
import sqlite3
import uuid
import fcntl
from pathlib import Path

from . import model, store
from .formatting import validate, render, contains
from ..agent_control import require_enabled
from ..delivery import channel, DeliveryRejected

SCHEMA = """
CREATE TABLE IF NOT EXISTS edition_corrections (
 correction_id TEXT PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(post_id),
 message_id TEXT NOT NULL, channel_id TEXT NOT NULL, original_text TEXT NOT NULL,
 candidate_json TEXT, edited_text TEXT, proof_json TEXT, bundle_hash TEXT NOT NULL,
 status TEXT NOT NULL, response_json TEXT, error_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS edition_corrections_original BEFORE UPDATE ON edition_corrections
 WHEN new.original_text != old.original_text OR new.post_id != old.post_id
 OR new.message_id != old.message_id OR new.channel_id != old.channel_id
 BEGIN SELECT RAISE(ABORT,'correction identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS edition_corrections_no_delete BEFORE DELETE ON edition_corrections
 BEGIN SELECT RAISE(ABORT,'correction history is append only'); END;
"""


def correct(db, config, message_id, send=None, *, empty_as_dot=False):
    require_enabled(config)
    db.executescript(SCHEMA)
    target = channel(config)
    post = db.execute("SELECT * FROM posts WHERE external_id=? AND status='PUBLISHED'", (str(message_id),)).fetchall()
    if len(post) != 1:
        raise ValueError('CORRECTION_PUBLISHED_POST_REQUIRED')
    post = post[0]
    destinations = {r[0] for r in db.execute('SELECT channel_id FROM publication_attempts WHERE post_id=?', (post['post_id'],))}
    if destinations != {target}:
        raise ValueError('CORRECTION_CHANNEL_MISMATCH')
    document = db.execute('SELECT * FROM edition_documents WHERE post_id=?', (post['post_id'],)).fetchone()
    if not document:
        raise ValueError('CORRECTION_SOURCE_REQUIRED')
    pending = db.execute("SELECT * FROM edition_corrections WHERE post_id=? AND status NOT IN ('APPLIED','REJECTED') ORDER BY created_at DESC LIMIT 1", (post['post_id'],)).fetchone()
    if pending and pending['status'] in ('SENDING', 'UNKNOWN'):
        raise ValueError('CORRECTION_DELIVERY_UNKNOWN')
    digest = model.bundle()[2]
    if pending and (pending['bundle_hash'] != digest or pending['original_text'] != post['text']):
        raise ValueError('CORRECTION_BASE_CHANGED')
    correction_id = pending['correction_id'] if pending else uuid.uuid4().hex
    if not pending:
        now = store.stamp()
        db.execute('INSERT INTO edition_corrections(correction_id,post_id,message_id,channel_id,original_text,bundle_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                   (correction_id, post['post_id'], str(message_id), target, post['text'], digest, 'PREPARING', now, now))
        db.commit()
    materials = store.read(document['materials_json'], [])
    settings = config['ai']
    try:
        saved = store.read(pending['proof_json'], {}) if pending else {}
        if empty_as_dot and not saved.get('semantic'):
            original_group = store.read(document['group_json'], {})
            scoped = []
            for material in materials:
                quotes = [f.get('scope') or f.get('excerpt') for f in original_group.get('focus', [])
                          if f.get('material_id') == material['material_id']]
                if not quotes or any(not contains(material, q) for q in quotes):
                    raise ValueError('CORRECTION_SOURCE_SCOPE_REQUIRED')
                scoped.append({**{k: material.get(k) for k in ('material_id', 'source_name', 'url', 'published_at', 'received_at')}, 'title': original_group.get('subject', ''),
                               'content': '\n\n'.join(quotes), 'description': '', 'raw_content': '',
                               'read_content': ''})
            result, semantic_receipt = model.semantic_screen(scoped, settings)
            entries = result.get('decisions', [])
            if len(entries) != len(scoped) or {e.get('material_id') for e in entries} != {m['material_id'] for m in scoped}:
                raise ValueError('CORRECTION_SEMANTIC_COVERAGE')
            for entry in entries:
                material = next(m for m in scoped if m['material_id'] == entry['material_id'])
                if entry.get('decision') not in ('allow', 'reject') or not entry.get('reason') or not entry.get('evidence'):
                    raise ValueError('CORRECTION_SEMANTIC_UNCONFIRMED')
                if any(e.get('material_id') != material['material_id'] or not contains(material, e.get('quote')) for e in entry['evidence']):
                    raise ValueError('CORRECTION_SEMANTIC_UNGROUNDED')
            if not semantic_receipt.get('response_id') or semantic_receipt.get('bundle_hash') != digest:
                raise ValueError('CORRECTION_SEMANTIC_RECEIPT_REQUIRED')
            saved.update(semantic=result, semantic_receipt=semantic_receipt,
                         original_document=dict(document), original_post=dict(post))
            if any(e['decision'] == 'allow' for e in entries):
                saved['group'] = original_group
            db.execute('UPDATE edition_corrections SET proof_json=?,updated_at=? WHERE correction_id=?',
                       (store.encoded(saved), store.stamp(), correction_id)); db.commit()
        empty = empty_as_dot and all(e['decision'] == 'reject' for e in saved['semantic']['decisions'])
        if empty:
            draft = {}; group = store.read(document['group_json'], {})
            edited = plain = '.'; receipt = saved['semantic_receipt']
            saved.update(empty_as_dot=True, text_hash=store.sha(edited), evidence_hash=store.sha(materials))
        else:
            if saved.get('group'):
                group = saved['group']
            else:
                plan, receipt = model.plan(materials, settings)
                if plan.get('fragment_errors') or len(plan.get('groups', [])) != 1:
                    raise ValueError('CORRECTION_SINGLE_PROFILE_EVENT_REQUIRED')
                group = plan['groups'][0]
                saved = {'group': group, 'planner': receipt, 'original_document': dict(document), 'original_post': dict(post)}
                db.execute('UPDATE edition_corrections SET proof_json=?,updated_at=? WHERE correction_id=?',
                           (store.encoded(saved), store.stamp(), correction_id)); db.commit()
            if pending and pending['candidate_json']:
                draft = store.read(pending['candidate_json'], {})
            else:
                draft, receipt = model.draft(group, materials, settings)
                saved['writer'] = receipt
            db.execute('UPDATE edition_corrections SET candidate_json=?,proof_json=?,status=?,updated_at=? WHERE correction_id=?',
                       (store.encoded(draft), store.encoded(saved), 'CHECKING', store.stamp(), correction_id))
            db.commit()
            verdict, receipt = model.check(draft, materials, settings, group=group)
            model.validate_review(verdict)
            saved.setdefault('checks', []).append({'draft': draft, 'verdict': verdict, 'receipt': receipt})
            db.execute('UPDATE edition_corrections SET proof_json=?,updated_at=? WHERE correction_id=?',
                       (store.encoded(saved), store.stamp(), correction_id)); db.commit()
            issues = validate(draft, materials, group) + verdict['issues']
            if issues or verdict.get('source_scope') != 'in_scope' or not verdict['approved']:
                if saved.get('repair_started'):
                    raise ValueError('CORRECTION_REVIEW_FAILED')
                saved['repair_started'] = True
                db.execute('UPDATE edition_corrections SET proof_json=?,updated_at=? WHERE correction_id=?',
                           (store.encoded(saved), store.stamp(), correction_id)); db.commit()
                draft, writer = model.draft(group, materials, settings, previous=draft, feedback=issues or [{'code':'scope','reason':'Подтверди профильность выбранного события.'}])
                saved['repair'] = writer
                db.execute('UPDATE edition_corrections SET candidate_json=?,proof_json=?,updated_at=? WHERE correction_id=?',
                           (store.encoded(draft), store.encoded(saved), store.stamp(), correction_id)); db.commit()
                verdict, receipt = model.check(draft, materials, settings, group=group)
                model.validate_review(verdict)
                saved['checks'].append({'draft': draft, 'verdict': verdict, 'receipt': receipt})
                db.execute('UPDATE edition_corrections SET proof_json=?,updated_at=? WHERE correction_id=?',
                           (store.encoded(saved), store.stamp(), correction_id)); db.commit()
            if validate(draft, materials, group) or verdict.get('source_scope') != 'in_scope' or not verdict['approved']:
                raise ValueError('CORRECTION_REVIEW_FAILED')
            if not receipt.get('response_id') or receipt.get('bundle_hash') != digest:
                raise ValueError('CORRECTION_REVIEW_RECEIPT_REQUIRED')
            edited, plain = render(draft, materials)
            saved.update(verdict=verdict, checker=receipt, draft_hash=store.sha(draft), text_hash=store.sha(edited), evidence_hash=store.sha(materials))
        require_enabled(config)
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT text,external_id FROM posts WHERE post_id=?', (post['post_id'],)).fetchone()
        if current['text'] != post['text'] or current['external_id'] != str(message_id):
            raise ValueError('CORRECTION_BASE_CHANGED')
        if model.bundle()[2] != digest:
            raise ValueError('CORRECTION_RULES_CHANGED')
        db.execute("UPDATE edition_corrections SET candidate_json=?,edited_text=?,proof_json=?,status='SENDING',updated_at=? WHERE correction_id=?",
                   (store.encoded(draft), edited, store.encoded(saved), store.stamp(), correction_id)); db.commit()
    except Exception as exc:
        db.rollback()
        db.execute("UPDATE edition_corrections SET status='WAITING',error_code=?,updated_at=? WHERE correction_id=?",
                   (getattr(exc, 'code', None) or (str(exc) if str(exc).startswith('CORRECTION_') else type(exc).__name__), store.stamp(), correction_id)); db.commit()
        raise
    if send is None:
        from ..cli import telegram_api
        send = lambda payload: telegram_api(config, 'editMessageText', payload)
    try:
        response = send({'chat_id': target, 'message_id': int(message_id), 'text': edited, 'parse_mode': 'HTML', 'link_preview_options': {'is_disabled': True}})
        if not isinstance(response, dict) or str(response.get('message_id')) != str(message_id):
            raise ValueError('CORRECTION_RECEIPT_MISMATCH')
    except Exception as exc:
        status = 'REJECTED' if isinstance(exc, DeliveryRejected) else 'UNKNOWN'
        db.execute('UPDATE edition_corrections SET status=?,error_code=?,updated_at=? WHERE correction_id=?',
                   (status, type(exc).__name__, store.stamp(), correction_id)); db.commit()
        raise
    db.execute('BEGIN IMMEDIATE')
    db.execute('UPDATE posts SET text=?,post_hash=?,version=version+1 WHERE post_id=?', (edited, store.sha(edited), post['post_id']))
    facts = store.read(post['fact_check_result'], {})
    facts['owner_correction'] = {'correction_id': correction_id, 'bundle_hash': digest, 'response_id': receipt['response_id']}
    db.execute('UPDATE posts SET fact_check_result=? WHERE post_id=?', (store.encoded(facts), post['post_id']))
    db.execute('UPDATE edition_documents SET draft_json=?,rendered_text=?,plain_text=?,group_json=?,updated_at=? WHERE document_id=?',
               (store.encoded(draft), edited, plain, store.encoded(group), store.stamp(), document['document_id']))
    db.execute("UPDATE edition_corrections SET status='APPLIED',response_json=?,updated_at=? WHERE correction_id=?", (store.encoded(response), store.stamp(), correction_id))
    store.event(db, document['job_id'], 'owner_correction', {'correction_id': correction_id, 'message_id': str(message_id), 'draft': draft, 'proof': saved}, document['document_id'])
    db.commit()
    return {'status': 'APPLIED', 'message_id': str(message_id), 'correction_id': correction_id}


def main():
    parser = argparse.ArgumentParser(description='Исправить опубликованный пост на месте по действующим правилам.')
    parser.add_argument('--config', required=True)
    parser.add_argument('--message-id', required=True, type=int)
    parser.add_argument('--empty-as-dot', action='store_true')
    args = parser.parse_args()
    from ..cli import load_config
    from ..topic_registry import attach_cached
    config = load_config(args.config); attach_cached(config)
    lock = Path(config['newsroom']['database'] + '.corrections.lock').open('a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError('CORRECTION_ALREADY_RUNNING')
    try:
        with sqlite3.connect(config['newsroom']['database'], timeout=30) as db:
            db.row_factory = sqlite3.Row
            print(json.dumps(correct(db, config, args.message_id, empty_as_dot=args.empty_as_dot), ensure_ascii=False))
    finally:
        lock.close()


if __name__ == '__main__':
    main()
