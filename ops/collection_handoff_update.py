"""Apply the inspected completed-collection handoff patch while preserving owner control."""
import argparse
import datetime
import grp
import hashlib
import json
import os
import py_compile
import shutil
import subprocess
from pathlib import Path

UPDATE='collection-handoff-v1'
FILES=('newsroom/cli.py','newsroom/edition/worker.py')
SERVICES=('kovalsky-newsroom.service','kovalsky-review.service','kovalsky-dashboard.service')


def command(*args,**kwargs):
    return subprocess.run(args,check=True,capture_output=True,text=True,**kwargs)


def active(service):
    return subprocess.run(['systemctl','is-active','--quiet',service]).returncode==0


def swap(root,target):
    link=root/'app.next';link.unlink(missing_ok=True);link.symlink_to(target)
    link.replace(root/'app')


def restart(previous):
    for service in SERVICES:
        if previous[service]:command('systemctl','try-restart',service)
    for service in SERVICES:
        if not previous[service]:
            if active(service):raise RuntimeError('Inactive service unexpectedly started')
            continue
        condition=command('systemctl','show','-p','ConditionResult','--value',service).stdout.strip()
        if condition not in ('no','false') and not active(service):
            raise RuntimeError('Service did not recover: '+service)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--expected-base',type=Path,required=True)
    args=parser.parse_args();root=args.root.resolve();source=args.source.resolve()
    current=(root/'app').resolve(strict=True)
    if current!=args.expected_base.resolve():raise RuntimeError('Active release changed')
    manifest=json.loads((source/'ops/collection_handoff_base.json').read_text())
    if set(manifest)!=set(FILES):raise RuntimeError('Unexpected patch manifest')
    for name,digest in manifest.items():
        if hashlib.sha256((current/name).read_bytes()).hexdigest()!=digest:
            raise RuntimeError('Base file changed: '+name)
    now=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target=root/'releases'/(UPDATE+'-'+now)
    # Keep the currently deployed surrounding code; unrelated worktree edits
    # are not copied into this named patch release.
    shutil.copytree(current,target,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    command('patch','--batch','--forward','--fuzz=0','-p1','-i',str(source/'ops/collection_handoff_v1.patch'),cwd=target)
    for name in ('tests/test_collection_handoff.py','ops/collection_handoff_update.py','ops/check_dashboard.py',
                 'ops/collection_handoff_base.json','ops/collection_handoff_v1.patch'):
        (target/name).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/name,target/name)
    for name in FILES:
        if name.endswith('.py'):py_compile.compile(str(target/name),doraise=True)
    # Test the exact staged release, not the surrounding uncommitted worktree.
    # Existing disabled-module tests may already fail in the deployed baseline.
    # Compare exact failure identities and messages; never waive a new failure.
    runner = """import unittest,json,hashlib,io,contextlib,re
suite=unittest.defaultTestLoader.discover('tests')
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
 result=unittest.TextTestRunner(stream=io.StringIO()).run(suite)
def fingerprints(entries):
 return sorted((test.id(),hashlib.sha256(re.sub(r'[A-Za-z0-9_-]{43}', '<random-token>', trace.splitlines()[-1]).encode()).hexdigest()) for test,trace in entries)
print(json.dumps({'count':result.testsRun,'errors':fingerprints(result.errors),'failures':fingerprints(result.failures)}))
"""
    baseline=json.loads(command(str(root/'venv/bin/python'),'-c',runner,cwd=current).stdout)
    candidate=json.loads(command(str(root/'venv/bin/python'),'-c',runner,cwd=target).stdout)
    if candidate['errors']!=baseline['errors'] or candidate['failures']!=baseline['failures']:
        raise RuntimeError('Candidate introduces or changes a test failure')
    if candidate['count']!=baseline['count']+2:
        raise RuntimeError('Expected both handoff regression tests to run')
    command(str(root/'venv/bin/python'),'-m','unittest','discover','-s','tests',
            '-p','test_collection_handoff.py','-q',cwd=target)
    print(json.dumps({'tests':candidate['count'],'new_regressions':0,
                     'preexisting_failures':len(baseline['errors'])+len(baseline['failures'])}),flush=True)
    gid=grp.getgrnam('kovalsky').gr_gid
    for path in (target,*target.rglob('*')):
        if path.is_symlink():continue
        os.chown(path,0,gid);os.chmod(path,0o750 if path.is_dir() else 0o640)
    if (root/'app').resolve()!=current:raise RuntimeError('Active release changed during validation')
    previous={s:active(s) for s in SERVICES}
    registry=root/'updates'/UPDATE;registry.mkdir(parents=True,exist_ok=True)
    record=registry/('release-'+now+'.json')
    record.write_text(json.dumps({'update':UPDATE,'previous':str(current),'release':str(target),
        'files':FILES,'owner_control_changed':False,'created_at':now},ensure_ascii=False,indent=2))
    os.chmod(record,0o640)
    swap(root,target)
    try:
        restart(previous)
        command(str(root/'venv/bin/python'),str(target/'ops/check_dashboard.py'))
    except Exception:
        swap(root,current);restart(previous);raise
    print(json.dumps({'update':UPDATE,'owner_control_changed':False,'activated':True}))


if __name__=='__main__':main()
