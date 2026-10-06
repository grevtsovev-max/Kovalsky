import copy
import json
from unittest.mock import patch
import unittest
import test_recovery_integration as recovery
from newsroom.core import process_item
from newsroom.cli import publish


class MemoryIntegrationTests(unittest.TestCase):
    setUp = recovery.RecoveryIntegrationTests.setUp

    def test_missing_fact_in_draft_is_redrafted_from_saved_analysis(self):
        from newsroom.core import _saved_material
        self.prepare()
        result = self.result()
        good = {key: copy.deepcopy(result[key]) for key in ('headline_ru','summary_ru','what_is_new','editorial_check')}
        bad = {**good, 'summary_ru': 'Банк России уточнил порядок доступа российских участников к цифровым активам.'}
        result['_needs_post_draft'] = True
        result['headline_ru'] = result['summary_ru'] = ''
        item = dict(self.item, url='https://example.org/draft-recovery')
        article = dict(self.article, **item)
        with patch('newsroom.core.fetch_publisher_article', return_value=article) as read, \
             patch('newsroom.core.get_api_key', return_value='test'), \
             patch('newsroom.core.analyze_with_ai', return_value=result) as analyze, \
             patch('newsroom.ai.draft_post', side_effect=[bad,good]) as writer:
            self.assertEqual(process_item(self.db,self.source,item,.35,3500,48,ai_settings=self.config['ai']), 'WAITING_CONFIRMATION')
            row = self.db.execute('SELECT * FROM items').fetchone()
            saved = json.loads(self.db.execute('SELECT result_json FROM item_analysis').fetchone()[0])
            self.assertTrue(saved['_needs_post_draft'])
            self.assertTrue(saved['memory_issues'])
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)
            resumed = _saved_material(row,self.source)
            outcome = process_item(self.db,self.source,resumed,.35,3500,48,ai_settings=self.config['ai'],existing_item_id=row['item_id'])
        self.assertEqual(outcome,'NEW_STORY')
        self.assertEqual(read.call_count,1)
        self.assertEqual(analyze.call_count,1)
        self.assertEqual(writer.call_count,2)
        self.assertTrue(writer.call_args.args[0]['memory_issues'])
        for call in writer.call_args_list:
            contract = call.args[2]['_draft_contract']
            self.assertEqual(contract['text_field'], 'summary_ru')
            self.assertFalse(contract['has_previous_publication'])
            self.assertIn(self.evidence, contract['required_fact_quotes'])
            self.assertTrue(contract['source_footer'].startswith('Источник: ['))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM story_facts').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],1)

    def result(self, action='NEW_STORY', value='доступ открыт', previous='', relation='NEW'):
        result=copy.deepcopy(self.result_data)
        result.update(action=action,story_id='1' if previous else '',what_is_new=self.evidence,
                      facts=[{'text':self.evidence,'claim_type':'FACT'}])
        result['memory']={'match_status':'CERTAIN','existing_event_id':'',
            'event':{'subject':'Банк России','action':'установил','object':'условия доступа','jurisdiction':'RU',
                     'event_date':self.now[:10],'statement_date':'','effective_date':'','document_id':'','stage':'APPROVED'},
            'claims':[{'subject':'Банк России','predicate':'условия доступа','scope':'цифровые активы RU','value':value,
                'statement':self.evidence,'claim_type':'FACT','source_quote':self.evidence,'post_quote':self.evidence,
                'valid_from':'','valid_to':'','previous_fact_id':previous,'relation':relation,
                'change_type':'ACCESS_CHANGE','material':True,'material_reason':'Изменяются условия доступа участников российского рынка.'}]}
        return result

    def prepare(self):
        self.result_data=self.result
        del self.result
        self.config['ai']['memory_mode']='enforce'
        self.config['newsroom']['auto_publish_since']=self.now
        self.config['telegram']={'chat_id':'@test_channel','chat_id_env':'KOVALSKY_TEST_UNUSED_ENV'}

    def run_item(self, number, result):
        item=dict(self.item,url=f'https://example.org/{number}',content='Отдельный материал '+str(number))
        article=dict(self.article,**item)
        with patch('newsroom.core.fetch_publisher_article',return_value=article),patch('newsroom.core.get_api_key',return_value='test'),patch('newsroom.core.analyze_with_ai',return_value=result):
            return process_item(self.db,self.source,item,.35,3500,48,ai_settings=self.config['ai'])

    def test_published_repeat_attaches_evidence_without_new_post(self):
        self.prepare()
        self.assertEqual(self.run_item(1,self.result()),'NEW_STORY')
        with patch('newsroom.cli.telegram_send',return_value='101'):
            publish(self.db,self.config,1,automatic=True)
        self.assertEqual(self.run_item(2,self.result('DUPLICATE',previous='1',relation='REPEAT')),'DUPLICATE')
        self.assertEqual(self.db.execute('SELECT count(*) FROM posts').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM fact_evidence').fetchone()[0],2)

    def test_known_unpublished_fact_can_be_first_post(self):
        self.prepare()
        result=self.result();result['publication_recommendation']='WAIT_FOR_AUTOMATION'
        self.assertEqual(self.run_item(1,result),'WAITING_CONFIRMATION')
        self.assertEqual(self.db.execute('SELECT count(*) FROM story_facts').fetchone()[0],1)
        self.assertEqual(self.run_item(2,self.result('DUPLICATE',previous='1',relation='REPEAT')),'UPDATE_CANDIDATE')
        with patch('newsroom.cli.telegram_send',return_value='101') as send:
            publish(self.db,self.config,1,automatic=True)
            self.assertNotIn('Ранее:',send.call_args.args[1])

    def test_malformed_memory_and_missing_source_do_not_publish(self):
        self.prepare()
        result=self.result();result['memory']['claims'][0]['source_quote']='Выдуманная длинная цитата которой в прочитанном материале нет.'
        self.assertEqual(self.run_item(1,result),'WAITING_CONFIRMATION')
        self.assertEqual(self.db.execute('SELECT count(*) FROM posts').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM story_facts').fetchone()[0],0)

    def test_send_gate_blocks_legacy_post_without_memory(self):
        self.prepare()
        self.config['ai']['memory_mode']='off'
        self.run_item(1,self.result())
        self.config['ai']['memory_mode']='enforce'
        with patch('newsroom.cli.telegram_send') as send:
            with self.assertRaisesRegex(RuntimeError,'MEMORY_BINDING_REQUIRED'):
                publish(self.db,self.config,1,automatic=True)
            send.assert_not_called()
