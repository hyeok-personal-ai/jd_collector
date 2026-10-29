"""Cross-process local writer/service locks, released automatically on exit."""
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.file = stream
        return True

    def release(self):
        if self.file:
            self.file.close()
            self.file = None


@asynccontextmanager
async def writer_guard(factory):
    with factory() as session:
        url = session.get_bind().url
    # Local SQLite writer serialisation; SQL dialects with server locks are separate deployments.
    if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
        yield
        return
    lock = FileLock(str(Path(url.database).resolve()) + '.writer.lock')
    for _ in range(3000):
        if lock.acquire():
            break
        await asyncio.sleep(.1)
    else:
        raise TimeoutError('Another local writer is still collecting; retry later')
    try:
        yield
    finally:
        lock.release()
