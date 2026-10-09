"""Per-post writer/checker metrics from persisted receipts; unknown stays unknown."""
import json
import math


def aggregate(events):
    receipts = {}
    by_stage = {k: {} for k in ('draft','review_response','repair')}
    latency = None
    incomplete = False
    for stage, raw in events:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if stage == 'published':
            latency = payload.get('received_to_channel_seconds')
        if stage == 'review_metrics' and payload.get('error'):
            incomplete = True
        if stage not in {'draft', 'repair', 'review_response'}:
            continue
        receipt = payload.get('receipt') or {}
        response_id = receipt.get('response_id')
        if response_id:
            receipts[response_id] = receipt
            by_stage[stage][response_id] = receipt
        else:
            incomplete = True
    def total(values):
        return sum(values) if values and not incomplete and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
            for v in values) else None
    return {'calls': len(receipts) if receipts and not incomplete else None,
            'api_seconds': total([r.get('elapsed_seconds') for r in receipts.values()]),
            'usd': total([(r.get('pricing') or {}).get('total_usd') for r in receipts.values()]),
            'scope': 'writer_and_checker', 'received_to_post_seconds':latency,
            'stages':{stage:{'calls':len(rows), 'seconds':total([r.get('elapsed_seconds') for r in rows.values()]),
                             'usd':total([(r.get('pricing') or {}).get('total_usd') for r in rows.values()])}
                      for stage,rows in by_stage.items()}}


def for_posts(db, post_ids):
    ids = list(dict.fromkeys(post_ids))
    result = {i: aggregate([]) for i in ids}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not ids or not {'edition_documents', 'edition_events'} <= tables:
        return result
    events = {i: [] for i in ids}
    for row in db.execute('SELECT d.post_id,e.stage,e.payload_json FROM edition_documents d '
                          'JOIN edition_events e USING(document_id) '
                          'WHERE d.post_id IN (SELECT value FROM json_each(?)) ORDER BY e.event_id',
                          (json.dumps(ids),)):
        events[row[0]].append((row[1], row[2]))
    result = {i: aggregate(rows) for i, rows in events.items()}
    for post_id, value in result.items():
        planning = {}
        job_sizes = {}
        for r in db.execute('SELECT DISTINCT j.job_id,(SELECT COUNT(*) FROM edition_documents x WHERE x.job_id=j.job_id) AS documents '
                            'FROM edition_documents d JOIN edition_jobs j USING(job_id) WHERE d.post_id=?', (post_id,)):
            job_sizes[r[0]] = r[1]
            for e in db.execute("SELECT payload_json FROM edition_events WHERE job_id=? AND stage IN ('plan','plan_response') ORDER BY event_id",(r[0],)):
                receipt = json.loads(e[0]).get('receipt') or {}
                if receipt.get('response_id'):
                    planning[receipt['response_id']] = (receipt,r[1])
        def share(field):
            values = [(r.get('pricing') or {}).get('total_usd') if field=='usd' else r.get('elapsed_seconds') for r,n in planning.values()]
            if not values or any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in values):
                return None
            return sum(v/n for v,(_,n) in zip(values,planning.values()) if n)
        value['planning_share_usd'] = share('usd')
        value['stages']['planning'] = {'seconds':share('seconds'),'usd':share('usd'),'shared':True}
        value['planning_documents'] = sum(job_sizes.values())
        value['accounted_path_usd'] = (value['usd']+value['planning_share_usd']
            if value['usd'] is not None and value['planning_share_usd'] is not None else None)
    return result
