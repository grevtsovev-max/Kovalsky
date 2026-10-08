import unittest

from newsroom.policy import absolute_narration_dates, relative_date_words, saved_date_context


class DateGateTests(unittest.TestCase):
    def test_source_footer_and_urls_are_not_calendar_narration(self):
        text = ('Вчера банк сообщил о выпуске.\n\n'
                'https://example.org/сегодня\n\n'
                'Источник: [Россия сегодня](https://example.org/вчера)')
        self.assertEqual(relative_date_words(text), ['Вчера'])
        result = absolute_narration_dates(text, {'source_calendar_day': '2026-01-01'})
        self.assertEqual(result, text.replace('Вчера банк', '31 декабря 2025 года банк'))
        self.assertEqual(relative_date_words(result), [])

    def test_nested_quotes_and_blockquotes_are_preserved(self):
        text = 'Вчера банк заявил: «Он сказал “Сегодня начинаем”».\n> Завтра откроем доступ.\nСегодня опубликован отчёт.'
        self.assertEqual(relative_date_words(text), ['Вчера', 'Сегодня'])
        result = absolute_narration_dates(text, {'source_calendar_day': '2026-01-01'})
        self.assertIn('«Он сказал “Сегодня начинаем”»', result)
        self.assertIn('> Завтра откроем доступ.', result)
        self.assertTrue(result.endswith('1 января 2026 года опубликован отчёт.'))

    def test_unclosed_quote_does_not_hide_narration(self):
        self.assertEqual(relative_date_words('Банк заявил: «Вчера началось обсуждение.'), ['Вчера'])

    def test_unknown_source_day_does_not_invent_a_date(self):
        text = 'Вчера банк получил разрешение.'
        self.assertEqual(absolute_narration_dates(text, {}), text)
        self.assertEqual(relative_date_words(text), ['Вчера'])

    def test_source_label_inside_body_does_not_hide_narration(self):
        self.assertEqual(relative_date_words('Источник: Сегодня банк сообщил.\nПродолжение.'), ['Сегодня'])

    def context(self):
        item = {'url': 'https://example.org/news', 'published_at': '2025-12-31T22:30:00+00:00',
                'source_timezone': 'Europe/Moscow', 'discovered_at': '2026-01-05T12:00:00+00:00'}
        citation = {'url': item['url'], 'content': 'Сегодня банк сообщил о выпуске.'}
        return {}, item, citation, {'timezone': 'Europe/Moscow'}

    def test_source_clock_survives_retry_and_returned_mutation(self):
        context, item, citation, source = self.context()
        first = saved_date_context(context, item, citation, source)
        self.assertEqual(first['source_calendar_day'], '2026-01-01')
        first['source_calendar_day'] = '2099-01-01'
        item['discovered_at'] = '2026-01-10T12:00:00+00:00'
        second = saved_date_context(context, item, citation, source)
        self.assertEqual(second['source_calendar_day'], '2026-01-01')
        self.assertEqual(second['discovered_at'], '2026-01-05T12:00:00+00:00')

    def test_changed_evidence_invalidates_saved_clock(self):
        for changed in ({'content': 'Новая версия.'}, {'published_at': '2026-02-02'},
                        {'source_timezone': 'America/New_York'}, {'url': 'https://other.example/news'}):
            with self.subTest(changed=changed):
                context, item, citation, source = self.context()
                saved_date_context(context, item, citation, source)
                binding = context['_date_context_binding']
                citation.update(changed)
                result = saved_date_context(context, item, citation, source)
                self.assertNotEqual(context['_date_context_binding'], binding)
                if 'published_at' in changed:
                    self.assertEqual(result['source_calendar_day'], '2026-02-02')
                elif 'source_timezone' in changed:
                    self.assertEqual(result['source_calendar_day'], '2025-12-31')
                elif 'url' in changed:
                    self.assertIsNone(result['source_calendar_day'])

    def test_updated_at_is_not_publication_date(self):
        context, item, citation, source = self.context()
        citation.update(url='https://primary.example/news', updated_at='2026-03-03')
        self.assertIsNone(saved_date_context(context, item, citation, source)['source_calendar_day'])

