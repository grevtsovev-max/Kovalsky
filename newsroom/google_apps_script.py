"""A table-bound Apps Script bridge; the shared secret stays in a private file."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REQUEST_TIMEOUT = 90

SCRIPT_PATH = Path(__file__).parent / 'integrations' / 'kovalsky_topics.gs'


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Укажите адрес подключения и код доступа')
    url, secret = data.get('url', ''), data.get('secret', '')
    if not isinstance(url, str) or not re.fullmatch(r'https://script\.google\.com/macros/s/[A-Za-z0-9_-]{20,200}/exec', url):
        raise ValueError('Нужен адрес веб-приложения Google, заканчивающийся на /exec')
    if not isinstance(secret, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', secret):
        raise ValueError('Скопируйте код доступа из окна настройки в таблице')
    return {'url': url, 'secret': secret}


def read_credentials(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_size > 4096):
            raise ValueError('APPS_SCRIPT_CREDENTIAL_PERMISSIONS')
        return validate(json.load(file))


class GoogleRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme != 'https' or target.hostname != 'script.googleusercontent.com'
                or target.username or target.password or target.port not in (None, 443)):
            raise ValueError('APPS_SCRIPT_REDIRECT_DENIED')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(settings, method, suffix, data=None, credentials=None):
    credentials = validate(credentials) if credentials is not None else read_credentials(settings['apps_script_file'])
    payload = {**credentials, 'spreadsheet_id': settings['spreadsheet_id'],
               'method': method, 'suffix': suffix, 'data': data}
    payload.pop('url')
    req = urllib.request.Request(credentials['url'], data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(GoogleRedirects()).open(req, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError('APPS_SCRIPT_RESPONSE_INVALID')
        result = json.loads(raw)
        if isinstance(result, dict) and result.get('ok') is False:
            reason = result.get('error')
            if reason in {'ACCESS_DENIED', 'WRONG_TABLE', 'BUSY', 'TABS_MISSING', 'GOOGLE_OPERATION_FAILED'}:
                raise RuntimeError('APPS_SCRIPT_' + reason)
        if (not isinstance(result, dict) or result.get('ok') is not True
                or result.get('spreadsheet_id') != settings['spreadsheet_id'] or not isinstance(result.get('result'), dict)):
            raise ValueError('APPS_SCRIPT_RESPONSE_INVALID')
        return result['result']
    except RuntimeError as error:
        if str(error) in {'APPS_SCRIPT_ACCESS_DENIED', 'APPS_SCRIPT_WRONG_TABLE', 'APPS_SCRIPT_BUSY', 'APPS_SCRIPT_TABS_MISSING', 'APPS_SCRIPT_GOOGLE_OPERATION_FAILED'}:
            raise RuntimeError(str(error)) from None
        raise RuntimeError('APPS_SCRIPT_REQUEST_FAILED') from None
    except TimeoutError:
        raise RuntimeError('APPS_SCRIPT_REQUEST_TIMEOUT') from None
    except urllib.error.HTTPError as error:
        raise RuntimeError('APPS_SCRIPT_HTTP_' + str(error.code)) from None
    except Exception:
        # Google error bodies and redirect URLs may contain private values.
        raise RuntimeError('APPS_SCRIPT_REQUEST_FAILED') from None


def store_credentials(db, data):
    from .google_sheets_auth import credential_path
    path = credential_path(db).with_name('google-apps-script.json')
    if path.is_symlink():
        raise ValueError('APPS_SCRIPT_CREDENTIAL_PATH_UNSAFE')
    fd, staging = tempfile.mkstemp(prefix='.google-script-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(validate(data), file)
            file.flush(); os.fsync(file.fileno())
        os.replace(staging, path)
    finally:
        if os.path.exists(staging): os.unlink(staging)
    return str(path)


def configure(db, data):
    from .source_registry import state, save
    from .topic_registry import SETTINGS
    settings = state(db, SETTINGS)
    if not settings:
        raise ValueError('Сначала подключите таблицу тем')
    credentials = validate(data)
    from .topic_registry import keyword_header_probe
    result = request(settings, 'POST', ':batchUpdate',
                     {'requests': [{'findReplace': keyword_header_probe(settings)}]}, credentials)
    replies = result.get('replies', [])
    if len(replies) != 1 or not isinstance(replies[0].get('findReplace'), dict):
        raise ValueError('Не удалось подтвердить проверку записи в таблицу')
    settings['apps_script_file'] = store_credentials(db, credentials)
    settings['write_verified_at'] = time.time()
    save(db, SETTINGS, settings); db.commit()
    return {'ok': True, 'writing_verified': True}
