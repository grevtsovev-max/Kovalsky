import unittest
from newsroom.vacancy_fields import extract
class VacancyFieldTests(unittest.TestCase):
    def test_clean_fields_from_source_not_hashtag_title(self):
        d=extract('#Вакансия #BingX #Remote\nТребуется Business Development Manager\nО компании\nBingX — биржа.\nЛокация: Москва\nЗарплата: 200 000 ₽ + бонус\nКонтакты: @hr')
        self.assertEqual(d['role'],'Business Development Manager')
        self.assertEqual(d['company'],'BingX')
        self.assertEqual(d['salary'],'200 000 ₽ + бонус')
        self.assertEqual(d['location'],'Москва')
    def test_channel_and_role_hashtags_do_not_become_employer_or_role(self):
        d=extract('#CryptoJobs #Solidity #Remote\nДругие наши каналы: Crypto Jobs')
        self.assertEqual(d['company'],'');self.assertEqual(d['role'],'')
    def test_explicit_contact_can_identify_anonymous_hirer(self):
        d=extract('Требуется Менеджер\nКонтакты: @team_hr\nОклад 70 000 ₽ + бонусы\nУдалённо')
        self.assertEqual(d['hiring_party'],'@team_hr');self.assertEqual(d['location'],'Удалённо')
        self.assertEqual(d['salary'],'70 000 ₽ + бонусы')
    def test_missing_fields_remain_missing(self):
        d=extract('Требуется Юрист')
        self.assertEqual(d['salary'],'');self.assertEqual(d['location'],'')
