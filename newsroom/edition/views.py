"""Read-only editorial history and explicit retry actions for the dashboard."""
from __future__ import annotations
from datetime import datetime,timezone
from . import model,store
from .formatting import material_source

LABELS={'QUEUED':'Ожидает подготовки','PLANNING':'Группировка','DRAFTING':'Написание','CHECKING':'Проверка','READY':'Ожидает доставки',
        'SENDING':'Отправляется','PUBLISHED':'Опубликовано','INCOMPLETE':'Не завершено','REJECTED':'Не подготовлено','UNKNOWN':'Результат отправки неизвестен',
        'FILTERED':'Не соответствует теме','CANCELLED':'Отменено владельцем'}


def overview(db,config):
    policy,refs,digest=model.bundle()
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='edition_jobs'").fetchone()
    if not exists:return {'enabled':config.get('editorial',{}).get('enabled') is True,'version':policy['version'],'bundle_hash':digest,'jobs':[],'counts':{},'references':len(refs['references'])}
    counts={r[0]:r[1] for r in db.execute('SELECT state,COUNT(*) FROM edition_jobs GROUP BY state')}
    jobs=[];now=datetime.now(timezone.utc)
    for row in db.execute('SELECT * FROM edition_jobs ORDER BY created_at DESC LIMIT 100'):
        job=dict(row);age=max(0,(now-datetime.fromisoformat(job['received_at'])).total_seconds())
        docs=[]
        for d in db.execute('SELECT * FROM edition_documents WHERE job_id=? ORDER BY created_at',(job['job_id'],)):
            group=store.read(d['group_json'],{})
            docs.append({'document_id':d['document_id'],'subject':group.get('subject'),'state':d['state'],
                'state_label':LABELS.get(d['state'],d['state']),'text':d['plain_text'],'draft':store.read(d['draft_json'],{}),
                'repairs':d['repairs'],'reasons':store.read(d['reasons_json'],[]),'post_id':d['post_id'],
                'sources':[{'material_id':m['material_id'],'url':material_source(m)[1],'name':material_source(m)[0]} for m in store.read(d['materials_json'],[])]})
        retryable=job['state'] in ('INCOMPLETE','REJECTED') and not any(d['state'] in ('UNKNOWN','SENDING','READY') for d in docs)
        if db.execute("SELECT 1 FROM edition_jobs WHERE retry_of=? AND state NOT IN ('INCOMPLETE','REJECTED')",(job['job_id'],)).fetchone():retryable=False
        jobs.append({'job_id':job['job_id'],'retry_of':job['retry_of'],'state':job['state'],'state_label':LABELS.get(job['state'],job['state']),
            'received_at':job['received_at'],'created_at':job['created_at'],'updated_at':job['updated_at'],
            'age_seconds':age,'delayed':age>600 and job['state']!='PUBLISHED','searches':job['searches'],
            'reasons':store.read(job['reasons_json'],[]),'documents':docs,'retryable':retryable})
    latencies=[]
    for row in db.execute("SELECT e.payload_json FROM edition_events e WHERE stage='published' ORDER BY event_id DESC LIMIT 100"):
        value=store.read(row[0],{}).get('received_to_channel_seconds')
        if isinstance(value,(int,float)):latencies.append(value)
    return {'enabled':config.get('editorial',{}).get('enabled') is True,'version':policy['version'],'bundle_hash':digest,
            'references':len(refs['references']),'counts':counts,'jobs':jobs,'target_minutes':[5,10],
            'published_sample':len(latencies),'within_10_minutes':sum(v<=600 for v in latencies),
            'median_seconds':sorted(latencies)[len(latencies)//2] if latencies else None}


def details(db,job_id):
    job=db.execute('SELECT * FROM edition_jobs WHERE job_id=?',(job_id,)).fetchone()
    if not job:raise ValueError('Подготовка не найдена')
    snapshots=store.materials(db,store.read(job['material_ids_json'],[]))
    events=[{**dict(e),'payload':store.read(e['payload_json'],{})} for e in db.execute('SELECT * FROM edition_events WHERE job_id=? ORDER BY event_id',(job_id,))]
    checks=[{**dict(c),'code':store.read(c['code_json'],[]),'review':store.read(c['review_json'],{})} for c in db.execute('SELECT c.* FROM edition_checks c JOIN edition_documents d USING(document_id) WHERE d.job_id=? ORDER BY check_id',(job_id,))]
    return {'job':dict(job),'materials':snapshots,'events':events,'checks':checks}


def retry(db,config,job_id):
    from ..agent_control import require_enabled
    require_enabled(config)
    if config.get('editorial',{}).get('enabled') is not True:raise ValueError('Редакция отключена')
    store.initialize(db)
    new_id=store.start(db,model.bundle()[2],retry_of=job_id)
    return {'job_id':new_id,'state':'QUEUED'}
