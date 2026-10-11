"""Read-only cabinet view of recruitment's own saved results."""
import json

def inbox(db,limit=500):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='recruitment_queue'").fetchone():return {'items':[],'pending':[]}
    items=[]
    for row in db.execute("SELECT c.*,q.source_json,q.updated_at FROM recruitment_cards c JOIN recruitment_queue q ON q.id=c.queue_id WHERE q.state='DONE' AND q.id=(SELECT MAX(newer.id) FROM recruitment_queue newer WHERE newer.item_id=q.item_id) ORDER BY q.id DESC,c.ordinal LIMIT ?",(min(1000,max(1,limit)),)):
        source=json.loads(row['source_json']);card=json.loads(row['card_json'])
        if not card.get('role','').strip():continue
        card.update(vacancy_id=str(row['queue_id'])+':'+str(row['ordinal']),state='SAVED',created_at=row['updated_at'],origins=[{'url':source['url'],'source_name':source['source_name']}])
        # Display format when no geographical location is explicitly available.
        card['location']=card['location'] or card['work_format']
        items.append(card)
    pending=[]
    for row in db.execute("SELECT id,state,error_code,source_json FROM recruitment_queue WHERE state IN ('PENDING','RUNNING','WAITING') ORDER BY id DESC LIMIT 100"):
        source=json.loads(row['source_json'])
        pending.append({'material_id':row['id'],'url':source['url'],'state':row['state'],'error_code':row['error_code'],
            'role':'','title':source.get('title',''),'content':source.get('content',''),'company':'','hiring_party':'','salary':'','location':''})
    return {'items':items,'pending':pending}


def is_running(database):
    import fcntl
    from pathlib import Path
    try:
        with Path(database).with_suffix('.recruitment.lock').open('r') as lock:
            try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return True
            fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
    except FileNotFoundError:pass
    return False
