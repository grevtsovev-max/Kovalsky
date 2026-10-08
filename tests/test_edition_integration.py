import json
import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch,Mock
from newsroom import dashboard,core
from newsroom.edition import model,store
from newsroom.edition.worker import run_once
from newsroom.edition.views import details
from newsroom.delivery import TelegramReceipt
from edition_helpers import review_result,EditionCase,draft,group,receipt,TEXT


class IntegrationTests(EditionCase):
    def test_collection_first_filter_editor_checker_and_delivery_are_connected(self):
        self.config['sources']=[{'name':'Feed','type':'rss','url':'https://example.org/feed','active':True}]
        self.config['ai']={'_keyword_prefilter':{'keywords':['Компания'],'version':'test'},'_edition_enabled':True}
        item={'url':'https://example.org/article','title':'Компания Альфа запустила сервис','content':TEXT,'description':TEXT}
        with patch('newsroom.core.fetch_rss',return_value=[item]):core.run_cycle(self.config)
        source=self.db.execute("SELECT * FROM edition_materials WHERE queue_state='QUEUED'").fetchone()
        self.assertIsNotNone(source);mid=source['material_id']
        send=Mock(return_value=TelegramReceipt({'message_id':555}))
        with patch.object(model,'plan',return_value=({'groups':[group(mid)],'excluded':[]},receipt())),patch.object(model,'draft',return_value=(draft(mid),receipt())),patch.object(model,'check',return_value=(review_result(),receipt())):
            result=run_once(self.config,send=send)
        self.assertEqual(result['state'],'PUBLISHED');send.assert_called_once()
        self.assertEqual(self.db.execute('SELECT external_id FROM posts').fetchone()[0],'555')

    def test_filter_rejection_never_reaches_model_or_automatic_queue(self):
        self.config['sources']=[{'name':'Feed','type':'rss','url':'https://example.org/feed','active':True}]
        self.config['ai']={'_keyword_prefilter':{'keywords':['биткоин'],'version':'test'},'_edition_enabled':True}
        item={'url':'https://example.org/article','title':'Компания Альфа запустила сервис','content':TEXT,'description':TEXT}
        with patch('newsroom.core.fetch_rss',return_value=[item]):core.run_cycle(self.config)
        with patch.object(model,'plan') as planning:
            self.assertIsNone(run_once(self.config,publish=False));planning.assert_not_called()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM edition_materials WHERE queue_state='QUEUED'").fetchone()[0],0)

    def test_dashboard_editor_history_and_authenticated_retry(self):
        mid=self.material();job=self.job();store.finish(self.db,job,'INCOMPLETE',[{'reason':'Проверка не завершена'}])
        ready=threading.Event();servers=[]
        def server_factory(address,handler):
            server=ThreadingHTTPServer(('127.0.0.1',0),handler);servers.append(server);ready.set();return server
        from pathlib import Path
        config_path=Path(self.tmp.name)/'config.toml';config_path.write_text('[newsroom]\ndatabase = '+json.dumps(self.database)+'\n[editorial]\nenabled = true\n')
        with patch('newsroom.dashboard.ThreadingHTTPServer',side_effect=server_factory):
            thread=threading.Thread(target=dashboard.serve,args=(self.config,'127.0.0.1',8765,str(config_path)),daemon=True);thread.start()
            self.assertTrue(ready.wait(3));server=servers[0];base='http://127.0.0.1:'+str(server.server_port)
            try:
                page=urllib.request.urlopen(base).read().decode();self.assertIn('Повторить подготовку',page)
                token=re.search(r"const token='([^']+)'",page).group(1)
                with urllib.request.urlopen(base+'/api/edition') as response:data=json.load(response)
                self.assertEqual(data['references'],14);self.assertTrue(data['jobs'][0]['retryable'])
                with urllib.request.urlopen(base+'/api/edition/job?job_id='+job) as response:data=json.load(response)
                self.assertEqual(data['materials'][0]['material_id'],mid)
                payload=json.dumps({'job_id':job}).encode()
                request=urllib.request.Request(base+'/api/edition/retry',data=payload,headers={'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as denied:urllib.request.urlopen(request)
                self.assertEqual(denied.exception.code,403);denied.exception.close()
                request.add_header('X-Dashboard-Token',token)
                with urllib.request.urlopen(request) as response:data=json.load(response)
                self.assertEqual(data['state'],'QUEUED');self.assertNotEqual(data['job_id'],job)
            finally:server.shutdown();server.server_close();thread.join(3)
