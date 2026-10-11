from edition_helpers import EditionCase
from newsroom.edition import vacancies,store,semantic_filter

TEXT='Вакансия: Solidity разработчик. Компания Альфа. Москва. Удалённо. 200 000 рублей. Контакт @alpha.'
class VacancyTests(EditionCase):
    def entry(self,mid,**changes):
        card=dict(role='Solidity разработчик',company='Компания Альфа',region='Москва',work_format='Удалённо',salary='200 000 рублей',contact='@alpha',excerpt=TEXT,topic='profile',geography='rf')
        card.update(changes)
        return dict(material_id=mid,content_kind='vacancy',vacancies=[card],decision='allow',reason='Объявление',evidence=[])
    def vacancy_material(self,text=TEXT):
        mid=self.material(text=str(self.db.execute('SELECT COUNT(*) FROM items').fetchone()[0])+'\n'+text,title='Вакансия');return store.materials(self.db,[mid])[0]
    def test_saved_separate_and_repeated_origins(self):
        a=self.vacancy_material();b=self.vacancy_material()
        self.assertIsNone(vacancies.route(self.db,a,self.entry(a['material_id'])))
        vacancies.route(self.db,b,self.entry(b['material_id']))
        rows=vacancies.inbox(self.db)['items'];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'SAVED');self.assertEqual(len(rows[0]['origins']),2)
    def test_geography_and_topic(self):
        for geography,topic,state in [('cis','profile','SAVED'),('foreign','profile','EXCLUDED'),('uncertain','profile','NEEDS_REVIEW'),('rf','nonprofile','EXCLUDED')]:
            m=self.vacancy_material();vacancies.route(self.db,m,self.entry(m['material_id'],geography=geography,topic=topic))
            self.assertTrue(any(c['state']==state for c in vacancies.inbox(self.db)['items']))
    def test_invented_salary_rejected(self):
        m=self.vacancy_material()
        with self.assertRaisesRegex(ValueError,'FIELD_EVIDENCE'):vacancies.route(self.db,m,self.entry(m['material_id'],salary='миллион'))
    def test_news_about_vacancies_remains_news(self):
        m=self.vacancy_material('Компания представила исследование: вакансии в крипто выросли.')
        self.assertEqual(vacancies.route(self.db,m,dict(content_kind='news',vacancies=[])),m)
    def test_unavailable_and_restart_hold(self):
        m=self.vacancy_material();job=self.job();self.semantic_mock.side_effect=RuntimeError('Unavailable')
        allowed,excluded=semantic_filter.screen(self.db,job,[m],self.settings)
        self.assertEqual(allowed,[]);self.assertEqual(excluded[-1]['code'],'vacancy_inbox');self.assertIsNone(vacancies.protect_recovery(self.db,m));self.assertEqual(len(vacancies.inbox(self.db)['pending']),1)
    def test_restart_before_screening(self):
        self.assertIsNone(vacancies.protect_recovery(self.db,self.vacancy_material()))
    def test_mixed_material_keeps_only_news(self):
        news='Компания Альфа запустила блокчейн для переводов.';m=self.vacancy_material(news+'\n'+TEXT);entry=self.entry(m['material_id']);entry.update(content_kind='mixed',evidence=[dict(material_id=m['material_id'],quote=news)])
        result=vacancies.route(self.db,m,entry);self.assertEqual(result['content'],news);self.assertEqual(vacancies.news_material(self.db,m),result)
    def test_job_evidence_cannot_be_used_as_news(self):
        m=self.vacancy_material();entry=self.entry(m['material_id']);entry.update(content_kind='mixed',evidence=[dict(material_id=m['material_id'],quote=TEXT)])
        self.assertIsNone(vacancies.route(self.db,m,entry))

    def test_unclassified_recruitment_not_used_as_context(self):
        self.assertIsNone(vacancies.context_material(self.db,self.vacancy_material()))

    def test_unmarked_recruitment_source_is_protected_on_outage(self):
        m=self.vacancy_material('Senior Solidity Engineer, remote');m['url']='https://t.me/workingincrypto/123'
        self.assertIsNone(vacancies.protect_recovery(self.db,m))

    def test_infrastructure_requirements_are_not_recruitment(self):
        for text in ['Когда требуется строительство новых генерирующих объектов.', 'Для майнинга требуется разрешение.', 'Требуются дополнительные инвестиции.']:
            self.assertFalse(vacancies.candidate({'content':text}))
        self.assertTrue(vacancies.candidate({'content':'Требуется разработчик Solidity в криптокомпанию.'}))
    def test_corrected_false_positive_returns_to_news_context(self):
        m=self.vacancy_material();vacancies.protect_recovery(self.db,m)
        self.db.execute("UPDATE edition_vacancy_routes SET kind='news' WHERE material_id=?",(m['material_id'],));self.db.commit()
        self.assertEqual(vacancies.news_material(self.db,m),m)
        self.assertEqual(vacancies.inbox(self.db)['pending'],[])

    def test_resume_pending_is_not_shown_as_vacancy(self):
        m=self.vacancy_material('#Резюме #OperationsManager #AML');m['title']='#Резюме #OperationsManager #AML'
        self.assertTrue(vacancies.is_resume(m))
        vacancies.route(self.db,m,{'decision':'uncertain','vacancies':[]})
        kind=self.db.execute('SELECT kind FROM edition_vacancy_routes WHERE material_id=?',(m['material_id'],)).fetchone()[0]
        self.assertEqual(kind,'resume');self.assertEqual(vacancies.inbox(self.db)['pending'],[])
