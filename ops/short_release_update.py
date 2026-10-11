"""Deploy/roll back the named edition overlay without replacing unrelated code."""
import argparse,datetime,grp,json,os,py_compile,shutil,sqlite3,subprocess
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path

UPDATE='short-release-v1'
FILES=['newsroom/edition/'+name for name in (
    'fragments.py','short_release.py','model.py','pipeline.py','fallback.py',
    'publication.py','worker.py','policy.json','references.json')]
FILES+=['AGENT_LOGIC.md','EDITORIAL_RULES.md','docs/updates/short-release-v1.md',
        'ops/short_release_update.py']
SERVICES=('kovalsky-newsroom.service','kovalsky-review.service','kovalsky-dashboard.service')


def command(*args):return subprocess.run(args,check=True,capture_output=True,text=True)
def active(service):return subprocess.run(['systemctl','is-active','--quiet',service]).returncode==0
def swap(root,target):
    link=root/'app.next';link.unlink(missing_ok=True);link.symlink_to(target)
    link.replace(root/'app')
def restart(previous):
    for service in SERVICES:
        command('systemctl','restart' if service==SERVICES[-1] else 'try-restart',service)
    for service in SERVICES:
        if not previous[service] and service!=SERVICES[-1]:continue
        condition=command('systemctl','show','-p','ConditionResult','--value',service).stdout.strip()
        if condition not in ('no','false') and not active(service):raise RuntimeError('Service did not recover: '+service)


def normalize_pending(config_path):
    with open(config_path,'rb') as handle:config=tomllib.load(handle)
    db=sqlite3.connect(config['newsroom']['database'],timeout=30)
    # Rollback changes only never-sent preparation states. Delivery attempts,
    # published messages, unknown outcomes and owner control are untouched.
    db.execute("""UPDATE edition_documents AS d SET state='INCOMPLETE'
        WHERE state='SHORT_PENDING' AND (post_id IS NULL OR EXISTS (
          SELECT 1 FROM posts p WHERE p.post_id=d.post_id AND p.status='PENDING'
          AND p.external_id IS NULL AND p.published_at IS NULL))
        AND NOT EXISTS (SELECT 1 FROM publication_attempts a
          WHERE a.post_id=d.post_id OR a.delivery_key LIKE '%:edition:'||d.document_id)""")
    db.commit();db.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('deploy','rollback'))
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--source',type=Path,default=Path.cwd())
    parser.add_argument('--expected-base',type=Path);parser.add_argument('--config',type=Path)
    args=parser.parse_args();root=args.root.resolve();current=(root/'app').resolve(strict=True)
    previous={s:active(s) for s in SERVICES};registry=root/'updates'/UPDATE
    registry.mkdir(parents=True,exist_ok=True)
    record_path=registry/'release.json'
    if args.action=='deploy':
        if args.expected_base is None or args.expected_base.resolve()!=current:
            raise RuntimeError('Current release changed; inspect it before deployment')
        prior_record=json.loads(record_path.read_text()) if record_path.exists() else None
        if prior_record and current!=Path(prior_record['release']):
            raise RuntimeError('Another release is active; inspect before updating the named release')
        now=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        target=root/'releases'/(UPDATE+'-'+now)
        shutil.copytree(current,target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for relative in FILES:
            source=args.source/relative
            if source.is_symlink() or not source.is_file():raise RuntimeError('Missing overlay file: '+relative)
            (target/relative).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target/relative)
        for file in (target/'newsroom/edition').glob('*.py'):py_compile.compile(str(file),doraise=True)
        policy=json.loads((target/'newsroom/edition/policy.json').read_text())
        references=json.loads((target/'newsroom/edition/references.json').read_text())
        if policy['version']!=references['version'] or not policy.get('short_release'):
            raise RuntimeError('Release policy is inconsistent or disabled')
        gid=grp.getgrnam('kovalsky').gr_gid
        for file in [target,*target.rglob('*')]:
            os.chown(file,0,gid);os.chmod(file,0o750 if file.is_dir() else 0o640)
        record={'update':UPDATE,'name':'Короткий выпуск','previous':prior_record['previous'] if prior_record else str(current),'release':str(target),
                'policy_version':policy['version'],'files':FILES,'created_at':now}
        record['revisions']=[*(prior_record.get('revisions',[]) if prior_record else []),str(target)]
        record_path.write_text(json.dumps(record,ensure_ascii=False,indent=2));os.chmod(record_path,0o640)
    else:
        record=json.loads(record_path.read_text())
        if current!=Path(record['release']):raise RuntimeError('Another release is active; inspect before rollback')
        if args.config is None:raise RuntimeError('Rollback requires the working configuration')
        target=Path(record['previous'])
        if not target.is_dir():raise RuntimeError('Previous release is missing')
        # Pause only already-running agents so database conversion has no writer race.
        for service in SERVICES[:-1]:
            if previous[service]:command('systemctl','stop',service)
        normalize_pending(args.config)
    swap(root,target)
    try:
        if args.action=='rollback':
            for service in SERVICES[:-1]:
                if previous[service]:command('systemctl','start',service)
        restart(previous)
    except Exception:
        swap(root,current);restart(previous)
        if args.action=='deploy':
            if prior_record:record_path.write_text(json.dumps(prior_record,ensure_ascii=False,indent=2))
            else:record_path.unlink(missing_ok=True)
        raise
    print(json.dumps({'update':UPDATE,'action':args.action,'release':str(target),
                      'owner_control_changed':False},ensure_ascii=False))


if __name__=='__main__':main()
