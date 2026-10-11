"""Russian publication checks; foreign names, tickers and source labels are allowed."""
import re


def russian_prose(value):
    if not any(c.isalpha() for c in str(value)) and any(c.isdigit() for c in str(value)):return True
    value=re.sub(r'https?://\S+|\b[A-Z][A-Za-z0-9.+-]*\b',' ',str(value))
    russian=re.findall('[а-яёА-ЯЁ]',value)
    foreign=re.findall('[a-zA-Z]',value)
    return bool(russian) and len(russian)>=len(foreign)


def issues(draft):
    fields=[draft.get('headline',''),draft.get('lead',''),*(b.get('text','') for b in draft.get('blocks',[]))]
    return [{'code':'language','post_fragment':value,'reason':'Текст публикации должен быть на русском языке.'}
            for value in fields if value.strip() and not russian_prose(value)]
