import re
from unittest.mock import patch
from tests.edition_helpers import EditionCase, receipt
from newsroom import retrospective as audit
from newsroom.edition import model


class RetrospectiveTests(EditionCase):
    def test_includes_rejected_and_pending_without_mutating(self):
        material=self.material()
        item_id=self.db.execute('SELECT item_id FROM edition_materials WHERE material_id=?',(material,)).fetchone()[0]
        self.db.execute("UPDATE items SET disposition='NOISE' WHERE item_id=?",(item_id,));self.db.commit()
        before=self.db.execute('SELECT disposition FROM items').fetchone()[0]
        report=audit.run(self.db,self.config,evaluate=False)
        self.assertEqual(report['summary']['total'],1)
        self.assertEqual(report['materials'][0]['state'],'closed')
        self.assertEqual(self.db.execute('SELECT disposition FROM items').fetchone()[0],before)
        self.assertEqual(audit.latest(self.db)['report']['report_id'],report['report_id'])
        with self.assertRaises(Exception):self.db.execute('DELETE FROM retrospective_reports')

    def test_evidence_and_final_state_required(self):
        record={'item':{'content':'Подтверждённый факт'},'documents':[],'filter_history':[], 'posts':[], 'state':'closed'}
        value={'verdict':'wrong_rejection','rule_id':'scope','source_quote':'Подтверждённый факт'}
        self.assertEqual(audit.validate(value,record,{'scope'}),value)
        record['state']='pending'
        with self.assertRaises(ValueError):audit.validate(value,record,{'scope'})
        record['state']='closed';value['source_quote']='выдумка'
        with self.assertRaises(ValueError):audit.validate(value,record,{'scope'})

    def test_invalid_model_evidence_is_uncertain(self):
        self.material()
        value={'verdict':'wrong_rejection','rule_id':'scope','source_quote':'выдумка','reason':'ошибка'}
        with patch.object(model,'ask',return_value=(value,receipt())):
            report=audit.run(self.db,self.config)
        self.assertEqual(report['summary']['verdicts'],{'uncertain':1})

    def test_owner_stop_prevents_model_calls(self):
        self.config['newsroom']['agent_enabled']=False
        with patch.object(model,'ask') as ask:
            with self.assertRaises(Exception):audit.run(self.db,self.config)
            ask.assert_not_called()

    def test_period_validation(self):
        with self.assertRaises(ValueError):audit.collect(self.db,0)

    def test_publication_path_and_latency(self):
        import json
        from newsroom.edition import store
        material=self.material()
        job=store.start(self.db,model.bundle()[2])
        item_id=self.db.execute('SELECT item_id FROM edition_materials WHERE material_id=?',(material,)).fetchone()[0]
        now=store.stamp()
        story=self.db.execute("INSERT INTO stories(canonical_topic,headline,first_seen_at,last_updated_at) VALUES('test','test',?,?)",(now,now)).lastrowid
        post=self.db.execute("INSERT INTO posts(story_id,origin_item_id,text,status,created_at,published_at,version,post_hash) VALUES(?,?,?,'PUBLISHED',?,?,1,'test')",(story,item_id,'Опубликованный текст',now,now)).lastrowid
        self.db.execute("INSERT INTO edition_documents(document_id,job_id,group_json,materials_json,state,post_id,created_at,updated_at) VALUES('doc',?,'{}',?,'PUBLISHED',?,?,?)",(job,json.dumps(store.materials(self.db,[material]),ensure_ascii=False),post,now,now));self.db.commit()
        record=audit.collect(self.db)['materials'][0]
        self.assertEqual(record['state'],'published')
        self.assertIsNotNone(record['received_to_channel_seconds'])
        self.assertEqual(record['posts'][0]['post_id'],post)
        self.assertTrue(record['events'])
        self.db.execute("UPDATE edition_documents SET state='UNKNOWN' WHERE document_id='doc'");self.db.commit()
        self.assertEqual(audit.collect(self.db)['materials'][0]['state'],'unknown')

    def test_model_failure_preserves_report(self):
        from newsroom.ai import AIResponseError
        self.material()
        with patch.object(model,'ask',side_effect=AIResponseError('CREDENTIALS_MISSING')):
            report=audit.run(self.db,self.config)
        self.assertEqual(report['status'],'PARTIAL')
        self.assertEqual(audit.latest(self.db)['progress']['status'],'PARTIAL')
        self.assertEqual(report['summary']['total'],1)

    def test_report_api_and_page(self):
        import json, threading, urllib.request
        from http.server import ThreadingHTTPServer
        from newsroom import cabinet_server
        self.material();audit.run(self.db,self.config,evaluate=False)
        ready=threading.Event();servers=[]
        def create(address,handler):
            server=ThreadingHTTPServer(('127.0.0.1',0),handler)
            servers.append(server);ready.set();return server
        with patch.object(cabinet_server,'ThreadingHTTPServer',side_effect=create):
            thread=threading.Thread(target=cabinet_server.serve,args=(self.config,),daemon=True);thread.start()
            self.assertTrue(ready.wait(3));server=servers[0]
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                with urllib.request.urlopen(base+'/?view=retrospective',timeout=3) as response:
                    page=response.read().decode()
                self.assertIn('view-retrospective',page)
                token=re.search(r"const TOKEN='([^']+)'",page).group(1)
                with urllib.request.urlopen(urllib.request.Request(base+'/api/retrospective',headers={'X-Dashboard-Token':token}),timeout=3) as response:
                    payload=json.load(response)
                self.assertEqual(payload['report']['summary']['total'],1)
            finally:server.shutdown();server.server_close();thread.join(3)

    def test_concurrent_runs_do_not_start(self):
        from newsroom.locking import acquire_named_lock
        lock=acquire_named_lock(self.database,'retrospective')
        try:
            with self.assertRaises(ValueError):audit.run(self.db,self.config,evaluate=False)
        finally:lock.close()

    def test_start_api_and_invalid_period_without_model(self):
        import json,re,threading,urllib.request,urllib.error
        from http.server import ThreadingHTTPServer
        from newsroom import cabinet_server
        ready=threading.Event();called=threading.Event();servers=[]
        def create(address,handler):
            server=ThreadingHTTPServer(('127.0.0.1',0),handler)
            servers.append(server);ready.set();return server
        def fake_run(*args,**kwargs):called.set()
        with patch.object(cabinet_server,'ThreadingHTTPServer',side_effect=create), patch('newsroom.cli.load_config',return_value=self.config), patch.object(audit,'run',side_effect=fake_run):
            thread=threading.Thread(target=cabinet_server.serve,args=(self.config,),daemon=True);thread.start()
            self.assertTrue(ready.wait(3));server=servers[0]
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                page=urllib.request.urlopen(base+'/?view=retrospective',timeout=3).read().decode()
                token=re.search(r"const TOKEN='([^']+)'",page).group(1)
                def request(hours):
                    return urllib.request.Request(base+'/api/retrospective',data=json.dumps({'hours':hours}).encode(),headers={'Content-Type':'application/json','X-Dashboard-Token':token},method='POST')
                with urllib.request.urlopen(request(24),timeout=3) as response:self.assertEqual(response.status,202)
                self.assertTrue(called.wait(3))
                with self.assertRaises(urllib.error.HTTPError) as raised:urllib.request.urlopen(request('invalid'),timeout=3)
                self.assertEqual(raised.exception.code,400)
                self.assertIn('Выберите период',json.load(raised.exception)['error'])
                self.config['newsroom']['agent_enabled']=False
                with self.assertRaises(urllib.error.HTTPError) as raised:urllib.request.urlopen(request(24),timeout=3)
                self.assertEqual(raised.exception.code,409)
                self.assertIn('отключён владельцем',json.load(raised.exception)['error'])
            finally:server.shutdown();server.server_close();thread.join(3)
