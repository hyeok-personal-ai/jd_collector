"""Local server supervisor. One instance, graceful stop, restart after crashes."""
import argparse
import os
import socket
import subprocess
import sys
import time
from .control import data_dir
from .locking import FileLock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024..65535')
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', args.port)) == 0:
            raise SystemExit(f'Port {args.port} is already in use')
    folder = data_dir()
    lock = FileLock(folder / 'server.lock')
    if not lock.acquire():
        raise SystemExit('Recruit Radar is already running')
    env = {**os.environ, 'RADAR_WORKER': '1', 'PYTHONUTF8': '1'}
    child = None
    try:
        while True:
            with (folder / 'server.log').open('a', encoding='utf-8') as log:
                child = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'uvicorn',
                                          'app.api:app', '--host', '127.0.0.1', '--port', str(args.port)],
                                         env=env, stdout=log, stderr=log)
                code = child.wait()
            child = None
            if code == 0:
                break
            time.sleep(5)
    except KeyboardInterrupt:
        if child:
            child.terminate()
            child.wait(timeout=15)
    finally:
        if child and child.poll() is None:
            child.terminate()
        lock.release()


if __name__ == '__main__':
    main()
