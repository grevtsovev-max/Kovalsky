import unittest
from datetime import datetime, timedelta, timezone
from newsroom.core import _development_date_issue


class EventActualityTests(unittest.TestCase):
    def check(self, day, quote, published=None):
        return _development_date_issue({'publication_recommendation': 'AUTO_PUBLISH',
            'development_date': day, 'development_date_evidence': quote},
            {'content': quote}, 48, published)

    def test_unknown_day_does_not_block_a_read_current_report(self):
        self.assertIsNone(self.check('', 'Банк сообщил о запуске сервиса.'))

    def test_relative_day_uses_publication_day_not_retry_day(self):
        now = datetime.now(timezone.utc)
        self.assertIsNone(self.check(now.date().isoformat(), 'Сегодня банк запустил сервис.', now.isoformat()))
        old = now - timedelta(days=10)
        self.assertEqual(self.check(old.date().isoformat(), 'Сегодня банк запустил сервис.', old.isoformat())[0], 'stale')

    def test_date_without_year_and_yesterday_are_resolved(self):
        now = datetime.now(timezone.utc)
        months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
        quote = f'{now.day} {months[now.month-1]} банк запустил сервис.'
        self.assertIsNone(self.check(now.date().isoformat(), quote, now.isoformat()))
        self.assertIsNone(self.check((now - timedelta(days=1)).date().isoformat(), 'Вчера банк запустил сервис.', now.isoformat()))

    def test_metadata_does_not_turn_explicit_old_event_into_new_event(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=400)
        quote = f'Банк запустил сервис {old.date().isoformat()}.'
        self.assertEqual(self.check(old.date().isoformat(), quote, now.isoformat())[0], 'stale')
        self.assertEqual(self.check(now.date().isoformat(), quote, now.isoformat())[0], 'unverified')

    def test_dateline_month_accepts_sentence_period_but_numeric_date_keeps_year_boundary(self):
        now = datetime.now(timezone.utc)
        months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
        quote = f'Москва. {now.day} {months[now.month-1]}. INTERFAX.RU - Банк внесён в реестр.'
        self.assertIsNone(self.check(now.date().isoformat(), quote, now.isoformat()))
        wrong_year = f"Банк внесён в реестр {now.strftime('%d.%m')}.1998."
        self.assertEqual(self.check(now.date().isoformat(), wrong_year, now.isoformat())[0], 'unverified')
