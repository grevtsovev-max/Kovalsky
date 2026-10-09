"""Read-only vacancy cards; independent of editorial routing."""
import json

def read(value, default):
    try: return json.loads(value)
    except (ValueError,TypeError): return default

def inbox(db,limit=500):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='edition_vacancies'").fetchone():return {'items':[],'pending':[]}
    items=[]
    for row in db.execute('SELECT * FROM edition_vacancies ORDER BY created_at DESC LIMIT ?',(min(1000,max(1,limit)),)):
        card=read(row['card_json'],{})
        origins=[dict(r) for r in db.execute('SELECT * FROM edition_vacancy_origins WHERE vacancy_id=? ORDER BY received_at',(row['vacancy_id'],))]
        items.append({**card,'vacancy_id':row['vacancy_id'],'state':row['state'],'created_at':row['created_at'],'origins':origins})
    pending=[dict(r) for r in db.execute("SELECT r.material_id,m.title,m.content,m.description,m.received_at,m.snapshot_json FROM edition_vacancy_routes r JOIN edition_materials m USING(material_id) WHERE r.kind='pending' ORDER BY r.created_at DESC LIMIT 100")]
    for p in pending:p['url']=read(p.pop('snapshot_json'),{}).get('url','')
    return {'items':items,'pending':pending}

