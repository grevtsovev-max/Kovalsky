import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from newsroom import editorial_registry as board, topic_registry as topics
from newsroom.ai import _load_editorial_rules, analysis_input
from newsroom.db import connect
from newsroom.source_registry import save, state


def snapshot():
    return {'version':'v1','sections':{
        'Редакторские правила':[{'values':['Язык и тон','Пиши кратко','Образец','TRUE'],'enabled':True,'row':2},
                               {'values':['Язык и тон','Выключенное правило','','FALSE'],'enabled':False,'row':3}],
        'Примеры редактуры':[], 'История обучения':[]}}


def change(op='ADD'):
    return {'operation':op,'section':'Язык и тон','rule':'Убирай канцелярит','previous_rule':'Пиши кратко' if op!='ADD' else '',
            'example':'Компания запустила','evidence':'Убирай канцелярит'}


class EditorialRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db=connect(str(Path(self.temp.name)/'state.db')); self.addCleanup(self.db.close)
        self.parent={'spreadsheet_id':'x'*25,'url':'https://docs.google.com/spreadsheets/d/'+'x'*25+'/edit','tabs':[]}
        save(self.db,topics.SETTINGS,self.parent); save(self.db,board.SETTINGS,self.parent)
        save(self.db,board.SNAPSHOT,snapshot()); self.db.commit()
        self.rows={name:[headers.copy()] for name,headers in board.HEADERS.items()}
        self.rows['Редакторские правила'] += [['Язык и тон','Пиши кратко','Образец',True],['Язык и тон','Выключенное правило','',False]]
        self.writes=[]; self.fail_replace=False

    def api(self, settings, method, suffix, data=None):
        if suffix.startswith('?fields='):
            return {'sheets':[{'properties':{'title':name,'sheetId':i+1,'gridProperties':{'rowCount':100}}} for i,name in enumerate(board.HEADERS)]}
        if suffix.startswith('/values:'):
            return {'valueRanges':[{'values':[r.copy() for r in self.rows[name]]} for name in board.HEADERS]}
        self.writes.append(data)
        replies=[]
        for req in data['requests']:
            if 'insertDimension' in req:
                r=req['insertDimension']['range']; name=list(board.HEADERS)[r['sheetId']-1]
                self.rows[name].insert(r['startIndex'],[]); replies.append({})
            elif 'updateCells' in req:
                r=req['updateCells']['range']; name=list(board.HEADERS)[r['sheetId']-1]
                cells=req['updateCells']['rows'][0]['values']; vals=[next(iter(c['userEnteredValue'].values())) for c in cells]
                index=r['startRowIndex']; self.rows[name][index][r['startColumnIndex']:r['endColumnIndex']]=vals; replies.append({})
            else:
                r=req['findReplace']['range']; name=list(board.HEADERS)[r['sheetId']-1]; f=req['findReplace']
                matches=not self.fail_replace and self.rows[name][r['startRowIndex']][1]==f['find']
                if matches: self.rows[name][r['startRowIndex']][1]=f['replacement']
                replies.append({'findReplace':{'occurrencesChanged':int(matches)}})
        return {'replies':replies}

    def test_parser_disabled_rules_and_invalid_headers(self):
        out=io.StringIO(); w=csv.writer(out); w.writerow(board.HEADERS['Редакторские правила']); w.writerow(['Лид','Конкретное событие','','FALSE'])
        w.writerow(['','','','FALSE'])
        parsed=board.parse(out.getvalue().encode(),'Редакторские правила')
        self.assertEqual(len(parsed),1)
        self.assertFalse(parsed[0]['enabled'])
        with self.assertRaises(ValueError): board.parse(b'wrong,header','Редакторские правила')

    def test_policy_and_ai_prompt_ignore_disabled_rules_and_keep_guardrails(self):
        value=board.policy(snapshot())
        self.assertNotIn('Выключенное правило',json.dumps(value,ensure_ascii=False))
        prompt=_load_editorial_rules({'_editorial_registry':value})
        self.assertIn('Пиши кратко',prompt); self.assertIn('достоверность',prompt.casefold())
        self.assertIn('точную атрибуцию',prompt)
        source={'name':'Example','reputation':'unknown','priority':1}
        self.assertEqual(analysis_input({},source,[],{'_editorial_registry':value})['editorial_policy']['version'],'v1')

    def test_read_failure_preserves_last_good_copy(self):
        config={}
        with patch.object(board,'read_registry',side_effect=ValueError('changed header')):
            board.sync(self.db,config,force=True)
        self.assertEqual(config['ai']['_editorial_registry']['version'],'v1')
        self.assertEqual(state(self.db,board.SNAPSHOT)['version'],'v1')

    def test_writer_is_idempotent_and_preserves_manual_disabled_rules(self):
        job={'changes':[change()], 'signal':{'reason':'Убирай канцелярит','created_at':'2026-01-01'}}
        with patch.object(topics,'api',side_effect=self.api):
            board.write_job(self.db,'editorial_learning:1',job)
            count=len(self.writes)
            board.write_job(self.db,'editorial_learning:1',job)
        self.assertEqual(len(self.writes),count)
        self.assertFalse(self.rows['Редакторские правила'][2][3])
        self.assertEqual(self.rows['История обучения'][-1][4],'editorial_learning:1')

    def test_retry_after_applied_rule_but_lost_audit_does_not_duplicate_rule(self):
        job={'changes':[change('REPLACE')], 'signal':{'reason':'Убирай канцелярит'}}
        self.rows['Редакторские правила'][1][1]='Убирай канцелярит'
        with patch.object(topics,'api',side_effect=self.api): board.write_job(self.db,'editorial_learning:2',job)
        self.assertEqual(len(self.rows['Редакторские правила']),3)
        self.assertEqual(len(self.rows['История обучения']),2)

    def test_failed_conditional_replacement_never_records_success(self):
        self.fail_replace=True
        with patch.object(topics,'api',side_effect=self.api):
            with self.assertRaises(ValueError): board.write_job(self.db,'editorial_learning:3',{'changes':[change('REPLACE')],'signal':{'reason':'Убирай канцелярит'}})
        self.assertEqual(len(self.rows['История обучения']),1)

    def test_disabled_matching_rule_is_not_enabled_by_add(self):
        c=change(); c['rule']='Выключенное правило'
        with patch.object(topics,'api',side_effect=self.api): board.write_job(self.db,'editorial_learning:4',{'changes':[c],'signal':{'reason':c['rule']}})
        self.assertEqual(len(self.rows['Редакторские правила']),3)
        self.assertFalse(self.rows['Редакторские правила'][2][3])

    def test_old_script_blocks_learning_before_spending_ai_budget(self):
        self.db.execute("INSERT INTO editorial_feedback(created_at,feedback_type,reason) VALUES('2026-01-01','OTHER','Убирай канцелярит')"); self.db.commit()
        with patch.object(board,'probe_writer',return_value=False),patch.object(board,'plan') as planner:
            board.learn_cycle(self.db,{'ai':{}})
        planner.assert_not_called()
        self.assertEqual(state(self.db,'editorial_learning:1')['status'],'BLOCKED')

    def test_preliminary_agent_lesson_is_only_example_not_rule(self):
        self.db.execute("INSERT INTO editorial_feedback(created_at,feedback_type,reason) VALUES('2026-01-01','TELEGRAM_EDIT','Предварительный вывод')"); self.db.commit()
        with patch.object(topics,'credentials_available',return_value=True),patch.object(board,'probe_writer',return_value=True),patch.object(board,'plan') as planner,patch.object(board,'write_job') as writer,patch.object(board,'sync'):
            board.learn_cycle(self.db,{'ai':{}})
        planner.assert_not_called()
        self.assertEqual(writer.call_args.args[2]['changes'],[])
        self.assertEqual(state(self.db,'editorial_learning:1')['status'],'DONE')

    def test_learning_plan_requires_exact_owner_evidence(self):
        c=change(); c['evidence']='Цитата отсутствует'
        response={'output':[{'content':[{'type':'output_text','text':json.dumps({'changes':[c]})}]}]}
        with patch('newsroom.ai.request_response',return_value=response):
            with self.assertRaises(ValueError): board.plan({'reason':'Убирай канцелярит'},snapshot(),{})


if __name__=='__main__': unittest.main()
