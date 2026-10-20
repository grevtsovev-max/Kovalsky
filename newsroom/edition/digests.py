"""Scheduled link collections from published posts, independent of collection/AI."""
import html
import json
import logging
import re
from datetime import datetime,timedelta,timezone,time
from zoneinfo import ZoneInfo
from ..agent_control import require_enabled,AgentDisabled
from ..db import connect
from ..delivery import deliver,confirm,channel,DeliveryUncertain
from ..locking import acquire_named_lock
from . import store

SCHEMA='''CREATE TABLE IF NOT EXISTS edition_digest_runs (
 run_key TEXT PRIMARY KEY, kind TEXT NOT NULL, period_start TEXT NOT NULL,
 period_end TEXT NOT NULL, state TEXT NOT NULL, posts_json TEXT NOT NULL,
 messages_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);'''


def settings(config):
    value=config.get('digests',{})
    zone=ZoneInfo(value.get('timezone','Europe/Moscow'))
    daily=time.fromisoformat(value.get('daily_time','20:05'))
    weekly=time.fromisoformat(value.get('weekly_time','19:00'))
    day=int(value.get('weekly_day',5))
    if daily.tzinfo or weekly.tzinfo or not 0<=day<=6:raise ValueError('DIGEST_SCHEDULE_INVALID')
    return value,zone,daily,weekly,day


def slots(config,now):
    value,zone,daily,weekly,day=settings(config)
    local=now.astimezone(zone);result=[]
    for kind,clock,days in [('daily',daily,1),('weekly',weekly,7)]:
        date=local.date()
        if kind=='weekly':date-=timedelta(days=(local.weekday()-day)%7)
        end=datetime.combine(date,clock,zone)
        if end>local:end-=timedelta(days=days)
        result.append((kind,end-timedelta(days=days),end))
    return result


def published(db,config,start,end):
    from ..cli import _telegram_message_url
    target=channel(config);result=[];seen=set()
    rows=db.execute('''SELECT p.* FROM posts p WHERE p.status='PUBLISHED'
      AND julianday(p.published_at)>julianday(?) AND julianday(p.published_at)<=julianday(?)
      AND EXISTS(SELECT 1 FROM publication_attempts a WHERE a.post_id=p.post_id
                 AND a.channel_id=? AND a.status IN ('SENT','CONFIRMED'))
      ORDER BY julianday(p.published_at),p.post_id''',(start.isoformat(),end.isoformat(),target))
    for row in rows:
        message=str(row['external_id'] or '')
        if not message.isdigit():continue
        observation=db.execute('SELECT status FROM channel_post_observations WHERE channel_id=? AND message_id=? ORDER BY observation_id DESC LIMIT 1',(target,message)).fetchone()
        if observation and observation[0]=='DELETED':continue
        # Current saved post includes acknowledged owner edits. Never re-summarize facts.
        headline=html.unescape(re.sub('<[^>]*>','',row['text'].splitlines()[0])).strip()
        if headline.startswith('**') and headline.endswith('**'):headline=headline[2:-2].strip()
        url=_telegram_message_url(config,message)
        identity=' '.join(row['text'].casefold().split())
        if not headline or not url or identity in seen:continue
        seen.add(identity);result.append({'post_id':row['post_id'],'headline':headline,'url':url})
    from .ranking import lookup
    ranks=lookup(db,[dict(r) for r in db.execute("SELECT post_id,text FROM posts WHERE status='PUBLISHED'")])
    for post in result:post['importance']=ranks.get(post['post_id'])
    # Stable ordering within each level preserves publication chronology.
    weights={'HIGH':0,'MEDIUM':1,'ORDINARY':2}
    result.sort(key=lambda p:weights.get((p.get('importance') or {}).get('level'),3))
    return result


def compose(kind,start,end,posts):
    period=end.strftime('%d.%m.%Y') if kind=='daily' else (start+timedelta(days=1)).strftime('%d.%m')+'–'+end.strftime('%d.%m.%Y')
    title='📣 Крипторынок: новости за '+('день' if kind=='daily' else 'неделю')+' · '+period
    messages=[];lines=[];size=len(title)
    for post in posts:
        line='<a href="'+html.escape(post['url'],quote=True)+'">'+html.escape(post['headline'])+'</a>'
        if lines and size+len(line)+2>3500:
            messages.append('<b>'+html.escape(title)+'</b>\n\n'+'\n\n'.join(lines));lines=[];size=len(title)
        lines.append(line);size+=len(line)+2
    if lines:messages.append('<b>'+html.escape(title)+'</b>\n\n'+'\n\n'.join(lines))
    return messages


def state(db,key,value):
    db.execute('INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))


def tick(db,config,*,now=None,send=None):
    require_enabled(config)
    value,zone,_,_,_=settings(config)
    if value.get('enabled') is not True:return []
    db.executescript(SCHEMA);now=now or datetime.now(timezone.utc)
    activation=db.execute("SELECT value FROM app_state WHERE key='digest_v2_activation'").fetchone()
    if not activation:
        state(db,'digest_v2_activation',now.isoformat());activation=(now.isoformat(),)
    activated=datetime.fromisoformat(activation[0]);done=[]
    for kind,start,end in slots(config,now):
        prefix='digest' if kind=='daily' else 'weekly_digest'
        state(db,prefix+'_next_at',(end+timedelta(days=1 if kind=='daily' else 7)).isoformat())
        if end<activated:continue
        key=channel(config)+':edition_digest:'+kind+':'+end.date().isoformat()
        row=db.execute('SELECT * FROM edition_digest_runs WHERE run_key=?',(key,)).fetchone()
        if not row:
            posts=published(db,config,start,end);messages=compose(kind,start,end,posts)
            stamp=store.stamp()
            db.execute('INSERT INTO edition_digest_runs VALUES(?,?,?,?,?,?,?,?,?)',
                       (key,kind,start.isoformat(),end.isoformat(),'PENDING' if messages else 'EMPTY',store.encoded(posts),store.encoded(messages),stamp,stamp))
            row=db.execute('SELECT * FROM edition_digest_runs WHERE run_key=?',(key,)).fetchone()
        db.commit()
    db.commit()
    target=channel(config)+':edition_digest:'
    for row in db.execute("SELECT * FROM edition_digest_runs WHERE state='PENDING' AND substr(run_key,1,?)=? ORDER BY period_end,kind",(len(target),target)).fetchall():
        kind=row['kind'];end=datetime.fromisoformat(row['period_end']);key=row['run_key']
        prefix='digest' if kind=='daily' else 'weekly_digest'
        messages=json.loads(row['messages_json'])
        if send is None:
            from ..cli import telegram_api
            from ..delivery import TelegramReceipt
            def send(conf,text):
                return TelegramReceipt(telegram_api(conf,'sendMessage',{'chat_id':channel(conf),'text':text,
                    'parse_mode':'HTML','disable_web_page_preview':True}))
        try:
            for index,message in enumerate(messages):
                operation='edition_digest:'+kind+':'+end.date().isoformat()+':'+str(index)
                deliver(db,config,operation,message,send);confirm(db,config,operation);db.commit()
        except DeliveryUncertain:
            db.execute("UPDATE edition_digest_runs SET state='UNKNOWN',updated_at=? WHERE run_key=?",(store.stamp(),key));db.commit()
            continue
        db.execute("UPDATE edition_digest_runs SET state='SENT',updated_at=? WHERE run_key=?",(store.stamp(),key))
        state(db,prefix+'_last_local_date',end.date().isoformat());state(db,prefix+'_last_sent_at',now.isoformat())
        db.commit();done.append(key)
    state(db,'digest_scheduler_last_tick_at',now.isoformat());db.commit()
    return done


def run(config_path,stop):
    from ..cli import load_config
    while not stop.is_set():
        lock=None;db=None
        try:
            config=load_config(config_path)
            require_enabled(config)
            if config.get('digests',{}).get('enabled') is True:
                lock=acquire_named_lock(config['newsroom']['database'],'digests')
                if lock:
                    db=connect(config['newsroom']['database']);tick(db,config)
                    from ..topic_registry import attach_cached
                    from .ranking import classify
                    attach_cached(config);classify(db,config)
        except AgentDisabled:pass
        except Exception as exc:
            logging.getLogger('newsroom.edition').error('DIGEST_WORKER:%s',getattr(exc,'code',None) or type(exc).__name__)
        finally:
            if db:db.close()
            if lock:lock.close()
        stop.wait(30)
