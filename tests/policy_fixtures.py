"""Deterministic final checks for fixture texts; no live provider calls in tests."""
def final_check(decision, source, draft, settings):
    import copy
    contract = settings.get('_draft_contract', {})
    text = draft.get('headline_ru', '') + '\n\n' + draft.get(contract.get('text_field', 'summary_ru'), '')
    facts = contract.get('material_facts', [])
    bindings = []
    for fact in facts:
        quote = fact['statement']
        if quote in text:
            bindings.append({'fact_id': fact['fact_id'], 'post_quote': quote})
    issues = [] if not facts or bindings else ['MATERIAL_CHANGE_MISSING_FROM_POST']
    audit = copy.deepcopy(draft.get('editorial_check') or {})
    return {'issues': issues, 'covered_claims': bindings, 'editorial_check': audit}
