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
    r"\b(?:в переданных|в предоставленных|в доступных|в доступном|среди доступных)\s+(?:материалах|источниках|архиве|публикациях)"
    r"|\b(?:поиск|архив)\s+(?:не\s+)?(?:необходим|заверш[её]н|не дал|не выявил|не позволил)"
    r"|\b(?:не удалось|не удалось найти|не удалось сопоставить|не найден[аоы]?|не обнаружен[аоы]?)\s+(?:найти\s+)?"
    r"(?:сопоставим\w*|подходящ\w*|релевантн\w*|аналогичн\w*)?\s*(?:материал\w*|источник\w*|публикац\w*|подтвержден\w*|совпаден\w*)?"
    r"|\bсопоставим\w*\s+(?:материал\w*|публикац\w*|источник\w*)?\s*(?:найти|обнаружить|сопоставить)\s+не\s+удалось"
    r"|\b(?:в ходе|по результатам|при)\s+поиск\w*\b"
)
ACTION = re.compile(r"(?i)\b(?:зарегистрирова\w*|опубликова\w*|предлож\w*|подготов\w*|приня\w*|утверд\w*|ввел\w*|ввёл\w*|установ\w*|разреш\w*|запрет\w*|объяв\w*|сообщ\w*|заяв\w*|запуст\w*|создал\w*|открыл\w*|закрыл\w*|выпуст\w*|подпис\w*|одобр\w*|выдал\w*|получил\w*|включил\w*|внес\w*|внёс\w*|изменил\w*|повысил\w*|снизил\w*|оценил\w*|указал\w*|вынес\w*|отменил\w*|приостановил\w*|разработал\w*|расширил\w*|согласова\w*|отчит\w*|запросил\w*|открыл доступ|провел\w*|провёл\w*|выступил\w*|столкнул\w*|приобрел\w*|приобрёл\w*|продал\w*|планирует|допуска\w*|рассмотр\w*)\b")


def editorial_issues(headline, body, facts, *, final_post=False):
    issues = []
    if facts.get('geographic_scope') == 'RUSSIA' and not headline.startswith('🇷🇺'):
        issues.append('RUSSIA_FLAG_MISSING')
    if len(headline) > 115:
        issues.append('HEADLINE_TOO_LONG')
    if not body.strip() or not re.search('[а-яА-ЯёЁ]', headline + body):
        issues.append('RUSSIAN_TEXT_REQUIRED')
    if re.search(r'https?://', headline):
        issues.append('URL_IN_HEADLINE')
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
