"""Prepare or activate the isolated, backward-readable filter audit storage update.

Preparation copies the deployed surrounding code, checks the expected file hashes,
and applies only the reviewed files. Activation restarts only already-active services
and never calls agent enable. Before historical migration, rollback is automatic on
health failure. After historical migration, retain a reader supporting compact rows.
"""
import argparse
import datetime
import grp
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

FILES = ('newsroom/filter_audit.py', 'newsroom/db.py', 'newsroom/retrospective.py')
EXTRAS = ('tests/test_database_backup.py', 'tests/test_filter_audit.py', 'ops/compact_filter_audit.py', 'ops/backup_database.py',
          'ops/filter_audit_update.py', 'ops/filter_audit_base.json', 'ops/check_dashboard.py')
SERVICES = ('kovalsky-newsroom.service', 'kovalsky-review.service', 'kovalsky-dashboard.service')


def command(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def active(service):
    return subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0


def swap(root, target):
    link = root/'app.filter-audit-next'
    link.unlink(missing_ok=True); link.symlink_to(target); link.replace(root/'app')


def restart(previous):
    for service, was_active in previous.items():
        if was_active:
            command('systemctl', 'try-restart', service)
    for service, was_active in previous.items():
        if not was_active and active(service):
            raise RuntimeError('INACTIVE_SERVICE_STARTED')
        if was_active:
            condition = command('systemctl','show','-p','ConditionResult','--value',service).stdout.strip()
            if condition not in ('no','false') and not active(service):
                raise RuntimeError('SERVICE_DID_NOT_RECOVER:'+service)


def prepare(root, source):
    current = (root/'app').resolve(strict=True)
    manifest = json.loads((source/'ops/filter_audit_base.json').read_text())
    if set(manifest) != set(FILES):
        raise ValueError('UNEXPECTED_PATCH_MANIFEST')
    for name, digest in manifest.items():
        if hashlib.sha256((current/name).read_bytes()).hexdigest() != digest:
            raise ValueError('DEPLOYED_FILE_CHANGED:'+name)
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = root/'releases'/('filter-audit-v2-'+now)
    shutil.copytree(current, target, symlinks=True, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in FILES+EXTRAS:
        (target/name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source/name, target/name)
    gid = grp.getgrnam('kovalsky').gr_gid
    for path in (target, *target.rglob('*')):
        if not path.is_symlink():
            os.chown(path, 0, gid); os.chmod(path, 0o750 if path.is_dir() else 0o640)
    return current, target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare','activate'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--release', type=Path)
    parser.add_argument('--expected-base', type=Path)
    args = parser.parse_args(); root = args.root.resolve()
    if args.action == 'prepare':
        old, target = prepare(root, args.source.resolve())
        print(json.dumps({'base':str(old), 'release':str(target)}), flush=True)
        return
    target = args.release.resolve(strict=True)
    current = (root/'app').resolve(strict=True)
    if current != args.expected_base.resolve(strict=True) or target.parent != root/'releases':
        raise ValueError('UNEXPECTED_RELEASE')
    # Test results are generated against the exact candidate before activation.
    proof = json.loads((target/'filter-audit-tests.json').read_text())
    expected = {n:hashlib.sha256((target/n).read_bytes()).hexdigest() for n in FILES+EXTRAS}
    if proof['files'] != expected or proof['new_failures'] or not proof['focused_passed']:
        raise ValueError('CANDIDATE_TEST_PROOF_INVALID')
    previous = {s:active(s) for s in SERVICES}
    swap(root, target)
    try:
        restart(previous)
        command(str(root/'venv/bin/python'), str(target/'ops/check_dashboard.py'))
    except Exception:
        swap(root, current); restart(previous); raise
    state = {'previous':str(current), 'release':str(target), 'services_before':previous,
             'owner_control_changed':False, 'activated':True}
    registry = root/'updates/filter-audit-v2'; registry.mkdir(parents=True, exist_ok=True)
    (registry/'activation.json').write_text(json.dumps(state, sort_keys=True))
    print(json.dumps(state), flush=True)


if __name__ == '__main__':
    main()
