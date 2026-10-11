import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from newsroom.core import process_item
from newsroom.db import connect
from newsroom.keyword_filter import match


class KeywordFilterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = connect(str(Path(folder.name) / 'news.db'))
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO sources(name,type,url) VALUES('Test','rss','https://example.org/rss')")
        self.source = self.db.execute('SELECT * FROM sources').fetchone()
        self.settings = {'_keyword_prefilter': {'version': 'sheet-v1', 'keywords': ['цифровой депозитарий']}}
        self.item = {'title': 'Реестр цифровых депозитариев опубликован',
                     'description': '', 'url': 'https://example.org/news',
                     'published_at': datetime.now(timezone.utc).isoformat()}

    def process(self):
        return process_item(self.db, self.source, dict(self.item), .35, 3500, 24, ai_settings=self.settings)

    def test_word_forms_phrases_and_boundaries(self):
        for text, keyword in [('В реестре цифровых депозитариев', 'цифровой депозитарий'),
                              ('Платёжными агентами стали банки', 'платежный агент'),
                              ('Получила новую лицензию', 'получить лицензию'),
                              ('Выпуск смарт контрактов', 'смарт-контракт'),
                              ('Рынок digital assets', 'digital asset'),
                              ('Bitcoin вырос', 'bitcoin'),
                              ('Налог на криптовалюты', 'криптовалют')]:
            self.assertEqual(match(text, [keyword]), keyword)
        self.assertIsNone(match('Контракт TONIC', ['TON']))
        self.assertIsNone(match('Отставка руководителя', ['ставка']))
        self.assertIsNone(match('Цифровой рынок далеко за пределами новых правил и условий депозитария', ['цифровой депозитарий']))

    def test_nonmatching_material_is_saved_without_reading_or_model_calls(self):
        self.item['title'] = 'Прогноз погоды на неделю'
        with patch('newsroom.core.fetch_publisher_article') as read, patch('newsroom.ai.request_response') as model:
            self.assertEqual(self.process(), 'NOISE')
        read.assert_not_called()
        model.assert_not_called()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM items').fetchone()[0], 1)
        evidence = json.loads(self.db.execute("SELECT result_json FROM material_stage_results WHERE stage='screening'").fetchone()[0])
        self.assertFalse(evidence['passed'])
        self.assertEqual(evidence['version'], 'sheet-v1')

    def test_matching_available_body_reaches_reading_without_ai_triage(self):
        self.item.update(title='Опубликовано решение', content='Создан реестр цифровых депозитариев.')
        with patch('newsroom.core.fetch_publisher_article', side_effect=TimeoutError) as read, patch('newsroom.core._recover_primary', return_value=None), patch('newsroom.ai.request_response') as model:
            self.assertEqual(self.process(), 'PRIMARY_RETRY')
        self.assertTrue(read.called)
        model.assert_not_called()
        evidence = json.loads(self.db.execute("SELECT result_json FROM material_stage_results WHERE stage='screening'").fetchone()[0])
        self.assertEqual(evidence['matched_keyword'], 'цифровой депозитарий')
        self.assertTrue(evidence['passed'])
        from newsroom.pipeline import pipeline_snapshot
        view = pipeline_snapshot(self.db, {}, {'period': ['all']}, [])
        self.assertEqual(view['totals']['first_filter'], 1)

    def test_empty_dictionary_blocks_configuration_without_rejecting_material(self):
        self.settings['_keyword_prefilter']['keywords'] = []
        with patch('newsroom.core.fetch_publisher_article') as read, patch('newsroom.ai.request_response') as model:
            self.assertEqual(self.process(), 'TECHNICAL_ERROR')
        read.assert_not_called()
        model.assert_not_called()
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0], 'TECHNICAL_ERROR')
