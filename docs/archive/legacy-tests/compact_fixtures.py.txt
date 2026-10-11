"""A complete bounded editor response used by pipeline regression scenarios."""
import copy


def compact_response(evidence='Сбер выпустил цифровые активы.', event_date=''):
    return {
        'publication_recommendation': 'AUTO_PUBLISH', 'reason': '',
        'headline_ru': '🇷🇺 Сбер выпустил цифровые активы', 'summary_ru': evidence,
        'event_status': 'IMPLEMENTATION', 'geographic_scope': 'RUSSIA',
        'development_date': event_date, 'development_date_evidence': evidence if event_date else '',
        'independent_check': 'NOT_ASSESSED', 'independent_check_note': '',
        'original_reporting_check': {'central_claim_supported': True, 'evidence': evidence},
        'memory': {
            'match_status': 'CERTAIN',
            'event': {'subject': 'Сбер', 'action': 'выпустил', 'object': 'цифровые активы',
                      'jurisdiction': 'RU', 'stage': 'IMPLEMENTED', 'event_date': event_date,
                      'statement_date': '', 'effective_date': '', 'document_id': ''},
            'claims': [{'subject': 'Сбер', 'predicate': 'выпуск', 'scope': 'цифровые активы RU',
                        'value': 'выпущены', 'statement': evidence, 'claim_type': 'FACT',
                        'source_quote': evidence, 'valid_from': '', 'valid_to': '',
                        'previous_fact_id': '', 'relation': 'NEW', 'change_type': 'NEW_FACT',
                        'material': True, 'material_reason': 'Сообщается о выпуске цифровых активов банком.'}],
        },
    }


def response(value):
    import json
    return {'output': [{'type': 'reasoning', 'content': None},
                       {'content': [{'type': 'output_text', 'text': json.dumps(copy.deepcopy(value))}]}]}
