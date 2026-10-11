"""Source-grounded recruitment inbox, isolated from news and delivery."""
import re
from . import store
from .formatting import contains,material_source
from .source_text import canonical
VERSION='vacancy-inbox-v1'
SCHEMA='''
CREATE TABLE IF NOT EXISTS edition_vacancy_routes (
 material_id INTEGER PRIMARY KEY REFERENCES edition_materials(material_id),
 kind TEXT NOT NULL, news_quotes_json TEXT NOT NULL, decision_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edition_vacancies (
 vacancy_id TEXT PRIMARY KEY, state TEXT NOT NULL, card_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edition_vacancy_origins (
 vacancy_id TEXT NOT NULL REFERENCES edition_vacancies(vacancy_id),
 material_id INTEGER NOT NULL REFERENCES edition_materials(material_id),
 excerpt TEXT NOT NULL, url TEXT NOT NULL, source_name TEXT NOT NULL,
 published_at TEXT, received_at TEXT NOT NULL,
 PRIMARY KEY(vacancy_id,material_id,excerpt)
);
'''
FIELDS=('role','company','region','work_format','salary','contact')

def candidate(material):
    if re.search(r'https?://t\.me/(?:workingincrypto|job_crypto_ru|cryptoheadhunter)(?:/|$)',str(material.get('url') or ''),re.I):return True
    text='\n'.join(str(material.get(k) or '') for k in ('title','content','description'))
    return bool(re.search(r'#(?:ваканси\w*|vacancy|резюме)|\b(?:вакансия|вакансии|резюме|ищем\s+(?:в\s+команду|разработчика|аналитика|юриста|менеджера)|требу(?:ется|ются)\s+(?:разработчик\w*|аналитик\w*|юрист\w*|менеджер\w*|специалист\w*|инженер\w*|сотрудник\w*|программист\w*)|we are hiring|we.re hiring)\b',text,re.I))

def is_resume(material):
    header=str(material.get('title') or material.get('content') or '').split('\n',1)[0]
    return bool(re.search(r'(?:^|\s)#(?:резюме|resume|cv)\b',header,re.I))


def validate(entry,material):
    for card in entry.get('vacancies',[]):
        excerpt=card.get('excerpt','')
        if not contains(material,excerpt):raise ValueError('EDITION_VACANCY_EVIDENCE')
        if card.get('topic') not in ('profile','nonprofile','uncertain') or card.get('geography') not in ('rf','cis','foreign','uncertain'):
            raise ValueError('EDITION_VACANCY_CLASSIFICATION')
        for key in FIELDS:
            value=card.get(key,'')
            if not isinstance(value,str) or len(value)>2000 or value and canonical(value) not in canonical(excerpt):
                raise ValueError('EDITION_VACANCY_FIELD_EVIDENCE')

def route(db,material,entry):
    """Persist before planning; no uncertain recruitment text reaches fallback."""
    mid=material['material_id'];kind=entry.get('content_kind','news');cards=entry.get('vacancies',[])
    if kind=='news' and not cards and (entry.get('content_kind')=='news' or not candidate(material)):return material
    if kind not in ('vacancy','mixed','resume') and not cards:
        kind='pending'
    validate(entry,material)
    if is_resume(material) and kind!='mixed':kind='resume'
    for card in cards:
        state='EXCLUDED' if card['topic']=='nonprofile' or card['geography']=='foreign' or kind=='resume' else 'SAVED' if card['topic']=='profile' and card['geography'] in ('rf','cis') else 'NEEDS_REVIEW'
        # Do not merge anonymous employers just because job titles match.
        identity={k:canonical(card.get(k,'')) for k in FIELDS}
        if not identity['role'] or not (identity['company'] or identity['contact']):identity={'excerpt':canonical(card['excerpt'])}
        vid=store.sha({'identity':identity,'state':state})
        db.execute('INSERT OR IGNORE INTO edition_vacancies VALUES(?,?,?,?)',(vid,state,store.encoded(card),store.stamp()))
        name,url=material_source(material)
        db.execute('INSERT OR IGNORE INTO edition_vacancy_origins VALUES(?,?,?,?,?,?,?)',(vid,mid,card['excerpt'],url,name,material.get('published_at'),material['received_at']))
    quotes=[]
    if kind=='mixed' and entry.get('decision')=='allow':
        quotes=[e['quote'] for e in entry.get('evidence',[]) if contains(material,e.get('quote'))]
        # A recruitment excerpt cannot be reintroduced as the news part.
        if any(canonical(q) in canonical(c['excerpt']) or canonical(c['excerpt']) in canonical(q) for q in quotes for c in cards):quotes=[]
    if not cards and kind in ('vacancy','mixed'):kind='pending';quotes=[]
    db.execute('INSERT INTO edition_vacancy_routes VALUES(?,?,?,?,?) ON CONFLICT(material_id) DO UPDATE SET kind=excluded.kind,news_quotes_json=excluded.news_quotes_json,decision_json=excluded.decision_json',(mid,kind,store.encoded(quotes),store.encoded(entry),store.stamp()))
    db.commit()
    return news_material(db,material)

def news_material(db,material):
    row=db.execute('SELECT news_quotes_json,kind FROM edition_vacancy_routes WHERE material_id=?',(material['material_id'],)).fetchone()
    if not row or row[1]=='news':return material
    quotes=store.read(row[0],[])
    if not quotes:return None
    return {**material,'title':'','content':'\n'.join(quotes),'description':'','primary_source_json':'{}'}

def inbox(db,limit=500):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='edition_vacancies'").fetchone():return {'items':[],'pending':[]}
    items=[]
    for row in db.execute('SELECT * FROM edition_vacancies ORDER BY created_at DESC LIMIT ?',(min(1000,max(1,limit)),)):
        card=store.read(row['card_json'],{})
        origins=[dict(r) for r in db.execute('SELECT * FROM edition_vacancy_origins WHERE vacancy_id=? ORDER BY received_at',(row['vacancy_id'],))]
        items.append({**card,'vacancy_id':row['vacancy_id'],'state':row['state'],'created_at':row['created_at'],'origins':origins})
    pending=[dict(r) for r in db.execute("SELECT r.material_id,m.title,m.content,m.description,m.received_at,m.snapshot_json FROM edition_vacancy_routes r JOIN edition_materials m USING(material_id) WHERE r.kind='pending' ORDER BY r.created_at DESC LIMIT 100")]
    pending=[p for p in pending if not is_resume(p)]
    for p in pending:p['url']=store.read(p.pop('snapshot_json'),{}).get('url','')
    return {'items':items,'pending':pending}


def protect_recovery(db,material):
    """A restart before classification cannot deliver a recruitment source."""
    if not db.execute('SELECT 1 FROM edition_vacancy_routes WHERE material_id=?',(material['material_id'],)).fetchone() and candidate(material):
        decision=db.execute('SELECT result_json FROM edition_semantic_decisions WHERE material_id=? ORDER BY created_at DESC LIMIT 1',(material['material_id'],)).fetchone()
        entry=store.read(decision[0],{}) if decision else {}
        if entry.get('content_kind')!='news':
            route(db,material,entry or {'decision':'uncertain','vacancies':[]})
    return news_material(db,material)


def context_material(db,material):
    result=news_material(db,material)
    if result is None:return None
    if candidate(result):
        row=db.execute('SELECT result_json FROM edition_semantic_decisions WHERE material_id=? ORDER BY created_at DESC LIMIT 1',(material['material_id'],)).fetchone()
        if not row or store.read(row[0],{}).get('content_kind')!='news':return None
    return result
