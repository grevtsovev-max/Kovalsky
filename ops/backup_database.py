"""Consistent SQLite snapshots and restoration into a new, separate database."""
import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]


def inspect_database(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('DATABASE_INTEGRITY_FAILED')
        if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('DATABASE_REFERENCES_FAILED')
        names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name:db.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in names}
    finally:db.close()


def snapshot(source,destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if destination==ROOT or ROOT in destination.parents:raise ValueError('DATABASE_MUST_STAY_OUTSIDE_RELEASE')
    if source==destination or destination.exists():raise ValueError('DATABASE_DESTINATION_EXISTS')
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temporary=tempfile.mkstemp(prefix='.snapshot-',suffix='.sqlite3',dir=destination.parent)
    os.close(fd)
    temporary=Path(temporary)
    try:
        reader=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
        try:
            # Pin one committed WAL snapshot so continuous collection cannot
            # restart a large backup from its first page after every write.
            reader.execute('BEGIN')
            reader.execute('SELECT count(*) FROM sqlite_master').fetchone()
            writer=sqlite3.connect(temporary)
            try:reader.backup(writer,pages=256);writer.commit()
            finally:writer.close()
        finally:reader.close()
        counts=inspect_database(temporary)
        # Link without replacement: another process cannot lose its destination.
        with temporary.open('rb') as saved:os.fsync(saved.fileno())
        os.link(temporary,destination)
        directory=os.open(destination.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return counts
    finally:temporary.unlink(missing_ok=True)


def restore(source,destination):
    # A restore is always to a new path; switching a live database is separate.
    expected=inspect_database(source)
    actual=snapshot(source,destination)
    if actual!=expected:raise ValueError('DATABASE_RESTORE_COUNTS_CHANGED')
    return actual


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','restore','verify'])
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--destination',type=Path)
    args=parser.parse_args()
    if args.action!='verify' and args.destination is None:parser.error('--destination is required')
    operation={'backup':snapshot,'restore':restore,'verify':inspect_database}[args.action]
    counts=operation(args.source) if args.action=='verify' else operation(args.source,args.destination)
    print(json.dumps({'verified':True,'tables':len(counts),'rows':sum(counts.values())}))


if __name__=='__main__':main()
