"""Independent worker. Never edits newsroom queues or publishes messages."""
import argparse
import fcntl
import json
import logging
import time
import tomllib
from datetime import datetime,timedelta,timezone
from pathlib import Path
from ..agent_control import require_enabled,AgentDisabled
from ..db import connect
from ..runtime import Runtime,BudgetDeferred
from . import model,store

def process_one(db,config,queue_id=None):
    raise ValueError("Модуль отключён владельцем")
    require_enabled(config)
    if queue_id is None:
        row=db.execute("SELECT * FROM recruitment_queue WHERE state IN ('PENDING','WAITING') AND next_at<=? ORDER BY id LIMIT 1",(store.stamp(),)).fetchone()
    else:
        row=db.execute("SELECT * FROM recruitment_queue WHERE id=? AND state IN ('PENDING','WAITING')",(queue_id,)).fetchone()
    if not row:return False
    db.execute("UPDATE recruitment_queue SET state='RUNNING',attempts=attempts+1,updated_at=? WHERE id=?",(store.stamp(),row['id']));db.commit()
    result=receipt=None;raw_result=None
    try:
        source=json.loads(row['source_json'])
        result,receipt=model.extract(source,config.get('ai',{}))
        require_enabled(config)
        raw_result=result
        result=model.prepare(result,source)
        model.validate(result,source)
        db.execute('DELETE FROM recruitment_cards WHERE queue_id=?',(row['id'],))
        for index,card in enumerate(result['cards']):
            flat={k:card[k]['value'].strip() for k in model.FIELDS}
            flat.update(hiring_party=flat['company'] or flat['contact'],excerpt=card['excerpt'],evidence=card,fields_version=store.VERSION)
            db.execute('INSERT INTO recruitment_cards VALUES(?,?,?)',(row['id'],index,store.encoded(flat)))
        db.execute('INSERT INTO recruitment_attempts(queue_id,created_at,status,result_json,receipt_json) VALUES(?,?,?,?,?)',(row['id'],store.stamp(),'DONE',store.encoded(raw_result if raw_result is not None else result),store.encoded(receipt)))
        db.execute("UPDATE recruitment_queue SET state=?,error_code=NULL,updated_at=? WHERE id=?",('DONE' if result['kind']=='vacancy' else result['kind'].upper(),store.stamp(),row['id']))
    except Exception as exc:
        db.rollback()
        code=getattr(exc,'code',None) or (str(exc) if isinstance(exc,ValueError) and str(exc).startswith('JOBS_') else type(exc).__name__)
        delay=exc.delay_seconds if isinstance(exc,BudgetDeferred) else min(3600,300*2**min(row['attempts'],4))
        if isinstance(exc,BudgetDeferred):receipt={'kind':'capacity_wait','reason':exc.reason,'delay_seconds':delay}
        retry=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat()
        db.execute('INSERT INTO recruitment_attempts(queue_id,created_at,status,result_json,receipt_json,error_code) VALUES(?,?,?,?,?,?)',(row['id'],store.stamp(),'ERROR',store.encoded(raw_result if raw_result is not None else result),store.encoded(receipt),code))
        db.execute("UPDATE recruitment_queue SET state='WAITING',next_at=?,error_code=?,updated_at=? WHERE id=?",(retry,code,store.stamp(),row['id']))
        logging.warning('Recruitment extraction deferred: %s',code)
    db.commit()
    return True

def run(config,once=False):
    raise ValueError("Модуль отключён владельцем")
    database=config['newsroom']['database']
    lock=Path(database).with_suffix('.recruitment.lock').open('a+')
    try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:lock.close();return
    db=connect(database)
    try:
        store.initialize(db)
        # This lock belongs only to recruitment, so an interrupted call can be retried.
        db.execute("UPDATE recruitment_queue SET state='WAITING',next_at=? WHERE state='RUNNING'",(store.stamp(),));db.commit()
        require_enabled(config)
        # Snapshot intake once. Later arrivals require a new explicit request.
        ceiling=db.execute('SELECT COALESCE(MAX(item_id),0) FROM items').fetchone()[0]
        while True:
            store.discover(db,ceiling=ceiling)
            cursor=db.execute('SELECT item_id FROM recruitment_cursor WHERE id=1').fetchone()[0]
            if cursor>=ceiling:break
        ids=[r[0] for r in db.execute("SELECT id FROM recruitment_queue WHERE state IN ('PENDING','WAITING') AND item_id<=? ORDER BY id",(ceiling,))]
        wait_deadline=None
        for queue_id in ids[:1] if once else ids:
            while True:
                require_enabled(config)
                process_one(db,config,queue_id)
                row=db.execute('SELECT state,error_code,next_at FROM recruitment_queue WHERE id=?',(queue_id,)).fetchone()
                if row['error_code']!='SHARED_BUDGET_DEFERRED':break
                attempt=db.execute('SELECT receipt_json FROM recruitment_attempts WHERE queue_id=? ORDER BY id DESC LIMIT 1',(queue_id,)).fetchone()
                receipt=json.loads(attempt[0] or '{}')
                if receipt.get('reason')=='account':return
                if wait_deadline is None:wait_deadline=time.monotonic()+min(600,max(30,int(config.get('ai',{}).get('api_budget_window_seconds',180))+30))
                delay=max(1,(datetime.fromisoformat(row['next_at'])-datetime.now(timezone.utc)).total_seconds())
                if time.monotonic()+delay>wait_deadline:return
                until=time.monotonic()+delay
                while time.monotonic()<until:
                    require_enabled(config)
                    time.sleep(min(1,max(0,until-time.monotonic())))
    finally:db.close();lock.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    with open(args.config,'rb') as f:config=tomllib.load(f)
    settings=config.setdefault('ai',{})
    settings['_agent_control_config']=config
    settings['_runtime']=Runtime(config['newsroom']['database'],settings)
    run(config,args.once)

if __name__=='__main__':main()
