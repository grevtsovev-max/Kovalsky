from __future__ import annotations

import fcntl
from pathlib import Path


def acquire_cycle_lock(database_path: str):
    """Acquire a non-blocking process-wide lock for one complete newsroom cycle."""
    db_path = Path(database_path).expanduser().resolve()
    lock_path = db_path.with_suffix(db_path.suffix + ".cycle.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return None
    return handle
