"""Consistent SQLite snapshots and offline restore with integrity verification."""
import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from sqlalchemy.engine import make_url
from .config import DATABASE_URL
from .control import ControlStore, data_dir
from .locking import FileLock


def database_paths():
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
        raise ValueError('Local backup supports file-based SQLite only')
    from .aggregate_store import AggregateStore
    return {'recruit.sqlite3': Path(url.database).resolve(),
            'control.sqlite3': ControlStore().path.resolve(),
            'inventory.sqlite3': Path(AggregateStore().path).resolve()}


def create_backup():
    paths = database_paths()
    folder = data_dir() / 'backups'
    folder.mkdir(exist_ok=True)
    destination = folder / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6] + '.zip')
    manifest = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'files': {}}
    with tempfile.TemporaryDirectory(prefix='radar-backup-') as temporary:
        with ZipFile(destination, 'w', ZIP_DEFLATED) as archive:
            for name, source in paths.items():
                if not source.exists():
                    raise FileNotFoundError(source)
                snapshot = Path(temporary) / name
                src = sqlite3.connect(source)
                dst = sqlite3.connect(snapshot)
                try:
                    src.backup(dst)
                    if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Database integrity check failed')
                finally:
                    dst.close(); src.close()
                manifest['files'][name] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
                archive.write(snapshot, name)
            archive.writestr('manifest.json', json.dumps(manifest, indent=2))
    return {'path': str(destination), 'bytes': destination.stat().st_size, 'files': list(paths)}


def restore_backup(archive_path):
    paths = database_paths()
    locks = [FileLock(paths['control.sqlite3'].with_suffix('.worker.lock')),
             FileLock(str(paths['recruit.sqlite3']) + '.writer.lock')]
    acquired = []
    try:
        for lock in locks:
            if not lock.acquire():
                raise RuntimeError('Stop Recruit Radar before restoring the backup')
            acquired.append(lock)
        with tempfile.TemporaryDirectory(prefix='radar-restore-') as temporary:
            with ZipFile(archive_path) as archive:
                names=set(archive.namelist())
                if names not in ({*paths,'manifest.json'},{'recruit.sqlite3','control.sqlite3','manifest.json'}):
                    raise ValueError('Unexpected backup archive contents')
                restore_paths={name:path for name,path in paths.items() if name in names}
                manifest = json.loads(archive.read('manifest.json'))
                if manifest.get('format') != 1:
                    raise ValueError('Unsupported backup format')
                for name in restore_paths:
                    if archive.getinfo(name).file_size > 2_000_000_000:
                        raise ValueError('Backup is too large')
                    content = archive.read(name)
                    if hashlib.sha256(content).hexdigest() != manifest['files'].get(name):
                        raise ValueError('Backup checksum mismatch')
                    candidate = Path(temporary) / name
                    candidate.write_bytes(content)
                    db = sqlite3.connect(candidate)
                    try:
                        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise ValueError('Backup database is corrupted')
                    finally:
                        db.close()
            previous = create_backup()
            for name, target in restore_paths.items():
                # SQLite backup updates destination pages without stale WAL sidecars.
                source = sqlite3.connect(Path(temporary) / name)
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination)
                finally:
                    destination.close(); source.close()
            if 'inventory.sqlite3' not in restore_paths:
                # Legacy backups have no inventory. Re-queue it instead of falsely claiming coverage.
                with sqlite3.connect(paths['inventory.sqlite3']) as db:
                    db.execute("UPDATE aggregate_refs SET state='pending'")
                    db.execute("UPDATE aggregate_pages SET state='pending'")
        return {'restored': True, 'previous_backup': previous['path']}
    finally:
        for lock in reversed(acquired):
            lock.release()


def main():
    from .console import configure_console
    configure_console()
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['backup', 'restore'])
    parser.add_argument('archive', nargs='?')
    args = parser.parse_args()
    if args.command == 'restore' and not args.archive:
        parser.error('restore requires a backup ZIP path; stop the app first')
    print(json.dumps(create_backup() if args.command == 'backup' else restore_backup(args.archive), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
