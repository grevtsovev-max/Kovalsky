"""Collection continues while the retired editor has no executable entrypoints."""
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from newsroom import ai, core, cli
from newsroom.db import connect


class EditorialRemovedTests(unittest.TestCase):
    def test_retired_editor_modules_and_entrypoints_are_absent(self):
        for module in ('quality','policy','processor','triage','editorial_registry','style_examples','review','analysis'):
            with self.subTest(module=module):
                self.assertIsNone(importlib.util.find_spec('newsroom.'+module))
        for module,names in [(ai,('analyze','draft_post','validate_draft','correct_published_post')),
                             (core,('process_item','process_item_steps')),
                             (cli,('auto_publish','is_eligible_for_auto_publish','digest'))]:
            for name in names:self.assertFalse(hasattr(module,name),name)

    def test_legacy_editor_settings_cannot_create_or_publish_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            database=str(Path(directory)/'news.sqlite3')
            config={'newsroom':{'database':database,'auto_publish':True,'independent_processing':True,
                                'weekly_analysis_enabled':True,'story_watch_enabled':True},'ai':{},
                    'sources':[{'name':'Feed','type':'rss','url':'https://example.org/rss','active':True}]}
            material={'url':'https://example.org/news','title':'Оригинальный заголовок',
                      'description':'Исходное описание','content':'Исходный текст без редактирования',
                      'published_at':None,'updated_at':None}
            with patch('newsroom.core.fetch_rss',return_value=[material]), patch('newsroom.ai.request_response') as api, patch('newsroom.cli.telegram_send') as telegram:
                result=core.run_cycle(config)
                self.assertNotIn('ERROR',result)
                self.assertNotIn('SOURCE_ERROR',result)
                api.assert_not_called();telegram.assert_not_called()
                with connect(database) as db:
                    row=db.execute('SELECT url,title,content FROM items').fetchone()
                    self.assertEqual(tuple(row),(material['url'],material['title'],material['content']))
                    for table in ('posts','item_analysis','processing_jobs','api_usage'):
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)
                core.run_cycle(config)
                with connect(database) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM items').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)

    def test_feedback_from_retired_editor_is_not_a_learning_input(self):
        from newsroom.topic_registry import collect_feedback
        import inspect
        self.assertNotIn('editorial_feedback',inspect.getsource(collect_feedback))

    def test_dashboard_has_no_editor_controls(self):
        from newsroom.dashboard import PAGE
        for endpoint in ('/api/policy','/api/editorial-registry','/api/corrections'):
            self.assertNotIn(endpoint,PAGE)
        self.assertIn('Редактор удалён',PAGE)

    def test_config_load_and_empty_database_diagnostics(self):
        from newsroom.diagnostics import snapshot
        with tempfile.TemporaryDirectory() as directory:
            configfile=Path(directory)/'config.toml';database=Path(directory)/'news.sqlite3'
            configfile.write_text('[newsroom]\ndatabase = "'+str(database)+'"\n')
            config=cli.load_config(str(configfile))
            with connect(str(database)) as db:
                data=snapshot(db,config)
                self.assertEqual(data['editorial'],'removed')
                self.assertFalse(data['independent_processing'])
