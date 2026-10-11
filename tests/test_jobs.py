import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from newsroom.jobs import model,store,worker,view

TEXT='Вакансия: Solidity разработчик\nКомпания: Альфа\nМосква\nУдалённо\n200 000 рублей в месяц\nОтклик: @alpha'
def answer():
    values=dict(role='Solidity разработчик',company='Альфа',contact='@alpha',salary='200 000 рублей в месяц',location='Москва',work_format='Удалённо')
    return {'kind':'vacancy','cards':[{**{k:{'value':v,'evidence':v} for k,v in values.items()},'excerpt':TEXT}]}

class JobsTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE sources(source_id INTEGER,name TEXT);
        INSERT INTO sources VALUES(1,'Канал публикации');
        CREATE TABLE items(item_id INTEGER,source_id INTEGER,url TEXT,title TEXT,content TEXT,description TEXT,published_at TEXT,discovered_at TEXT,disposition TEXT);
        CREATE TABLE edition_jobs(job_id INTEGER,state TEXT);INSERT INTO edition_jobs VALUES(1,'SCREENING');
        CREATE TABLE posts(post_id INTEGER,text TEXT);INSERT INTO posts VALUES(1,'Опубликованный текст');''')
        self.config={'newsroom':{}}
        self.add(1,TEXT)
    def tearDown(self):self.db.close()
    def add(self,n,text):
        self.db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',(n,1,'https://t.me/workingincrypto/'+str(n),'Вакансия',text,'',None,store.stamp(),'EXCLUDED'));self.db.commit()
    def test_admission_independent_of_news_filter_and_idempotent(self):
        self.assertEqual(store.discover(self.db),1);self.assertEqual(store.discover(self.db),0)
        self.add(2,TEXT);self.assertEqual(store.discover(self.db),1)
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0],'EXCLUDED')
    def test_late_source_reading_creates_new_version(self):
        store.discover(self.db)
        self.db.execute("UPDATE items SET content=content||' Новый контакт' WHERE item_id=1");self.db.commit()
        self.assertEqual(store.discover(self.db),1)
        self.assertEqual(self.db.execute('SELECT state FROM recruitment_queue ORDER BY id').fetchone()[0],'SUPERSEDED')
        self.assertEqual(store.discover(self.db),0)
    def test_complete_path_saves_fields_without_touching_news(self):
        store.discover(self.db)
        with patch.object(model,'extract',return_value=(answer(),{'response_id':'test'})):
            worker.process_one(self.db,self.config)
        card=view.inbox(self.db)['items'][0]
        self.assertEqual(card['company'],'Альфа');self.assertEqual(card['location'],'Москва')
        self.assertEqual(view.inbox(self.db)['pending'],[])
        self.assertEqual(self.db.execute('SELECT state FROM edition_jobs').fetchone()[0],'SCREENING')
        self.assertEqual(self.db.execute('SELECT text FROM posts').fetchone()[0],'Опубликованный текст')
        self.assertFalse(worker.process_one(self.db,self.config))
    def test_bad_evidence_is_logged_and_retried_without_blocking_other_jobs(self):
        store.discover(self.db);bad=answer();bad['cards'][0]['salary']['value']='миллион рублей'
        with patch.object(model,'extract',return_value=(bad,{'response_id':'invalid'})):
            worker.process_one(self.db,self.config)
        self.assertEqual(self.db.execute('SELECT state FROM recruitment_queue').fetchone()[0],'WAITING')
        self.assertEqual(json.loads(self.db.execute('SELECT result_json FROM recruitment_attempts').fetchone()[0]),bad)
        self.add(2,TEXT);store.discover(self.db)
        with patch.object(model,'extract',return_value=(answer(),{})):worker.process_one(self.db,self.config)
        self.assertEqual(len(view.inbox(self.db)['items']),1)
    def test_resume_not_card_and_absent_fields_not_invented(self):
        store.discover(self.db)
        with patch.object(model,'extract',return_value=({'kind':'resume','cards':[]},{})):worker.process_one(self.db,self.config)
        self.assertEqual(view.inbox(self.db),{'items':[],'pending':[]})
        data=answer();data['cards'][0]['company']={'value':'','evidence':''}
        self.assertEqual(model.validate(data,{'content':TEXT}),data)
    def test_stitched_excerpt_uses_original_and_keeps_field_validation(self):
        data=answer();data['cards'][0]['excerpt']='Solidity разработчик\n200 000 рублей в месяц'
        prepared=model.prepare(data,{'content':TEXT})
        model.validate(prepared,{'content':TEXT})
        self.assertEqual(prepared['cards'][0]['excerpt'].strip(),TEXT)
        self.assertNotEqual(data['cards'][0]['excerpt'],TEXT)
        prepared['cards'][0]['salary']['value']='миллион'
        with self.assertRaises(ValueError):model.validate(prepared,{'content':TEXT})
    def test_generic_company_recruitment_is_not_a_vacancy(self):
        data=answer();data['cards'][0]['role']={'value':'','evidence':''}
        self.assertEqual(model.prepare(data,{'content':TEXT}),{'kind':'other','cards':[]})
    def test_pending_is_status_not_role(self):
        store.discover(self.db);pending=view.inbox(self.db)['pending'][0]
        self.assertEqual(pending['role'],'');self.assertEqual(pending['state'],'PENDING')
        self.assertEqual(pending['content'],TEXT)
    def test_multiple_contacts_are_grounded_individually(self):
        data=answer();text=TEXT+'\nКонтакт 2: @other'
        data['cards'][0]['contact']={'value':'@alpha; @other','evidence':'Отклик: @alpha; Контакт 2: @other'}
        model.validate(model.prepare(data,{'content':text}),{'content':text})
        data['cards'][0]['contact']['value']='@alpha; @invented'
        with self.assertRaises(ValueError):model.validate(model.prepare(data,{'content':text}),{'content':text})
    def test_capacity_delay_is_saved(self):
        from newsroom.runtime import BudgetDeferred
        store.discover(self.db)
        with patch.object(model,'extract',side_effect=BudgetDeferred('concurrency',1)):
            worker.process_one(self.db,self.config)
        row=self.db.execute('SELECT receipt_json FROM recruitment_attempts').fetchone()
        self.assertEqual(json.loads(row[0])['delay_seconds'],1)
    def test_owner_stop_prevents_ai(self):
        store.discover(self.db)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'stop';path.touch()
            with patch.object(model,'extract') as ai:
                with self.assertRaises(worker.AgentDisabled):worker.process_one(self.db,{'newsroom':{'agent_stop_file':str(path)}})
                ai.assert_not_called()
    def test_requested_batch_finishes_and_later_arrivals_wait(self):
        class Connection:
            def __getattr__(proxy,name):return getattr(self.db,name)
            def close(proxy):pass
        def reply(source,settings):
            if source['item_id']==1:self.add(2,TEXT)
            return answer(),{}
        with tempfile.TemporaryDirectory() as temp:
            config={'newsroom':{'database':str(Path(temp)/'jobs.db')}}
            with patch.object(worker,'connect',return_value=Connection()),patch.object(model,'extract',side_effect=reply) as ai:
                worker.run(config)
                self.assertEqual(ai.call_count,1)
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recruitment_queue').fetchone()[0],1)
                worker.run(config)
                self.assertEqual(ai.call_count,2)
    def test_reading_cards_never_calls_ai(self):
        store.discover(self.db)
        with patch.object(model,'extract') as ai:
            view.inbox(self.db);view.inbox(self.db)
            ai.assert_not_called()
    def test_no_editorial_delivery_or_collector_workflow_imports(self):
        for path in Path('newsroom/jobs').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom):
                    self.assertFalse(any(x in (node.module or '').split('.') for x in ('edition','delivery','workflow','cli')))
    def test_hashtag_title_readable_but_invented_company_rejected(self):
        data=answer();data['cards'][0]['role']={'value':'Operations Manager','evidence':'#OperationsManager'}
        data['cards'][0]['excerpt']=TEXT+'\n#OperationsManager'
        model.validate(data,{'content':data['cards'][0]['excerpt']})
        data['cards'][0]['company']={'value':'Другой работодатель','evidence':'Компания: Альфа'}
        with self.assertRaisesRegex(ValueError,'FIELD_VALUE'):model.validate(data,{'content':data['cards'][0]['excerpt']})

if __name__=='__main__':unittest.main()
