"""Finite editorial preparation and independent verification on frozen evidence."""
from __future__ import annotations
import json
import re
import uuid
from . import model,store
from .formatting import contains,normalize,render,validate


def merge_groups(groups):
    """Union shared named actors or the same event, never a generic topic."""
    output=[]
    for group in groups:
        names={n.casefold().strip() for n in group['entities'] if n.strip()}
        keys={k.casefold().strip() for k in group.get('_event_keys',[group['event_key']]) if k.strip()}
        group['_event_keys']=list(keys)
        matches=[g for g in output if names & {n.casefold().strip() for n in g['entities']} or keys & set(g['_event_keys'])]
        if not matches:output.append(group);continue
        merged=matches[0]
        for other in [group,*matches[1:]]:
            merged['_event_keys']=list(dict.fromkeys([*merged['_event_keys'],*other['_event_keys']]))
            merged['entities']=list(dict.fromkeys([*merged['entities'],*other['entities']]))
            merged['material_ids']=list(dict.fromkeys([*merged['material_ids'],*other['material_ids']]))
            merged['focus']+=other['focus']
            merged['lookup_query']=' '.join(filter(None,[merged['lookup_query'],other['lookup_query']]))
            if other in output and other is not merged:output.remove(other)
    return output


def plan_all(db,job_id,inputs,settings):
    # Chunk complete materials for transport; never silently truncate an article.
    batches=[];batch=[];size=0
    for source in inputs:
        length=len(store.encoded(source))
        if batch and size+length>70000:batches.append(batch);batch=[];size=0
        batch.append(source);size+=length
    if batch:batches.append(batch)
    groups=[];excluded=[]
    for batch in batches:
        try:result,receipt=model.plan(batch,settings)
        except Exception as exc:
            if not model.bundle()[0].get('short_release'):raise
            store.event(db,job_id,'plan_group_error',{'code':getattr(exc,'code',None) or type(exc).__name__,'material_ids':[m['material_id'] for m in batch]})
            result={'groups':[],'excluded':[]};receipt={'kind':'planner_unavailable'}
        store.event(db,job_id,'plan_response',{'result':result,'receipt':receipt})
        known={m['material_id']:m for m in batch};covered=set()
        for group in result.get('groups',[]):
            if model.bundle()[0].get('short_release'):
                try:
                    checked=validate_group(group,known)
                except (ValueError,TypeError,KeyError) as exc:
                    store.event(db,job_id,'plan_group_error',{'code':str(exc),'material_ids':group.get('material_ids',[])})
                    continue
                covered.update(checked);groups.append(group);continue
            ids=group.get('material_ids')
            if not ids or not set(ids)<=set(known) or not group.get('focus') or not group.get('subject'):
                raise ValueError('EDITION_INVALID_PLAN')
            for focus in group['focus']:
                if focus.get('material_id') not in ids or not contains(known[focus['material_id']],focus.get('excerpt')):
                    raise ValueError('EDITION_UNGROUNDED_PLAN')
                if focus.get('scope') and (not contains(known[focus['material_id']],focus['scope']) or not contains({'content':focus['scope']},focus['excerpt'])):
                    raise ValueError('EDITION_UNGROUNDED_SCOPE')
            for evidence in group.get('topic_evidence',[]):
                if evidence.get('material_id') not in ids or not contains(known[evidence['material_id']],evidence.get('quote')):
                    raise ValueError('EDITION_UNGROUNDED_TOPIC')
                scopes=[f.get('scope') for f in group['focus'] if f['material_id']==evidence['material_id'] and f.get('scope')]
                if scopes and not any(contains({'content':scope},evidence['quote']) for scope in scopes):
                    raise ValueError('EDITION_TOPIC_FROM_DIFFERENT_EVENT')
            if set(ids)!={f['material_id'] for f in group['focus']}:raise ValueError('EDITION_PLAN_COVERAGE')
            covered.update(ids);groups.append(group)
        for entry in result.get('excluded',[]):
            if entry.get('material_id') not in known or not entry.get('reason'):
                if not model.bundle()[0].get('short_release'):raise ValueError('EDITION_INVALID_EXCLUSION')
                store.event(db,job_id,'plan_group_error',{'code':'EDITION_INVALID_EXCLUSION','material_ids':[entry.get('material_id')]})
                continue
            if entry['material_id'] in covered and entry.get('kind')=='out_of_scope':
                # A rejected neighbour in a digest cannot revoke an admitted
                # event from the same material, including on recovery.
                entry={**entry,'kind':'event_out_of_scope'}
            covered.add(entry['material_id']);excluded.append(entry)
        if covered!=set(known):
            if not model.bundle()[0].get('short_release'):raise ValueError('EDITION_PLAN_COVERAGE')
            from .fallback import source_group
            for mid in set(known)-covered:
                emergency={'subject':known[mid].get('title') or 'Сообщение','entities':[],
                           'event_key':'','material_ids':[mid],'focus':[],'lookup_query':'','_short_required':True}
                groups.append(source_group(db,emergency,[known[mid]]))
        store.event(db,job_id,'plan',{'result':result,'receipt':receipt})
    return merge_groups(groups),excluded


def validate_group(group,known):
    ids=group.get('material_ids')
    if not ids or not set(ids)<=set(known) or not group.get('focus') or not group.get('subject'):raise ValueError('EDITION_INVALID_PLAN')
    for focus in group['focus']:
        material=known.get(focus.get('material_id'))
        if focus.get('material_id') not in ids or not contains(material or {},focus.get('excerpt')):raise ValueError('EDITION_UNGROUNDED_PLAN')
        if focus.get('scope') and (not contains(material,focus['scope']) or not contains({'content':focus['scope']},focus['excerpt'])):raise ValueError('EDITION_UNGROUNDED_SCOPE')
    if set(ids)!={f['material_id'] for f in group['focus']}:raise ValueError('EDITION_PLAN_COVERAGE')
    for proof in group.get('topic_evidence',[]):
        if proof.get('material_id') not in ids or not contains(known[proof['material_id']],proof.get('quote')):raise ValueError('EDITION_UNGROUNDED_TOPIC')
        scopes=[f.get('scope') for f in group['focus'] if f['material_id']==proof['material_id'] and f.get('scope')]
        if scopes and not any(contains({'content':s},proof.get('quote')) for s in scopes):raise ValueError('EDITION_TOPIC_FROM_DIFFERENT_EVENT')
    return ids


def save_draft(db,document,draft,receipt,stage):
    draft=normalize(draft)
    db.execute('UPDATE edition_documents SET draft_json=?,state=?,updated_at=? WHERE document_id=?',
               (store.encoded(draft),'CHECKING',store.stamp(),document['document_id']))
    rendered,plain=render(draft,store.read(document['materials_json'],[]))
    db.execute('UPDATE edition_documents SET rendered_text=?,plain_text=? WHERE document_id=?',(rendered,plain,document['document_id']))
    db.commit();store.event(db,document['job_id'],stage,{'draft':draft,'receipt':receipt},document['document_id'])
    return draft


def review(db,document,draft,materials,settings):
    import time
    from .layout_review import UPDATE
    started=time.perf_counter()
    try:
        result=_review(db,document,draft,materials,settings)
    except Exception:
        store.event(db,document['job_id'],'review_metrics',{'update':UPDATE,'seconds':time.perf_counter()-started,'passed':False,'error':True},document['document_id'])
        raise
    store.event(db,document['job_id'],'review_metrics',{'update':UPDATE,'seconds':time.perf_counter()-started,'passed':result[0],'error':False},document['document_id'])
    return result


def _review(db,document,draft,materials,settings):
    from .layout_review import prepare
    draft,code=prepare(db,document,draft,materials,store.read(document['group_json'],{}),settings)
    from . import publication_history
    group=store.read(document['group_json'],{})
    history=publication_history.context(db,document,draft,group)
    options={'group':group}
    if history:options['publication_history']=history
    try:verdict,receipt=model.check(draft,materials,settings,**options)
    except model.AIResponseError as exc:
        if hasattr(exc,'review'):
            store.event(db,document['job_id'],'review_response',{'verdict':exc.review,'receipt':exc.receipt,'draft_hash':store.sha(draft),'error':exc.code},document['document_id'])
        raise
    store.event(db,document['job_id'],'review_response',{'verdict':verdict,'receipt':receipt,'draft_hash':store.sha(draft)},document['document_id'])
    update_issues=publication_history.update_issues(verdict,history,materials,group,draft)
    if update_issues:
        verdict={**verdict,'approved':False,'issues':[*verdict.get('issues',[]),*update_issues]}
        store.event(db,document['job_id'],'publication_update_invalid',{'issues':update_issues},document['document_id'])
    verdict=model.reconcile_review(verdict,draft,materials)
    model.validate_review(verdict)
    if verdict.get('source_scope')=='out_of_scope':
        sources={m['material_id']:m for m in materials}
        initial=set(store.read(document['group_json'],{}).get('material_ids',[]))
        grounded=[i for i in verdict['issues'] if i.get('material_id') in initial and i.get('code')=='scope' and i.get('reason') and contains(sources.get(i.get('material_id'),{}),i.get('source_fragment'))]
        if not grounded:raise ValueError('EDITION_UNGROUNDED_SOURCE_SCOPE')
        db.execute("UPDATE edition_documents SET state='FILTERED',reasons_json=?,updated_at=? WHERE document_id=?",
            (store.encoded(grounded),store.stamp(),document['document_id']))
        db.commit();store.event(db,document['job_id'],'source_out_of_scope',{'verdict':verdict,'receipt':receipt},document['document_id'])
        return False,grounded
    duplicate=publication_history.confirmed(verdict,history,materials,group)
    if duplicate:
        rendered,_=render(draft,materials)
        db.execute('INSERT INTO edition_checks(document_id,draft_hash,rendered_hash,bundle_hash,evidence_hash,code_json,review_json,response_json,passed,created_at) VALUES(?,?,?,?,?,?,?,?,0,?)',
            (document['document_id'],store.sha(draft),store.sha(rendered),receipt['bundle_hash'],store.sha(materials),store.encoded(code),store.encoded(verdict),store.encoded(receipt),store.stamp()))
        publication_history.cover(db,document,duplicate)
        return False,[duplicate]
    all_text='\n'.join([draft.get('headline',''),draft.get('lead',''),*(b.get('text','') for b in draft.get('blocks',[]))])
    sources={m['material_id']:m for m in materials}
    allowed={r['id'] for r in model.bundle()[0]['rules']}|{'unsupported_fact','main_conflict','new_clarification'}
    for issue in verdict['issues']:
        if issue.get('code') not in allowed or not issue.get('reason') or not issue.get('post_fragment') or issue['post_fragment'] not in all_text:
            if issue.get('code') in ('headline_meaning','lead','paragraph','body','background','details','terms','duplicates','length'):
                issue['post_fragment']=draft.get('lead') or draft.get('headline')
            else:raise ValueError('EDITION_UNSPECIFIC_REVIEW')
        source=sources.get(issue.get('material_id'))
        if issue.get('source_fragment') and (not source or not contains(source,issue['source_fragment'])):
            raise ValueError('EDITION_UNGROUNDED_REVIEW')
        if issue['code'] in ('status','main_conflict','new_clarification') and not issue.get('source_fragment'):
            raise ValueError('EDITION_REVIEW_NEEDS_SOURCE_FRAGMENT')
    if verdict['approved'] != (not verdict['issues']):raise ValueError('EDITION_CONTRADICTORY_REVIEW')
    rendered,plain=render(draft,materials)
    passed=not code and verdict['approved']
    check_id=db.execute('INSERT INTO edition_checks(document_id,draft_hash,rendered_hash,bundle_hash,evidence_hash,code_json,review_json,response_json,passed,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (document['document_id'],store.sha(draft),store.sha(rendered),receipt['bundle_hash'],store.sha(materials),
         store.encoded(code),store.encoded(verdict),store.encoded(receipt),int(passed),store.stamp())).lastrowid
    db.execute('UPDATE edition_documents SET rendered_text=?,plain_text=?,materials_json=?,reasons_json=?,state=?,updated_at=? WHERE document_id=?',
        (rendered,plain,store.encoded(materials),store.encoded([*code,*verdict['issues']]),'READY' if passed else 'CHECKING',store.stamp(),document['document_id']))
    db.commit();store.event(db,document['job_id'],'check',{'check_id':check_id,'passed':passed,'issues':[*code,*verdict['issues']]},document['document_id'])
    return passed,[*code,*verdict['issues']]


def release_failed_document(db,document,exc,settings):
    db.rollback()
    safe=getattr(exc,'code',None) or (str(exc) if re.fullmatch(r'EDITION_[A-Z_]+',str(exc)) else type(exc).__name__)
    from .fallback import release
    current=db.execute('SELECT * FROM edition_documents WHERE document_id=?',(document['document_id'],)).fetchone()
    release(db,current,source=True,reason={'code':safe,'reason':'Ошибка подготовки этого поста; выпуск продолжен по его исходнику.'},settings=settings)


def process(db,job_id,settings,*,send=None,publish=True):
    job=db.execute('SELECT * FROM edition_jobs WHERE job_id=?',(job_id,)).fetchone()
    if job and job['state']=='QUEUED' and job['retry_of']:
        db.execute("UPDATE edition_jobs SET state='SCREENING',updated_at=? WHERE job_id=?",(store.stamp(),job_id));db.commit()
    if not job or job['state'] not in ('SCREENING','PLANNING','QUEUED'):raise ValueError('EDITION_JOB_NOT_PLANNING')
    try:
        inputs=store.materials(db,store.read(job['material_ids_json'],[]))
        from .semantic_filter import screen
        inputs,semantic_excluded=screen(db,job_id,inputs,settings)
        db.execute("UPDATE edition_jobs SET state='PLANNING',updated_at=? WHERE job_id=?",(store.stamp(),job_id));db.commit()
        failed=db.execute("SELECT group_json,draft_json,reasons_json FROM edition_documents WHERE job_id=? AND state IN ('INCOMPLETE','LANGUAGE_PENDING')",(job['retry_of'],)).fetchall() if job['retry_of'] else []
        if failed:
            groups=[store.read(d[0],{}) for d in failed];excluded=[]
            admitted={m['material_id'] for m in inputs}
            groups=[g for g in groups if set(g['material_ids'])<=admitted]
            for g in groups:g['lookup_query']=' '.join([*g['entities'],g['event_key']])
            store.event(db,job_id,'retry_plan',{'groups':groups,'retry_of':job['retry_of']})
        else:
            groups,excluded=plan_all(db,job_id,inputs,settings)
        excluded=semantic_excluded+excluded
        db.execute("UPDATE edition_jobs SET groups_json=?,reasons_json=?,state='DRAFTING',updated_at=? WHERE job_id=?",(store.encoded(groups),store.encoded(excluded),store.stamp(),job_id));db.commit()
        query=' '.join(g['lookup_query'] for g in groups if g['lookup_query'])
        context=store.lookup(db,job_id,query,topic_spec=settings.get('_keyword_prefilter')) if query else []
        for group in groups:
            selected=[m for m in inputs if m['material_id'] in group['material_ids']]
            combined={m['material_id']:m for m in [*selected,*context]}
            materials=list(combined.values())
            for m in materials:m['role']='initial' if m['material_id'] in group['material_ids'] else 'context'
            docid=uuid.uuid4().hex;now=store.stamp()
            db.execute('INSERT INTO edition_documents(document_id,job_id,group_json,materials_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       (docid,job_id,store.encoded(group),store.encoded(materials),now,now));db.commit()
            document=db.execute('SELECT * FROM edition_documents WHERE document_id=?',(docid,)).fetchone()
            if group.get('_short_required'):
                from .short_release import prepare
                prepare(db,document,settings,{'code':'plan_group_invalid','reason':'Этот сюжет подготовлен коротко; остальные группы продолжают обычную редактуру.'})
                continue
            previous=next((d for d in failed if store.read(d['group_json'],{})['material_ids']==group['material_ids']),None)
            try:
                if previous:
                    draft,receipt=model.draft(group,materials,settings,previous=store.read(previous['draft_json'],{}),feedback=store.read(previous['reasons_json'],[]))
                else:
                    draft,receipt=model.draft(group,materials,settings)
                save_draft(db,document,draft,receipt,'draft')
            except Exception as exc:
                release_failed_document(db,document,exc,settings)
        query=' '.join(n for g in groups for n in [*g['entities'],g['event_key']])
        late=store.lookup(db,job_id,query,late=True,topic_spec=settings.get('_keyword_prefilter'))
        for material in late:material['role']='late_clarification_only'
        db.execute("UPDATE edition_jobs SET state='CHECKING',updated_at=? WHERE job_id=?",(store.stamp(),job_id));db.commit()
        documents=db.execute('SELECT * FROM edition_documents WHERE job_id=? ORDER BY created_at',(job_id,)).fetchall()
        for document in documents:
            if document['state'] not in ('DRAFTING','CHECKING'):continue
            materials=store.read(document['materials_json'],[])
            combined={m['material_id']:m for m in materials}
            for m in late:combined.setdefault(m['material_id'],m)
            materials=list(combined.values());draft=store.read(document['draft_json'],{})
            try:
                passed,issues=review(db,document,draft,materials,settings)
                draft=store.read(db.execute('SELECT draft_json FROM edition_documents WHERE document_id=?',(document['document_id'],)).fetchone()[0],{})
                if db.execute('SELECT state FROM edition_documents WHERE document_id=?',(document['document_id'],)).fetchone()[0] in ('FILTERED','DUPLICATE'):continue
                if not passed:
                    # A single explicit correction, with no search or re-planning loop.
                    db.execute('UPDATE edition_documents SET repairs=repairs+1 WHERE document_id=? AND repairs=0',(document['document_id'],));db.commit()
                    corrected,receipt=model.draft(store.read(document['group_json'],{}),materials,settings,previous=draft,feedback=issues)
                    draft=save_draft(db,document,corrected,receipt,'repair')
                    passed,issues=review(db,document,draft,materials,settings)
                if db.execute('SELECT state FROM edition_documents WHERE document_id=?',(document['document_id'],)).fetchone()[0] in ('FILTERED','DUPLICATE'):continue
                if not passed:
                    from .fallback import release,can_release
                    current=db.execute('SELECT * FROM edition_documents WHERE document_id=?',(document['document_id'],)).fetchone()
                    release(db,current,source=not can_release(issues),settings=settings)
            except Exception as exc:
                release_failed_document(db,document,exc,settings)
            if publish:
                from .publication import publish_document
                publish_document(db,document['document_id'],settings['_edition_config'],send=send)
        from .fallback import ensure_job
        ensure_job(db,job_id,{'code':'release_coverage','reason':'Все прошедшие первый фильтр материалы включены в выпуск.'},settings=settings)
        if publish:
            from .publication import publish_document
            for pending in db.execute("SELECT document_id FROM edition_documents WHERE job_id=? AND state='READY'",(job_id,)).fetchall():publish_document(db,pending[0],settings['_edition_config'],send=send)
        states=store.document_states(db,job_id)
        final=('FILTERED' if (not states and any(e.get('kind')=='out_of_scope' for e in excluded)) or states and all(s=='FILTERED' for s in states) else 'PUBLISHED' if states and all(s in ('PUBLISHED','FILTERED') for s in states) else 'READY' if states and all(s in ('READY','PUBLISHED','FILTERED') for s in states) else 'INCOMPLETE')
        # Planner exclusions are advisory; ensure_job supplies their source releases.
        reasons=excluded+[{'code':'preparation_incomplete','reason':'Подготовка остановлена; подробности сохранены у постов.'}] if final=='INCOMPLETE' else excluded
        store.finish(db,job_id,final,reasons)
        db.executemany("UPDATE edition_materials SET queue_state='DONE' WHERE material_id=? AND queue_state<>'QUEUED'",[(i,) for i in store.read(job['material_ids_json'],[])]);db.commit()
    except Exception as exc:
        # Keep drafts, exact check history and snapshots; never auto-restart model work.
        db.rollback()
        safe=getattr(exc,'code',None) or (str(exc) if re.fullmatch(r'EDITION_[A-Z_]+',str(exc)) else type(exc).__name__)
        from .fallback import ensure_job
        reason={'code':safe,'reason':'Ошибка подготовки: выпуск продолжается по исходному материалу.'}
        ensure_job(db,job_id,reason,settings=settings)
        if publish:
            from .publication import publish_document
            for pending in db.execute("SELECT document_id FROM edition_documents WHERE job_id=? AND state='READY'",(job_id,)).fetchall():
                publish_document(db,pending[0],settings['_edition_config'],send=send)
    return dict(db.execute('SELECT * FROM edition_jobs WHERE job_id=?',(job_id,)).fetchone())
