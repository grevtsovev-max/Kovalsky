from functools import lru_cache


import re


@lru_cache(maxsize=1)
def _morphology():
    from pymorphy3 import MorphAnalyzer
    return MorphAnalyzer()

