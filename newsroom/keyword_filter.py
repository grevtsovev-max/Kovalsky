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


def evaluate(text, spec, title=''):
    if spec.get('mode') == 'intake_rules':
        return apply_negatives(evaluate_rules(text, title, spec), text, title, spec)
    keyword = match(text, spec.get('keywords') or [])
    brand = match(text, spec.get('brands') or [])
    person = match(text, spec.get('people') or [])
    topic_keyword = match(text, spec.get('topic_keywords') or []) if brand else None
    brand_only = bool(brand and not person and not topic_keyword)
    return apply_negatives({'matched_keyword': keyword, 'matched_brand': brand,
            'matched_topic_keyword': topic_keyword, 'brand_only': brand_only,
            'passed': bool(keyword) and not brand_only}, text, title, spec)


def apply_negatives(result, text, title, spec):
    if result.get('configuration_error') or not spec.get('keywords') and spec.get('mode') != 'intake_rules':
        return result
    for entry in spec.get('negative_keywords') or []:
        if not entry.get('enabled'):
            continue
        scope = entry['scope']
        target = title if scope == 'Заголовок' else text
        target = html.unescape(re.sub(r'<[^>]*>', ' ', target))
        variants = [entry['word'], *entry.get('aliases', [])]
        if entry['kind'] == 'Название':
            found = next((v for v in variants if re.search(
                r'(?<!\w)' + r'\s+'.join(re.escape(t) for t in normalize(v).split()) + r'(?!\w)', normalize(target))), None)
        else:
            found = match(target, variants)
        if found:
            result.update(passed=False, matched_negative_keyword=entry['word'],
                          matched_negative_variant=found, negative_scope=scope,
                          negative_row=entry.get('row'), negative_kind=entry['kind'],
                          reason=f"Первый фильтр не пройден: минус-слово «{entry['word']}», найдено «{found}»; область: {scope.lower()}.")
            break
    return result


def evaluate_rules(text, title, spec):
    entries = spec.get('entries') or []
    entities = {normalize(v) for v in (spec.get('brands') or []) + (spec.get('people') or [])}
    profiles = [r for r in entries if r.get('role') == 'Профильный' and normalize(r['description']) not in entities]
    # A thematic word inside an entity name (e.g. Crypto.com) is not evidence.
    topical_text = normalize(text)
    for entity in sorted(entities, key=len, reverse=True):
        tokens = words(entity)
        if tokens:
            topical_text = re.sub(r'(?<!\w)' + r'\W+'.join(re.escape(t) for t in tokens) + r'(?!\w)', ' ', topical_text)
    profile = match(topical_text, [r['description'] for r in profiles])
    brand = match(text, spec.get('brands') or [])
    person = match(title, spec.get('people') or [])
    context = match(topical_text, [r['description'] for r in entries if r.get('role') == 'Контекстный'
                           and normalize(r['description']) not in entities])
    ambiguous = refinement = None
    for entry in entries:
        if entry.get('role') != 'Требует уточнения' or not match(topical_text, [entry['description']]):
            continue
        ambiguous = entry['description']
        # Clarifiers belong to this exact row, never a document-wide hidden list.
        refinement = match(topical_text, [s.strip() for s in entry.get('refinement', '').split('|') if s.strip()])
        if refinement:
            break
    found = {'Бренд':brand, 'Профильный':profile, 'Лицо в заголовке':person,
             'Контекстный':context, 'Требует уточнения':ambiguous, 'Уточнение':refinement}
    rule = next((r for r in spec.get('rules', []) if r.get('enabled') and r.get('required')
                 and all(found.get(part) for part in r['required'])), None)
    configuration_error = not spec.get('rules') or not profiles
    if configuration_error:
        reason = 'Первый фильтр не настроен: нужны включённые правила и профильные ключевики с ролями.'
    elif rule:
        parts = list(dict.fromkeys(found[p] for p in rule['required']))
        reason = 'Прошёл первый фильтр: ' + ' + '.join(parts) + '. Правило: ' + rule['name'] + '.'
        if context and 'Контекстный' not in rule['required']:
            reason += ' Контекст: ' + context + '.'
    else:
        reason = ('Первый фильтр не пройден: найден ' + brand + ', профильного ключевика или разрешённого сочетания нет.'
                  if brand else 'Первый фильтр не пройден: профильного ключевика, разрешённого сочетания или лица в заголовке нет.')
    selected_keyword = profile or (ambiguous if rule else None) or person
    topic = (next((r['title'] or None for r in profiles if r['description'] == profile), None)
             if profile else 'Публичные активности брендов и лиц' if person else
             next((r['title'] or None for r in entries if r['description'] == ambiguous), None))
    return {'mode':'intake_rules', 'passed':bool(rule) and not configuration_error, 'rule':rule['name'] if rule else None,
            'matched_keyword':selected_keyword, 'matched_brand':brand, 'matched_person':person,
            'matched_topic_keyword':profile, 'matched_context_keyword':context,
            'matched_ambiguous_keyword':ambiguous, 'matched_refinement':refinement,
            'topic':topic, 'reason':reason, 'configuration_error':configuration_error}


def screen(db, item_id, item, settings):
    from .material_flow import mark, put, get, revision
    from .runtime import cache_key, stamp
    spec = settings.get('_keyword_prefilter')
    if spec is None:
        return None  # Unconfigured tools/tests do not invent another word list.
    keywords = spec.get('keywords') or []
    text = '\n'.join(str(item.get(field) or '') for field in ('title', 'description', 'content'))
    dependency = cache_key('keyword-prefilter-v1', {'text': text, 'keywords': spec})
    if spec.get('mode') == 'intake_rules':
        dependency = cache_key('intake-rules-v1', spec)
        saved = get(db, item_id, 'screening', dependency)
        if saved and saved.get('passed') is True:
            item['_intake_filter'] = saved
            return None
    mark(db, item_id, 'screening', 'RUNNING', 'Фильтр ключевых слов и словоформ; без ИИ.')
    result = {'kind': 'keyword_prefilter', 'version': spec.get('version'), **evaluate(text, spec, item.get('title', ''))}
    if spec.get('mode') == 'intake_rules':
        result.update(input_sha256=cache_key('intake-text', text), item_id=item_id, revision=revision(db, item_id))
        item['_intake_filter'] = result
    keyword = result['matched_keyword']
    put(db, item_id, 'screening', dependency, result)
    if result['passed']:
        label = (result['matched_brand'] + ' + ' + result['matched_topic_keyword']
                 if result['matched_brand'] and result['matched_topic_keyword'] else keyword)
        mark(db, item_id, 'screening', 'DONE', result.get('reason') or 'Первый фильтр пройден: ' + label)
        db.commit()
        return None
    if result.get('configuration_error') or (spec.get('mode') != 'intake_rules' and not keywords):
        outcome, reason = 'TECHNICAL_ERROR', 'Нет включённых ключевых слов в таблице; требуется исправить настройку.'
        reason = result.get('reason') or reason
        mark(db, item_id, 'screening', 'ERROR', reason, block_kind='technical')
    else:
        outcome = 'NOISE'
        reason = result.get('reason') or ('Первый фильтр не пройден: найден бренд ' + result['matched_brand'] + ', но нет дополнительного тематического ключевика.'
                  if result['brand_only'] else 'Первый фильтр не пройден: в заголовке и доступном тексте нет ключевых слов или их словоформ.')
        mark(db, item_id, 'screening', 'CLOSED', reason)
    item['_retry_reason'] = reason
    db.execute('UPDATE items SET disposition=?,processed_at=? WHERE item_id=?', (outcome, stamp(), item_id))
    db.commit()
    return outcome
