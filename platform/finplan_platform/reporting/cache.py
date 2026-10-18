"""Private, bounded, disposable cache of checksum-verified immutable artifact bytes.

No mutable DynamoDB heads are cached. Cache corruption is a miss: the original
approved artifact is read and verified again. All eviction is confined to this
cache's own indexed digest files, never report archives or unrelated scratch.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import gzip
import zlib
import os
from pathlib import Path
import re
import sqlite3
from threading import Lock
import time
import uuid

MAX_BYTES = 4 * 1024**3
MAX_ENTRIES = 200000
DIGEST = re.compile(r"[0-9a-f]{64}")


class ArtifactCache:
    def __init__(self, directory: Path, *, max_bytes: int = MAX_BYTES, max_entries: int = MAX_ENTRIES):
        if not 1 <= max_bytes <= MAX_BYTES or not 1 <= max_entries <= MAX_ENTRIES:
            raise ValueError("Immutable cache bounds exceed approved limits")
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("Immutable cache directory must be absolute")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_uid != os.getuid():
            raise ValueError("Immutable cache directory must be owned and private")
        self.directory.chmod(0o700)
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._mutex = Lock()
        self._lock_fd = os.open(self.directory / "cache.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(self._lock_fd, 0o600)
        try:
            self._initialize()
        except BaseException:
            if hasattr(self, "_db"):
                self._db.close()
            os.close(self._lock_fd)
            raise

    def _initialize(self):
        with self._locked():
            marker = self.directory / ".modeling-report-cache-v1"
            if marker.exists():
                if marker.is_symlink() or marker.read_bytes() != b"disposable verified modeling artifact cache v1\n":
                    raise ValueError("Invalid immutable cache directory marker")
            else:
                if any(p.name != "cache.lock" for p in self.directory.iterdir()):
                    raise ValueError("Refuse an unrelated preexisting cache directory")
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(b"disposable verified modeling artifact cache v1\n")
            path = self.directory / "cache.sqlite"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600); os.close(fd)
            self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("CREATE TABLE IF NOT EXISTS artifacts(digest TEXT PRIMARY KEY,size INTEGER NOT NULL,used INTEGER NOT NULL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS artifacts_lru ON artifacts(used,digest)")
            self._db.execute("CREATE TABLE IF NOT EXISTS totals(id INTEGER PRIMARY KEY, count INTEGER NOT NULL,size INTEGER NOT NULL)")
            self._db.execute("INSERT OR REPLACE INTO totals SELECT 1,COUNT(*),COALESCE(SUM(size),0) FROM artifacts")
            self._db.commit()
            # A process may have crashed between atomic byte publication and its
            # disposable index update. Remove only this cache's orphan filenames.
            known = {r[0] for r in self._db.execute("SELECT digest FROM artifacts")}
            for item in self.directory.iterdir():
                if (item.name.endswith(".bin") and DIGEST.fullmatch(item.stem) and item.stem not in known) or re.fullmatch(r"\.tmp-[0-9a-f]{32}",item.name):
                    item.unlink(missing_ok=True)
            self._evict()

    @contextmanager
    def _locked(self):
        with self._mutex:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    @staticmethod
    def _digest(checksum):
        if not isinstance(checksum, str) or not checksum.startswith("sha256:") or not DIGEST.fullmatch(checksum[7:]):
            raise ValueError("Immutable artifact cache requires an exact SHA256")
        return checksum[7:]

    def _remove(self, digest):
        if not DIGEST.fullmatch(digest):
            raise ValueError("Invalid cache index digest")
        (self.directory / (digest + ".bin")).unlink(missing_ok=True)
        old = self._db.execute("SELECT size FROM artifacts WHERE digest=?", (digest,)).fetchone()
        self._db.execute("DELETE FROM artifacts WHERE digest=?", (digest,))
        if old:
            self._db.execute("UPDATE totals SET count=count-1,size=size-? WHERE id=1", (old[0],))

    def _evict(self):
        count, size = self._db.execute("SELECT count,size FROM totals WHERE id=1").fetchone()
        while count > self.max_entries or size > self.max_bytes:
            digest, removed = self._db.execute("SELECT digest,size FROM artifacts ORDER BY used,digest LIMIT 1").fetchone()
            self._remove(digest)
            count -= 1; size -= removed
        self._db.commit()

    def get(self, checksum, *, max_size):
        digest = self._digest(checksum)
        with self._locked():
            record = self._db.execute("SELECT size FROM artifacts WHERE digest=?", (digest,)).fetchone()
            if not record:
                return None
            try:
                fd = os.open(self.directory / (digest + ".bin"), os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    if record[0] > max_size or os.fstat(stream.fileno()).st_size != record[0]:
                        raise ValueError("Invalid cache size")
                    stored = stream.read(max_size + 1)
                if len(stored) != record[0]:
                    raise ValueError("Invalid cache file size")
                if stored.startswith(b"\x00FMCA1"):
                    if len(stored)<14:
                        raise ValueError("Invalid compressed cache header")
                    expected=int.from_bytes(stored[6:14],"big")
                    if expected>max_size:
                        raise ValueError("Compressed cache exceeds original artifact bound")
                    inflater=zlib.decompressobj(16+zlib.MAX_WBITS)
                    raw=inflater.decompress(stored[14:],expected+1)
                    if len(raw)!=expected or not inflater.eof or inflater.unused_data:
                        raise ValueError("Invalid compressed cache expansion")
                else:
                    raw=stored
                if len(raw)>max_size or hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError("Invalid cache checksum")
            except (OSError, ValueError, zlib.error):
                self._remove(digest); self._db.commit()
                return None
            # LRU metadata is disposable and process-serialized; original bytes
            # remain immutable and are verified on every hit.
            self._db.execute("UPDATE artifacts SET used=? WHERE digest=?", (time.time_ns(), digest))
            self._db.commit()
            return raw

    def put(self, checksum, raw):
        digest = self._digest(checksum)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Unverified bytes cannot enter immutable cache")
        stored=raw
        if len(raw)>=256:
            compressed=b"\x00FMCA1"+len(raw).to_bytes(8,"big")+gzip.compress(raw,compresslevel=1,mtime=0)
            if len(compressed)<len(raw):
                stored=compressed
        if len(stored) > self.max_bytes:
            return
        with self._locked():
            path = self.directory / (digest + ".bin")
            temporary = self.directory / (".tmp-" + uuid.uuid4().hex)
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(stored)  # Disposable cache: checksum detects crash-torn bytes.
                os.replace(temporary, path)
                old = self._db.execute("SELECT size FROM artifacts WHERE digest=?", (digest,)).fetchone()
                self._db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?)", (digest, len(stored), time.time_ns()))
                self._db.execute("UPDATE totals SET count=count+?,size=size+? WHERE id=1", (0 if old else 1,len(stored)-(old[0] if old else 0)))
                self._evict()
            finally:
                temporary.unlink(missing_ok=True)

    def close(self):
        with self._locked():
            self._db.close()
        os.close(self._lock_fd)
