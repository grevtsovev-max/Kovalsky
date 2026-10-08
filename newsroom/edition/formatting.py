"""Pure rendering and measurable admission checks; no rewriting of facts or quotes."""
from __future__ import annotations
import copy
import html
import json
import re
import unicodedata
from urllib.parse import urlsplit
from .model import bundle


def normalized(text):return ' '.join(str(text).split())


def contains(material,quote):
    return bool(quote and any(normalized(quote) in normalized(material.get(k,'')) for k in ('title','description','content')))


def visible(block):
    return ('➤ ' if block.get('kind')=='bullet' else '')+block.get('text','')


def normalize(draft):
    # Do not reformat numbers, punctuation or internal whitespace, especially in quotations.
    result=copy.deepcopy(draft)
    for field in ('headline','lead'):
        if isinstance(result.get(field),str):result[field]=result[field].strip()
    for block in result.get('blocks',[]):
        if isinstance(block.get('text'),str):block['text']=block['text'].strip()
    return result


def citations(draft):
    return [*draft.get('headline_evidence',[]),*draft.get('lead_evidence',[]),
            *(citation for block in draft.get('blocks',[]) for citation in block.get('evidence',[]))]


def material_source(material):
    """Pair the actual material publisher with its own URL, never a monitoring feed label."""
    metadata=material.get('primary_source_json') or {}
    if isinstance(metadata,str):
        try:metadata=json.loads(metadata)
        except (ValueError,TypeError):metadata={}
    if not isinstance(metadata,dict):metadata={}
    def usable(value):
        if not isinstance(value,str):return False
        try:
            parsed=urlsplit(value)
            return parsed.scheme in ('http','https') and bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:return False
    saved=metadata.get('_material_url')
    url=saved if usable(saved) else material.get('url','')
    publisher=metadata.get('_material_publisher')
    if not isinstance(publisher,str) or not publisher.strip():
        publisher=material.get('source_name') or ''
        if publisher.startswith('Упоминания:'):publisher=''
    try:host=urlsplit(url).hostname
    except ValueError:host=None
    return (publisher.strip() or host or 'Источник',url)


def source_list(draft,materials):
    used={c.get('material_id') for c in citations(draft)}
    result=[];seen=set()
    for material in materials:
        if material['material_id'] not in used:continue
        pair=material_source(material)
        if pair not in seen:seen.add(pair);result.append(pair)
    return result


def render(draft,materials):
    output=['<b>'+html.escape(draft['headline'])+'</b>',html.escape(draft['lead'])]
    plain=[draft['headline'],draft['lead']]
    details=[]
    def flush():
        if details:
            output.append('<blockquote expandable>'+'\n\n'.join(details)+'</blockquote>');details.clear()
    for block in draft.get('blocks',[]):
        text=visible(block);escaped=html.escape(text)
        plain.append(text)
        if block['kind']=='details':details.append(escaped)
        else:
            flush()
            output.append('<blockquote>'+escaped+'</blockquote>' if block['kind']=='quote' else escaped)
    flush()
    sources=source_list(draft,materials)
    for label,url in sources:
        label=label if len(label)<=180 else label[:179]+'…'
        parsed=urlsplit(url)
        linked=parsed.scheme in ('http','https') and parsed.hostname and not parsed.username and not parsed.password
        output.append('Источник: '+('<a href="'+html.escape(url,quote=True)+'">'+html.escape(label)+'</a>' if linked else html.escape(label)))
        plain.append('Источник: '+label)
    return '\n\n'.join(output),'\n\n'.join(plain)


def problem(code,fragment,reason):return {'code':code,'post_fragment':fragment,'reason':reason}


def validate(draft,materials,group=None):
    limits=bundle()[0]['limits'];issues=[]
    if not isinstance(draft,dict) or not all(isinstance(draft.get(k),str) for k in ('headline','lead')) or not isinstance(draft.get('blocks'),list):
        return [problem('structure','','Некорректная структура текста')]
    headline=draft['headline'];lead=draft['lead'];evidence={m['material_id']:m for m in materials}
    if len(headline)>limits['headline_max']:
        issues.append(problem('headline_length',headline,f"Заголовок: {len(headline)} символов; максимум {limits['headline_max']}"))
    # Recognise one leading emoji cluster, including flags, variation selectors and ZWJ sequences.
    first=headline.split(' ',1)[0]
    emoji_parts=[c for c in first if ord(c) not in (0xfe0f,0x200d) and not 0x1f3fb<=ord(c)<=0x1f3ff]
    flags=bool(len(emoji_parts)==2 and all(0x1f1e6<=ord(c)<=0x1f1ff for c in emoji_parts))
    emoji=bool(emoji_parts and all(unicodedata.category(c)=='So' for c in emoji_parts) and (len(emoji_parts)==1 or '\u200d' in first or flags))
    if not emoji or ' ' not in headline:issues.append(problem('headline_emoji',headline,'Нужно одно начальное тематическое эмодзи и пробел'))
    if any(unicodedata.category(c)=='So' for c in headline[len(first):]):issues.append(problem('headline_emoji',headline,'В заголовке допускается одно эмодзи'))
    if '\n' in headline:issues.append(problem('headline_length',headline,'Заголовок должен быть одной строкой'))
    if normalized(headline[len(first):]).casefold()==normalized(lead).casefold():issues.append(problem('headline_meaning',lead,'Лид повторяет заголовок'))
    sentences=re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z«])',lead)
    if not lead.strip() or len(sentences)>2:issues.append(problem('lead',lead,'Лид должен содержать одно-два предложения'))
    if re.match(r'^\s*(?:\d{4}[-./]\d|\d{1,2}[./]\d|\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр))',lead,re.I):
        issues.append(problem('lead',lead,'Лид не начинается с даты'))
    blocks=[('headline',headline,draft.get('headline_evidence')),('lead',lead,draft.get('lead_evidence'))]
    for index,block in enumerate(draft['blocks']):
        if not isinstance(block,dict) or block.get('kind') not in ('paragraph','bullet','quote','details') or not isinstance(block.get('text'),str):
            issues.append(problem('structure','','Некорректный блок'));continue
        text=visible(block)
        if not block['text'].strip():issues.append(problem('paragraph',text,'Пустой блок'))
        if block['text'].lstrip().startswith('➤'):issues.append(problem('body',text,'Маркер ➤ добавляет форматирование; не дублировать'))
        if '\n' in block['text']:issues.append(problem('paragraph',text,'Каждый абзац задаётся отдельным блоком'))
        blocks.append((str(index),text,block.get('evidence')))
        quote=block.get('quote_text','');author=block.get('quote_author','')
        if block['kind']=='quote' and not quote:issues.append(problem('quotes',text,'У прямой цитаты отсутствует точный фрагмент'))
        if quote:
            valid=[evidence.get(c.get('material_id')) for c in block.get('evidence',[]) if isinstance(c,dict)]
            if not author or author not in block['text'] or quote not in block['text'] or not any(m and contains(m,quote) for m in valid):
                issues.append(problem('quotes',text,'Цитата и автор должны присутствовать в тексте; цитата — точный фрагмент источника'))
    for kind,text,proofs in blocks:
        if kind!='headline' and len(text)>limits['block_max']:issues.append(problem('paragraph',text,f'Блок: {len(text)} символов; максимум 210'))
        if kind!='headline' and not isinstance(text,str):continue
        if not isinstance(proofs,list) or not proofs:
            issues.append(problem('facts',text,'Нет подтверждающего фрагмента'));continue
        for proof in proofs:
            material=evidence.get(proof.get('material_id')) if isinstance(proof,dict) else None
            if not material or not isinstance(proof.get('quote'),str) or not contains(material,proof['quote']):
                issues.append(problem('evidence',text,'Подтверждающий фрагмент отсутствует в сохранённом источнике'))
        for quoted in re.findall(r'«([^»]+)»',text):
            supported=[evidence.get(c.get('material_id')) for c in proofs if isinstance(c,dict)]
            if not any(m and contains(m,quoted) for m in supported):
                issues.append(problem('quotes',text,'Текст в кавычках не является точным фрагментом использованного источника'))
        if re.search(r'\b(?:по данным|по сообщению|как сообщает|сообщает издание)\b',text,re.I):
            # Participant statements are permitted; the checker distinguishes actors from publishers.
            for material in materials:
                name=material.get('source_name','')
                if name and re.search(r'\b(?:по данным|по сообщению|как сообщает|сообщает издание)\s+'+re.escape(name),text,re.I):
                    issues.append(problem('sources',text,'Вводную ссылку на издание перенести в источник'))
                    break
    for label,url in source_list(draft,materials):
        parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            issues.append(problem('sources',label,'Некорректная ссылка источника'))
        if len('Источник: '+label)>limits['block_max']:issues.append(problem('sources',label,'Название источника превышает предел блока'))
    try:
        rendered,plain=render(draft,materials)
        if len(re.findall(r'\S+',plain))>limits['word_max']:issues.append(problem('length','','Пост превышает 400 слов'))
        if len(plain.encode('utf-16-le'))//2>4096:issues.append(problem('telegram_length','','Пост не помещается в сообщение Telegram; сократить второстепенные подробности'))
    except (KeyError,TypeError,ValueError):issues.append(problem('structure','','Текст не удалось оформить'))
    return issues
