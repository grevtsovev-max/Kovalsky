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
        ensure_job(db,job_id,{'code':'process_interrupted','reason':'Подготовка прервана; выпуск продолжен по исходному материалу.'})
    db.commit()


def run_once(config,*,send=None,publish=True):
    require_enabled(config)
    if config.get('editorial',{}).get('enabled') is not True:return None
    db=connect(config['newsroom']['database'])
    try:
        store.initialize(db)
        # Delivery retries reuse the same durable intent; model work never restarts here.
        for doc in db.execute("SELECT document_id FROM edition_documents WHERE state='READY' ORDER BY created_at").fetchall():
            if publish:publish_document(db,doc[0],config,send=send)
        job=db.execute("SELECT job_id FROM edition_jobs WHERE state='QUEUED' AND retry_of IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
        job_id=job[0] if job else store.start(db,model.bundle()[2])
        if job_id:
            settings={**config.get('ai',{}),'_edition_config':config,'_agent_control_config':config}
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
