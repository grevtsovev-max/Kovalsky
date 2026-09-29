import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from newsroom.ai import AIResponseError,request_response
from newsroom.core import (fetch_web_search,FetchedItems,fetch_publisher_article,is_non_news_telegram_format,require_primary_source_review,_recover_primary,_read_telegram_primary)
from newsroom.quality import editorial_issues
from newsroom.db import connect

class ReliabilityTests(unittest.TestCase):
    def test_api_error_code_is_specific_without_message_or_secret(self):
        err=HTTPError('https://api.openai.com',403,'private',{},io.BytesIO(json.dumps({'error':{'code':'unsupported_country_region_territory','message':'secret'}}).encode()))
        with patch('newsroom.ai.get_api_key',return_value='secret'),patch('newsroom.ai.urllib.request.urlopen',side_effect=err) as call:
            with self.assertRaises(AIResponseError) as caught:request_response({}, {})
        self.assertEqual(str(caught.exception),'HTTP_403:unsupported_country_region_territory');call.assert_called_once()
    def test_search_uses_independent_index_when_api_fails(self):
        with patch('newsroom.core.request_response',side_effect=AIResponseError('HTTP_403')),patch('newsroom.core.fetch_google_news',return_value=FetchedItems([{'url':'https://tass.ru/1'}])) as fallback:
            result=fetch_web_search('цифровая валюта',{})
        self.assertEqual(len(result),1);self.assertIn('SEARCH_FALLBACK:HTTP_403',result.diagnostics);fallback.assert_called_once()
    def test_search_fallback_failure_preserves_both_safe_error_codes(self):
        with patch('newsroom.core.request_response',side_effect=AIResponseError('NETWORK_TIMEOUT')), \
             patch('newsroom.core.fetch_google_news',side_effect=TimeoutError('https://private.example timed out')):
            with self.assertRaises(AIResponseError) as caught:
                fetch_web_search('цифровая валюта',{})
        self.assertEqual(caught.exception.code,
                         'SEARCH_FALLBACK_FAILED:NETWORK_TIMEOUT:NETWORK_TIMEOUT')
        self.assertNotIn('private.example',str(caught.exception))
    def test_roundups_screened_before_read_for_all_sources(self):
        for typ in ['rss','telegram']:
            for title in ['#календарь Ключевые события','Что случилось на крипторынке, пока все спали — обзор','Дайджест новостей']:
                self.assertTrue(is_non_news_telegram_format({'type':typ},{'title':title}))
        self.assertFalse(is_non_news_telegram_format({'type':'rss'},{'title':'В СФ предложили использовать цифровую валюту в качестве залога'}))
    def test_wait_decision_is_never_upgraded(self):
        result=require_primary_source_review({'publication_recommendation':'WAIT_FOR_AUTOMATION'},'READ',{'url':'https://cbr.ru/1','content':'Прочитанный документ'})
        self.assertEqual(result['publication_recommendation'],'WAIT_FOR_AUTOMATION')
    def test_dates_from_actual_article_metadata(self):
        url='https://tass.ru/1';page='<html><meta property="article:published_time" content="2026-09-28T01:05:32+00:00"><p>'+('Об этом сообщил ТАСС сенатор. '*8)+'</p></html>'
        with patch('newsroom.core._request_with_url',return_value=(page.encode(),url,'text/html')):
            article=fetch_publisher_article(url,'ТАСС',None)
        self.assertEqual(article['published_at'],'2026-09-28T01:05:32+00:00')
    def test_quality_checks_history_stage_and_missing_audit(self):
        audit={'source_matches_event':True,'attribution_preserved':True,'stage_preserved':True,'history_required':True,'history_explained':True,'history_note':'Ранее ведомство поддерживало такой подход.','headline_main_event':True,'lead_event_first':True,'paragraphs_concise_distinct':True,'no_editorial_process_notes':True}
        facts={'geographic_scope':'RUSSIA','event_status':'PROPOSAL','editorial_check':audit}
        body='Предложено проработать изменения.\n\n'+audit['history_note']
        self.assertEqual(editorial_issues('🇷🇺 Секция совета предложила залог криптовалюты',body,facts),[])
        self.assertIn('POSITION_HISTORY_MISSING',editorial_issues('🇷🇺 Новость','Предложено проработать изменения.',facts))
        self.assertIn('PROPOSAL_PRESENTED_AS_LAW',editorial_issues('🇷🇺 Закон принят',body,facts))
        self.assertIn('SOURCE_MATCHES_EVENT',editorial_issues('🇷🇺 Новость',body,{}))
    def test_editorial_gate_rejects_crypto_banks_process_note_and_checks_final_source(self):
        bad={'geographic_scope':'CIS','event_status':'IMPLEMENTATION','editorial_check':{'source_matches_event':True,'attribution_preserved':True,'stage_preserved':True,'history_required':True,'history_explained':True,'history_note':'В переданных материалах прежних заявлений не найдено.'}}
        headline='🇧🇾 ПВТ сообщил о двух крипторезидентах до аккредитации Нацбанка'
        body='В переданных материалах нет предыдущих заявлений Рябовой по этой теме для сравнения.\n\nИсточник: https://tass.ru/28159153'
        issues=editorial_issues(headline,body,bad,final_post=True)
        self.assertIn('EDITORIAL_PROCESS_NOTE',issues)
        self.assertIn('HEADLINE_MAIN_EVENT',issues)
        self.assertIn('SOURCE_LINK_FORMAT',issues)
        good={'geographic_scope':'CIS','event_status':'IMPLEMENTATION','primary_source':{'url':'https://tass.ru/28159153'},'editorial_check':{'source_matches_event':True,'attribution_preserved':True,'stage_preserved':True,'history_required':False,'history_explained':False,'history_note':'','headline_main_event':True,'lead_event_first':True,'paragraphs_concise_distinct':True,'no_editorial_process_notes':True}}
        text='В Беларуси зарегистрировали первые два криптобанка\n\nВ реестр вошли две организации — резиденты ПВТ. Для начала работы им ещё потребуется аккредитация Нацбанка.\n\nИсточник: [ТАСС](https://tass.ru/28159153)'
        self.assertEqual(editorial_issues(text.splitlines()[0],text.partition('\n')[2],good,final_post=True),[])
    def test_digest_gate_rejects_missing_link_and_accepts_compact_linked_headline(self):
        from newsroom.quality import digest_issues
        self.assertIn('DIGEST_ENTRY_FORMAT',digest_issues('📣 Крипторынок: главное за день · 28.09.2026\n\n📌 Новая важная новость'))
        self.assertEqual(digest_issues('📣 Крипторынок: главное за день · 28.09.2026\n\n📌 ЦБ [предложил](https://t.me/channel/1) изменения'),[])
    def test_recovery_search_bounded_and_unrelated_result_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            db=connect(str(Path(d)/'test.db'));self.addCleanup(db.close)
            settings={'_recovery_search_budget':1};item={'url':'https://tass.ru/1','title':'Секция совета предложила залог цифровой валюты'}
            with patch('newsroom.core.fetch_google_news',return_value=[{'title':'Биржа запустила торги акциями','primary_source_status':'READ'}]) as search:
                self.assertIsNone(_recover_primary(db,item,settings));self.assertIsNone(_recover_primary(db,item,settings));search.assert_called_once()
            self.assertEqual(settings['_recovery_search_budget'],0)
    def test_own_media_telegram_post_and_forward(self):
        source={'name':'ТАСС (Telegram)','url':'https://t.me/tass_agency','reputation':'reputable_media'}
        base={'url':'https://t.me/tass_agency/12','title':'Новость','content':'Цифровую валюту предложили использовать как залог, сообщил ТАСС сенатор Шейкин.','discovery_links':[]}
        own=dict(base);_read_telegram_primary(own,source);self.assertEqual(own['primary_source_type'],'ORIGINAL_MEDIA_INTERVIEW')
        forwarded=dict(base,telegram_forwarded=True);_read_telegram_primary(forwarded,source);self.assertNotEqual(forwarded['primary_source_status'],'READ')

class QueueTriageTests(unittest.TestCase):
    def test_unreadable_off_topic_material_leaves_retry_queue(self):
        from datetime import datetime,timezone
        from newsroom.core import process_item
        with tempfile.TemporaryDirectory() as d:
            db=connect(str(Path(d)/'test.db'));self.addCleanup(db.close)
            db.execute("INSERT INTO sources(name,type,url) VALUES('News','rss','https://example.org/rss')")
            source=db.execute('SELECT * FROM sources').fetchone()
            item={'url':'https://example.org/1','title':'Биржа в Корее запустила торги','published_at':datetime.now(timezone.utc).isoformat()}
            result={'action':'NOISE','is_relevant':False,'geographic_scope':'OTHER','russia_cis_impact':'NONE','publication_recommendation':'DO_NOT_PUBLISH'}
            with patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError),patch('newsroom.core.get_api_key',return_value='test'),patch('newsroom.core.analyze_with_ai',return_value=result):
                self.assertEqual(process_item(db,source,item,.35,3500,48,ai_settings={}), 'NOISE')
            self.assertEqual(db.execute('select count(*) from posts').fetchone()[0],0)
    def test_quality_failure_blocks_manual_send_too(self):
        from newsroom.cli import publish
        facts={'primary_source_status':'READ','primary_source':{'url':'https://cbr.ru/1','content_sha256':'hash'},'geographic_scope':'RUSSIA'}
        row={'post_id':1,'text':'Закон принят\nТекст новости\nИсточник: https://cbr.ru/1','fact_check_result':json.dumps(facts)}
        from unittest.mock import MagicMock
        db=MagicMock();db.execute.return_value.fetchone.return_value=row
        with patch('newsroom.cli.telegram_send') as send:
            with self.assertRaises(RuntimeError):publish(db,{},1)
            send.assert_not_called()
    def test_missing_evidence_does_not_discard_local_news(self):
        from datetime import datetime,timezone
        from newsroom.core import process_item
        with tempfile.TemporaryDirectory() as d:
            db=connect(str(Path(d)/'test.db'));self.addCleanup(db.close)
            db.execute("INSERT INTO sources(name,type,url) VALUES('News','rss','https://example.org/rss')")
            source=db.execute('SELECT * FROM sources').fetchone()
            item={'url':'https://example.org/1','title':'В России предложили залог цифровой валюты','published_at':datetime.now(timezone.utc).isoformat()}
            result={'action':'NOISE','is_relevant':False,'geographic_scope':'RUSSIA','russia_cis_impact':'INDIRECT','publication_recommendation':'DO_NOT_PUBLISH'}
            with patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError),patch('newsroom.core._recover_primary',return_value=None),patch('newsroom.core.screen_item',return_value={'decision':'KEEP','reason':'Новое предложение о залоге','what_is_new':'Новый механизм'}),patch('newsroom.core.analyze_with_ai',return_value=result) as editor:
                self.assertEqual(process_item(db,source,item,.35,3500,48,ai_settings={'triage_enabled':True}), 'PRIMARY_RETRY')
                editor.assert_not_called()
            self.assertEqual(db.execute('select count(*) from posts').fetchone()[0],0)
