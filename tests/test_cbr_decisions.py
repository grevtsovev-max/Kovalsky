import unittest
from unittest.mock import patch

from newsroom.core import fetch_rss
from newsroom.source_registry import apply_snapshot


FEED = 'https://www.cbr.ru/rss/rbr'
PARENT = 'https://www.cbr.ru/rss/RssNews'
PAYLOAD = '''<rss><channel><item>
<title>19.10.2026 О внесении в реестр: ООО "ЦИФРОВОЙ ПОРЯДОК"</title>
<guid isPermaLink="false">msg_51710</guid>
<description>&lt;p&gt;Ведение реестров&lt;/p&gt;Право обмена цифровых валют.</description>
<pubDate>Mon, 19 Oct 2026 18:43:12 +0300</pubDate>
</item></channel></rss>'''.encode()


class CbrDecisionsTests(unittest.TestCase):
    def test_opaque_guid_becomes_individual_document_with_full_text_and_date(self):
        with patch('newsroom.core._request', return_value=PAYLOAD):
            items = fetch_rss(FEED)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['url'], 'https://www.cbr.ru/rbr/rbr_fr/doc?id=51710')
        self.assertIn('Право обмена цифровых валют.', items[0]['content'])
        self.assertEqual(items[0]['published_at'], '2026-10-19T15:43:12+00:00')

    def test_full_official_feed_text_is_already_read_before_editorial_capture(self):
        with patch('newsroom.core._request', return_value=PAYLOAD):
            item = fetch_rss(FEED)[0]
        self.assertTrue(item['material_read'])
        self.assertEqual(item['primary_source_status'], 'READ')
        self.assertEqual(item['primary_source_content'], item['content'])
        self.assertEqual(item['description'], item['content'])
        self.assertNotIn('<p>', item['description'])

    def test_explicit_link_is_preserved(self):
        payload = PAYLOAD.replace(b'<guid', b'<link>https://www.cbr.ru/example</link><guid')
        with patch('newsroom.core._request', return_value=payload):
            self.assertEqual(fetch_rss(FEED)[0]['url'], 'https://www.cbr.ru/example')

    def test_unknown_decision_guid_is_not_used_as_url(self):
        with patch('newsroom.core._request', return_value=PAYLOAD.replace(b'msg_51710', b'unknown')):
            self.assertEqual(fetch_rss(FEED), [])

    def test_other_feeds_keep_existing_guid_handling(self):
        with patch('newsroom.core._request', return_value=PAYLOAD):
            self.assertEqual(fetch_rss('https://example.org/rss/rbr')[0]['url'], 'msg_51710')

    def config(self, enabled=True, extra_rows=(), sources=()):
        config = {'sources': list(sources)}
        rows = [dict(name='Банк России', url=PARENT, section='Регуляторы',
                     enabled=enabled, url_allowed=True), *extra_rows]
        apply_snapshot(config, {'rows': rows, 'schema_version': 2})
        return config

    def test_parent_registry_source_includes_decisions_and_preserves_trust(self):
        config = self.config(sources=[dict(url=PARENT, type='rss', reputation='primary_source')])
        child = next(s for s in config['sources'] if s['url'] == FEED)
        self.assertTrue(child['active'])
        self.assertEqual(child['reputation'], 'primary_source')
        self.assertEqual(child['type'], 'rss')

    def test_disabled_or_removed_parent_does_not_enable_decisions(self):
        config = self.config(enabled=False)
        self.assertFalse(next(s for s in config['sources'] if s['url'] == FEED)['active'])
        apply_snapshot(config, {'rows': [], 'schema_version': 2})
        self.assertEqual(config['sources'], [])

    def test_explicit_disabled_decisions_are_respected_without_duplicates(self):
        row = dict(name='Решения ЦБ', url=FEED, enabled=False, url_allowed=True)
        config = self.config(extra_rows=[row])
        children = [s for s in config['sources'] if s['url'] == FEED]
        self.assertEqual(len(children), 1)
        self.assertFalse(children[0]['active'])
        config = self.config(sources=[dict(url=FEED, type='rss', active=False)])
        self.assertFalse(next(s for s in config['sources'] if s['url'] == FEED)['active'])
