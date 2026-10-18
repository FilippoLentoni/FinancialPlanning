"""Bounded parallel immutable reads for reports; never writes authoritative evidence.

Only the real ModelingEvidence reader is adapted. Its low-level boto3 clients are
shared (clients, not Sessions/resources); mutable credentials/config/event hooks
are never changed. Metadata pagination stays scoped and sequential. At most thirty-two
record/artifact reads are submitted, and results retain the original query order.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import re
import os
import sqlite3
from pathlib import Path
from threading import Lock

from .cache import ArtifactCache
from ..core.errors import PlatformError
from ..core.modeling_evidence import KINDS, ModelingEvidence

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_WORKERS = 32
CACHE_BYTES = 32 * 1024 * 1024
CACHE_ENTRIES = 2048


class ReportReader:
    """One report's bounded I/O scope; validation and packing remain on the caller."""

    def __init__(self, backend: ModelingEvidence, *, workers: int = MAX_WORKERS, cache_dir: Path | None = None):
        if not 1 <= workers <= MAX_WORKERS:
            raise ValueError("report I/O workers must be between one and thirty-two")
        self._owned_clients = []
        self.backend = self._pooled_backend(backend, workers)
        self.workers = workers
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="report-evidence")
        self._lock = Lock()
        self._cache = OrderedDict()
        self._cache_size = 0
        self._pending = {}
        self.cache_failures = []
        self.cache_configured = cache_dir is not None
        self._disk_enabled = False
        self._disk = None
        if cache_dir is not None:
            try:
                self._disk = ArtifactCache(cache_dir)
                self._disk_enabled = True
            except (OSError, sqlite3.Error, ValueError) as exc:
                self.cache_failures.append(type(exc).__name__)

    def _pooled_backend(self, backend, workers):
        # Real EC2 backends are constructed by ModelingEvidence.from_ec2 using
        # boto3's normal cached credential chain. Create report-owned clients on
        # the main thread; do not mutate a shared client's TLS/config/hooks.
        from botocore.client import BaseClient
        if type(backend) is not ModelingEvidence or not isinstance(backend.repo.client, BaseClient):
            return backend
        if not isinstance(backend.store._s3, BaseClient):
            return backend
        import boto3
        from botocore.config import Config
        from ..core.artifacts import ArtifactStore
        from ..core.repository import MetadataRepository
        from copy import copy
        clients=[]
        for original in (backend.repo.client, backend.store._s3):
            client=boto3.client(original.meta.service_model.service_name,
                region_name=original.meta.region_name,endpoint_url=original.meta.endpoint_url,
                config=original.meta.config.merge(Config(max_pool_connections=workers)))
            clients.append(client)
        self._owned_clients=clients
        copied=copy(backend)
        copied.repo=MetadataRepository(clients[0],backend.repo.env,names=backend.repo.names,
            idempotency_ttl_days=backend.repo.idempotency_ttl_days,transaction_retries=backend.repo.transaction_retries)
        copied.store=ArtifactStore(clients[1],backend.store._buckets,kms_key_id=backend.store._kms_key_id,clock=backend.store._clock)
        return copied

    def __getattr__(self, key):
        return getattr(self.backend, key)

    def close(self):
        self._pool.shutdown(wait=True, cancel_futures=True)
        for client in self._owned_clients:
            client.close()
        if self._disk is not None:
            try:
                self._disk.close()
            except (OSError, sqlite3.Error):
                pass
        with self._lock:
            self._cache.clear()
            self._cache_size = 0

    def ordered_map(self, operation, values):
        """No unbounded executor.map queue: submit only one <=32-item batch."""
        iterator = iter(values)
        while True:
            batch = []
            for _ in range(self.workers):
                try:
                    batch.append(next(iterator))
                except StopIteration:
                    break
            if not batch:
                return
            futures = [self._pool.submit(operation, value) for value in batch]
            for future in futures:
                yield future.result()

    def read_artifact_bytes(self, ref):
        declared = ref.get("size_bytes")
        if declared is not None and (not isinstance(declared, int) or not 0 <= declared <= MAX_ARTIFACT_BYTES):
            raise ValueError("Captured artifact exceeds the 32 MiB report bound")
        if not str(ref.get("artifact_key", "")).startswith("modeling-evidence/v1/") or ref.get("bucket_role", "reports") != "reports":
            raise PlatformError.not_permitted("invalid modeling artifact reference")
        key = (ref.get("bucket_role", "reports"), ref.get("artifact_key"), ref.get("checksum"))
        # Per-report byte/entry bounded single-flight cache removes duplicate
        # execution receipt reads shared by the run and execution-day event.
        with self._lock:
            if key in self._cache:
                raw = self._cache.pop(key)
                self._cache[key] = raw
                if declared is not None and len(raw) != declared:
                    raise ValueError("Captured artifact size differs from immutable reference")
                return raw
            promise = self._pending.get(key)
            owner = promise is None
            if owner:
                promise = self._pending[key] = Future()
        if not owner:
            raw = promise.result()
            if declared is not None and len(raw) != declared:
                raise ValueError("Captured artifact size differs from immutable reference")
            return raw
        try:
            raw = None
            if self._disk_enabled:
                try:
                    raw = self._disk.get(ref.get("checksum"), max_size=MAX_ARTIFACT_BYTES)
                except (OSError, sqlite3.Error, ValueError) as exc:
                    self._disable_disk(exc)
            cache_hit = raw is not None
            if not cache_hit:
                raw = self.backend.read_artifact_bytes(ref)
            if len(raw) > MAX_ARTIFACT_BYTES or (declared is not None and len(raw) != declared):
                raise ValueError("Captured artifact size differs from immutable reference")
            if "sha256:" + hashlib.sha256(raw).hexdigest() != ref.get("checksum"):
                raise ValueError("Captured artifact checksum differs from immutable reference")
            if self._disk_enabled and not cache_hit:
                try:
                    self._disk.put(ref["checksum"], raw)
                except (OSError, sqlite3.Error, ValueError) as exc:
                    self._disable_disk(exc)
            with self._lock:
                if len(raw) <= CACHE_BYTES:
                    self._cache[key] = raw
                    self._cache_size += len(raw)
                    while self._cache_size > CACHE_BYTES or len(self._cache) > CACHE_ENTRIES:
                        _, evicted = self._cache.popitem(last=False)
                        self._cache_size -= len(evicted)
            promise.set_result(raw)
            return raw
        except BaseException as exc:
            promise.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def _disable_disk(self, error):
        with self._lock:
            self._disk_enabled = False
            code = type(error).__name__
            if code not in self.cache_failures:
                self.cache_failures.append(code)

    def io_status(self):
        return {"workers": self.workers, "maximum_submitted_reads": self.workers,
                "disk_cache_configured": self.cache_configured, "disk_cache_enabled": self._disk_enabled,
                "cache_failures": list(self.cache_failures), "mutable_heads_cached": False}

    def read_input(self, input_id):
        # One strong metadata read + one original-byte fetch, rather than the old
        # read_input followed by read_input_bytes duplicate Dynamo/S3 round trip.
        doc = self.backend.repo.require("research_input", input_id).doc
        raw = self.read_artifact_bytes(doc)
        return {"metadata": doc, "payload": json.loads(raw), "raw_bytes": raw}

    def read_run(self, run_id):
        record = self.backend.repo.require("research_run", run_id)
        doc = record.doc
        result = {**doc, **{k: record.attrs[k] for k in ("forecast_state", "forecast_target", "realization_ref") if k in record.attrs},
                  "outcome": json.loads(self.read_artifact_bytes(doc["output_ref"])) if doc.get("output_ref") else None,
                  "execution": json.loads(self.read_artifact_bytes(doc["execution_ref"])) if doc.get("execution_ref") else None}
        if record.attrs.get("realization_ref"):
            result["forecast_realization_ref"] = record.attrs["realization_ref"]
            result["forecast_realization"] = json.loads(self.read_artifact_bytes(record.attrs["realization_ref"]))
        if doc.get("execution_disposition_ref"):
            result["execution_disposition"] = json.loads(self.read_artifact_bytes(doc["execution_disposition_ref"]))
        return result

    def query_day(self, experiment_id, day, *, kind="runs", page_size=100, next_token=None):
        if kind not in KINDS or not 1 <= page_size <= 100 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise PlatformError.validation("invalid bounded modeling day query")
        table = KINDS[kind]
        scope = f"{table}:{experiment_id}#{day}"
        cursor = None
        if next_token:
            try:
                token = json.loads(base64.b64decode(next_token, altchars=b"-_", validate=True))
                if token["scope"] != scope:
                    raise ValueError()
                cursor = token["cursor"]
            except (ValueError, KeyError, TypeError):
                raise PlatformError.validation("modeling page token has wrong query scope") from None
        rows, token = self.backend.repo.query_index(table, "day-index", f"{experiment_id}#{day}", newest_first=False, limit=page_size, page_token=cursor)

        def expand(row):
            try:
                if table == "research_run":
                    return self.read_run(row.id)  # Keep the original strong read.
                if table == "portfolio_observation":
                    return {**row.doc, "payload": json.loads(self.read_artifact_bytes(row.doc))}
                return row.doc
            except (PlatformError, ValueError, KeyError) as exc:
                return {**row.doc, "outcome": None, "execution": None, "payload": None,
                        "evidence_error": {"code": getattr(exc, "code", "invalid_artifact"), "message": str(exc)}}

        values = list(self.ordered_map(expand, rows))
        encoded = base64.urlsafe_b64encode(json.dumps({"scope": scope, "cursor": token}).encode()).decode() if token else None
        return values, encoded


@contextmanager
def report_reader(reader):
    if isinstance(reader, ReportReader) or not isinstance(reader, ModelingEvidence):
        yield reader
        return
    cache_dir = os.environ.get("FINPLAN_REPORT_CACHE_DIR")
    optimized = ReportReader(reader, cache_dir=Path(cache_dir) if cache_dir else None)
    try:
        yield optimized
    finally:
        optimized.close()


def with_report_io(function):
    @wraps(function)
    def wrapped(reader, *args, **kwargs):
        with report_reader(reader) as scoped:
            return function(scoped, *args, **kwargs)
    return wrapped


def prefetched_inputs(reader, runs):
    def fetch(run):
        try:
            return run, reader.read_input(run.get("input_id"))
        except (PlatformError, ValueError, KeyError) as exc:
            return run, exc
    if isinstance(reader, ReportReader):
        yield from reader.ordered_map(fetch, runs)
    else:
        for run in runs:
            yield fetch(run)
