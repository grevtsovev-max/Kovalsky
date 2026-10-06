import sqlite3
import unittest
from unittest.mock import patch

from newsroom import source_registry as registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE app_state(key TEXT PRIMARY KEY, value TEXT)')
        self.addCleanup(self.db.close)

    def row(self, name='Example', url='https://t.me/example'):
        return dict(name=name, url=url, section='СМИ', row=2, task='', url_allowed=True)

    def test_headers_spaces_and_blank_links(self):
        rows = registry.parse_tab('Название ,Ссылка\nКомпания,\nКанал,https://t.me/example\n'.encode(), 'Бренды')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['url'], '')
        with self.assertRaises(ValueError):
            registry.parse_tab(b'<html>Login</html>', 'СМИ')

    def test_sources_are_also_entities_and_blank_links_are_searched(self):
        rows = [self.row('Бизнес FM'), self.row('Бизнес FM (RSS)'),
                self.row('Человек', url='')]
        sources = registry.entity_sources(rows)
        names = [name for source in sources for name in source['registry_entities']]
        self.assertEqual(names, ['Бизнес FM', 'Человек'])
        self.assertTrue(all(s['source_role'] == 'discovery' for s in sources))

    def test_rotation_covers_all_entities_and_keeps_direct_source(self):
        rows = [self.row('Компания ' + str(i), url='') for i in range(100)]
        seen = set()
        direct = {'url': 'https://t.me/example', 'type': 'telegram', 'active': False}
        for _ in range(4):
            config = {'sources': [direct]}
            registry.schedule_entities(self.db, config, rows)
            self.assertEqual(config['sources'][0], direct)
            for source in config['sources'][1:]:
                self.assertLessEqual(len(source['url']), 1800)
                seen.update(source['registry_entities'])
        self.assertEqual(len(seen), 100)

    def test_shared_channel_does_not_become_a_personal_source(self):
        person = self.row('Иван Чебесков', 'https://t.me/minfin')
        person['section'] = 'Лица'
        config = {'sources': [{'name': 'Минфин России', 'url': person['url'], 'type': 'telegram'}]}
        registry.apply_snapshot(config, {'rows': [person]})
        self.assertEqual(config['sources'][0]['name'], 'Минфин России')
        config = {'sources': []}
        registry.apply_snapshot(config, {'rows': [person]})
        self.assertEqual(config['sources'][0]['name'], 'Канал @minfin')
        config = {'sources': []}
        registry.apply_snapshot(config, {'rows': [self.row('ВЭФ', 'https://t.me/roscongress'),
                                                   self.row('ПМЭФ', 'https://t.me/roscongress')]})
        self.assertEqual(config['sources'][0]['name'], 'Канал @roscongress')

    def test_merge_preserves_disabled_trust_and_other_sources(self):
        original = dict(name='Old', url='https://t.me/example', type='telegram',
                        active=False, reputation='unknown', source_role='aggregator')
        config = {'sources': [original, dict(name='Other', url='https://other.test/feed')]}
        snapshot = {'rows': [self.row('New'), self.row('Duplicate')]}
        rows = registry.apply_snapshot(config, snapshot)
        self.assertEqual(len(config['sources']), 2)
        source = config['sources'][0]
        self.assertFalse(source['active'])
        self.assertEqual(source['reputation'], 'unknown')
        self.assertEqual(source['source_role'], 'aggregator')
        self.assertEqual(rows[0]['status'], 'Выключен в текущих настройках')

    def test_unapproved_url_and_site_are_not_connected(self):
        row = self.row(url='https://127.0.0.1/feed')
        row['url_allowed'] = False
        self.assertIsNone(registry.source_for(row, {})[0])
        self.assertIsNone(registry.source_for(self.row(url='https://example.com/'), {})[0])

    def test_failure_keeps_last_snapshot_and_backs_off(self):
        registry.save(self.db, registry.SETTINGS, {'url': 'https://docs.google.com/'})
        registry.save(self.db, registry.SNAPSHOT, {'rows': [self.row()], 'checked_at': 1})
        self.db.commit()
        with patch.object(registry, 'read_registry', side_effect=TimeoutError) as read:
            config = {'sources': []}
            result = registry.sync(self.db, config, force=True)
            self.assertEqual(result['error'], 'TimeoutError')
            self.assertEqual(config['sources'][0]['url'], 'https://t.me/example')
            registry.sync(self.db, {'sources': []})
            self.assertEqual(read.call_count, 1)

    def test_configure_does_not_replace_settings_after_failed_read(self):
        original = {'url': 'previous'}
        registry.save(self.db, registry.SETTINGS, original)
        self.db.commit()
        payload = {'url': 'https://docs.google.com/spreadsheets/d/' + 'x'*25 + '/edit',
                   'tabs': [{'name': 'СМИ', 'gid': 123}]}
        with patch.object(registry, 'read_registry', side_effect=ValueError):
            with self.assertRaises(ValueError):
                registry.configure(self.db, payload)
        self.assertEqual(registry.state(self.db, registry.SETTINGS), original)

    def test_read_failure_in_one_tab_rejects_entire_snapshot(self):
        settings = {'spreadsheet_id': 'x'*25, 'tabs': [{'name': 'СМИ', 'gid': '1'}, {'name': 'Лица', 'gid': '2'}]}
        def fetch(url, **kwargs):
            if 'gid=2' in url:
                raise TimeoutError
            return 'Название,Ссылка\nExample,\n'.encode(), url, 'text/csv'
        with patch('newsroom.core._request_with_url', side_effect=fetch):
            with self.assertRaises(TimeoutError):
                registry.read_registry(settings)


if __name__ == '__main__':
    unittest.main()
