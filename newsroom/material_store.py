"""Portable immutable snapshots of actually read source material."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_settings = {}


def configure(config):
    global _settings
    database = config.get('newsroom', {}).get('database')
    settings = dict(config.get('reader', {}))
    if database:
        settings.setdefault('jina_cloud_ledger', str(Path(database).expanduser().resolve().parent / 'jina-cloud.sqlite3'))
        settings.setdefault('archive_directory', str(Path(database).expanduser().resolve().parent / 'materials'))
    _settings = settings


def settings():
    return dict(_settings)


def save(article):
    directory = _settings.get('archive_directory')
    if not directory or article.get('material_read') is not True:
        return article
    content = str(article.get('content') or '')
    if not content.strip():
        return article
    record = {key: article.get(key) for key in (
        'url', 'material_url', 'title', 'publisher_name', 'published_at', 'updated_at',
        'content', 'reader_content', 'reading_method', 'primary_source_url', 'primary_source_title',
        'primary_source_content', 'primary_source_status')}
    record['schema_version'] = 1
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True).encode('utf-8')
    version = hashlib.sha256(encoded).hexdigest()
    material = hashlib.sha256(str(article.get('material_url') or article['url']).encode()).hexdigest()
    relative = Path(material[:2]) / material / (version + '.json')
    target = Path(directory).expanduser() / relative
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.exists():
        record['read_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = handle.name
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True).encode('utf-8'))
                handle.flush()
                os.fsync(handle.fileno())
            # Concurrent readers may save the same version. The first snapshot
            # wins; never replace its recorded reading time with a later one.
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    article.update(material_id=material, material_version=version,
                   archive_key=relative.as_posix(), archived_characters=len(content))
    return article
