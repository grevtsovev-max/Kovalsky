import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from newsroom.core import TelegramPreviewParser, _read_telegram_primary, is_relevant, process_item, _retry_ai_held_items, run_cycle
from newsroom.db import connect

class RecoverySeptember28Tests(unittest.TestCase):
    def setUp(self):
        from policy_fixtures import final_check
        checker = patch('newsroom.ai.validate_draft', side_effect=final_check)
        checker.start()
        self.addCleanup(checker.stop)

    def test_tass_digital_currency_declensions(self):
        terms = ['цифровая валюта', 'цифровые активы', 'цифровой рубль']
        for text in ['В СФ предложили использовать цифровую валюту в качестве предмета залога',
                     'Использование цифровой валюты бизнесом', 'Расчёты цифровыми валютами',
                     'Операции с цифровыми активами', 'Эксперимент с цифровым рублём']:
            with self.subTest(text=text): self.assertTrue(is_relevant(text, terms))
        for text in ['Цифровую инфраструктуру обновят', 'Курс валюты изменился', 'Нецифровой рубль']:
            self.assertFalse(is_relevant(text, terms))

    def test_telegram_collects_only_message_links_and_forward_marker(self):
        p = TelegramPreviewParser('news')
        p.feed('''<div class="tgme_widget_message_wrap"><div data-post="news/123">
          <a href="https://navigation.example">navigation</a>
          <div class="tgme_widget_message_forwarded_from">Forward</div>
          <div class="tgme_widget_message_text">Крипто <a href="https://cbr.ru/decision">официальное сообщение</a></div>
          <time datetime="2026-09-28T01:05:32+00:00"></time></div></div>''')
        self.assertEqual(p.items[0]['discovery_links'], [{'href':'https://cbr.ru/decision','text':'официальное сообщение'}])
        self.assertTrue(p.items[0]['telegram_forwarded'])

    def test_official_channel_is_primary_but_forwarded_and_secondary_are_not(self):
        source={'url':'https://t.me/regulator','name':'Регулятор','reputation':'primary_source'}
        base={'url':'https://t.me/regulator/1','title':'Решение','content':'Текст решения','discovery_links':[]}
        item=dict(base);_read_telegram_primary(item,source)
        self.assertEqual(item['primary_source_status'],'READ')
        for candidate, channel in [(dict(base,telegram_forwarded=True),source),(dict(base),dict(source,reputation='secondary_media'))]:
            _read_telegram_primary(candidate,channel)
            self.assertNotEqual(candidate['primary_source_status'],'READ')

    def test_telegram_linked_html_is_read_and_failed_link_stays_blocked(self):
        item={'url':'https://t.me/news/1','title':'Тема','content':'Новость','discovery_links':[{'href':'https://cbr.ru/decision','text':'Решение'}]}
        source={'url':'https://t.me/news','name':'Новости','reputation':'secondary_media'}
        article={'url':'https://cbr.ru/decision','title':'Решение','content':'Полный текст решения','primary_source_status':'NOT_CHECKED'}
        with patch('newsroom.core.fetch_publisher_article',return_value=article):
            good=dict(item);_read_telegram_primary(good,source)
        self.assertEqual(good['primary_source_status'],'READ')
        with patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError):
            bad=dict(item);_read_telegram_primary(bad,source)
        self.assertEqual(bad['primary_source_status'],'UNREADABLE')

    def test_media_link_without_primary_is_not_promoted(self):
        item={'url':'https://t.me/news/1','title':'Тема','content':'Новость','discovery_links':[{'href':'https://media.example/story','text':'сообщает'}]}
        source={'url':'https://t.me/news','name':'Новости','reputation':'secondary_media'}
        with patch('newsroom.core.fetch_publisher_article',return_value={'primary_source_status':'NO_LINK'}):
            _read_telegram_primary(item,source)
        self.assertEqual(item['primary_source_status'],'NO_LINK')

    def test_google_retry_rereads_page_and_preserves_item(self):
        with tempfile.TemporaryDirectory() as directory:
            db=connect(str(Path(directory)/'test.db'))
            self.addCleanup(db.close)
            db.execute("INSERT INTO sources(name,type,url) VALUES('Google','google_news','https://google.example')")
            source=db.execute('SELECT * FROM sources').fetchone()
            now=datetime.now(timezone.utc).isoformat()
            item={'url':'https://media.example/story','title':'Цифровую валюту используют как залог','content':'Описание','published_at':now,'primary_source_status':'NO_LINK'}
            self.assertEqual(process_item(db,source,item,.35,3500,48,ai_settings={}), 'PRIMARY_RETRY')
            evidence=f'Российские организации смогут использовать цифровую валюту в качестве предмета залога {now[:10]}.'
            article=dict(item,primary_source_status='READ',primary_source_url='https://cbr.ru/decision',primary_source_content=evidence)
            result={'action':'NEW_STORY','is_relevant':True,'geographic_scope':'RUSSIA','confidence':0.9,'russia_cis_impact':'DIRECT','impact_evidence':evidence,'publication_recommendation':'AUTO_PUBLISH','editorial_check':{"source_matches_event": True, "attribution_preserved": True, "stage_preserved": True, "history_required": False, "history_explained": False, "history_note": "", "headline_main_event": True, "lead_event_first": True, "paragraphs_concise_distinct": True, "no_editorial_process_notes": True},'headline_ru':'🇷🇺 Банк России установил новые правила','summary_ru':evidence}
            result.update(development_date=now[:10], development_date_evidence=evidence)
            with patch('newsroom.core.fetch_publisher_article',return_value=article) as fetch, patch('newsroom.core.get_api_key',return_value='test'), patch('newsroom.core.analyze_with_ai',return_value=result):
                db.execute("UPDATE app_state SET value=json_set(value,'$.next_at','2000-01-01T00:00:00+00:00') WHERE key LIKE 'selection_retry:%'")
                db.commit()
                outcomes=_retry_ai_held_items(db,{source['source_id']:source},{'newsroom':{},'ai':{}})
            self.assertEqual(outcomes,{'NEW_STORY':1});fetch.assert_called_once()
            self.assertEqual(db.execute('select count(*) from items').fetchone()[0],1)

    def test_disabled_ai_flag_does_not_leak_between_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg={'newsroom':{'database':str(Path(directory)/'test.db')},'ai':{'_disabled_for_cycle':True},'sources':[]}
            with patch('newsroom.core._retry_ai_held_items',return_value={}) as retry:
                run_cycle(cfg)
            self.assertNotIn('_disabled_for_cycle',retry.call_args.args[2]['ai'])

class AITransportRecoveryTests(unittest.TestCase):
    def analyze(self):
        from newsroom.ai import analyze
        return analyze({'title':'Test'},{'name':'Test','reputation':'unknown','priority':1},[],{})

    def test_transient_disconnect_retries_once(self):
        import http.client
        from test_ai_credentials_and_responses import FakeResponse
        body=json.dumps({'output':[{'content':[{'type':'output_text','text':'{"action":"NOISE"}'}]}]}).encode()
        with patch('newsroom.ai.get_api_key',return_value='test'), patch('newsroom.ai.time.sleep'), patch('newsroom.ai.urllib.request.urlopen',side_effect=[http.client.RemoteDisconnected(),FakeResponse(body)]) as opener:
            from newsroom.ai import AIResponseError
            with self.assertRaises(AIResponseError):
                self.analyze()
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(self.analyze()['action'],'NOISE')
            self.assertEqual(opener.call_count,2)

    def test_persistent_timeout_is_bounded_and_safe(self):
        from newsroom.ai import AIResponseError
        with patch('newsroom.ai.get_api_key',return_value='test'), patch('newsroom.ai.time.sleep'), patch('newsroom.ai.urllib.request.urlopen',side_effect=TimeoutError('secret payload')) as opener:
            with self.assertRaises(AIResponseError) as raised: self.analyze()
        self.assertEqual(opener.call_count,1)
        self.assertEqual(str(raised.exception),'NETWORK_TIMEOUT')

    def test_auth_error_does_not_retry(self):
        from urllib.error import HTTPError
        from newsroom.ai import AIResponseError
        with patch('newsroom.ai.get_api_key',return_value='test'), patch('newsroom.ai.urllib.request.urlopen',side_effect=HTTPError('https://api.openai.com',401,'private text',{},None)) as opener:
            with self.assertRaises(AIResponseError) as raised: self.analyze()
        self.assertEqual(opener.call_count,1)
        self.assertEqual(str(raised.exception),'HTTP_401')
