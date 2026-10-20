"""Small local publication context; similarity retrieves candidates, never rejects."""
from __future__ import annotations
import math
import re
from collections import Counter
from functools import lru_cache
from datetime import datetime,timedelta,timezone
from . import store
from .source_text import text

STOP={'источник','компания','который','также','этот','новый','сообщить','свой','быть','для','что','как','при','или','его','она','они','это','из','на','по','в','и','с','за','от','the','and','with','from'}


@lru_cache(maxsize=2048)
def terms(value):
    from ..keyword_filter import forms
    result=set()
    for word in re.findall(r'[^\W_]+',text(value).casefold()):
        if len(word)<3 or word in STOP:continue
        variants=forms(word)
        result.add(sorted(variants)[0] if variants else word)
    return result-STOP


def candidates(db,draft,group,*,now=None):
    now=now or datetime.now(timezone.utc)
    rows=db.execute("SELECT post_id,external_id,published_at,text FROM posts WHERE status='PUBLISHED' AND external_id IS NOT NULL AND julianday(published_at)>=julianday(?) AND julianday(published_at)<=julianday(?) ORDER BY published_at DESC,post_id DESC",((now-timedelta(days=7)).isoformat(),now.isoformat())).fetchall()
    query=terms(' '.join([draft.get('headline',''),draft.get('lead',''),
        *(b.get('text','') for b in draft.get('blocks',[])),
        group.get('subject',''),group.get('event_key',''),*group.get('entities',[])]))
    if not rows or not query:return []
    documents=[terms(r['text']) for r in rows]
    frequency=Counter(t for doc in documents for t in doc)
    weight=lambda t:math.log(1+len(rows)/(1+frequency[t]))
    ranked=[]
    for row,doc in zip(rows,documents):
        overlap=query & doc
        if len(overlap)<2:continue
        score=sum(weight(t) for t in overlap)/max(1,sum(weight(t) for t in query))
        ranked.append((score,row))
    ranked.sort(key=lambda entry:entry[0],reverse=True)
    return [{'post_id':r['post_id'],'message_id':r['external_id'],'published_at':r['published_at'],'text':text(r['text'])} for _,r in ranked[:3]]


def context(db,document,draft,group):
    try:result=candidates(db,draft,group)
    except Exception as exc:
        store.event(db,document['job_id'],'publication_history_unavailable',{'error':type(exc).__name__},document['document_id'])
        return []
    store.event(db,document['job_id'],'publication_history',{'posts':result},document['document_id'])
    return result


def update_issues(verdict,history,materials,group,draft):
    """New facts must belong to the selected event and appear in the post.

    Older saved verdicts have no updates field; do not invalidate their proof.
    Current structured responses always include it, even when the list is empty.
    """
    decision=verdict.get('publication_history',{})
    if not history or decision.get('decision')!='new_facts' or 'updates' not in decision:return []
    from .formatting import contains
    initial=set(group.get('material_ids',[]));sources={m['material_id']:m for m in materials}
    fields=[draft.get('headline',''),draft.get('lead',''),*(b.get('text','') for b in draft.get('blocks',[]))]
    anchor=draft.get('lead') or draft.get('headline','')
    issues=[];updates=decision.get('updates')
    if not isinstance(updates,list) or not updates:
        return [{'code':'duplicates','material_id':next(iter(initial),0),'post_fragment':anchor,
                 'source_fragment':'','reason':'Допуск как обновления не содержит доказательства нового факта выбранного события в тексте поста.','main_fact':False}]
    for update in updates:
        mid=update.get('material_id');quote=update.get('quote');fragment=update.get('post_fragment')
        scopes=[f['scope'] for f in group.get('focus',[]) if f.get('material_id')==mid and f.get('scope')]
        grounded=mid in initial and contains(sources.get(mid,{}),quote)
        selected=grounded and (not scopes or any(contains({'content':s},quote) for s in scopes))
        retained=isinstance(fragment,str) and bool(fragment.strip()) and any(fragment in field for field in fields)
        if selected and retained:continue
        issues.append({'code':'new_clarification' if selected else 'duplicates',
            'material_id':mid if mid in initial else next(iter(initial),0),'post_fragment':anchor,
            'source_fragment':quote if selected else '',
            'reason':('Новое существенное сведение выбранного события не вошло в текст поста.' if selected else
                      'Сведение для допуска обновления не подтверждено выбранным событием; соседний сюжет не разрешает повтор.'),
            'main_fact':False})
    return issues


def confirmed(verdict,history,materials,group):
    """Require a selected delivered post and verbatim evidence on both sides."""
    decision=verdict.get('publication_history',{})
    if not isinstance(decision,dict) or decision.get('decision')!='duplicate':return None
    if decision.get('updates'):return None  # Contradictory novelty is not proof of a repeat.
    if not isinstance(decision.get('reason'),str) or not decision['reason'].strip():return None
    post=next((p for p in history if p['post_id']==decision.get('post_id')),None)
    fragment=decision.get('published_fragment')
    if not post or not isinstance(fragment,str) or not fragment.strip() or fragment not in post['text']:return None
    from .formatting import contains
    evidence=decision.get('evidence',[])
    initial=set(group.get('material_ids',[]));sources={m['material_id']:m for m in materials}
    if not isinstance(evidence,list) or not evidence or not all(isinstance(e,dict) and isinstance(e.get('quote'),str) for e in evidence):return None
    if {e.get('material_id') for e in evidence}!=initial:return None
    if any(not e.get('quote','').strip() or not contains(sources.get(e.get('material_id'),{}),e['quote']) for e in evidence):return None
    return {**decision,'message_id':post['message_id'],'code':'published_duplicate'}


def cover(db,document,reason):
    # No post or delivery intent is created for a confirmed semantic repeat.
    db.execute("UPDATE edition_documents SET state='DUPLICATE',reasons_json=?,updated_at=? WHERE document_id=?",(store.encoded([reason]),store.stamp(),document['document_id']))
    db.commit()
    store.event(db,document['job_id'],'published_duplicate',reason,document['document_id'])
