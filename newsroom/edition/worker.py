"""Single persistent editor alongside collection; no automatic model retry loops."""
from __future__ import annotations
import logging
import threading
from ..agent_control import require_enabled
from ..db import connect
from ..locking import acquire_named_lock
from . import model,store
from .pipeline import process
from .publication import publish_document,published


def recover(db,config):
    from .freshness import protect_document,queued
    queued(db)
    for document in db.execute("SELECT * FROM edition_documents WHERE state IN ('READY','INCOMPLETE','LANGUAGE_PENDING','SHORT_PENDING','DRAFTING','CHECKING')").fetchall():
        protect_document(db,document)
    db.commit()
    settings={**config.get('ai',{}),'_edition_config':config,'_agent_control_config':config}
    # Only the worker holding the OS lock can recover a previous process's state.
    for row in db.execute("SELECT * FROM edition_documents WHERE state='SENDING'").fetchall():
        from ..delivery import channel
        attempt=db.execute('SELECT status,telegram_message_id FROM publication_attempts WHERE delivery_key=?',(channel(config)+':edition:'+row['document_id'],)).fetchone()
        if attempt and attempt[0] in ('SENT','CONFIRMED'):
            published(db,row,config,attempt[1])
        else:
            state='UNKNOWN' if attempt and attempt[0] in ('SENDING','UNKNOWN') else 'READY'
            db.execute('UPDATE edition_documents SET state=?,updated_at=? WHERE document_id=?',(state,store.stamp(),row['document_id']))
    interrupted=[r[0] for r in db.execute("SELECT job_id FROM edition_jobs WHERE state IN ('PLANNING','DRAFTING','CHECKING')")]
    for job_id in interrupted:
        from .fallback import ensure_job
        current_hash=model.bundle()[2]
        old_hash=db.execute('SELECT bundle_hash FROM edition_jobs WHERE job_id=?',(job_id,)).fetchone()[0]
        if old_hash!=current_hash:
            db.execute('UPDATE edition_jobs SET bundle_hash=? WHERE job_id=?',(current_hash,job_id))
            store.event(db,job_id,'policy_recovery',{'previous_bundle_hash':old_hash,'bundle_hash':current_hash})
        ensure_job(db,job_id,{'code':'process_interrupted','reason':'Подготовка прервана; выпуск продолжен по исходному материалу.'},settings=settings)
    # Only refresh never-sent preparation. Published posts and all existing
    # delivery intents (including unknown outcomes) are explicitly excluded.
    for row in db.execute("SELECT d.*,j.bundle_hash FROM edition_documents d JOIN edition_jobs j USING(job_id) LEFT JOIN posts p ON p.post_id=d.post_id WHERE d.state IN ('READY','INCOMPLETE','LANGUAGE_PENDING','SHORT_PENDING') AND j.state NOT IN ('CANCELLED','FILTERED') AND j.bundle_hash<>? AND (d.post_id IS NULL OR (p.status='PENDING' AND p.external_id IS NULL AND p.published_at IS NULL))",(model.bundle()[2],)).fetchall():
        from ..delivery import channel
        intent=db.execute('SELECT status FROM publication_attempts WHERE delivery_key=? OR (post_id IS NOT NULL AND post_id=?)',(channel(config)+':edition:'+row['document_id'],row['post_id'])).fetchone()
        if intent:continue
        from .fallback import release
        store.event(db,row['job_id'],'policy_recovery',{'previous_bundle_hash':row['bundle_hash'],'bundle_hash':model.bundle()[2],'previous_draft':store.read(row['draft_json'],{})},row['document_id'])
        db.execute('UPDATE edition_jobs SET bundle_hash=? WHERE job_id=?',(model.bundle()[2],row['job_id']))
        reason={'code':'policy_updated','reason':'Неотправленный текст приведён к текущим правилам.'}
        try:release(db,row,source=True,reason=reason,settings=settings)
        except ValueError as exc:
            if str(exc)!='EDITION_FALLBACK_NEEDS_SPLIT':raise
            # A large never-sent bundle must not terminate startup recovery.
            # Other documents, including all delivery intents, remain untouched.
            from .fallback import ensure_job
            db.execute("UPDATE edition_documents SET state='INCOMPLETE' WHERE document_id=?",(row['document_id'],));db.commit()
            ensure_job(db,row['job_id'],reason,settings=settings,document_ids={row['document_id']})
        if row['post_id'] is not None:
            updated=db.execute('SELECT rendered_text FROM edition_documents WHERE document_id=?',(row['document_id'],)).fetchone()[0]
            db.execute("UPDATE posts SET text=?,post_hash=? WHERE post_id=? AND status='PENDING' AND external_id IS NULL AND published_at IS NULL",(updated,store.sha(updated),row['post_id']))
    db.commit()


def run_once(config,*,send=None,publish=True):
    require_enabled(config)
    if config.get('editorial',{}).get('enabled') is not True:return None
    from ..topic_registry import attach_cached
    attach_cached(config)  # Read the same saved thematic authority as the collector; no network.
    db=connect(config['newsroom']['database'])
    try:
        store.initialize(db)
        settings={**config.get('ai',{}),'_edition_config':config,'_agent_control_config':config}
        from .short_release import enabled,prepare,SCHEMA
        db.executescript(SCHEMA)
        if enabled():
            for pending in db.execute("SELECT d.* FROM edition_documents d JOIN edition_short_work w USING(document_id) WHERE d.state='SHORT_PENDING' AND w.retry_after<=? ORDER BY d.created_at LIMIT 1",(store.stamp(),)).fetchall():
                prepare(db,pending,settings)
            # Old failed translations continue once through the new terminal path.
            for pending in db.execute("SELECT d.* FROM edition_documents d LEFT JOIN edition_short_work w USING(document_id) WHERE d.state='LANGUAGE_PENDING' AND w.document_id IS NULL ORDER BY d.created_at LIMIT 1").fetchall():prepare(db,pending,settings)
        from .fallback import release
        for pending in db.execute("SELECT d.* FROM edition_documents d JOIN edition_translation_waits w USING(document_id) WHERE d.state='LANGUAGE_PENDING' AND w.automatic=1 AND w.retry_after<=? ORDER BY d.created_at LIMIT 1",(store.stamp(),)).fetchall():
            release(db,pending,source=True,settings=settings)
        # Delivery retries reuse the same durable intent; model work never restarts here.
        for doc in db.execute("SELECT document_id FROM edition_documents WHERE state='READY' ORDER BY created_at").fetchall():
            if publish:publish_document(db,doc[0],config,send=send)
        from ..locking import acquire_cycle_lock
        cohort_lock=acquire_cycle_lock(config['newsroom']['database'])
        if cohort_lock is None:return None
        try:
            job=db.execute("SELECT job_id FROM edition_jobs WHERE state='SCREENING' OR (state='QUEUED' AND retry_of IS NOT NULL) ORDER BY created_at LIMIT 1").fetchone()
            job_id=job[0] if job else store.start(db,model.bundle()[2])
        finally:cohort_lock.close()
        if job_id:
            return process(db,job_id,settings,send=send,publish=publish)
        store.archive_step(db)
        return None
    finally:db.close()


def run(config_path,stop):
    from ..cli import load_config
    config=load_config(config_path)
    lock=acquire_named_lock(config['newsroom']['database'],'edition')
    if lock is None:return
    try:
        db=connect(config['newsroom']['database'])
        try:store.initialize(db);recover(db,config)
        finally:db.close()
        while not stop.is_set():
            try:
                config=load_config(config_path)
                require_enabled(config)
                run_once(config)
            except Exception as exc:
                # Content-free diagnostics; history remains in the database.
                logging.getLogger('newsroom.edition').error('EDITION_WORKER:%s',getattr(exc,'code',None) or type(exc).__name__)
            stop.wait(2)
    finally:lock.close()
