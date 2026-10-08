import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from newsroom import google_sheets_auth as auth
from newsroom import topic_registry as topics
from newsroom.db import connect
from newsroom.source_registry import save, state


class GoogleSheetsAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = subprocess.run(['/usr/bin/openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048'],capture_output=True,check=True).stdout.decode()

    def setUp(self):
        csv_read = patch('newsroom.core._request_with_url', return_value=('Тема,Слово или фраза,Мониторинг\n'.encode(), {}, ''))
        csv_read.start(); self.addCleanup(csv_read.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=connect(str(Path(self.tmp.name)/'state.db'));self.addCleanup(self.db.close)
        self.creds={'type':'service_account','client_email':'kovalsky@sample-project.iam.gserviceaccount.com',
                    'private_key':self.key,'private_key_id':'a'*40,'token_uri':auth.TOKEN_URL}
        self.settings={'spreadsheet_id':'x'*25,'tabs':[{'name':'Ключевые слова','gid':'2001'}]}
        save(self.db,topics.SETTINGS,self.settings);self.db.commit()

    def test_flat_header_permission_probe_preserves_first_column(self):
        with patch('newsroom.core._request_with_url', return_value=('Ключевик,Мониторинг,Роль,Уточнение\n'.encode(), {}, '')):
            probe=topics.keyword_header_probe(self.settings)
        self.assertEqual(probe['find'],'Ключевик')
        self.assertEqual(probe['replacement'],'Ключевик')
        self.assertEqual(probe['range']['startColumnIndex'],0)

    def test_rejects_other_credential_types_and_arbitrary_token_server(self):
        for data in [dict(self.creds,type='authorized_user'),dict(self.creds,token_uri='https://evil.example/token'),dict(self.creds,client_email='personal@example.com')]:
            with self.assertRaises(ValueError):auth.validate_credentials(data)

    def test_private_file_is_outside_database_and_has_owner_only_permissions(self):
        path=Path(auth.store_credentials(self.db,self.creds))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertEqual(auth.read_credentials(path)['client_email'],self.creds['client_email'])
        self.assertNotIn(self.key,self.db.execute('SELECT group_concat(value) FROM app_state').fetchone()[0])
        path.chmod(0o644)
        with self.assertRaises(ValueError):auth.read_credentials(path)

    def test_symlink_destination_is_rejected(self):
        target=Path(self.tmp.name)/'target';target.write_text('keep')
        auth.credential_path(self.db).symlink_to(target)
        with self.assertRaises(ValueError):auth.store_credentials(self.db,self.creds)
        self.assertEqual(target.read_text(),'keep')

    def test_signed_jwt_uses_only_sheet_scope_and_has_a_valid_rsa_signature(self):
        requests=[]
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return b'{"access_token":"test-token","token_type":"Bearer"}'
        with patch('newsroom.google_sheets_auth.urllib.request.urlopen',side_effect=lambda req,**kw:(requests.append(req) or Reply())):
            self.assertEqual(auth.access_token(self.creds),'test-token')
        from urllib.parse import parse_qs
        assertion=parse_qs(requests[0].data.decode())['assertion'][0]
        header,payload,signature=assertion.split('.')
        claims=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        self.assertEqual(claims['scope'],auth.SCOPE)
        self.assertEqual(claims['aud'],auth.TOKEN_URL)
        self.assertEqual(claims['exp']-claims['iat'],3600)
        self.assertNotIn('sub',claims)
        public=subprocess.run(['/usr/bin/openssl','pkey','-pubout'],input=self.key.encode(),capture_output=True,check=True).stdout
        pub=Path(self.tmp.name)/'public.pem';pub.write_bytes(public)
        sig=Path(self.tmp.name)/'signature';sig.write_bytes(base64.urlsafe_b64decode(signature+'='*(-len(signature)%4)))
        verified=subprocess.run(['/usr/bin/openssl','dgst','-sha256','-verify',str(pub),'-signature',str(sig)],input=(header+'.'+payload).encode(),capture_output=True)
        self.assertEqual(verified.returncode,0)

    def test_failed_google_permission_check_does_not_activate_or_store_key(self):
        with patch.object(auth,'access_token',return_value='token'),patch('newsroom.topic_registry.api',side_effect=PermissionError):
            with self.assertRaises(PermissionError):auth.configure(self.db,self.creds)
        self.assertEqual(state(self.db,topics.SETTINGS),self.settings)
        self.assertFalse(auth.credential_path(self.db).exists())

    def test_google_permission_check_preserves_header_and_activates_private_file(self):
        with patch.object(auth,'access_token',return_value='token'),patch('newsroom.topic_registry.api',return_value={}) as api:
            result=auth.configure(self.db,self.creds)
        self.assertTrue(result['writing_verified'])
        request=api.call_args.args[3]['requests'][0]['findReplace']
        self.assertEqual(request['find'],request['replacement'])
        self.assertEqual(request['range']['startRowIndex'],0)
        saved=state(self.db,topics.SETTINGS)
        self.assertTrue(topics.credentials_available(saved))
        self.assertNotIn('private_key',saved)
        self.assertNotIn('_access_token',saved)

    def test_existing_env_oauth_still_works(self):
        with patch.dict(os.environ,{'GOOGLE_SHEETS_ACCESS_TOKEN':'test'}):self.assertTrue(topics.credentials_available({}))
