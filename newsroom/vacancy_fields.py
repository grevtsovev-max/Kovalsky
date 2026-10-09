"""Conservative, source-grounded vacancy fields for cards and future filters."""
import re
VERSION = 'vacancy-fields-v2'

def clean(value):
    value = re.sub(r'#[\w]+', '', str(value or ''))
    value = re.sub(r'https?://\S+', '', value)
    return re.sub(r'\s+', ' ', value).strip(' \t•▪▫️🔹🔸📍💰💼:;|-')

def extract(text, known=None):
    known = known or {}
    lines = [clean(x) for x in str(text or '').splitlines()]
    lines = [x for x in lines if x]
    def labeled(pattern):
        for n,line in enumerate(lines):
            m = re.match(pattern+r'\s*[:—–-]\s*(.+)',line,re.I)
            if m:return m.group(1).strip()
            if re.fullmatch(pattern+r'\s*[:—–-]?',line,re.I) and n+1<len(lines):
                return lines[n+1]
        return ''
    role = clean(known.get('role')) or labeled(r'(?:Должность|Позиция|Вакансия)')
    if not role:
        for line in lines:
            m=re.match(r'(?:Требуется|Требуются|Ищем|Нужен|Нужна|Hiring)\s+(.+)',line,re.I)
            if m:
                candidate=m.group(1)
                if len(candidate)<=120 and not re.search(r'\b(?:который|человека|того|просто)\b',candidate,re.I):
                    role=candidate;break
    company=clean(known.get('company')) or labeled(r'(?:Компания|Работодатель|Наниматель)')
    if not company:
        for n,line in enumerate(lines):
            if re.fullmatch(r'(?:О компании|О нас)',line,re.I) and n+1<len(lines):
                m=re.match(r'(.{1,80}?)\s+[—–-]\s+',lines[n+1])
                if m:company=m.group(1);break
    contact=clean(known.get('contact')) or labeled(r'(?:Контакты?|Для отклика|Отклик|Связаться)')
    # An anonymous employer's explicit application contact is preferable to the publisher.
    handle = re.search(r'@[A-Za-z0-9_]+|[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',contact)
    hiring_party=company or (handle.group(0) if handle else contact)
    salary=clean(known.get('salary')) or labeled(r'(?:Зарплата|Заработная плата|Оплата|Вознаграждение|Salary)')
    if not salary:
        for line in lines:
            if re.search(r'(?:\d[\d ,.]*\s*(?:₽|руб(?:лей|ля|\.)?|RUB|USD|USDT|EUR|\$|€)|[$€]\s*\d)',line,re.I):
                if len(line)<=150 and not re.search(r'\b(?:оборот|выручка|инвестици|клиент|миллион.*пользоват)\w*',line,re.I):
                    salary=re.sub(r'^(?:оклад|от)\s+',lambda m:m.group(0) if m.group(0).lower().startswith('от') else '',line,flags=re.I);break
    location=clean(known.get('location') or known.get('region')) or labeled(r'(?:Локация|Место работы|Город|Location)')
    work_format=clean(known.get('work_format'))
    if not work_format and re.search(r'(?:удал[её]н\w*|remote)',str(text or ''),re.I):work_format='Удалённо'
    if not location and work_format=='Удалённо':location='Удалённо'
    return {'role':clean(role),'company':clean(company),'hiring_party':clean(hiring_party),
            'salary':clean(salary),'location':clean(location),'region':clean(location),
            'work_format':work_format,'contact':clean(contact),'fields_version':VERSION}
