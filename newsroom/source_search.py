"""Bounded, distinct source recovery strategies with append-only search evidence."""
from __future__ import annotations
import hashlib
import json
import re
import urllib.parse
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_search_log (
 search_id INTEGER PRIMARY KEY, item_url TEXT NOT NULL, attempt INTEGER NOT NULL,
 strategy TEXT NOT NULL, query TEXT NOT NULL, outcome TEXT NOT NULL,
 revision_hash TEXT NOT NULL DEFAULT '',
 checked_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_search_item_idx ON source_search_log(item_url,search_id);
CREATE TRIGGER IF NOT EXISTS source_search_no_update BEFORE UPDATE ON source_search_log
BEGIN SELECT RAISE(ABORT,'search history is append only'); END;
CREATE TRIGGER IF NOT EXISTS source_search_no_delete BEFORE DELETE ON source_search_log
BEGIN SELECT RAISE(ABORT,'search history is append only'); END;
"""
STRATEGIES=('TITLE_AND_QUOTE_SEARCH','OFFICIAL_SOURCE_SEARCH','ALTERNATIVE_SOURCE_SEARCH')


def query_for(item, attempt):
    title=str(item.get('title',''))[:220]
    if attempt==1:
        return title+' when:2d'
    if attempt==2:
        domains='site:cbr.ru OR site:minfin.gov.ru OR site:government.ru OR site:pravo.gov.ru'
        if re.search('беларус|ПВТ',title,re.I):
            domains='site:nbrb.by OR site:president.gov.by OR site:park.by'
        elif re.search('казахстан|МФЦА',title,re.I):
            domains='site:afsa.aifc.kz OR site:nationalbank.kz OR site:gov.kz'
        return title+' ('+domains+')'
    quoted=re.findall('[«“"]([^»”"]{16,120})[»”"]',str(item.get('content','')))
    if quoted:
        return '"'+quoted[0]+'"'
    words=re.findall(r'[\w-]+',title)
    return ' '.join(words[:12])+' заявление документ интервью первоисточник'


def log(db,item_url,attempt,strategy,query,outcome,checked,revision_hash=''):
    db.execute('INSERT INTO source_search_log(item_url,attempt,strategy,query,outcome,revision_hash,checked_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
               (item_url,attempt,strategy,query,outcome,revision_hash,json.dumps(checked,ensure_ascii=False),datetime.now(timezone.utc).isoformat(timespec='seconds')))


def recover(db,item,settings,news_search,web_search,terms,similarity):
    item.pop('_source_search_deferred',None)
    revision_hash=hashlib.sha256((str(item.get('title',''))+'\n'+str(item.get('content',''))).encode()).hexdigest()
    prior=db.execute('SELECT MAX(attempt) FROM source_search_log WHERE item_url=? AND revision_hash=?',(item['url'],revision_hash)).fetchone()[0] or 0
    if prior>=3:
        return None
    if settings.get('_recovery_search_budget',0)<=0:
        item['_source_search_deferred']=True
        return None
    settings['_recovery_search_budget']-=1
    attempt=prior+1; strategy=STRATEGIES[attempt-1]; query=query_for(item,attempt)
    quota_reserve=getattr(web_search,'reserve_primary_recovery',None)
    if quota_reserve is None:
        quota_reserve=getattr(web_search,'reserve',None)
    if attempt > 1 and quota_reserve and not quota_reserve():
        settings['_recovery_search_budget']+=1
        item['_source_search_deferred']=True
        return None
    log(db,item['url'],attempt,strategy,query,'STARTED',[],revision_hash)
    db.commit()
    try:
        if attempt==1:
            url='https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':query,'hl':'ru','gl':'RU','ceid':'RU:ru'})
            found=news_search(url)
        else:
            found=web_search(query,settings)
    except Exception as exc:
        log(db,item['url'],attempt,strategy,query,'ERROR',[{'error_code':type(exc).__name__}],revision_hash)
        db.commit()
        return None
    checked=[]; selected=None
    for article in found:
        checked.append({key:article.get(key) for key in ('url','title','content','primary_source_url','primary_source_content','primary_source_status','primary_source_type')})
        if (selected is None and article.get('primary_source_status')=='READ'
                and article.get('primary_source_url') and article.get('primary_source_content')
                and len(terms(item['title']) & terms(article.get('title',''))) >=3
                and similarity(item['title'],article.get('title',''))>=.2):
            selected=article
    log(db,item['url'],attempt,strategy,query,'FOUND_CANDIDATE' if selected else 'NOT_FOUND',checked,revision_hash)
    db.commit()
    return selected
