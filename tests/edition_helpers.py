import copy
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from newsroom.db import connect
from newsroom.core import NOW,_save_item
from newsroom.edition import store,model

HEADLINE='🏦 Компания Альфа запустила новый сервис переводов с комиссией 0,1% для клиентов банка'
LEAD='Компания Альфа запустила сервис переводов с комиссией 0,1%. Он доступен клиентам банка.'
BODY='Сервис работает в пяти сетях. Банк планирует расширить доступ в декабре.'
TEXT=LEAD+' '+BODY+' «Мы планируем расширить доступ» — Иван Иванов.'


def draft(material_id=1):
    evidence=[{'material_id':material_id,'quote':LEAD}]
    return {'headline':HEADLINE,'headline_evidence':copy.deepcopy(evidence),'lead':LEAD,'lead_evidence':copy.deepcopy(evidence),
            'blocks':[{'text':BODY,'kind':'paragraph','evidence':[{'material_id':material_id,'quote':BODY}],'quote_text':'','quote_author':''}]}


def group(material_id=1):
    return {'subject':'Компания Альфа','entities':['Компания Альфа'],'event_key':'Запуск сервиса переводов',
            'material_ids':[material_id],'focus':[{'material_id':material_id,'excerpt':LEAD,'summary':'Запуск сервиса переводов'}],'lookup_query':''}


def review_result(approved=True,issues=None):
    issues=issues or []
    codes={i['code'] for i in issues}
    if 'unsupported_fact' in codes:codes.add('facts')
    if 'main_conflict' in codes:codes.add('conflicts')
    return {'source_scope':'in_scope','approved':approved,'issues':issues,'assessments':{key:{'passed':key not in codes,'explanation':'Проверено на условном материале'} for key in model.REVIEW_RULES}}


def receipt():return {'response_id':'fake-test-response','model':'test','bundle_hash':model.bundle()[2]}


class EditionCase(TestCase):
    def setUp(self):
        # Preserve the legacy contracts as rollback tests. New release tests
        # explicitly enable the terminal path instead of changing these fixtures.
        self.real_bundle=model.bundle
        def legacy_bundle():
            policy,refs,digest=self.real_bundle()
            return {**policy,'short_release':False},refs,digest
        self.legacy_policy=patch.object(model,'bundle',side_effect=legacy_bundle)
        self.legacy_policy.start();self.addCleanup(self.legacy_policy.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.database=str(Path(self.tmp.name)/'news.sqlite3');self.db=connect(self.database);self.addCleanup(self.db.close)
        self.config={'newsroom':{'database':self.database},'editorial':{'enabled':True},'telegram':{'chat_id':'@test'},'ai':{}}
        store.initialize(self.db)
        self.source=self.db.execute("INSERT INTO sources(name,type,url) VALUES('Проверенный источник','rss','https://example.org/rss')").lastrowid
        self.db.commit()
        self.settings={'_edition_config':self.config,'_agent_control_config':self.config}
        self.semantic=patch.object(model,'semantic_screen',side_effect=lambda materials,settings: (
            {'decisions':[{'material_id':m['material_id'],'decision':'allow','reason':'Допустимая тестовая новость.','evidence':[]} for m in materials]},receipt()))
        self.semantic_mock=self.semantic.start();self.addCleanup(self.semantic.stop)

    def material(self,*,text=TEXT,title='Компания Альфа запустила сервис',queue=True,url=None):
        source=dict(self.db.execute('SELECT * FROM sources WHERE source_id=?',(self.source,)).fetchone())
        count=self.db.execute('SELECT COUNT(*) FROM items').fetchone()[0]
        item={'url':url or 'https://example.org/news/'+str(count+1),'title':title,'content':text,'description':text,'published_at':NOW()}
        item_id=_save_item(self.db,source,item);self.db.commit()
        material_id=store.capture(self.db,item_id,eligible=queue,queue=queue);self.db.commit()
        return material_id

    def job(self):
        # Most unit fixtures start at editorial admission. Dedicated recovery
        # tests use store.start directly to exercise interrupted screening.
        job=store.start(self.db,model.bundle()[2])
        if job:self.db.execute("UPDATE edition_jobs SET state='PLANNING' WHERE job_id=?",(job,));self.db.commit()
        return job
