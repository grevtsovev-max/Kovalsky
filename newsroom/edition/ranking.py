"""Advisory importance of published news; never an admission or delivery gate."""
import html
import json
import re
from datetime import datetime,timedelta,timezone
from . import model,store
from ..agent_control import require_enabled,AgentDisabled

VERSION='news-importance-v1'
LEVELS={'HIGH':'Высокий','MEDIUM':'Средний','ORDINARY':'Обычный'}
CRITERIA={
 'HIGH':{'law_adopted':'Принятый закон', 'sanctions_rf':'Санкции на РФ',
         'major_exchange_loss':'Потеря клиентских средств крупных мировых бирж',
         'rf_cis_loss':'Потеря клиентских средств любого участника РФ/СНГ',
         'rf_cis_access':'Изменение доступа для РФ/СНГ',
         'cbr_register':'Вступление в реестры ЦБ',
         'crypto_launch':'Запуск криптобирж, цифровых депозитариев или криптообменников',
         'aml_launch':'Запуск AML-решений'},
 'MEDIUM':{'regulation_draft':'Проект регулирования','product_launch':'Запуск продукта',
           'substantive_update':'Существенное обновление известного события',
           'event':'Ивенты','tracked_statement':'Высказывания отслеживаемых лиц'},
 'ORDINARY':{'local_event':'Локальное событие','minor_service_change':'Небольшое изменение сервиса',
             'commentary':'Комментарий без новых последствий'}}
SCHEMA='''CREATE TABLE IF NOT EXISTS edition_post_ranks (
 post_id INTEGER PRIMARY KEY REFERENCES posts(post_id),text_hash TEXT NOT NULL,
 policy_hash TEXT NOT NULL,level TEXT,status TEXT NOT NULL,reason TEXT NOT NULL,
 criterion TEXT,quote TEXT,receipt_json TEXT NOT NULL,next_at TEXT,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edition_rank_events (
 event_id INTEGER PRIMARY KEY,post_ids_json TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL
);'''
RESPONSE=model.obj({'ranks':model.arr(model.obj({'post_id':model.I,
 'level':{'type':'string','enum':list(LEVELS)},'criterion':model.S,'reason':model.S,'quote':model.S}))})


def policy_hash(config):
    people=config.get('ai',{}).get('_keyword_prefilter',{}).get('people',[])
    return store.sha({'version':VERSION,'criteria':CRITERIA,'tracked_people':people})


def text(value):return html.unescape(re.sub('<[^>]*>','',value))


def lookup(db,posts):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='edition_post_ranks'").fetchone():return {}
    saved={r['post_id']:dict(r) for r in db.execute('SELECT * FROM edition_post_ranks')}
    result={}
    for post in posts:
        row=saved.get(post['post_id'])
        if row and row['text_hash']==store.sha(post.get('text','')) and row['status']=='RANKED' and store.read(row['receipt_json'],{}).get('rank_version')==VERSION:
            result[post['post_id']]={'level':row['level'],'label':LEVELS[row['level']],
                'reason':row['reason'],'criterion':row['criterion'],'quote':row['quote']}
    return result


def classify(db,config,*,now=None):
    require_enabled(config)
    db.executescript(SCHEMA)
    now=now or datetime.now(timezone.utc);digest=policy_hash(config)
    cutoff=(now-timedelta(days=7)).isoformat();pending=[]
    for row in db.execute("SELECT p.*,r.text_hash AS ranked_hash,r.policy_hash,r.status AS rank_status,r.next_at FROM posts p LEFT JOIN edition_post_ranks r USING(post_id) WHERE p.status='PUBLISHED' AND julianday(p.published_at)>=julianday(?) ORDER BY p.published_at DESC,p.post_id DESC",(cutoff,)):
        if row['ranked_hash']==store.sha(row['text']) and row['policy_hash']==digest:
            if row['rank_status']=='RANKED' or row['next_at'] and row['next_at']>now.isoformat():continue
        pending.append({'post_id':row['post_id'],'text':row['text']})
        if len(pending)==20:break
    if not pending:return 0
    db.commit();known={p['post_id']:p for p in pending}
    result=None;receipt={};error=None
    try:
        result,receipt=model.ask('news_ranker',RESPONSE,
            'Оцени важность УЖЕ опубликованных новостей по rank_criteria. Это отдельная оценка для порядка в дайджесте; '
            'не проверяй допуск, не отклоняй новости и не переписывай их. '
            'При нескольких критериях выбери самый высокий применимый уровень. '
            'Принятый закон не путай с проектом. Запуск инфраструктуры/AML выше обычного запуска продукта. '
            'Высказывание лица из tracked_people — MEDIUM, обычный комментарий без новых последствий — ORDINARY. '
            'Если высказывание сообщает событие HIGH, выбирай HIGH. '
            'Географию, масштаб потери средств, статус и последствия не придумывай. '
            'Вес и известность источника не определяют важность события. '
            'Верни ровно одну запись на каждый post_id: level, criterion из соответствующего уровня, '
            'короткую причину по-русски и дословную quote из текста поста. '
            'Все посты останутся в дайджесте.',
            {'rank_criteria':CRITERIA,'tracked_people':config.get('ai',{}).get('_keyword_prefilter',{}).get('people',[]),
             'posts':[{'post_id':p['post_id'],'text':text(p['text'])} for p in pending]},
            {**config.get('ai',{}),'_agent_control_config':config,'edition_max_output_tokens':6000})
        entries=result.get('ranks',[])
        if len(entries)!=len(known) or {e.get('post_id') for e in entries}!=set(known):raise ValueError('RANK_COVERAGE')
    except AgentDisabled:raise
    except Exception as exc:
        error=getattr(exc,'code',None) or type(exc).__name__;entries=[]
    receipt={**receipt,'rank_version':VERSION}
    db.execute('INSERT INTO edition_rank_events(post_ids_json,payload_json,created_at) VALUES(?,?,?)',
               (store.encoded(list(known)),store.encoded({'result':result,'receipt':receipt,'error':error}),store.stamp()))
    by_id={e['post_id']:e for e in entries};ranked=0
    for pid,post in known.items():
        entry=by_id.get(pid);valid=bool(entry and entry.get('level') in LEVELS and entry.get('criterion') in CRITERIA[entry['level']]
            and entry.get('reason') and entry.get('quote') and ' '.join(entry['quote'].split()) in ' '.join(text(post['text']).split()))
        status='RANKED' if valid else 'UNRANKED'
        if valid:ranked+=1
        db.execute('INSERT OR REPLACE INTO edition_post_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (pid,store.sha(post['text']),digest,entry['level'] if valid else None,status,
             entry['reason'] if valid else 'Важность пока не подтверждена.',entry['criterion'] if valid else None,
             entry['quote'] if valid else None,store.encoded(receipt),(now+timedelta(minutes=30)).isoformat() if not valid else None,store.stamp()))
    db.commit();return ranked
