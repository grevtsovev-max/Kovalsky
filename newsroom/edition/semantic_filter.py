"""Durable semantic admission before any editorial model work."""
from . import model,store
from .formatting import contains


def screen(db,job_id,materials,settings):
    from . import vacancies
    db.executescript(vacancies.SCHEMA)
    from .freshness import admit
    materials,date_excluded=admit(db,materials)
    digest=model.bundle()[2]
    decisions={};pending=[]
    for material in materials:
        row=db.execute('SELECT result_json FROM edition_semantic_decisions WHERE material_id=? AND bundle_hash=?',
                       (material['material_id'],digest)).fetchone()
        if row and (store.read(row[0],{}).get('decision')=='reject' or not vacancies.candidate(material) or 'content_kind' in store.read(row[0],{})):decisions[material['material_id']]=store.read(row[0],{})
        else:pending.append(material)
    # Bound transport, not source text: a long article is never truncated.
    batches=[];batch=[];size=0
    for material in pending:
        length=len(store.encoded(material))
        if batch and size+length>70000:batches.append(batch);batch=[];size=0
        batch.append(material);size+=length
    if batch:batches.append(batch)
    for batch in batches:
        known={m['material_id']:m for m in batch}
        try:
            result,receipt=model.semantic_screen(batch,settings)
            store.event(db,job_id,'semantic_response',{'result':result,'receipt':receipt})
            entries=result.get('decisions',[])
            if len(entries)!=len(known) or {e.get('material_id') for e in entries}!=set(known):
                raise ValueError('EDITION_SEMANTIC_COVERAGE')
            for entry in entries:
                vacancies.validate(entry,known[entry['material_id']])
                if entry.get('decision') not in ('allow','reject','uncertain') or not entry.get('reason'):
                    raise ValueError('EDITION_INVALID_SEMANTIC_DECISION')
                if any(e.get('material_id')!=entry['material_id'] or not contains(known[entry['material_id']],e.get('quote')) for e in entry.get('evidence',[])):
                    raise ValueError('EDITION_UNGROUNDED_SEMANTIC_EVIDENCE')
                if entry['decision']=='reject' and (not entry.get('evidence') or any(
                    e.get('material_id')!=entry['material_id'] or not contains(known[entry['material_id']],e.get('quote'))
                    for e in entry['evidence'])):
                    raise ValueError('EDITION_UNGROUNDED_SEMANTIC_REJECTION')
        except Exception as exc:
            # Unavailability is not evidence of irrelevance. Preserve the error;
            # planner/checker still apply the same event-level exclusion.
            store.event(db,job_id,'semantic_error',{'code':getattr(exc,'code',None) or type(exc).__name__})
            entries=[{'material_id':m['material_id'],'decision':'uncertain',
                      'reason':'Смысловой отбор недоступен; решение не подтверждено.','evidence':[]} for m in batch]
            receipt={'kind':'semantic_unavailable'}
        for entry in entries:
            db.execute('INSERT INTO edition_semantic_decisions VALUES(?,?,?,?,?) ON CONFLICT(material_id,bundle_hash) DO UPDATE SET result_json=excluded.result_json,receipt_json=excluded.receipt_json,created_at=excluded.created_at',
                       (entry['material_id'],digest,store.encoded(entry),store.encoded(receipt),store.stamp()))
            decisions[entry['material_id']]=entry
        db.commit()
    excluded=[{'material_id':mid,'kind':'out_of_scope','code':entry.get('rule_code','onchain_noise'),
               'reason':entry['reason'],'evidence':entry.get('evidence',[])}
              for mid,entry in decisions.items() if entry['decision']=='reject']
    excluded=date_excluded+excluded
    if excluded:
        job=db.execute('SELECT reasons_json FROM edition_jobs WHERE job_id=?',(job_id,)).fetchone()
        reasons=store.read(job[0],[])
        reasons.extend(e for e in excluded if e not in reasons)
        db.execute('UPDATE edition_jobs SET reasons_json=? WHERE job_id=?',(store.encoded(reasons),job_id));db.commit()
    allowed=[]
    for material in materials:
        entry=decisions[material['material_id']]
        routed=vacancies.route(db,material,entry)
        if routed is None:
            excluded.append({'material_id':material['material_id'],'kind':'out_of_scope','code':'vacancy_inbox','reason':'Объявление сохранено отдельно для подборок; в новостной выпуск не направляется.'})
        elif entry['decision']!='reject':allowed.append(routed)
    return allowed,excluded
