"""Append-only source snapshots and durable editorial state, separate from legacy data."""
from __future__ import annotations
import hashlib
import json
import re
import uuid
from datetime import datetime,timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS edition_admissions (
 material_id INTEGER PRIMARY KEY REFERENCES edition_materials(material_id),
 decision TEXT NOT NULL, related_material_id INTEGER,
 reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edition_materials (
 material_id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(item_id),
 revision TEXT NOT NULL, received_at TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 title TEXT NOT NULL, content TEXT NOT NULL, description TEXT NOT NULL,
 eligible INTEGER NOT NULL DEFAULT 0, queue_state TEXT NOT NULL DEFAULT 'ARCHIVE',
 UNIQUE(item_id,revision)
);
CREATE TRIGGER IF NOT EXISTS edition_materials_immutable BEFORE UPDATE ON edition_materials
 WHEN new.snapshot_json!=old.snapshot_json OR new.received_at!=old.received_at OR new.revision!=old.revision
 OR new.item_id!=old.item_id OR new.title!=old.title OR new.content!=old.content OR new.description!=old.description
 BEGIN SELECT RAISE(ABORT,'edition source snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS edition_materials_no_delete BEFORE DELETE ON edition_materials
 BEGIN SELECT RAISE(ABORT,'edition source snapshots are append only'); END;
CREATE INDEX IF NOT EXISTS edition_materials_queue ON edition_materials(queue_state,received_at);
CREATE VIRTUAL TABLE IF NOT EXISTS edition_search USING fts5(title,content,description,
 content='edition_materials',content_rowid='material_id',tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS edition_search_insert AFTER INSERT ON edition_materials BEGIN
 INSERT INTO edition_search(rowid,title,content,description) VALUES(new.material_id,new.title,new.content,new.description);
END;
CREATE TABLE IF NOT EXISTS edition_jobs (
 job_id TEXT PRIMARY KEY, retry_of TEXT REFERENCES edition_jobs(job_id),
 state TEXT NOT NULL, cutoff TEXT NOT NULL, received_at TEXT NOT NULL,
 material_ids_json TEXT NOT NULL, bundle_hash TEXT NOT NULL,
 searches INTEGER NOT NULL DEFAULT 0, late_checked INTEGER NOT NULL DEFAULT 0,
 groups_json TEXT NOT NULL DEFAULT '[]', reasons_json TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS edition_jobs_state ON edition_jobs(state,created_at);
CREATE TABLE IF NOT EXISTS edition_documents (
 document_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES edition_jobs(job_id),
 group_json TEXT NOT NULL, materials_json TEXT NOT NULL, draft_json TEXT NOT NULL DEFAULT '{}',
 rendered_text TEXT NOT NULL DEFAULT '', plain_text TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'DRAFTING', repairs INTEGER NOT NULL DEFAULT 0,
 reasons_json TEXT NOT NULL DEFAULT '[]', post_id INTEGER REFERENCES posts(post_id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edition_events (
 event_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES edition_jobs(job_id),
 document_id TEXT REFERENCES edition_documents(document_id), stage TEXT NOT NULL,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS edition_events_no_update BEFORE UPDATE ON edition_events
 BEGIN SELECT RAISE(ABORT,'edition events are append only'); END;
CREATE TRIGGER IF NOT EXISTS edition_events_no_delete BEFORE DELETE ON edition_events
 BEGIN SELECT RAISE(ABORT,'edition events are append only'); END;
CREATE TABLE IF NOT EXISTS edition_checks (
 check_id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES edition_documents(document_id),
 draft_hash TEXT NOT NULL, rendered_hash TEXT NOT NULL, bundle_hash TEXT NOT NULL,
 evidence_hash TEXT NOT NULL, code_json TEXT NOT NULL, review_json TEXT NOT NULL,
 response_json TEXT NOT NULL, passed INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS edition_checks_no_update BEFORE UPDATE ON edition_checks
 BEGIN SELECT RAISE(ABORT,'edition checks are append only'); END;
CREATE TRIGGER IF NOT EXISTS edition_checks_no_delete BEFORE DELETE ON edition_checks
 BEGIN SELECT RAISE(ABORT,'edition checks are append only'); END;
CREATE TABLE IF NOT EXISTS edition_lookups (
 lookup_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES edition_jobs(job_id),
 kind TEXT NOT NULL, query TEXT NOT NULL, material_ids_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def stamp():return datetime.now(timezone.utc).isoformat(timespec='microseconds')
def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True)
def sha(value):return hashlib.sha256((value if isinstance(value,str) else encoded(value)).encode()).hexdigest()
def read(raw,fallback=None):
    try:return json.loads(raw)
    except (TypeError,ValueError):return fallback


def event(db,job_id,stage,payload,document_id=None):
    db.execute('INSERT INTO edition_events(job_id,document_id,stage,payload_json,created_at) VALUES(?,?,?,?,?)',
               (job_id,document_id,stage,encoded(payload),stamp()))
    db.commit()


def initialize(db):
    db.executescript(SCHEMA)
    now=stamp()
    db.execute("INSERT OR IGNORE INTO app_state(key,value) VALUES('edition_v2_activation',?)",(now,))
    db.commit()
    return db.execute("SELECT value FROM app_state WHERE key='edition_v2_activation'").fetchone()[0]


def capture(db,item_id,*,eligible=False,queue=False):
    row=db.execute('SELECT i.*,s.name AS source_name FROM items i JOIN sources s USING(source_id) WHERE item_id=?',(item_id,)).fetchone()
    if not row:return None
    item=dict(row)
    revision=item['ingest_revision'] or sha({k:item[k] for k in ('title','content','description','published_at')})
    receipt=db.execute('SELECT value FROM app_state WHERE key=?',(f'material_received:{item_id}:{revision}',)).fetchone()
    received=(read(receipt[0],{}) or {}).get('at') if receipt else None
    received=received or item['discovered_at']
    snapshot={k:item.get(k) for k in ('item_id','url','title','description','content','author','published_at','source_id','source_name','primary_source_json')}
    snapshot['revision']=revision
    cur=db.execute('INSERT OR IGNORE INTO edition_materials(item_id,revision,received_at,snapshot_json,title,content,description,eligible,queue_state) VALUES(?,?,?,?,?,?,?,?,?)',
        (item_id,revision,received,encoded(snapshot),item['title'],item['content'],item['description'],int(eligible),'QUEUED' if queue and eligible else 'ARCHIVE'))
    if queue and eligible:
        db.execute("UPDATE edition_materials SET eligible=1,queue_state='QUEUED' WHERE item_id=? AND revision=? AND queue_state='ARCHIVE'",(item_id,revision))
    material_id=db.execute('SELECT material_id FROM edition_materials WHERE item_id=? AND revision=?',(item_id,revision)).fetchone()[0]
    if queue and eligible and db.execute('SELECT queue_state FROM edition_materials WHERE material_id=?',(material_id,)).fetchone()[0]=='QUEUED':
        from .source_text import same_version,article_identity,layout_version
        from .formatting import material_source
        previous=db.execute("SELECT material_id,snapshot_json,eligible,queue_state FROM edition_materials WHERE item_id=? AND material_id<? ORDER BY material_id DESC LIMIT 1",(item_id,material_id)).fetchone()
        prior=read(previous['snapshot_json'],{}) if previous else {}
        duplicate=previous if previous and (layout_version(prior,snapshot) or previous['eligible'] and previous['queue_state'] in ('QUEUED','CLAIMED','DONE','DUPLICATE') and same_version(prior,snapshot)) else None
        if not duplicate:
            # Exact evidence equality is required across publishers, not just a similar title.
            candidates=db.execute("SELECT material_id,snapshot_json FROM edition_materials WHERE material_id<>? AND eligible=1 AND queue_state IN ('QUEUED','CLAIMED','DONE') AND julianday(received_at)>=julianday(?)-10.0/1440 ORDER BY material_id DESC LIMIT 300",(material_id,received)).fetchall()
            key=article_identity(snapshot)
            duplicate=next((r for r in candidates if key[0] and material_source(read(r['snapshot_json'],{}))[0]!=material_source(snapshot)[0] and article_identity(read(r['snapshot_json'],{}))==key),None)
        if duplicate:
            db.execute("UPDATE edition_materials SET queue_state='DUPLICATE' WHERE material_id=? AND queue_state='QUEUED'",(material_id,))
            db.execute('INSERT OR IGNORE INTO edition_admissions VALUES(?,?,?,?,?)',(material_id,'DUPLICATE',duplicate['material_id'],'Материал уже покрыт: тот же текст/косметическая правка, новых фактов нет.',stamp()))
        else:
            # A newer substantive version replaces an older unclaimed snapshot only.
            older=db.execute("SELECT material_id FROM edition_materials WHERE item_id=? AND material_id<>? AND queue_state='QUEUED'",(item_id,material_id)).fetchall()
            for row in older:
                db.execute("UPDATE edition_materials SET queue_state='SUPERSEDED' WHERE material_id=?",(row[0],))
                db.execute('INSERT OR IGNORE INTO edition_admissions VALUES(?,?,?,?,?)',(row[0],'SUPERSEDED',material_id,'До начала подготовки получена более новая содержательная версия.',stamp()))
    return material_id


def archive_step(db,limit=500):
    """Index a bounded archive page; never enqueue historical publications."""
    cursor=db.execute("SELECT value FROM app_state WHERE key='edition_v2_archive_cursor'").fetchone()
    cursor=int(cursor[0]) if cursor else 0
    rows=db.execute('SELECT item_id FROM items WHERE item_id>? ORDER BY item_id LIMIT ?',(cursor,limit)).fetchall()
    for row in rows:capture(db,row[0])
    if rows:
        db.execute("INSERT INTO app_state(key,value) VALUES('edition_v2_archive_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(rows[-1][0]),))
    db.commit()
    return len(rows)


def ingest(db,item_id,settings):
    if settings.get('_edition_enabled') is not True:return
    found=db.execute("SELECT value FROM app_state WHERE key='edition_v2_activation'").fetchone()
    activation=found[0] if found else initialize(db)
    row=db.execute('SELECT disposition,discovered_at,ingest_revision FROM items WHERE item_id=?',(item_id,)).fetchone()
    screened=db.execute("SELECT status FROM material_stage_state WHERE item_id=? AND revision=? AND stage='screening'",(item_id,row['ingest_revision'])).fetchone()
    eligible=bool(screened and screened[0]=='DONE' and row['disposition']=='PENDING')
    receipt=db.execute('SELECT value FROM app_state WHERE key=?',(f"material_received:{item_id}:{row['ingest_revision']}",)).fetchone()
    received=(read(receipt[0],{}) or {}).get('at',row['discovered_at']) if receipt else row['discovered_at']
    capture(db,item_id,eligible=eligible,queue=eligible and received>=activation)
    db.commit()


def materials(db,ids):
    result=[]
    for material_id in dict.fromkeys(ids):
        row=db.execute('SELECT * FROM edition_materials WHERE material_id=?',(material_id,)).fetchone()
        if not row:raise ValueError('EDITION_MATERIAL_MISSING')
        result.append({**read(row['snapshot_json'],{}),'material_id':row['material_id'],'received_at':row['received_at']})
    return result


def start(db,bundle_hash,*,retry_of=None):
    cutoff=stamp()
    db.execute('BEGIN IMMEDIATE')
    if retry_of:
        original=db.execute('SELECT * FROM edition_jobs WHERE job_id=?',(retry_of,)).fetchone()
        if not original or original['state'] not in ('INCOMPLETE','REJECTED','FILTERED'):
            db.rollback();raise ValueError('Повтор доступен только для незавершённой или исключённой подготовки')
        # No automatic or concurrent replay of a prior attempt.
        if db.execute("SELECT 1 FROM edition_jobs WHERE retry_of=? AND state NOT IN ('INCOMPLETE','REJECTED','FILTERED')",(retry_of,)).fetchone():
            db.rollback();raise ValueError('Повторная подготовка уже запущена')
        docs=db.execute('SELECT * FROM edition_documents WHERE job_id=?',(retry_of,)).fetchall()
        if original['state']=='FILTERED' and any(d['state']=='PUBLISHED' for d in docs):
            db.rollback();raise ValueError('Исключённая подготовка содержит опубликованный пост; повтор запрещён')
        if any(d['state'] in ('UNKNOWN','SENDING','READY') for d in docs):
            db.rollback();raise ValueError('Сначала необходимо разрешить результат доставки предыдущей попытки')
        failed=[d for d in docs if d['state']=='INCOMPLETE']
        ids=list(dict.fromkeys(i for d in failed for i in read(d['group_json'],{})['material_ids'])) if failed else read(original['material_ids_json'],[])
        received=original['received_at']
    else:
        rows=db.execute("SELECT material_id,received_at FROM edition_materials WHERE queue_state='QUEUED' AND received_at<=? ORDER BY material_id",(cutoff,)).fetchall()
        if not rows:db.commit();return None
        ids=[r[0] for r in rows];received=min(r[1] for r in rows)
    job=uuid.uuid4().hex
    db.execute('INSERT INTO edition_jobs(job_id,retry_of,state,cutoff,received_at,material_ids_json,bundle_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
               (job,retry_of,'QUEUED' if retry_of else 'PLANNING',cutoff,received,encoded(ids),bundle_hash,cutoff,cutoff))
    if not retry_of:
        db.executemany("UPDATE edition_materials SET queue_state='CLAIMED' WHERE material_id=?",[(i,) for i in ids])
    db.commit();event(db,job,'start',{'material_ids':ids,'cutoff':cutoff,'retry_of':retry_of})
    return job


def lookup(db,job_id,query,*,late=False,topic_spec=None):
    """An indexed local lookup. The durable shared budget includes the final arrival check."""
    from .model import bundle
    limits=bundle()[0]['limits']
    db.execute('BEGIN IMMEDIATE')
    job=db.execute('SELECT * FROM edition_jobs WHERE job_id=?',(job_id,)).fetchone()
    # Reserve one lookup for the mandatory one-time arrival check.
    ceiling=limits['search_max'] if late else limits['search_max']-1
    if job['searches']>=ceiling or (late and job['late_checked']):db.commit();return []
    terms=list(dict.fromkeys(re.findall(r'[\w]{3,}',query,flags=re.UNICODE)))[:20]
    expression=' OR '.join('"'+t.replace('"','""')+'"' for t in terms)
    rows=[]
    if expression:
        time_clause='m.received_at>?' if late else 'm.received_at<=?'
        rows=db.execute('SELECT m.material_id,m.snapshot_json FROM edition_search JOIN edition_materials m ON m.material_id=edition_search.rowid '
            'WHERE edition_search MATCH ? AND '+time_clause+' ORDER BY rank,m.material_id DESC LIMIT ?',
            (expression,job['cutoff'],50 if topic_spec else limits['search_results'])).fetchall()
        if topic_spec:
            from ..keyword_filter import evaluate
            from .source_text import body,text
            rows=[r for r in rows if evaluate(body(read(r['snapshot_json'],{})),topic_spec,text(read(r['snapshot_json'],{}).get('title')))['passed']][:limits['search_results']]
    ids=[r[0] for r in rows]
    db.execute('UPDATE edition_jobs SET searches=searches+1,late_checked=CASE WHEN ? THEN 1 ELSE late_checked END,updated_at=? WHERE job_id=?',(int(late),stamp(),job_id))
    db.execute('INSERT INTO edition_lookups(job_id,kind,query,material_ids_json,created_at) VALUES(?,?,?,?,?)',(job_id,'arrivals' if late else 'context',query,encoded(ids),stamp()))
    db.commit()
    return materials(db,ids)


def finish(db,job_id,state,reasons=None):
    db.execute('UPDATE edition_jobs SET state=?,reasons_json=?,updated_at=? WHERE job_id=?',(state,encoded(reasons or []),stamp(),job_id))
    db.commit();event(db,job_id,state,{'reasons':reasons or []})
