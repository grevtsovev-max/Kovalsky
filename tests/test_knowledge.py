import copy
import json
import tempfile
import unittest
from pathlib import Path
from newsroom.db import connect
from newsroom.knowledge import ingest, bind_post, publication_issues, context, MemoryInvalid


def memory(value='октябрь', **changes):
    statement = f'Компания запускает криптосервис для российских клиентов в {value}.'
    result = {'match_status':'CERTAIN', 'existing_event_id':'',
              'event':{'subject':'Компания','action':'объявила срок','object':'криптосервис','jurisdiction':'RU',
                       'event_date':'2026-09-28','statement_date':'2026-09-28','effective_date':'',
                       'document_id':'','stage':'PROPOSED'},
              'claims':[{'subject':'Компания','predicate':'срок запуска','scope':'криптосервис RU',
                         'value':value,'statement':statement,'claim_type':'CLAIM','source_quote':statement,
                         'post_quote':statement,'valid_from':'','valid_to':'','previous_fact_id':'',
                         'relation':'NEW','change_type':'DATE_CHANGE','material':True,
                         'material_reason':'Срок определяет доступ российских клиентов к криптосервису.'}]}
    result['claims'][0].update(changes)
    return {'memory':result}


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=connect(str(Path(self.tmp.name)/'db'));self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO sources(source_id,name,type,url) VALUES(1,'Источник','rss','https://example.test')")
        self.db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'crypto','crypto','2026','2026')")

    def add(self, number, result=None, **kwargs):
        result=result or memory()
        self.db.execute("INSERT INTO items(item_id,source_id,url,canonical_url,title,discovered_at,content_hash,title_hash) VALUES(?,1,?,?,?,'2026',?,?)", (number,str(number),str(number),'title'+str(number),str(number),str(number)))
        source={'url':'https://example.test/'+str(number),'content':' '.join(c['source_quote'] for c in result['memory']['claims']), 'type':'OFFICIAL','status':'READ'}
        source.update(kwargs.pop('source', {}))
        return ingest(self.db,number,1,result,source,**kwargs)

    def post(self, diff, item_id=1, status='PUBLISHED'):
        text=diff['post_claims'][0]['post_quote']
        post_id=self.db.execute("INSERT INTO posts(story_id,text,status,created_at,version,post_hash) VALUES(1,?,?, '2026',1,'hash')",(text,status)).lastrowid
        bind_post(self.db,post_id,item_id,diff,text)
        return post_id,text

    def test_three_articles_share_event_and_multiple_evidence(self):
        first=self.add(1);self.post(first)
        for i in (2,3):
            diff=self.add(i,memory(relation='REPEAT',previous_fact_id='1'))
            self.assertFalse(diff['significant_update'])
            self.assertEqual(diff['repeated_facts'],[1])
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM event_items').fetchone()[0],3)
        self.assertEqual(self.db.execute('SELECT count(*) FROM fact_evidence').fetchone()[0],3)

    def test_false_repeat_explains_exact_prior_fields_without_accepting_change(self):
        self.add(1)
        with self.assertRaises(MemoryInvalid) as error:
            self.add(2, memory('декабрь', relation='REPEAT', previous_fact_id='1'))
        message = str(error.exception)
        self.assertIn('FALSE_REPEAT', message)
        self.assertIn('"value": "октябрь"', message)
        self.assertIn('"claim_type": "CLAIM"', message)
        self.assertIn('"valid_from": ""', message)
        self.assertEqual(self.db.execute('SELECT count(*) FROM story_facts').fetchone()[0], 1)

    def test_known_but_unpublished_fact_remains_eligible(self):
        first=self.add(1);self.post(first,status='REJECTED')
        diff=self.add(2,memory(relation='REPEAT',previous_fact_id='1'))
        self.assertTrue(diff['significant_update'])
        self.assertEqual(diff['new_facts'],[])
        self.assertEqual(diff['material_unpublished_facts'],[1])
        post_id,text=self.post(diff,2,status='PENDING')
        self.assertEqual(publication_issues(self.db,post_id,text),[])

    def test_date_change_preserves_historical_fact_and_new_event(self):
        first=self.add(1);self.post(first)
        analysis=memory('декабрь',relation='SUPERSEDES',previous_fact_id='1')
        analysis['memory']['event']['event_date']='2026-09-29'
        diff=self.add(2,analysis)
        self.assertEqual(diff['changed_facts'],[2])
        self.assertTrue(diff['significant_update'])
        self.assertEqual(self.db.execute('SELECT value FROM story_facts WHERE fact_id=1').fetchone()[0],'октябрь')
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT relation FROM fact_relations').fetchone()[0],'SUPERSEDES')

    def test_official_confirmation_is_material_without_promoting_report(self):
        first=self.add(1,memory(claim_type='REPORT'),publisher_report=True);self.post(first)
        diff=self.add(2,memory(claim_type='FACT',relation='CONFIRMS',previous_fact_id='1',change_type='OFFICIAL_CONFIRMATION'))
        self.assertTrue(diff['significant_update'])
        self.assertEqual(diff['confirmed_facts'],[2])
        self.assertEqual(self.db.execute('SELECT fact_type FROM story_facts WHERE fact_id=1').fetchone()[0],'REPORT')
        self.assertEqual(self.db.execute('SELECT origin_status FROM source_snapshots WHERE item_id=1').fetchone()[0],'UNKNOWN')

    def test_source_quote_and_type_are_machine_checked(self):
        with self.assertRaisesRegex(MemoryInvalid,'UNGROUNDED'):
            self.add(1,source={'content':'Совсем иной текст, не содержащий подтверждения утверждения.'})
        with self.assertRaisesRegex(MemoryInvalid,'PROMOTED'):
            self.add(2,memory(claim_type='FACT'),publisher_report=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM story_facts').fetchone()[0],0)

    def test_conflict_never_authorizes_publication(self):
        self.add(1)
        diff=self.add(2,memory('декабрь',relation='CONTRADICTS',previous_fact_id='1'))
        self.assertEqual(diff['conflict_state'],'UNRESOLVED')
        self.assertFalse(diff['significant_update'])
        # Merely omitting the previous fact does not hide incompatible values.
        diff=self.add(3,memory('ноябрь'))
        self.assertEqual(diff['conflict_state'],'UNRESOLVED')

    def test_bad_relation_rolls_back_all_knowledge_mutations(self):
        self.add(1)
        broken=memory('декабрь',relation='SUPERSEDES',previous_fact_id='99')
        with self.assertRaisesRegex(MemoryInvalid,'INVALID_PREVIOUS'):
            self.add(2,broken)
        self.assertEqual(self.db.execute('SELECT count(*) FROM event_items').fetchone()[0],1)

    def test_send_gate_rechecks_coverage_and_post_text(self):
        first=self.add(1);post,text=self.post(first,status='PENDING')
        self.assertEqual(publication_issues(self.db,post,text),[])
        self.assertEqual(publication_issues(self.db,post,'Другой текст'),['POST_FACT_TEXT_CHANGED'])
        second=self.add(2,memory(relation='REPEAT',previous_fact_id='1'))
        self.post(second,2)
        self.assertEqual(publication_issues(self.db,post,text),['NO_UNPUBLISHED_MATERIAL_FACT'])

    def test_material_change_must_appear_in_post(self):
        first=self.add(1)
        self.db.execute("INSERT INTO posts(story_id,text,created_at,version,post_hash) VALUES(1,'unrelated','2026',1,'hash')")
        with self.assertRaisesRegex(MemoryInvalid,'MISSING_FROM_POST'):
            bind_post(self.db,1,1,first,'unrelated')

    def test_changed_stage_creates_second_event_in_same_story(self):
        self.add(1)
        analysis=memory('ноябрь',relation='SUPERSEDES',previous_fact_id='1')
        analysis['memory']['event']['stage']='APPROVED'
        self.add(2,analysis)
        self.assertEqual(self.db.execute('SELECT count(*) FROM events WHERE story_id=1').fetchone()[0],2)

    def test_context_distinguishes_stored_and_published(self):
        diff=self.add(1)
        self.assertFalse(context(self.db,[1])[0]['facts'][0]['published'])
        self.post(diff)
        self.assertTrue(context(self.db,[1])[0]['facts'][0]['published'])

    def test_uncertain_match_does_not_pollute_memory(self):
        analysis=memory();analysis['memory']['match_status']='UNCERTAIN'
        with self.assertRaisesRegex(MemoryInvalid,'UNCERTAIN'):
            self.add(1,analysis)
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0],0)

    def test_document_match_finds_dormant_story_with_different_headline(self):
        from newsroom.knowledge import related_story_ids, exact_story
        analysis=memory();analysis['memory']['event']['document_id']='RU ЦБ 123-ФЗ'
        self.add(1,analysis)
        self.db.execute("UPDATE stories SET status='DORMANT',headline='Иная формулировка' WHERE story_id=1")
        self.assertEqual(related_story_ids(self.db,'RU ЦБ 123-ФЗ вступил в силу'),[1])
        updated=copy.deepcopy(analysis['memory']);updated['event']['stage']='EFFECTIVE'
        self.assertEqual(exact_story(self.db,updated),1)

    def test_repeat_cannot_hide_previously_unresolved_conflict(self):
        self.add(1)
        self.add(2,memory('декабрь',relation='CONTRADICTS',previous_fact_id='1'))
        diff=self.add(3,memory('декабрь',relation='REPEAT',previous_fact_id='2'))
        self.assertEqual(diff['conflict_state'],'UNRESOLVED')
        self.assertFalse(diff['significant_update'])

    def test_unread_text_is_never_marked_read(self):
        with self.assertRaisesRegex(MemoryInvalid,'READ_EVIDENCE_REQUIRED'):
            self.add(1,source={'status':'UNREADABLE'})
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_snapshots').fetchone()[0],0)

    def test_invalid_temporal_interval_is_rejected(self):
        with self.assertRaisesRegex(MemoryInvalid,'VALIDITY_INTERVAL'):
            self.add(1,memory(valid_from='2026-12-01',valid_to='2026-10-01'))

    def test_competing_posts_cannot_send_same_fact_concurrently(self):
        from newsroom.delivery import deliver,DeliveryUncertain
        from unittest.mock import Mock
        first=self.add(1);post,text=self.post(first,status='PENDING')
        second=self.add(2,memory(relation='REPEAT',previous_fact_id='1'));other,_=self.post(second,2,status='PENDING')
        self.db.commit()
        config={'telegram':{'chat_id':'@test'}}
        sender=Mock(return_value='999')
        def sending(config,text):
            connection=connect(str(Path(self.tmp.name)/'db'))
            try:
                with self.assertRaises(DeliveryUncertain):
                    deliver(connection,config,'post:'+str(other),text,sender,post_id=other)
            finally:connection.close()
            return '101'
        self.assertEqual(deliver(self.db,config,'post:'+str(post),text,sending,post_id=post),'101')
        sender.assert_not_called()


    def test_late_archive_import_blocks_already_prepared_duplicate(self):
        from newsroom.archive_memory import save
        first=self.add(1);post_id,text=self.post(first,status='PENDING')
        self.assertEqual(publication_issues(self.db,post_id,text),[])
        old_id=self.db.execute("INSERT INTO posts(story_id,text,status,created_at,version,post_hash) VALUES(1,?,'PUBLISHED','2026',1,'old')",(text,)).lastrowid
        old=self.db.execute('SELECT * FROM posts WHERE post_id=?',(old_id,)).fetchone()
        claim=memory()['memory']['claims'][0]
        save(self.db,old,{'claims':[dict(claim)]},'test')
        self.assertEqual(publication_issues(self.db,post_id,text),['NO_UNPUBLISHED_MATERIAL_FACT'])


    def test_quote_locator_copies_real_source_and_rejects_changed_words(self):
        from newsroom.knowledge import grounded_span
        source='Банк России подготовил изменения регулирования для кредитных ЦФА и сообщил о планах.'
        proposed='Банк России подготовил изменения регулирования для кредитных ЦФА.'
        self.assertEqual(grounded_span(proposed,source),proposed[:-1])
        self.assertIsNone(grounded_span(proposed.replace('подготовил','утвердил'),source))
