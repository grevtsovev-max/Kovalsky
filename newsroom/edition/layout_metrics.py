"""Measured review cohorts; unknown costs stay unknown, no inferred dollar savings."""
from statistics import median
from . import store
from .layout_review import UPDATE


def summary(db,limit=500):
    documents=db.execute('SELECT document_id,repairs,state FROM edition_documents ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
    cohorts={'with_layout':[], 'without_layout':[]}
    for document in documents:
        events=[(r['stage'],store.read(r['payload_json'],{})) for r in db.execute('SELECT stage,payload_json FROM edition_events WHERE document_id=? ORDER BY event_id',(document['document_id'],))]
        layout=[p for s,p in events if s=='layout_review' and p.get('update')==UPDATE]
        reviews=[p for s,p in events if s=='review_metrics' and p.get('update')==UPDATE]
        if not reviews:continue
        receipts={}
        for stage,payload in events:
            if stage in ('draft','repair','review_response'):
                receipt=payload.get('receipt',{})
                if receipt.get('response_id'):receipts[receipt['response_id']]=receipt
        costs=[r.get('pricing',{}).get('total_usd') for r in receipts.values()]
        durations=[r.get('elapsed_seconds') for r in receipts.values()]
        complete=not any(r.get('error') for r in reviews)
        row={'changed':any(p.get('changed') for p in layout),'repairs':document['repairs'],
             'avoided_format_return':bool(layout and layout[0].get('before_codes') and not layout[0].get('after_codes') and reviews[0].get('passed') and document['repairs']==0),
             'review_seconds':sum(r['seconds'] for r in reviews), 'calls':len(receipts),
             'api_seconds':sum(durations) if complete and durations and all(isinstance(v,(int,float)) for v in durations) else None,
             'usd':sum(costs) if complete and costs and all(isinstance(v,(int,float)) for v in costs) else None,
             'published_seconds':next((p.get('received_to_channel_seconds') for s,p in reversed(events) if s=='published'),None)}
        cohorts['with_layout' if layout and layout[0].get('enabled') else 'without_layout'].append(row)
    result={'update':UPDATE,'sample_limit':limit,'scope':'writer_and_checker; planning, transport retries and short fallback excluded','comparison':'observational; matched sandbox is required for causal savings'}
    for key,rows in cohorts.items():
        result[key]={'documents':len(rows),'changed_documents':sum(r['changed'] for r in rows),
            'avoided_format_returns':sum(r['avoided_format_return'] for r in rows),
            'repair_rate':sum(r['repairs']>0 for r in rows)/len(rows) if rows else None,
            'median_ai_calls':median(r['calls'] for r in rows) if rows else None,
            'known_cost_documents':sum(r['usd'] is not None for r in rows)}
        for field in ('review_seconds','api_seconds','usd','published_seconds'):
            values=[r[field] for r in rows if r[field] is not None]
            result[key]['median_'+field]=median(values) if values else None
    return result
