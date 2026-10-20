"""Persistent local cabinet session, kept beside operational data, outside releases."""
import fcntl
import os
import secrets
from pathlib import Path


def session_token(database):
    if database == ':memory:':
        return secrets.token_urlsafe(32)
    path=Path(database).expanduser().resolve().parent / '.cabinet-session-token'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'r+',encoding='ascii') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
        token=handle.read().strip()
        if not token:
            token=secrets.token_urlsafe(32)
            handle.write(token+'\n');handle.flush();os.fsync(handle.fileno())
        if len(token)<32 or len(token)>128 or not all(c.isalnum() or c in '_-' for c in token):
            raise ValueError('Invalid cabinet session file')
        return token
