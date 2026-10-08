"""Google service-account access; private keys remain outside source and SQLite."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/spreadsheets'


def validate_credentials(data):
    if not isinstance(data, dict) or data.get('type') != 'service_account':
        raise ValueError('Нужен JSON-ключ служебного аккаунта Google')
    email = data.get('client_email', '')
    key = data.get('private_key', '')
    if (not isinstance(email, str) or not re.fullmatch(r'[a-zA-Z0-9._-]+@[a-zA-Z0-9-]+\.iam\.gserviceaccount\.com', email)
            or not isinstance(key, str) or not 1000 <= len(key) <= 7000
            or not key.startswith('-----BEGIN PRIVATE KEY-----\n') or not key.rstrip().endswith('-----END PRIVATE KEY-----')
            or data.get('token_uri') != TOKEN_URL):
        raise ValueError('Некорректный ключ служебного аккаунта Google')
    key_id = data.get('private_key_id', '')
    if not isinstance(key_id, str) or not re.fullmatch(r'[a-fA-F0-9]{20,128}', key_id):
        raise ValueError('Некорректный идентификатор ключа Google')
    return {'type': 'service_account', 'client_email': email, 'private_key': key,
            'private_key_id': key_id, 'token_uri': TOKEN_URL}


def read_credentials(path):
    # Refuse symlinks and permissive files rather than risking reading another
    # process's secret. Error messages never include paths or key material.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_size > 16384):
            raise ValueError('GOOGLE_SHEETS_CREDENTIAL_FILE_PERMISSIONS')
        return validate_credentials(json.load(file))


def _encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')


def access_token(data):
    """RS256 signing is delegated to OpenSSL; endpoints/scopes are fixed."""
    data = validate_credentials(data)
    issued = int(time.time())
    message = b'.'.join([_encoded({'alg': 'RS256', 'typ': 'JWT', 'kid': data['private_key_id']}),
        _encoded({'iss': data['client_email'], 'scope': SCOPE, 'aud': TOKEN_URL,
                  'iat': issued, 'exp': issued + 3600})])
    # Pass the key via an inherited anonymous pipe, never argv or a disk file.
    key_fd, key_out = os.pipe()
    try:
        os.write(key_out, data['private_key'].encode())
        os.close(key_out); key_out = None
        signed = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', f'/proc/self/fd/{key_fd}'],
            input=message, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(key_fd,), timeout=10, check=False)
        if signed.returncode or not signed.stdout:
            raise ValueError('GOOGLE_SHEETS_KEY_SIGNATURE_FAILED')
    finally:
        os.close(key_fd)
        if key_out is not None: os.close(key_out)
    assertion = message + b'.' + base64.urlsafe_b64encode(signed.stdout).rstrip(b'=')
    form = urllib.parse.urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                                  'assertion': assertion.decode()}).encode()
    request = urllib.request.Request(TOKEN_URL, data=form, headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    token = result.get('access_token')
    if not isinstance(token, str) or not token or result.get('token_type', '').casefold() != 'bearer':
        raise ValueError('GOOGLE_SHEETS_TOKEN_RESPONSE_INVALID')
    return token


def credential_path(db):
    path = next((row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main'), '')
    if not path:
        raise ValueError('GOOGLE_SHEETS_PERSISTENT_STORAGE_REQUIRED')
    return Path(path).resolve().parent / 'google-sheets-service-account.json'


def store_credentials(db, data):
    data = validate_credentials(data)
    path = credential_path(db)
    if path.is_symlink():
        raise ValueError('GOOGLE_SHEETS_CREDENTIAL_PATH_UNSAFE')
    fd, staging = tempfile.mkstemp(prefix='.google-sheets-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(data, file)
            file.flush(); os.fsync(file.fileno())
        os.replace(staging, path)
    finally:
        if os.path.exists(staging): os.unlink(staging)
    return str(path)


def configure(db, data):
    from .source_registry import state, save
    from .topic_registry import SETTINGS, api
    settings = state(db, SETTINGS)
    if not settings:
        raise ValueError('Сначала подключите таблицу тем')
    data = validate_credentials(data)
    # Validate the real account and edit permission BEFORE activating credentials.
    token = access_token(data)
    temporary = {**settings, '_access_token': token}
    from .topic_registry import keyword_header_probe
    api(temporary, 'POST', ':batchUpdate', {'requests': [{'findReplace': keyword_header_probe(settings)}]})
    settings['credentials_file'] = store_credentials(db, data)
    settings.pop('apps_script_file', None)
    settings['service_account_email'] = data['client_email']
    settings['write_verified_at'] = time.time()
    save(db, SETTINGS, settings); db.commit()
    return {'ok': True, 'service_account_email': data['client_email'], 'writing_verified': True}
