"""Explicit local data lifecycle helpers.

Only known runtime artifacts are removed. User-authored settings and denylist
files are preserved unless a caller explicitly removes them.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil


RUNTIME_FILES = (
    "proxies.sqlite3",
    "proxies.sqlite3-wal",
    "proxies.sqlite3-shm",
    "sources-report.json",
    "last-profile.txt",
    "gui-targets.json",
    "gui-sources.json",
    "gui-input.txt",
    "gui-progress.json",
    "gui-job.json",
    "gui-run.log",
    "gui-address.json",
    "gui-stop",
    "gui-instance.lock",
    "workbench.lock",
)
RUNTIME_DIRS = ("exports",)


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield handle
    except OSError as exc:
        raise RuntimeError('Данные уже используются другим процессом.') from exc
    finally:
        if acquired:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def clear_runtime(data, *, keep_lock=False):
    data = Path(data)
    removed = []
    for name in RUNTIME_FILES:
        if keep_lock and name.endswith(".lock"):
            continue
        path = data / name
        if path.exists() or path.is_symlink():
            path.unlink()
            removed.append(name)
    for name in RUNTIME_DIRS:
        path = data / name
        if path.exists():
            shutil.rmtree(path)
            removed.append(name + "/")
    return removed
