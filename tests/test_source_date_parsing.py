import unittest

from newsroom.core import parse_date


class SourceDateParsingTests(unittest.TestCase):
    def test_unknown_timezone_is_not_replaced_with_utc(self):
        for value in ('2026-10-09T12:00:00', 'Fri, 09 Oct 2026 12:00:00'):
            with self.subTest(value=value):
                self.assertIsNone(parse_date(value))

    def test_known_timezone_is_normalized_without_changing_instant(self):
        for value in ('2026-10-09T12:00:00+03:00', 'Fri, 09 Oct 2026 12:00:00 +0300'):
            with self.subTest(value=value):
                self.assertEqual(parse_date(value), '2026-10-09T09:00:00+00:00')
