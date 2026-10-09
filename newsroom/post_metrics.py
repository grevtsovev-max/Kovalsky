"""Per-post writer/checker metrics from persisted receipts; unknown stays unknown."""
import json
import math


def aggregate(events):
    receipts = {}
    incomplete = False
    for stage, raw in events:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if stage == 'review_metrics' and payload.get('error'):
            incomplete = True
        if stage not in {'draft', 'repair', 'review_response'}:
            continue
        receipt = payload.get('receipt') or {}
        response_id = receipt.get('response_id')
        if response_id:
            receipts[response_id] = receipt
        else:
            incomplete = True
    def total(values):
        return sum(values) if values and not incomplete and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
            for v in values) else None
    return {'calls': len(receipts) if receipts and not incomplete else None,
            'api_seconds': total([r.get('elapsed_seconds') for r in receipts.values()]),
            'usd': total([(r.get('pricing') or {}).get('total_usd') for r in receipts.values()]),
            'scope': 'writer_and_checker'}


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
    return {i: aggregate(rows) for i, rows in events.items()}
