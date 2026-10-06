"""Locate the event verb by Russian morphology, without an editorial word list."""
from functools import lru_cache
import re


@lru_cache(maxsize=1)
def _morphology():
    from pymorphy3 import MorphAnalyzer
    return MorphAnalyzer()


def _parse(word):
    return _morphology().parse(word)[0]


def digest_action_span(headline):
    words = list(re.finditer(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*", headline))
    parsed = [_parse(word.group()) for word in words]
    for index, (word, analysis) in enumerate(zip(words, parsed)):
        if analysis.tag.POS != "VERB":
            continue
        # In future/modal constructions the lexical infinitive names the event.
        if (analysis.normal_form in {"быть", "мочь", "стать"}
                and index + 1 < len(words) and parsed[index + 1].tag.POS == "INFN"):
            word = words[index + 1]
        return word.span()
    for word, analysis in zip(words, parsed):
        if analysis.tag.POS == "INFN":
            return word.span()
    return None


def is_digest_action(word):
    return bool(re.fullmatch(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*", word)
                and _parse(word).tag.POS in {"VERB", "INFN"})


def has_finite_action(headline):
    for word in re.findall(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*", headline):
        tag = _parse(word).tag
        if tag.POS == 'VERB' and tag.mood != 'impr':
            return True
    return False
