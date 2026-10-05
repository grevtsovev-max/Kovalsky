"""Shared checks used both when drafting and at the Telegram send boundary."""
import re
import hashlib


def attributed_report_supported(report, facts, *, stored=False):
    """Check the read account and its attribution without requiring an original."""
    audit = facts.get('original_reporting_check') or {}
    quote = audit.get('evidence', '')
    content = report.get('content', '')
    claims = facts.get('facts') or []
    supported = (report.get('type') == 'ATTRIBUTED_REPORT'
                 and report.get('material_read') is True
                 and bool(report.get('url')) and bool(report.get('publisher'))
                 and audit.get('central_claim_supported') is True
                 and audit.get('attribution_preserved') is True
                 and len(quote.strip()) >= 24
                 and ' '.join(quote.casefold().split()) in ' '.join(content.casefold().split())
                 and bool(claims)
                 and all(c.get('claim_type') in {'CLAIM', 'REPORT', 'OPINION'} for c in claims))
    if stored:
        supported = (supported and report.get('evidence') == quote
                     and report.get('content_sha256') == hashlib.sha256(content.encode()).hexdigest())
    return bool(supported)


def publication_source_ready(facts, text):
    primary = facts.get('primary_source') or {}
    report = facts.get('publisher_report') or {}
    return bool((facts.get('primary_source_status') == 'READ'
                 and primary.get('url') and primary.get('content_sha256') and primary['url'] in text)
                or (facts.get('publisher_report_exception') is True
                    and attributed_report_supported(report, facts, stored=True)
                    and report['url'] in text))

PROCESS_META = re.compile(
    r"(?i)"
    r"\b(?:в|среди)\s+(?:переданн\w*|предоставленн\w*|доступн\w*)\s+(?:материал\w*|источник\w*|архив\w*|публикац\w*)"
    r"|\b(?:предыдущ\w*|прежн\w*|сопоставим\w*)\s+(?:заявлен\w*|высказыван\w*|позиц\w*|сообщен\w*)\b.{0,120}\b(?:нет|не\s+найден\w*|не\s+обнаружен\w*|отсутству\w*)"
    r"|\b(?:нет|отсутству\w*|не\s+найден\w*|не\s+обнаружен\w*)\s+(?:(?:сопоставим\w*|предыдущ\w*|прежн\w*)\s+){0,2}(?:заявлен\w*|высказыван\w*|позиц\w*|сообщен\w*)"
    r"|\b(?:поиск|архив)\s+(?:не\s+)?(?:необходим|заверш[её]н|не дал|не выявил|не позволил)"
    r"|\b(?:не удалось|не удалось найти|не удалось сопоставить|не найден[аоы]?|не обнаружен[аоы]?)\s+(?:найти\s+)?"
    r"(?:сопоставим\w*|подходящ\w*|релевантн\w*|аналогичн\w*)?\s*(?:материал\w*|источник\w*|публикац\w*|подтвержден\w*|совпаден\w*)?"
    r"|\bсопоставим\w*\s+(?:материал\w*|публикац\w*|источник\w*)?\s*(?:найти|обнаружить|сопоставить)\s+не\s+удалось"
    r"|\b(?:в ходе|по результатам|при)\s+поиск\w*\b"
)
ACTION = re.compile(r"(?i)\b(?:зарегистрирова\w*|опубликова\w*|предлож\w*|подготов\w*|приня\w*|утверд\w*|ввел\w*|ввёл\w*|установ\w*|разреш\w*|запрет\w*|объяв\w*|сообщ\w*|заяв\w*|подал\w*|запуст\w*|создал\w*|открыл\w*|закрыл\w*|выпуст\w*|подпис\w*|одобр\w*|выдал\w*|получил\w*|включил\w*|внес\w*|внёс\w*|изменил\w*|повысил\w*|снизил\w*|оценил\w*|указал\w*|вынес\w*|отменил\w*|приостановил\w*|разработал\w*|расширил\w*|согласова\w*|отчит\w*|запросил\w*|открыл доступ|провел\w*|провёл\w*|выступил\w*|столкнул\w*|приобрел\w*|приобрёл\w*|продал\w*|планирует|допуска\w*|рассмотр\w*)\b")
GENERIC_UPDATE_LABEL = re.compile(r"(?i)(?:^|\s)(?:обновление|дополнение)\s*[:—–-]")
ATTRIBUTION_TO_OUTLET = re.compile(
    r"(?iu)\b(?:сообщил\w*|рассказал\w*|заявил\w*|подтвердил\w*|передал\w*)"
    r"\s+(?:своему\s+|этому\s+)?(?:издани\w*|редакци\w*|газет\w*|журнал\w*|"
    r"портал\w*|агентств\w*|телеканал\w*|канал\w*|сми)\b"
)
OUTLET_REPORTING_VERB = r"(?:сообщил\w*|рассказал\w*|заявил\w*|подтвердил\w*|передал\w*|сообщает|пишет|передаёт|передает|по\s+данным|по\s+сообщению)"


def _footer_source_name(body):
    match = re.search(r"(?m)^(?:Источник|Источники):\s*\[([^\]]+)\]\(https?://[^)]+\)\s*$", body)
    return match.group(1).strip() if match else ""


def _source_attribution_is_redundant(body, facts, source_name=None, source_is_report=False):
    primary = facts.get("primary_source") or {}
    report = facts.get("publisher_report") or {}
    footer_source = _footer_source_name(body)
    has_linked_source = bool(footer_source or source_name or primary.get("url") or report.get("url"))
    if not has_linked_source:
        return False
    if ATTRIBUTION_TO_OUTLET.search(body):
        return True
    if "citation_is_report" in facts:
        source_is_report = source_is_report or facts.get("citation_is_report") is True
    else:
        source_is_report = (source_is_report or facts.get("publisher_report_exception") is True
                            or str(primary.get("type") or "").startswith("ORIGINAL_MEDIA_")
                            or str(primary.get("type") or "").startswith("ORIGINAL_SOCIAL_"))
    if not source_is_report:
        return False
    name = source_name or _footer_source_name(body) or report.get("publisher") or primary.get("publisher") or ""
    name = re.sub(r"\s+", " ", str(name)).strip(" \t«»\"'")
    if not name:
        return False
    escaped = re.escape(name)
    return bool(re.search(rf"(?iu)\b{escaped}\b.{{0,60}}\b{OUTLET_REPORTING_VERB}\b", body)
                or re.search(rf"(?iu)\b{OUTLET_REPORTING_VERB}\b.{{0,60}}\b{escaped}\b", body))


def editorial_issues(headline, body, facts, *, final_post=False, source_name=None, source_is_report=False):
    issues = []
    if facts.get('geographic_scope') == 'RUSSIA' and not headline.startswith('🇷🇺'):
        issues.append('RUSSIA_FLAG_MISSING')
    if len(headline) > 115:
        issues.append('HEADLINE_TOO_LONG')
    if not body.strip() or not re.search('[а-яА-ЯёЁ]', headline + body):
        issues.append('RUSSIAN_TEXT_REQUIRED')
    if re.search(r'https?://', headline):
        issues.append('URL_IN_HEADLINE')
    if GENERIC_UPDATE_LABEL.search(headline):
        issues.append('GENERIC_UPDATE_LABEL')
    if _source_attribution_is_redundant(body, facts, source_name, source_is_report):
        issues.append('REDUNDANT_SOURCE_ATTRIBUTION')
    audit = facts.get('editorial_check') or {}
    for key in ('source_matches_event', 'attribution_preserved', 'stage_preserved',
                'headline_main_event', 'lead_event_first', 'paragraphs_concise_distinct'):
        if audit.get(key) is not True:
            issues.append(key.upper())
    if not ACTION.search(headline):
        issues.append('HEADLINE_NOT_EVENT_LED')
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    prose = [p for p in paragraphs if not p.startswith(('Источник:', 'Источники:', 'Ранее:', '**', '➠ '))]
    if not prose or len(prose) > 5 or any(len(p) > 700 for p in prose):
        issues.append('PARAGRAPH_STRUCTURE')
    if PROCESS_META.search(headline + '\n' + body):
        issues.append('EDITORIAL_PROCESS_NOTE')
    if audit.get('history_required'):
        note = audit.get('history_note', '').strip()
        if audit.get('history_explained') is not True or len(note) < 20 or note not in body:
            issues.append('POSITION_HISTORY_MISSING')
    if facts.get('event_status') in {'DISCUSSION','PROPOSAL'}:
        if re.search(r'вступил[аио]?\s+в\s+силу|(?:закон|правил[ао])\s+(?:принят|утвержден)', headline, re.I):
            issues.append('PROPOSAL_PRESENTED_AS_LAW')
    if final_post:
        lines = [line.strip() for line in (headline + '\n' + body).splitlines() if line.strip()]
        source_lines = [line for line in lines if line.startswith(('Источник:', 'Источники:'))]
        if (not lines or lines[0] != headline.strip() or len(source_lines) != 1
                or lines[-1] != source_lines[0]):
            issues.append('SOURCE_FOOTER_FORMAT')
        elif not re.search(r"^(?:Источник|Источники):\s*\[[^\]]+\]\(https?://[^)]+\)$", source_lines[0]):
            issues.append('SOURCE_LINK_FORMAT')
        source = facts.get('primary_source') or facts.get('publisher_report') or {}
        source_url = source.get('url') if isinstance(source, dict) else None
        if source_url and source_url not in (source_lines[0] if source_lines else ''):
            issues.append('SOURCE_URL_MISMATCH')
    return issues


def digest_issues(message, selected_count=None):
    """Enforce the channel's compact, linked-headline digest format."""
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines or not lines[0].startswith('📣 '):
        return ['DIGEST_TITLE_MISSING']
    entries = lines[1:]
    if entries and all(line.startswith('За период дайджеста ') for line in entries):
        return []
    for line in entries:
        links = re.findall(r"\[([^\]]+)\]\((https://t\.me/[^)]+)\)", line)
        if (not re.match(r"^(?:🏛|🔐|🧾|⚙️|📈|📌) .+", line)
                or len(links) != 1 or not ACTION.fullmatch(links[0][0])):
            return ['DIGEST_ENTRY_FORMAT']
    return []
