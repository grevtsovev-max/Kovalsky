"""Read-only collected material history."""
import json

def obj(raw):
    try: return json.loads(raw or '{}')
    except (TypeError, ValueError): return {}

def decision_history(db,item_id,available=True): return []

def pipeline_snapshot(db,config,params,posts,now=None):
    rows=db.execute('SELECT i.item_id,i.title,i.url,i.disposition,i.discovered_at,s.name AS source_name FROM items i JOIN sources s USING(source_id) ORDER BY i.item_id DESC LIMIT 200').fetchall()
    return {'editorial':'removed','items':[dict(r) for r in rows]}
