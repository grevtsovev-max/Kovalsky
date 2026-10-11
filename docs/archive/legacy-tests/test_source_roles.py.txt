import unittest
from unittest.mock import patch
from newsroom.core import _read_telegram_primary, require_primary_source_review
from newsroom.ai import FILTER_VERSION

class SourceRoleTests(unittest.TestCase):
    text = "Мы провели заседание участников российского крипторынка и обсудили новые требования к цифровым активам. Итоги встречи публикуем от имени организатора."

    def source(self, role):
        return dict(url="https://t.me/organizer",name="Организатор",type="telegram",reputation="secondary_media",priority=1,source_role=role)

    def item(self, **changes):
        return dict(dict(url="https://t.me/organizer/1",title="Собственное заявление",content=self.text,discovery_links=[],telegram_forwarded=False),**changes)

    def primary(self, role="participant"):
        item=self.item();_read_telegram_primary(item,self.source(role))
        return dict(url=item["primary_source_url"],content=item["primary_source_content"],type=item["primary_source_type"])

    def result(self, supported=True, attribution=True, evidence=None):
        return dict(action="NEW_STORY",publication_recommendation="SEND_TO_REVIEW",facts=[dict(claim_type="CLAIM",text="Заявление организатора")],original_reporting_check=dict(central_claim_supported=supported,attribution_preserved=attribution,evidence=evidence or self.text))

    def test_roles_make_read_candidates_not_automatic_approval(self):
        for role in ("participant","publisher","expert"):
            p=self.primary(role)
            self.assertEqual(p['type'],'ORIGINAL_SOCIAL_'+role.upper())
            self.assertEqual(require_primary_source_review(self.result(),"READ",p)['publication_recommendation'],'SEND_TO_REVIEW')
            self.assertEqual(require_primary_source_review(dict(action='NEW_STORY'),"READ",p)['publication_recommendation'],'WAIT_FOR_AUTOMATION')

    def test_aggregate_priority_does_not_replace_origin(self):
        for role in ("aggregator","discovery"):
            source=self.source(role);source['priority']=3
            item=self.item();_read_telegram_primary(item,source)
            self.assertEqual(item['primary_source_status'],'NO_LINK')

    def test_forwarded_short_and_other_channel_not_promoted(self):
        for changes in ({'telegram_forwarded':True},{'content':'Short'},{'url':'https://t.me/another/1'}):
            item=self.item(**changes);_read_telegram_primary(item,self.source('participant'))
            self.assertEqual(item['primary_source_status'],'NO_LINK')

    def test_unread_exact_message_is_not_promoted_from_summary(self):
        item=self.item();del item['discovery_links']
        with patch('newsroom.core._request_with_url',side_effect=TimeoutError):
            _read_telegram_primary(item,self.source('publisher'))
        self.assertEqual(item['primary_source_status'],'ARTICLE_UNREADABLE')

    def test_reprint_unattributed_and_fabricated_evidence_wait(self):
        for result in (self.result(supported=False),self.result(attribution=False),self.result(evidence='Эта выдуманная цитата не содержится в исходном тексте.')):
            self.assertEqual(require_primary_source_review(result,'READ',self.primary())['publication_recommendation'],'WAIT_FOR_AUTOMATION')

    def test_claim_cannot_be_upgraded_to_verified_fact(self):
        result=self.result();result['facts']=[{'claim_type':'FACT'}]
        self.assertEqual(require_primary_source_review(result,'READ',self.primary())['publication_recommendation'],'WAIT_FOR_AUTOMATION')

    def test_unread_document_still_allows_audited_own_claim_only(self):
        item=self.item(discovery_links=[{'href':'https://cbr.ru/doc','text':'официальный документ'}])
        with patch('newsroom.core.fetch_publisher_article',side_effect=TimeoutError):
            _read_telegram_primary(item,self.source('participant'))
        self.assertEqual(item['primary_source_type'],'ORIGINAL_SOCIAL_PARTICIPANT')
        primary={'url':item['primary_source_url'],'content':item['primary_source_content'],'type':item['primary_source_type']}
        self.assertEqual(require_primary_source_review(self.result(supported=False),'READ',primary)['publication_recommendation'],'WAIT_FOR_AUTOMATION')


class ServiceDocumentTests(unittest.TestCase):
    def test_cookie_policy_in_script_is_not_article_source(self):
        from newsroom.core import PublisherArticleParser, _embedded_document_candidates
        parser=PublisherArticleParser()
        parser.feed('<script>const consent=\'<a href="/pdf/politica.pdf">Политика конфиденциальности</a>\';</script>')
        self.assertEqual(_embedded_document_candidates(parser,'https://procfa.ru/story'),[])

    def test_real_embedded_report_is_kept(self):
        from newsroom.core import PublisherArticleParser, _embedded_document_candidates
        parser=PublisherArticleParser()
        parser.feed('<script>const report="/pdf/report.pdf";</script>')
        self.assertEqual(_embedded_document_candidates(parser,'https://example.org/story')[0]['url'],'https://example.org/pdf/report.pdf')
