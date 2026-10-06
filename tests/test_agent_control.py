import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsroom.agent_control import AgentDisabled, enabled, set_enabled
from newsroom import cli
from newsroom.ai import request_response
from newsroom.delivery import deliver, SCHEMA, DeliveryRejected


class AgentControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'newsroom': {'database': str(Path(self.temp.name) / 'state.sqlite3'),
                                    'auto_publish_since': '2026-01-01T00:00:00+00:00'},
                       'telegram': {'chat_id': 'test'}}

    def test_stop_survives_new_config_and_only_explicit_enable_removes_it(self):
        set_enabled(self.config, False)
        reloaded = json.loads(json.dumps(self.config))
        self.assertFalse(enabled(reloaded))
        set_enabled(reloaded, False)
        self.assertFalse(enabled(self.config))
        set_enabled(reloaded, True)
        self.assertTrue(enabled(self.config))

    def test_runtime_blocks_request_even_if_caller_drops_control_settings(self):
        from newsroom.runtime import Runtime
        runtime = Runtime(self.config['newsroom']['database'], {})
        set_enabled(self.config, False)
        with self.assertRaises(AgentDisabled):
            runtime.reserve({}, {})

    def test_config_false_is_not_overridden_on_reload(self):
        path = Path(self.temp.name) / 'config.toml'
        path.write_text('[newsroom]\nauto_publish = false\n')
        with patch('newsroom.runtime.attach'):
            self.assertFalse(cli.load_config(str(path))['newsroom']['auto_publish'])

    def test_disabled_entry_points_do_not_start_work_or_network(self):
        set_enabled(self.config, False)
        from newsroom.core import run_cycle
        from newsroom.manual_intake import submit_article_url
        from newsroom.review import run_review_bot, process_feedback_corrections
        with patch('urllib.request.urlopen') as network, patch('newsroom.cli.run_cycle') as cycle:
            for work in [lambda: cli.run_one_cycle(self.config, self.config['newsroom']['database']),
                         lambda: run_cycle(self.config),
                         lambda: submit_article_url(self.config, 'https://example.org/article'),
                         lambda: run_review_bot(self.config),
                         lambda: process_feedback_corrections(self.config, None),
                         lambda: cli._telegram_api(self.config, 'editMessageText', {}),
                         lambda: request_response({}, {'_agent_control_config': self.config})]:
                with self.assertRaises(AgentDisabled):
                    work()
            self.assertEqual(cli.auto_publish_since('unused', self.config), (0, 0, 0))
            self.assertEqual(cli._publish_digest(None, self.config, 'daily'), (False, 0))
            network.assert_not_called()
            cycle.assert_not_called()

    def test_delivery_blocks_custom_transport_before_intent(self):
        set_enabled(self.config, False)
        with patch('newsroom.delivery.channel') as destination:
            with self.assertRaises(AgentDisabled):
                deliver(None, self.config, 'post:1', 'text', lambda *a: self.fail('sent'))
            destination.assert_not_called()

    def test_stop_during_delivery_preparation_prevents_send_and_is_known_failure(self):
        from newsroom.delivery import event as record_event
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        db.executescript('CREATE TABLE posts(post_id INTEGER PRIMARY KEY);' + SCHEMA)
        self.addCleanup(db.close)
        def stop_after_reservation(*args, **kwargs):
            record_event(*args, **kwargs)
            if args[2] == 'SENDING':
                set_enabled(self.config, False)
        with patch('newsroom.delivery.event', side_effect=stop_after_reservation), patch('newsroom.cli.telegram_send') as send:
            with self.assertRaises(DeliveryRejected):
                deliver(db, self.config, 'post:1', 'text', send)
            send.assert_not_called()
            self.assertEqual(db.execute('SELECT status FROM publication_attempts').fetchone()[0], 'FAILED')

    def test_false_autopublish_blocks_both_news_and_digest(self):
        self.config['newsroom']['auto_publish'] = False
        self.assertEqual(cli.auto_publish_since('unused', self.config), (0, 0, 0))
        self.assertEqual(cli._publish_digest(None, self.config, 'weekly'), (False, 0))


class DeploymentStopTests(unittest.TestCase):
    def test_deployment_keeps_stopped_services_stopped(self):
        import subprocess
        script = (Path(__file__).resolve().parents[1] / 'ops' / 'deploy.sh').read_text()
        activate = script[script.index('newsroom_active=false'):script.index('if ! activate;')]
        for running, condition in [('false', 'no'), ('false', 'yes'), ('true', 'yes')]:
            with self.subTest(running=running, condition=condition):
                fake = """systemctl() {
 case "$1" in
 is-active) case "$*" in *dashboard*) return 0;; *) RUNNING;; esac;;
 show) echo CONDITION;;
 try-restart|restart) echo "$*";;
 esac
}
""".replace('RUNNING', 'true' if running == 'true' else 'false').replace('CONDITION', condition)
                result = subprocess.run(['sh', '-c', fake + activate + '\nactivate'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('\nrestart kovalsky-newsroom', '\n' + result.stdout)
                self.assertNotIn('\nrestart kovalsky-review', '\n' + result.stdout)
                self.assertIn('try-restart kovalsky-newsroom', result.stdout)
