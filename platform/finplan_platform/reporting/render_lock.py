"""One read-model builder per output root, before expensive evidence aggregation."""
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat

from .reports import EvidenceError, PublicationDeferred


@contextmanager
def render_lock(output_root: Path):
    """Nonblocking, crash-released lock shared by daily, weekly and repair CLIs.

    Keep the file after releasing it: unlinking would let another builder lock a
    different inode. Model/research jobs never acquire this read-only report lock.
    """
    root = Path(output_root)
    if root.is_symlink():
        raise EvidenceError("Report output root must not be a symbolic link")
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        raise EvidenceError("Report output root cannot safely be created") from error
    try:
        fd = os.open(root / ".render.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    except OSError as error:
        raise EvidenceError("Report render lock cannot safely be opened") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size:
            raise EvidenceError("Report render lock must be an empty, singly linked regular file owned by the current user")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EACCES):
                raise PublicationDeferred("Another report builder owns this output root; retry this report command within its deadline") from error
            raise EvidenceError("Report render lock could not be acquired") from error
        yield
    finally:
        os.close(fd)  # Also releases flock after an exception; OS closes it on crash.
