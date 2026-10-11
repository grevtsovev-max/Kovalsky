import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom import regulatory as reg

TEXT = 'Проект указания Банка России о правилах обмена цифровых валют. Обсуждение до 29 сентября 2026 года.'
ANALYSIS = dict(relevant=True, kind='PROJECT', stage='PROJECT',
                evidence='о правилах обмена цифровых валют', stage_evidence='Проект указания Банка России',
                summary='Проект правил обмена цифровых валют.', affected='Операторы обмена',
                next_step='Проверить итоговый текст.', document_number='', deadlines=[
                    dict(date='2026-09-29', meaning='Срок обсуждения проекта',
                         evidence='Обсуждение до 29 сентября 2026 года')])


class RegulatoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {'newsroom': {'database': str(Path(self.tmp.name)/'newsroom.sqlite3'),
                                   'relevance_terms': ['цифровая валюта']}, 'ai': {}}
        self.db = reg.connect(self.config)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_host_boundary_and_protocol(self):
        self.assertTrue(reg.official('https://www.cbr.ru/document/1'))
        for url in ['https://cbr.ru.evil.org/doc', 'https://evilcbr.ru/doc', 'file:///etc/passwd',
                    'https://u@cbr.ru/doc', 'https://cbr.ru:5555/doc']:
            self.assertFalse(reg.official(url))

    def test_old_documents_are_queued_and_canonical_duplicates_are_not(self):
        self.assertEqual(reg.enqueue(self.db, 'cbr', [dict(url='https://cbr.ru/document/2020?utm_source=x',title='Старый акт')]),1)
        self.assertEqual(reg.enqueue(self.db, 'cbr', [dict(url='https://cbr.ru/document/2020', title='Повтор')]),0)

    def test_evidence_and_date_validation_fail_closed(self):
        reg.validate_analysis(ANALYSIS, TEXT)
        for field in ['evidence', 'stage_evidence']:
            result = copy.deepcopy(ANALYSIS)
            result[field] = 'Выдуманное подтверждение, которого нет в документе'
            with self.assertRaises(ValueError):
                reg.validate_analysis(result,TEXT)
        result=copy.deepcopy(ANALYSIS)
        result['deadlines'][0]['date']='2026-02-31'
        with self.assertRaises(ValueError):
            reg.validate_analysis(result,TEXT)

    def document(self):
        reg.enqueue(self.db,'cbr',[dict(url='https://cbr.ru/document/1',title='Документ')])
        return self.db.execute('SELECT * FROM reg_documents').fetchone()

    @patch.object(reg,'analyze_document',return_value=ANALYSIS)
    @patch.object(reg,'read_document',return_value=(TEXT,'https://cbr.ru/document/1',[]))
    def test_unchanged_content_no_extra_analysis_changed_content_keeps_history(self,read,analyze):
        reg.process_document(self.db,self.document(),self.config)
        self.assertEqual(reg.process_document(self.db,self.document(),self.config),'UNCHANGED')
        self.assertEqual(analyze.call_count,1)
        read.return_value=(TEXT+' Новая редакция.','https://cbr.ru/document/1',[])
        reg.process_document(self.db,self.document(),self.config)
        self.assertEqual(len(reg.snapshot(self.config)['items'][0]['history']),2)

    @patch.object(reg,'discover',side_effect=RuntimeError('sensitive details'))
    @patch.object(reg,'read_document',side_effect=RuntimeError('sensitive details'))
    def test_source_failure_and_unreadable_document_visible_without_fake_analysis(self,read,discover):
        self.document()
        result=reg.run_cycle(self.config,discover_limit=1)
        self.assertEqual(result['SOURCE_ERROR'],1)
        snap=reg.snapshot(self.config)
        self.assertEqual(snap['items'][0]['status'],'RETRY')
        self.assertEqual(snap['items'][0]['analysis'],{})
        self.assertNotIn('sensitive',str(snap))

    def test_snapshot_does_not_create_database_before_first_run(self):
        config={'newsroom':{'database':str(Path(self.tmp.name)/'unstarted/news.sqlite3')}}
        self.assertEqual(reg.snapshot(config)['total'],0)
        self.assertFalse(Path(reg.database_path(config)).exists())

    @patch.object(reg,'analyze_document',return_value={**ANALYSIS,'relevant':False,'kind':'OTHER'})
    @patch.object(reg,'read_document',return_value=(TEXT,'https://cbr.ru/document/1',[]))
    def test_noise_stored_but_not_shown_in_review_queue(self,read,analyze):
        reg.process_document(self.db,self.document(),self.config)
        self.assertEqual(reg.snapshot(self.config)['total'],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM reg_versions').fetchone()[0],1)


if __name__=='__main__':
    unittest.main()
