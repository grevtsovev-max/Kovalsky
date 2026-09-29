import copy
import sys
import types
import unittest
from unittest.mock import patch
from newsroom.regulatory_reader import DocumentHTML, pdf_pages, attachment_url
from newsroom.regulatory_research import check_evidence, locate_evidence, validate
from newsroom.regulatory import canonical_document


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.sources={'D1':{'pages':['ПРОЕКТ\nНастоящее указание устанавливает состав информации.',
                                    '1.1.5. Сведения о факте совершения сделки: данные сторон.']}}
        self.e=dict(source='D1',page=2,point='1.1.5',quote='Сведения о факте совершения сделки:')

    def report(self):
        return dict(stage='PROJECT',stage_basis=[dict(source='D1',page=1,point='Титул',quote='ПРОЕКТ')],
                    steps=[dict(text='Указан состав сведений.',kind='FACT',evidence=[copy.deepcopy(self.e)])],
                    angles=[],draft_paragraphs=[],relations=[],deadlines=[],relevant=True,draft_title='')

    def test_quote_cannot_be_attributed_to_unread_source(self):
        with self.assertRaisesRegex(ValueError,'SOURCE_NOT_READ'):
            check_evidence([{**self.e,'source':'D2'}],self.sources)

    def test_paraphrase_is_not_a_quote(self):
        e={**self.e,'quote':'Каждый депозитарий обязан собирать IMEI'}
        with self.assertRaisesRegex(ValueError,'QUOTE_NOT_FOUND'):
            check_evidence([e],self.sources)

    def test_unique_quote_recovers_page_and_case_without_rewriting_claim(self):
        r=self.report();r['steps'][0]['evidence'][0].update(page=1,quote='сведения о факте совершения сделки:')
        locate_evidence(r,self.sources);validate(r,self.sources)
        self.assertEqual(r['steps'][0]['evidence'][0]['page'],2)
        self.assertTrue(r['evidence_locator_corrections'])

    def test_ambiguous_quote_does_not_guess_page(self):
        self.sources['D1']['pages'].append(self.sources['D1']['pages'][1])
        r=self.report();r['steps'][0]['evidence'][0]['page']=1
        locate_evidence(r,self.sources)
        with self.assertRaisesRegex(ValueError,'QUOTE_NOT_FOUND'):validate(r,self.sources)

    def test_unread_law_must_be_an_unresolved_relationship(self):
        r=self.report();r['relations']=[dict(from_source='D1',to_source='D2',relationship='IMPLEMENTS',reference='Закон',explanation='Связь',evidence=[self.e])]
        with self.assertRaisesRegex(ValueError,'TARGET_NOT_READ'):validate(r,self.sources)
        r['relations'][0].update(relationship='UNRESOLVED',to_source='')
        validate(r,self.sources)

    def test_all_147_pages_are_extracted_without_old_80_page_cap(self):
        fake=types.SimpleNamespace(PdfReader=lambda _:types.SimpleNamespace(pages=[types.SimpleNamespace(extract_text=lambda i=i:'Страница '+str(i)+' Текст нормативного документа.'*3) for i in range(147)]))
        with patch.dict(sys.modules,{'pypdf':fake}):
            pages,ocr=pdf_pages(b'pdf')
        self.assertEqual(len(pages),147);self.assertIn('146',pages[-1]);self.assertEqual(ocr,[])

    def test_cbr_heading_is_attached_to_file_link(self):
        p=DocumentHTML();p.feed('<nav>шум</nav><h4>Проект о цифровых валютах</h4><div><a href="/Queries/XsltBlock/File/1/2">Проект указания</a></div>')
        self.assertEqual(p.links[0]['heading'],'Проект о цифровых валютах')
        self.assertTrue(attachment_url(p.links[0]['href']));self.assertNotIn('шум',p.text)

    def test_canonical_alias_does_not_create_another_document(self):
        self.assertEqual(canonical_document('https://www.cbr.ru/Doc/1?ysclid=abc#page=1'),'https://cbr.ru/Doc/1')


if __name__=='__main__':unittest.main()
