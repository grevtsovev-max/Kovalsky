"""Local intake selection from the owner's keyword sheet; no model requests."""
from functools import lru_cache
import html
import re

from .topic_registry import lemmas, normalize


def forms(word):
    result = set(lemmas(word))
    if re.fullmatch('[a-z]+', word) and len(word) > 3:
        if word.endswith('ies'):
            result.add(word[:-3] + 'y')
        if word.endswith('es'):
            result.add(word[:-2])
        if word.endswith('s') and not word.endswith('ss'):
            result.add(word[:-1])
    return frozenset(result)


def words(text):
    return re.findall(r'[а-яёa-z0-9]+', normalize(html.unescape(text)))


@lru_cache(maxsize=8)
def compiled(keywords):
    phrases, index = [], {}
    for keyword in keywords:
        tokens = words(keyword)
        if not tokens:
            continue
        phrase_forms = tuple(forms(token) for token in tokens)
        number = len(phrases)
        phrases.append((keyword, phrase_forms))
        for lemma in phrase_forms[0]:
            index.setdefault(lemma, []).append(number)
    return phrases, index


def match(text, keywords):
    """Return a listed phrase, allowing inflection, reordering and short gaps."""
    parsed = [forms(token) for token in words(text)]
    phrases, index = compiled(tuple(dict.fromkeys(keywords)))
    for position, token in enumerate(parsed):
        candidates = {number for lemma in token for number in index.get(lemma, ())}
        for number in sorted(candidates):
            keyword, phrase_forms = phrases[number]
            width = len(phrase_forms) + 4
            for start in range(max(0, position - width + 1), position + 1):
                window = parsed[start:start + width]
                if all(any(form & word for word in window) for form in phrase_forms):
                    return keyword
    return None


def evaluate(text, spec):
    keyword = match(text, spec.get('keywords') or [])
    brand = match(text, spec.get('brands') or [])
    person = match(text, spec.get('people') or [])
    topic_keyword = match(text, spec.get('topic_keywords') or []) if brand else None
    brand_only = bool(brand and not person and not topic_keyword)
    return {'matched_keyword': keyword, 'matched_brand': brand,
            'matched_topic_keyword': topic_keyword, 'brand_only': brand_only,
            'passed': bool(keyword) and not brand_only}


def screen(db, item_id, item, settings):
    from .material_flow import mark, put
    from .runtime import cache_key, stamp
    spec = settings.get('_keyword_prefilter')
    if spec is None:
        return None  # Unconfigured tools/tests do not invent another word list.
    keywords = spec.get('keywords') or []
    text = '\n'.join(str(item.get(field) or '') for field in ('title', 'description', 'content'))
    dependency = cache_key('keyword-prefilter-v1', {'text': text, 'keywords': spec})
    mark(db, item_id, 'screening', 'RUNNING', 'Фильтр ключевых слов и словоформ; без ИИ.')
    result = {'kind': 'keyword_prefilter', 'version': spec.get('version'), **evaluate(text, spec)}
    keyword = result['matched_keyword']
    put(db, item_id, 'screening', dependency, result)
    if result['passed']:
        label = (result['matched_brand'] + ' + ' + result['matched_topic_keyword']
                 if result['matched_brand'] and result['matched_topic_keyword'] else keyword)
        mark(db, item_id, 'screening', 'DONE', 'Первый фильтр пройден: ' + label)
        db.commit()
        return None
    if not keywords:
        outcome, reason = 'TECHNICAL_ERROR', 'Нет включённых ключевых слов в таблице; требуется исправить настройку.'
        mark(db, item_id, 'screening', 'ERROR', reason, block_kind='technical')
    else:
        outcome = 'NOISE'
        reason = ('Первый фильтр не пройден: найден бренд ' + result['matched_brand'] + ', но нет дополнительного тематического ключевика.'
                  if result['brand_only'] else 'Первый фильтр не пройден: в заголовке и доступном тексте нет ключевых слов или их словоформ.')
        mark(db, item_id, 'screening', 'CLOSED', reason)
    item['_retry_reason'] = reason
    db.execute('UPDATE items SET disposition=?,processed_at=? WHERE item_id=?', (outcome, stamp(), item_id))
    db.commit()
    return outcome
