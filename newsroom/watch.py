"""Scheduled discovery for known stories. Results enter the ordinary guarded pipeline."""
from __future__ import annotations
import json
from datetime import datetime,timedelta,timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS story_monitoring (
 story_id INTEGER PRIMARY KEY REFERENCES stories(story_id), lifecycle TEXT NOT NULL,
 priority INTEGER NOT NULL, interval_minutes INTEGER NOT NULL, dormant_after_days INTEGER NOT NULL,
 last_event_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS story_monitoring_jobs (
 job_id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL REFERENCES stories(story_id),
 target_type TEXT NOT NULL, target TEXT NOT NULL, next_check_at TEXT NOT NULL,
 last_checked_at TEXT, active INTEGER NOT NULL DEFAULT 1, UNIQUE(story_id,target)
);
CREATE INDEX IF NOT EXISTS story_jobs_due_idx ON story_monitoring_jobs(active,next_check_at);
CREATE TABLE IF NOT EXISTS story_watch_log (
 log_id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL REFERENCES stories(story_id),
 job_id INTEGER REFERENCES story_monitoring_jobs(job_id), action TEXT NOT NULL,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS story_watch_no_update BEFORE UPDATE ON story_watch_log
BEGIN SELECT RAISE(ABORT,'watch history is append only'); END;
CREATE TRIGGER IF NOT EXISTS story_watch_no_delete BEFORE DELETE ON story_watch_log
BEGIN SELECT RAISE(ABORT,'watch history is append only'); END;
"""


def stamp(now): return now.isoformat(timespec='seconds')


def log(db,story_id,job_id,action,payload,now):
    db.execute('INSERT INTO story_watch_log(story_id,job_id,action,payload_json,created_at) VALUES(?,?,?,?,?)',
               (story_id,job_id,action,json.dumps(payload,ensure_ascii=False),stamp(now)))


def policy(identity):
    # Lifecycle and urgency are independent. Legal developments remain watched longer.
    if identity.get('document_id') or identity.get('stage') in {'PROPOSED','DRAFTED','SUBMITTED','SIGNED','APPROVED','EFFECTIVE'}:
        return 2,360,90
    if any(w in (identity.get('object','')+' '+identity.get('action','')).casefold() for w in ('взлом','атак','приостанов','восстанов')):
        return 3,30,7
    return 1,180,30


def touch(db,story_id,event_id,now=None):
    now=now or datetime.now(timezone.utc)
    event=db.execute('SELECT identity_json FROM events WHERE event_id=? AND story_id=?',(event_id,story_id)).fetchone()
    if not event:return
    identity=json.loads(event[0]); priority,interval,dormant=policy(identity)
    prior=db.execute('SELECT * FROM story_monitoring WHERE story_id=?',(story_id,)).fetchone()
    if prior and prior['lifecycle'] in {'DORMANT','CLOSED'}:
        log(db,story_id,None,'REOPENED',{'event_id':event_id},now)
    db.execute('INSERT INTO story_monitoring(story_id,lifecycle,priority,interval_minutes,dormant_after_days,last_event_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(story_id) DO UPDATE SET lifecycle=excluded.lifecycle,priority=excluded.priority,interval_minutes=excluded.interval_minutes,dormant_after_days=excluded.dormant_after_days,last_event_at=excluded.last_event_at,updated_at=excluded.updated_at',
               (story_id,'ACTIVE',priority,interval,dormant,stamp(now),stamp(now)))
    log(db,story_id,None,'ACTIVE',{'event_id':event_id},now)
    subject=identity.get('subject','').replace('"','')
    target=(identity.get('document_id') or (subject+' '+identity.get('object',''))).strip()
    target_type='DOCUMENT' if identity.get('document_id') else 'ENTITY_PROJECT'
    if not target:return
    target=target[:300]
    db.execute('INSERT INTO story_monitoring_jobs(story_id,target_type,target,next_check_at) VALUES(?,?,?,?) ON CONFLICT(story_id,target) DO UPDATE SET active=1,next_check_at=MIN(story_monitoring_jobs.next_check_at,excluded.next_check_at)',
               (story_id,target_type,target,stamp(now+timedelta(minutes=interval))))


def advance(db,now=None):
    now=now or datetime.now(timezone.utc)
    for row in db.execute("SELECT * FROM story_monitoring WHERE lifecycle NOT IN ('CLOSED','ARCHIVED')").fetchall():
        age=now-datetime.fromisoformat(row['last_event_at'])
        state='DORMANT' if age>=timedelta(days=row['dormant_after_days']) else 'MONITORING'
        if row['lifecycle']!=state:
            db.execute('UPDATE story_monitoring SET lifecycle=?,updated_at=? WHERE story_id=?',(state,stamp(now),row['story_id']))
            log(db,row['story_id'],None,state,{},now)


def run(db,config,search,process,now=None):
    now=now or datetime.now(timezone.utc)
    settings=config.get('newsroom',{});ai=config.get('ai',{})
    if not settings.get('story_watch_enabled',False):return {}
    advance(db,now);db.commit()
    budget=max(0,min(10,int(settings.get('story_watch_per_cycle',2))))
    counts={}
    for _ in range(budget):
        if ai.get('_analysis_budget',1)<=0 or ai.get('_triage_budget',1)<=0:break
        db.execute('BEGIN IMMEDIATE')
        job=db.execute("SELECT j.*,m.priority,m.interval_minutes,m.lifecycle FROM story_monitoring_jobs j JOIN story_monitoring m USING(story_id) WHERE j.active=1 AND j.next_check_at<=? AND m.lifecycle NOT IN ('CLOSED','ARCHIVED') ORDER BY j.next_check_at,m.priority DESC,j.job_id LIMIT 1",(stamp(now),)).fetchone()
        if not job:
            db.commit();break
        reserve=getattr(search,'reserve',None)
        if reserve and not reserve():
            db.rollback()
            counts['DEFERRED']=counts.get('DEFERRED',0)+1
            break
        interval=max(1440,job['interval_minutes']) if job['lifecycle']=='DORMANT' else job['interval_minutes']
        db.execute('UPDATE story_monitoring_jobs SET next_check_at=?,last_checked_at=? WHERE job_id=?',
                   (stamp(now+timedelta(minutes=interval)),stamp(now),job['job_id']))
        query=job['target']+' when:7d'
        log(db,job['story_id'],job['job_id'],'SEARCH_STARTED',{'query':query},now)
        db.commit()
        try:
            articles=search(query,ai)
            source_url='story-watch://'+str(job['story_id'])
            db.execute("INSERT INTO sources(name,type,url,source_role,priority,reputation,active) VALUES(?,'web_search',?,'discovery',1,'unknown',0) ON CONFLICT(url) DO NOTHING",('Наблюдение за сюжетом '+str(job['story_id']),source_url))
            source=db.execute('SELECT * FROM sources WHERE url=?',(source_url,)).fetchone()
            db.commit()
            outcomes=[]
            for article in articles[:max(1,min(10,int(settings.get('story_watch_items_per_job',3))))]:
                # A search target is context, never proof that a result belongs to this story.
                outcome=process(db,source,article,settings.get('similarity_threshold',.35),
                    settings.get('max_post_length',3500),settings.get('freshness_window_hours',48),None,
                    settings.get('relevance_terms',[]),ai)
                outcomes.append({'url':article.get('url'),'outcome':outcome})
                counts[outcome]=counts.get(outcome,0)+1
            log(db,job['story_id'],job['job_id'],'SEARCH_FINISHED',{'query':query,'results':outcomes,'found':len(articles)},now)
        except Exception as exc:
            db.rollback()
            log(db,job['story_id'],job['job_id'],'SEARCH_ERROR',{'error_code':type(exc).__name__},now)
            counts['ERROR']=counts.get('ERROR',0)+1
        db.commit()
    return {'STORY_WATCH_'+key:value for key,value in counts.items()}


def seed_publication_targets(db):
    """Discovery targets from historical coverage, never verified facts/events."""
    import re
    now=datetime.now(timezone.utc)
    added=0
    rows=db.execute("SELECT c.*,p.published_at FROM publication_coverage c JOIN posts p USING(post_id) WHERE p.status='PUBLISHED' ORDER BY p.published_at DESC,c.coverage_id").fetchall()
    for row in rows:
        if db.execute('SELECT 1 FROM story_monitoring WHERE story_id=?',(row['story_id'],)).fetchone():continue
        text=row['subject']+' '+row['scope']+' '+row['post_text'].splitlines()[0]
        if not re.search(r'росси|беларус|казахстан|кыргыз|узбек|армени|азербайджан|таджик|туркмен|молдов|\bЦБ\b|Минфин',text,re.I):continue
        db.execute('INSERT INTO story_monitoring(story_id,lifecycle,priority,interval_minutes,dormant_after_days,last_event_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   (row['story_id'],'MONITORING',2,360,90,row['published_at'] or stamp(now),stamp(now)))
        target=(row['subject']+' '+row['scope'])[:300]
        db.execute('INSERT OR IGNORE INTO story_monitoring_jobs(story_id,target_type,target,next_check_at) VALUES(?,?,?,?)',
                   (row['story_id'],'HISTORICAL_COVERAGE',target,stamp(now)))
        log(db,row['story_id'],None,'SEEDED_FROM_PUBLICATION',{'post_id':row['post_id'],'provenance':'HISTORICAL_POST_ONLY'},now)
        added+=1
    return added
