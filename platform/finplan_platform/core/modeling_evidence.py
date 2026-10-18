"""Beta-only experiment evidence, committed through the platform's authoritative stores.

S3 artifacts are immutable canonical JSON. A DynamoDB pointer is published only after
its bytes exist; failed transactions may leave harmless unreferenced artifacts. A
permanent run ID owns an experiment/approach/slot, independently of expiring request
idempotency. The only automated ledger method conditions on isolated-book ownership.
This backend interface is for the authorized EC2 operator, never a browser credential.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from finplan_contracts.canonical import canonicalize

from .artifacts import ArtifactStore, sha256_checksum
from .audit import audit_event
from .clock import FrozenClock
from .context import Caller, OperationContext
from .errors import PlatformError
from .ids import IdFactory
from .repository import Check, Cond, HeadMove, MetadataRepository, PutNew, Transition
from .contract_io import require_valid

FAMILIES = frozenset({"reinforcement_learning", "optimization", "language_model", "agentic", "control"})
TERMINAL = frozenset({"succeeded", "hold", "skipped", "failed", "unavailable", "reused"})
KINDS = {"runs": "research_run", "inputs": "research_input", "observations": "portfolio_observation"}
SECRET_KEYS = re.compile(r"^(authorization|password|token|credentials|api[_-]?token|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret[_-]?access[_-]?key|session[_-]?token|aws[_-]?credentials)$", re.I)


def _timestamp(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.utcoffset() is None:
            raise ValueError()
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        raise PlatformError.validation("a timezone-qualified timestamp is required") from None


def _slot(value: str) -> str:
    return _timestamp(value).isoformat().replace("+00:00", "Z")


def _index_time(value: str) -> str:
    return _timestamp(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _id(prefix: str, *parts: str) -> str:
    parts = tuple(_slot(p) if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})", p) else p for p in parts)
    # Not a public contract ULID: internal identifiers are deliberately namespaced.
    return prefix + "_" + hashlib.sha256(canonicalize(list(parts))).hexdigest()


def _public_id(prefix: str, *parts: str) -> str:
    # Deterministic valid ULID encoding for existing portfolio/decision contracts.
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    number = int.from_bytes(hashlib.sha256(canonicalize(list(parts))).digest()[:16], "big")
    chars = []
    for _ in range(26):
        chars.append(alphabet[number & 31]); number >>= 5
    return prefix + "_" + "".join(reversed(chars))


def _safe(payload: Any) -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if SECRET_KEYS.fullmatch(str(key)):
                raise PlatformError.validation("credential fields are forbidden in modeling evidence")
            _safe(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _safe(value)
    elif isinstance(payload, float) and not math.isfinite(payload):
        raise PlatformError.validation("non-finite evidence numbers are forbidden")
    elif isinstance(payload, str) and ("X-Amz-Signature=" in payload or "-----BEGIN PRIVATE KEY" in payload or re.search(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", payload, re.I)):
        raise PlatformError.validation("credentials or signed grants are forbidden in modeling evidence")


class ModelingEvidence:
    execution_events_supported = True

    def __init__(self, repo: MetadataRepository, store: ArtifactStore, *, principal: str = "authorized-beta-experiment-operator"):
        if repo.env != "beta":
            raise PlatformError.not_permitted("autonomous experiment evidence is beta only")
        self.repo, self.store, self.principal = repo, store, principal

    def _ctx(self, at: str) -> OperationContext:
        clock = FrozenClock(at)
        return OperationContext(caller=Caller(principal=self.principal, role_class="operator", channel="ec2_experiment"), env="beta", correlation_id="cor_" + IdFactory(clock).ulid(), clock=clock, ids=IdFactory(clock), trigger="scheduled", synthetic=True)

    @staticmethod
    def _keys(experiment_id: str, approach_id: str, at: str, rid: str) -> dict[str, str]:
        day = _timestamp(at).astimezone(ZoneInfo("America/New_York")).date().isoformat()
        return {"report_key": f"{experiment_id}#{day}", "approach_key": f"{experiment_id}#{approach_id}", "time_key": f"{_timestamp(at).isoformat()}#{rid}"}

    def _artifact(self, kind: str, rid: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        _safe(payload)
        raw = canonicalize(dict(payload))
        if len(raw) > 32 * 1024 * 1024:
            raise PlatformError.validation("modeling artifact exceeds 32 MiB bound")
        checksum = sha256_checksum(raw)
        key = f"modeling-evidence/v1/{kind}/{rid}/{checksum.split(':')[1]}.json"
        saved = self.store.put_once_or_verify("reports", key, raw)
        return {"artifact_key": key, "checksum": saved.checksum, "size_bytes": len(raw), "serialization": "RFC8785 JSON", "bucket_role": "reports"}

    def read_artifact_bytes(self, ref: Mapping[str, Any]) -> bytes:
        key = ref.get("artifact_key", "")
        if not key.startswith("modeling-evidence/v1/") or ref.get("bucket_role", "reports") != "reports":
            raise PlatformError.not_permitted("invalid modeling artifact reference")
        raw, _ = self.store.get("reports", key, expected_checksum=ref["checksum"])
        return raw

    @staticmethod
    def _same_capture(table: str, old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
        ignored = {"captured_at"} if table == "research_input" else {"observed_at", "portfolio_revision"} if table == "portfolio_observation" else set()
        return {k:v for k,v in old.items() if k not in ignored} == {k:v for k,v in new.items() if k not in ignored}

    def _put(self, table: str, doc: dict[str, Any], at: str) -> dict[str, Any]:
        _safe(doc)
        rid = doc[{"research_input": "input_id", "research_run": "run_id", "portfolio_observation": "observation_id"}[table]]
        old = self.repo.get(table, rid)
        if old:
            if not self._same_capture(table, old.doc, doc):
                raise PlatformError.immutable("permanent experiment record conflicts with retry", record_type=table)
            return old.doc
        ctx = self._ctx(at)
        try:
            self.repo.commit([PutNew(table, doc=doc, contract_version="1.12.0")], audit=[audit_event(ctx, record_id=rid, record_type=table, operation="capture_modeling_evidence", new={"status": doc.get("status", "captured")})])
        except PlatformError:
            old = self.repo.get(table, rid)
            if not old or not self._same_capture(table, old.doc, doc):
                raise
            return old.doc
        return doc

    def capture_shared_source(self, experiment_id: str, slot: str, payload: Mapping[str, Any], available_at: str) -> dict[str, Any]:
        return self.capture_input(experiment_id, "__shared__", slot, payload, None, available_at, kind="shared_source")

    def capture_input(self, experiment_id: str, approach_id: str, slot: str, payload: Mapping[str, Any], shared_source_ref: Mapping[str, Any] | None, captured_at: str, *, kind: str = "model_input") -> dict[str, Any]:
        """Call immediately BEFORE inference. Same slot can never acquire replacement input."""
        slot = _slot(slot); _timestamp(captured_at)
        cutoff = payload.get("availability_cutoff", payload.get("decision_time", slot))
        if _timestamp(cutoff) > _timestamp(captured_at):
            raise PlatformError.validation("input cutoff exceeds capture time")
        self._check_availability(payload, cutoff)
        if shared_source_ref:
            source = self.repo.require("research_input", shared_source_ref["input_id"]).doc
            if source["experiment_id"] != experiment_id or source["kind"] != "shared_source" or source["checksum"] != shared_source_ref["checksum"]:
                raise PlatformError.validation("shared source does not belong to this experiment")
            if "shared_source_checksum" in payload and payload["shared_source_checksum"] != source["checksum"]:
                raise PlatformError.validation("model input shared source checksum does not match its authoritative source")
        iid = _id("mi", experiment_id, approach_id, slot, kind)
        ref = self._artifact("inputs", iid, payload)
        existing = self.repo.get("research_input", iid)
        if existing:
            if existing.doc["checksum"] != ref["checksum"] or existing.doc.get("shared_source_ref") != (dict(shared_source_ref) if shared_source_ref else None):
                raise PlatformError.immutable("permanent experiment record conflicts with retry", record_type="research_input")
            return existing.doc
        doc = {"schema_version": "modeling-evidence/1", "environment": "beta", "synthetic": True, "input_id": iid, "experiment_id": experiment_id, "approach_id": approach_id, "slot": slot, "captured_at": captured_at, "availability_cutoff": cutoff, "kind": kind, "shared_source_ref": dict(shared_source_ref) if shared_source_ref else None, **ref, **self._keys(experiment_id, approach_id, slot, iid)}
        return self._put("research_input", doc, captured_at)

    @staticmethod
    def _check_availability(payload: Any, cutoff: str) -> None:
        if isinstance(payload, Mapping):
            # All actual source availability fields, including nested eligible news/prices.
            for key, value in payload.items():
                if key in {"available_at", "platform_available_at", "availability_at"} and value and _timestamp(value) > _timestamp(cutoff):
                    raise PlatformError.validation("future-available source in model input")
                ModelingEvidence._check_availability(value, cutoff)
        elif isinstance(payload, list):
            for value in payload:
                ModelingEvidence._check_availability(value, cutoff)

    def read_input_bytes(self, input_id: str) -> bytes:
        return self.read_artifact_bytes(self.repo.require("research_input", input_id).doc)

    def read_input(self, input_id: str) -> dict[str, Any]:
        doc = self.repo.require("research_input", input_id).doc
        return {"metadata": doc, "payload": json.loads(self.read_artifact_bytes(doc))}

    def start_run(self, experiment_id: str, approach_id: str, slot: str, input_id: str, portfolio_id: str, started_at: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        slot = _slot(slot); started_at = _slot(started_at)
        supplied = self.repo.require("research_input", input_id).doc
        if (supplied["experiment_id"], supplied["approach_id"], supplied["slot"]) != (experiment_id, approach_id, slot):
            raise PlatformError.validation("captured input/run association mismatch")
        book = self.get_portfolio(portfolio_id)
        if (book["experiment_id"], book["approach_id"]) != (experiment_id, approach_id):
            raise PlatformError.not_permitted("experiment does not own the isolated portfolio")
        metadata = dict(metadata or {})
        if metadata.get("family") and metadata["family"] not in FAMILIES:
            raise PlatformError.validation("unsupported honest model family")
        rid = _id("mr", experiment_id, approach_id, slot)
        old = self.repo.get("research_run", rid)
        if old:
            if old.doc["input_id"] != input_id or old.doc["portfolio_id"] != portfolio_id:
                raise PlatformError.immutable("permanent slot already belongs to another invocation")
            return old.doc
        doc = {"schema_version": "modeling-evidence/1", "environment": "beta", "synthetic": True, "run_id": rid, "experiment_id": experiment_id, "approach_id": approach_id, "slot": slot, "input_id": input_id, "input_checksum": supplied["checksum"], "portfolio_id": portfolio_id, "portfolio_revision": book["revision"], "started_at": started_at, "status": "running", "metadata": metadata, **self._keys(experiment_id, approach_id, slot, rid)}
        return self._put("research_run", doc, started_at)

    def claim_run(self, run_id: str, owner: str, at: str, *, lease_seconds: int = 900) -> bool:
        """Claim one model invocation, or recover an expired claim with the SAME input.

        A crash cannot replace the original captured payload. A recovered invocation
        may use the existing payload bytes; the run's permanent output and execution
        guards still prevent duplicate publication or fills. The lease is bounded and
        every successful recovery records a new append-only audit attempt.
        """
        if not owner or not 1 <= lease_seconds <= 1800:
            raise PlatformError.validation("invocation lease requires owner and 1..1800 seconds")
        old = self.repo.require("research_run", run_id)
        now = int(_timestamp(at).timestamp())
        if old.doc["status"] != "running" or old.attrs.get("lease_until", 0) > now:
            return False
        revision = int(old.attrs.get("invocation_revision", 0))
        lease_condition = Cond.eq("status", "running") & (Cond.not_exists("lease_until") | Cond("cmp", "lease_until", "<=", now))
        try:
            self.repo.commit([HeadMove("research_run", record_id=run_id, expected_revision=revision, revision_attr="invocation_revision", allow_missing_revision=(revision == 0), set_attrs={"lease_owner": owner, "lease_until": now + lease_seconds}, additional_condition=lease_condition)], audit=[audit_event(self._ctx(at), record_id=run_id, record_type="research_run", operation="claim_model_invocation", new={"attempt": revision + 1, "owner": owner, "lease_until": now + lease_seconds})])
            return True
        except PlatformError as exc:
            if exc.code in {"CONFLICT", "PRECONDITION_FAILED"}:
                return False
            raise

    def finish_run(self, run_id: str, outcome: Mapping[str, Any], finished_at: str) -> dict[str, Any]:
        if outcome.get("status") not in TERMINAL:
            raise PlatformError.validation("run outcome requires an explicit terminal status")
        old = self.repo.require("research_run", run_id).doc
        forecast = outcome.get("forecast")
        if forecast and forecast.get("available", True) is not False:
            target = forecast.get("target_at", forecast.get("target_timestamp"))
            if not target or _timestamp(target) <= _timestamp(old["started_at"]):
                raise PlatformError.validation("forecast requires a future declared target timestamp")
            value = forecast.get("expected_value")
            baseline = forecast.get("baseline_value")
            if forecast.get("units") != "USD" or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in (value, baseline)):
                raise PlatformError.validation("forecast requires finite USD expected/baseline values")
        ref = self._artifact("outputs", run_id, outcome)
        if old["status"] != "running":
            if old.get("output_ref", {}).get("checksum") != ref["checksum"]:
                raise PlatformError.immutable("completed invocation output cannot change")
            return old
        new = {**old, "status": outcome["status"], "finished_at": finished_at, "output_ref": ref}
        forecast_heads = {"forecast_revision": 0, "forecast_state": "pending", "forecast_partition": old["experiment_id"] + "#pending", "forecast_target": _index_time(forecast.get("target_at", forecast.get("target_timestamp")))} if forecast and forecast.get("available", True) is not False else {}
        self.repo.commit([Transition("research_run", record_id=run_id, old_doc=old, new_doc=new, extra_attrs=forecast_heads)], audit=[audit_event(self._ctx(finished_at), record_id=run_id, record_type="research_run", operation="finish_modeling_run", prior={"status": "running"}, new={"status": new["status"]})])
        return new

    def read_run(self, run_id: str) -> dict[str, Any]:
        record = self.repo.require("research_run", run_id)
        doc = record.doc
        result = {**doc, **{k:record.attrs[k] for k in ("forecast_state", "forecast_target", "realization_ref") if k in record.attrs}, "outcome": json.loads(self.read_artifact_bytes(doc["output_ref"])) if doc.get("output_ref") else None, "execution": json.loads(self.read_artifact_bytes(doc["execution_ref"])) if doc.get("execution_ref") else None}
        if record.attrs.get("realization_ref"):
            result["forecast_realization_ref"] = record.attrs["realization_ref"]
            result["forecast_realization"] = json.loads(self.read_artifact_bytes(record.attrs["realization_ref"]))
        if doc.get("execution_disposition_ref"):
            result["execution_disposition"] = json.loads(self.read_artifact_bytes(doc["execution_disposition_ref"]))
        return result

    @staticmethod
    def _run_header(record) -> dict[str, Any]:
        # Metadata only. In particular, do not read output/input/forecast artifacts
        # merely to decide which old policy is still eligible for execution.
        fields = ("invocation_revision", "lease_owner", "lease_until", "forecast_state", "forecast_target", "realization_ref", "execution_frontier_revision", "execution_frontier_ref")
        return {**record.doc, **{key: record.attrs[key] for key in fields if key in record.attrs}}

    def read_run_header(self, run_id: str) -> dict[str, Any]:
        """One strongly consistent metadata read, without any S3 expansion."""
        return self._run_header(self.repo.require("research_run", run_id))

    def query_run_headers(self, experiment_id: str, approach_id: str, before_slot: str, *, page_size: int = 100, next_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        """Bounded newest-first run metadata strictly before a decision slot.

        The existing GSI is eventually consistent. Call read_run_header on a
        selected candidate/barrier/lease before acting; mutations independently
        recheck affected heads and conditionally commit. This method performs no
        per-row GET or artifact read, so historical model tensors are not fetched.
        """
        if not 1 <= page_size <= 100:
            raise PlatformError.validation("run header page must contain 1..100 rows")
        before_slot = _slot(before_slot)
        partition = f"{experiment_id}#{approach_id}"
        scope = f"research_run:approach-index:{partition}:{before_slot}"
        cursor = None
        if next_token:
            try:
                token = json.loads(base64.urlsafe_b64decode(next_token))
                if token["scope"] != scope:
                    raise ValueError()
                cursor = token["cursor"]
            except (ValueError, KeyError, TypeError):
                raise PlatformError.validation("run header token has wrong query scope") from None
        # _keys uses UTC isoformat (+00:00), not the Z-form observation-time key.
        # Omitting '#run_id' excludes every run exactly at before_slot.
        rows, cursor = self.repo.query_index("research_run", "approach-index", partition, newest_first=True, limit=page_size, page_token=cursor, sort_upper_bound=_timestamp(before_slot).isoformat())
        encoded = base64.urlsafe_b64encode(json.dumps({"scope": scope, "cursor": cursor}).encode()).decode() if cursor else None
        return [self._run_header(row) for row in rows], encoded

    def mark_execution_superseded(self, run_id: str, replacement_run_id: str, recorded_at: str) -> dict[str, Any]:
        """Permanently retire an older policy without creating any paper execution.

        Only the run status/disposition pointer, immutable ancillary observation,
        and audit change. Original inference/input/forecast bytes and portfolio
        state/history remain untouched. The status CAS makes execution and
        supersession mutually exclusive. Callers cleaning multiple old policies
        must work oldest first and stop on failure before using a terminal header
        as proof that every older pending policy has already been disposed.
        """
        recorded_at = _slot(recorded_at)
        old = self.repo.require("research_run", run_id).doc
        if old.get("execution_disposition_ref"):
            return json.loads(self.read_artifact_bytes(old["execution_disposition_ref"]))
        eligible = {"succeeded", "hold", "reused", "pending_execution"}
        if old.get("execution_ref") or old["status"] not in eligible:
            raise PlatformError.precondition("only an unexecuted completed policy can be superseded")
        replacement = self.repo.require("research_run", replacement_run_id).doc
        identity = ("experiment_id", "approach_id", "portfolio_id")
        if any(old[field] != replacement[field] for field in identity) or _timestamp(replacement["slot"]) <= _timestamp(old["slot"]):
            raise PlatformError.validation("replacement policy must belong to the same approach/book at a later slot")
        if replacement["status"] not in eligible | {"applied", "no_op"} or replacement.get("execution_disposition_ref") or not replacement.get("output_ref"):
            raise PlatformError.precondition("replacement must be a genuine completed policy")
        if _timestamp(replacement.get("finished_at", replacement["started_at"])) > _timestamp(recorded_at):
            raise PlatformError.validation("replacement policy was not available at disposition time")
        outcome = json.loads(self.read_artifact_bytes(replacement["output_ref"]))
        if outcome.get("status") not in {"succeeded", "hold", "reused"} or "target_weights" not in outcome or "cash_weight" not in outcome:
            raise PlatformError.precondition("replacement has no validated allocation policy")
        book = self.get_portfolio(old["portfolio_id"])
        if (book["experiment_id"], book["approach_id"]) != (old["experiment_id"], old["approach_id"]):
            raise PlatformError.not_permitted("run does not own its isolated synthetic book")
        weights = outcome["target_weights"]
        if isinstance(weights, list):
            if any(not isinstance(row, dict) or "instrument_id" not in row or "weight" not in row for row in weights) or len({row["instrument_id"] for row in weights}) != len(weights):
                raise PlatformError.precondition("replacement has an invalid allocation policy")
            weights = {row["instrument_id"]: row["weight"] for row in weights}
        cash_weight = outcome["cash_weight"]
        values = [*weights.values(), cash_weight] if isinstance(weights, dict) else []
        if not isinstance(weights, dict) or not set(weights) <= set(book["universe"]) or not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1 for value in values) or not math.isclose(sum(values), 1, rel_tol=1e-9, abs_tol=1e-8):
            raise PlatformError.precondition("replacement has an invalid allocation policy")
        payload = {"schema_version": "modeling-execution-disposition/1", "status": "superseded", "run_id": run_id, "input_id": old["input_id"], "experiment_id": old["experiment_id"], "approach_id": old["approach_id"], "portfolio_id": old["portfolio_id"], "decision_slot": old["slot"], "replacement_run_id": replacement_run_id, "replacement_input_id": replacement["input_id"], "replacement_slot": replacement["slot"], "reason": "A newer completed policy supersedes this pending policy before any eligible execution", "recorded_at": recorded_at, "ledger_committed": False, "simulated_fills": []}
        ref = self._artifact("execution-dispositions", run_id, payload)
        new = {**old, "status": "superseded", "execution_disposition_state": "superseded", "execution_disposition_ref": ref}
        oid = _id("mo", run_id, "execution_disposition")
        event = {"schema_version": "modeling-evidence/1", "environment": "beta", "synthetic": True, "observation_id": oid, "kind": "execution_disposition", "experiment_id": old["experiment_id"], "approach_id": old["approach_id"], "portfolio_id": old["portfolio_id"], "portfolio_revision": old["portfolio_revision"], "run_id": run_id, "input_id": old["input_id"], "slot": recorded_at, "decision_slot": old["slot"], "observed_at": recorded_at, "observed_key": _index_time(recorded_at) + "#" + oid, **ref, **self._keys(old["experiment_id"], old["approach_id"], recorded_at, oid)}
        try:
            self.repo.commit([Transition("research_run", record_id=run_id, old_doc=old, new_doc=new), Check("research_run", record_id=replacement_run_id, condition=Cond.eq("status", replacement["status"])), PutNew("portfolio_observation", doc=event)], audit=[audit_event(self._ctx(recorded_at), record_id=run_id, record_type="research_run", operation="supersede_pending_experiment_policy", prior={"status": old["status"]}, new={"status": "superseded"}, replacement_run_id=replacement_run_id)])
        except PlatformError:
            current = self.repo.require("research_run", run_id).doc
            if not current.get("execution_disposition_ref"):
                raise
            return json.loads(self.read_artifact_bytes(current["execution_disposition_ref"]))
        return payload

    def read_execution_frontier(self, run_id: str) -> dict[str, Any] | None:
        """Strongly read a small cleanup proof, never the model input/output."""
        header = self.read_run_header(run_id)
        ref = header.get("execution_frontier_ref")
        if not ref:
            return None
        payload = json.loads(self.read_artifact_bytes(ref))
        if payload.get("frontier_run_id") != run_id or any(payload.get(field) != header[field] for field in ("experiment_id", "approach_id", "portfolio_id")) or _timestamp(payload["covered_through_slot"]) != _timestamp(header["slot"]):
            raise PlatformError.validation("execution frontier proof belongs to another run/book")
        return payload

    def record_execution_frontier(self, frontier_run_id: str, pending_run_id: str | None, recorded_at: str, *, previous_frontier_run_id: str | None = None) -> dict[str, Any]:
        """Checkpoint all older execution candidates except one explicit pending run.

        The first proof checks bounded metadata history; later proofs check only
        the interval since a verified previous frontier, plus its pending pointer.
        No model artifacts, forecasts, ledger state or observations are rewritten.
        Call only after oldest-first cleanup succeeds. Any unresolved extra run,
        running lease, corrupt prior proof or page overflow prevents advancement.
        A caller must follow the retained pending pointer on subsequent ticks,
        even if the newest many runs have failed/unavailable outcomes.
        """
        recorded_at = _slot(recorded_at)
        frontier = self.read_run_header(frontier_run_id)
        existing = self.read_execution_frontier(frontier_run_id)
        if existing:
            return existing
        identity = ("experiment_id", "approach_id", "portfolio_id")
        if _timestamp(frontier["started_at"]) > _timestamp(recorded_at):
            raise PlatformError.validation("execution frontier is not yet available")
        book = self.get_portfolio(frontier["portfolio_id"])
        if (book["experiment_id"], book["approach_id"]) != (frontier["experiment_id"], frontier["approach_id"]):
            raise PlatformError.not_permitted("frontier does not own an isolated synthetic book")
        eligible = {"succeeded", "hold", "reused", "pending_execution"}
        checked = {}

        def check(header):
            if any(header[field] != frontier[field] for field in identity) or _timestamp(header["slot"]) > _timestamp(frontier["slot"]):
                raise PlatformError.validation("execution frontier candidate belongs to another scope")
            if header["status"] == "running":
                raise PlatformError.precondition("execution frontier cannot conceal a running invocation")
            unresolved = header["status"] in eligible and not header.get("execution_ref") and not header.get("execution_disposition_ref")
            if unresolved and header["run_id"] != pending_run_id:
                raise PlatformError.precondition("execution frontier has another unresolved policy", run_id=header["run_id"])
            checked[header["run_id"]] = {"run_id": header["run_id"], "slot": header["slot"], "status": header["status"], "execution_checksum": (header.get("execution_ref") or {}).get("checksum"), "disposition_checksum": (header.get("execution_disposition_ref") or {}).get("checksum")}
            return unresolved

        if pending_run_id:
            pending = self.read_run_header(pending_run_id)
            if not check(pending) or not pending.get("output_ref"):
                raise PlatformError.precondition("frontier pending pointer must name an unexecuted completed policy")
        check(frontier)
        previous = None
        previous_ref = None
        if previous_frontier_run_id:
            previous = self.read_run_header(previous_frontier_run_id)
            prior = self.read_execution_frontier(previous_frontier_run_id)
            if not prior or any(previous[field] != frontier[field] for field in identity) or _timestamp(previous["slot"]) >= _timestamp(frontier["slot"]) or _timestamp(prior["recorded_at"]) > _timestamp(recorded_at):
                raise PlatformError.validation("previous execution frontier is missing or belongs to another scope/time")
            previous_ref = {"frontier_run_id": previous_frontier_run_id, **previous["execution_frontier_ref"]}
            if prior.get("pending_run_id"):
                # A failed streak can retain an old recommendation outside the
                # newly scanned interval. It cannot silently expire or disappear.
                check(self.read_run_header(prior["pending_run_id"]))
        cursor = None
        partition = f"{frontier['experiment_id']}#{frontier['approach_id']}"
        for _ in range(100):
            rows, cursor = self.repo.query_index("research_run", "approach-index", partition, newest_first=False, limit=100, page_token=cursor, sort_lower_bound=previous["time_key"] if previous else None, sort_upper_bound=frontier["time_key"])
            for row in rows:
                if previous and row.id == previous_frontier_run_id:
                    continue
                header = self._run_header(row)
                # Terminal failures are immutable. A projected running or usable
                # policy can have moved since GSI propagation; refresh only it.
                if header["status"] == "running" or header["status"] in eligible:
                    header = self.read_run_header(row.id)
                check(header)
            if not cursor:
                break
        if cursor:
            raise PlatformError.precondition("execution frontier metadata exceeds bounded 10000 rows; cleanup checkpoint not advanced")
        payload = {"schema_version": "modeling-execution-frontier/1", "frontier_run_id": frontier_run_id, "experiment_id": frontier["experiment_id"], "approach_id": frontier["approach_id"], "portfolio_id": frontier["portfolio_id"], "covered_through_slot": frontier["slot"], "pending_run_id": pending_run_id, "previous_frontier_ref": previous_ref, "checked_headers": sorted(checked.values(), key=lambda row: (_timestamp(row["slot"]), row["run_id"])), "recorded_at": recorded_at, "ledger_committed": False, "semantics": "Every older execution candidate is resolved except the explicit pending pointer; original inference and forecasts remain unchanged"}
        ref = self._artifact("execution-frontiers", frontier_run_id, payload)
        try:
            self.repo.commit([HeadMove("research_run", record_id=frontier_run_id, expected_revision=0, revision_attr="execution_frontier_revision", allow_missing_revision=True, set_attrs={"execution_frontier_ref": ref}, additional_condition=Cond.not_exists("execution_frontier_ref"))], audit=[audit_event(self._ctx(recorded_at), record_id=frontier_run_id, record_type="research_run", operation="checkpoint_execution_cleanup", new={"pending_run_id": pending_run_id, "covered_through_slot": frontier["slot"]})])
        except PlatformError:
            existing = self.read_execution_frontier(frontier_run_id)
            if not existing:
                raise
            return existing
        return payload

    def query_pending_forecasts(self, experiment_id: str, through: str, *, page_size: int = 100, next_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        """Due unmatched forecasts independent of decision age; no historic table scan."""
        if not 1 <= page_size <= 100:
            raise PlatformError.validation("pending forecast page must contain 1..100 rows")
        if next_token:
            from .repository import decode_page_token
            cursor = decode_page_token(next_token)
            if cursor.get("forecast_partition", {}).get("S") != experiment_id + "#pending":
                raise PlatformError.validation("pending forecast token has wrong experiment")
        rows, token = self.repo.query_index("research_run", "pending-forecast-index", experiment_id + "#pending", newest_first=False, limit=page_size, page_token=next_token, sort_upper_bound=_index_time(through))
        # GSI propagation may lag a realized head: strong reads prevent double matching.
        return [self.read_run(r.id) for r in rows if self.repo.require("research_run", r.id).attrs.get("forecast_state") == "pending"], token

    def query_observations_after(self, experiment_id: str, approach_id: str, start_at: str, through: str, *, page_size: int = 100, next_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        """Original observations in actual observation-time order, independent of slot/day."""
        if not 1 <= page_size <= 100 or _timestamp(start_at) > _timestamp(through):
            raise PlatformError.validation("invalid bounded observation time range")
        partition = f"{experiment_id}#{approach_id}"
        if next_token:
            from .repository import decode_page_token
            if decode_page_token(next_token).get("approach_key", {}).get("S") != partition:
                raise PlatformError.validation("observation cursor belongs to another approach")
        rows, token = self.repo.query_index("portfolio_observation", "observation-time-index", partition, newest_first=False, limit=page_size, page_token=next_token, sort_lower_bound=_index_time(start_at) + "#", sort_upper_bound=_index_time(through) + "#~")
        return [{**r.doc, "payload": json.loads(self.read_artifact_bytes(r.doc))} for r in rows], token

    def mark_forecast_realized(self, run_id: str, observation_id: str, recorded_at: str) -> dict[str, Any]:
        """Link an immutable forecast match exactly once, without changing its forecast."""
        record = self.repo.require("research_run", run_id)
        match = self.repo.require("portfolio_observation", observation_id).doc
        payload = json.loads(self.read_artifact_bytes(match))
        outcome = self.read_run(run_id)["outcome"]
        forecast = (outcome or {}).get("forecast") or {}
        if match.get("kind") != "forecast_match" or payload.get("run_id") != run_id or payload.get("forecast_checksum") != forecast.get("forecast_checksum") or match["portfolio_id"] != record.doc["portfolio_id"]:
            raise PlatformError.validation("realization does not match immutable forecast/book")
        actual = self.repo.require("portfolio_observation", payload.get("observation_id", "")).doc
        actual_payload = json.loads(self.read_artifact_bytes(actual))
        target = forecast.get("target_at", forecast.get("target_timestamp"))
        if actual["portfolio_id"] != record.doc["portfolio_id"] or actual_payload.get("observation_kind") != "valid" or actual_payload.get("quote_status") != "fresh" or _timestamp(actual_payload["observed_at"]) < _timestamp(target) or _timestamp(actual_payload.get("value_as_of", actual_payload["observed_at"])) < _timestamp(target) or _timestamp(actual_payload.get("value_as_of", actual_payload["observed_at"])) > _timestamp(actual_payload["observed_at"]):
            raise PlatformError.validation("forecast match requires a valid fresh post-horizon book observation")
        realized = actual_payload["value"]
        flows = actual_payload.get("net_external_cash_flows_since_decision", 0)
        error = realized - flows - forecast["expected_value"]
        if payload.get("observed_at") != actual_payload["observed_at"] or any(not math.isclose(payload.get(k, float("inf")), v, rel_tol=1e-10, abs_tol=1e-8) for k,v in {"realized_value": realized, "signed_error": error, "absolute_error": abs(error), "squared_error": error*error}.items()):
            raise PlatformError.validation("forecast realization/error differs from authoritative observation")
        ref = {"observation_id": observation_id, "artifact_key": match["artifact_key"], "checksum": match["checksum"]}
        if record.attrs.get("forecast_state") == "realized":
            if record.attrs.get("realization_ref") != ref:
                raise PlatformError.immutable("forecast already matched another observation")
            return ref
        self.repo.commit([HeadMove("research_run", record_id=run_id, expected_revision=int(record.attrs.get("forecast_revision", 0)), revision_attr="forecast_revision", set_attrs={"forecast_state": "realized", "forecast_partition": record.doc["experiment_id"] + "#realized", "realization_ref": ref}, additional_condition=Cond.eq("forecast_state", "pending"))], audit=[audit_event(self._ctx(recorded_at), record_id=run_id, record_type="research_run", operation="reconcile_frozen_forecast", new={"forecast_state": "realized"}, observation_id=observation_id)])
        return ref

    def register_approaches(self, experiment_id: str, approaches: list[dict[str, Any]], policy: Mapping[str, Any], recorded_at: str) -> dict[str, Any]:
        ids = [a["approach_id"] for a in approaches]
        if len(ids) != len(set(ids)) or any(a.get("family") not in FAMILIES for a in approaches):
            raise PlatformError.validation("registry requires distinct approaches and honest families")
        # Versioned immutable registry; pointer lookup is a bounded experiment index query.
        payload = {"approaches": approaches, "comparison_policy": dict(policy), "recorded_at": recorded_at}
        return self.capture_input(experiment_id, "__registry__", recorded_at, payload, None, recorded_at, kind="registry")

    def read_registry(self, experiment_id: str) -> dict[str, Any] | None:
        rows, _ = self.repo.query_index("research_input", "approach-index", f"{experiment_id}#__registry__", limit=1)
        return self.read_input(rows[0].id)["payload"] if rows else None

    def ensure_portfolio(self, experiment_id: str, approach_id: str, initial_capital: float, universe: list[str], created_at: str) -> dict[str, Any]:
        created_at = _slot(created_at)
        if not math.isfinite(initial_capital) or initial_capital <= 0 or not universe or len(universe) != len(set(universe)):
            raise PlatformError.validation("isolated book requires positive capital and distinct investable universe")
        pid = _public_id("pf", experiment_id, approach_id)
        old = self.repo.get("portfolio", pid)
        if old:
            book = self.get_portfolio(pid)
            if book["initial_capital"] != initial_capital or book["universe"] != universe:
                raise PlatformError.immutable("isolated portfolio comparison policy cannot change")
            return book
        doc = {"portfolio_id": pid, "name": f"{experiment_id}: {approach_id}"[:200], "base_currency": "USD", "synthetic": True, "revision": 1, "created_at": created_at, "settings": {"isolated_experiment": True, "experiment_id": experiment_id, "approach_id": approach_id, "initial_capital": initial_capital, "universe": list(universe)}, "paper_state_revision": 1, "paper_state": {"cash_balance": initial_capital, "positions": [], "high_watermark": initial_capital, "as_of": _timestamp(created_at).date().isoformat(), "base_currency": "USD", "mode": "paper", "source": "paper_initialization"}}
        state = doc["paper_state"]
        history_id = f"{pid}:{1:020d}"
        ref = self._artifact("history", history_id, {"portfolio_id": pid, "revision": 1, "paper_state": state, "recorded_at": created_at, "reason": "isolated_experiment_baseline", "synthetic": True, "contract_version": "1.12.0"})
        ctx = self._ctx(created_at)
        try:
            self.repo.commit([PutNew("portfolio", doc={k:v for k,v in doc.items() if k not in {"paper_state", "paper_state_revision"}}, extra_attrs={"paper_state": state, "paper_state_revision": 1}, contract_version="1.12.0"), PutNew("portfolio_history", doc={"history_id": history_id, "portfolio_id": pid, "revision": 1, **ref})], audit=[audit_event(ctx, record_id=pid, record_type="portfolio", operation="create_isolated_experiment_book", new={"revision": 1})])
        except PlatformError:
            if not self.repo.get("portfolio", pid):
                raise
        return self.get_portfolio(pid)

    def get_portfolio(self, portfolio_id: str) -> dict[str, Any]:
        rec = self.repo.require("portfolio", portfolio_id)
        settings = rec.doc.get("settings", {})
        if rec.doc.get("synthetic") is not True or settings.get("isolated_experiment") is not True:
            raise PlatformError.not_permitted("automatic experiment path requires an isolated synthetic book")
        return {"portfolio_id": portfolio_id, "revision": rec.attrs["paper_state_revision"], "paper_state": copy.deepcopy(rec.attrs["paper_state"]), **settings}

    def _native_input_identity(self, source, shared_ref, run, book):
        """Verify an already captured native source; never manufacture a snapshot ID."""
        native = source.get("native_crypto_dataset")
        if not isinstance(native, dict) or not shared_ref:
            raise PlatformError.precondition("paper ledger requires approved snapshot or captured supported native input")
        require_valid(native, "api/crypto-dataset-response")
        shared = self.repo.require("research_input", shared_ref["input_id"]).doc
        if shared["kind"] != "shared_source" or shared["experiment_id"] != run["experiment_id"] or shared["checksum"] != shared_ref["checksum"]:
            raise PlatformError.precondition("native source reference does not match immutable shared input")
        universe = ["BTC-USD", "ETH-USD"]
        if native["dataset_id"] != "coinbase-crypto-hourly-v1" or native["products"] != universe or source.get("investable_universe") != universe or book["universe"] != universe or native["granularity_seconds"] != 3600 or source.get("checksum") != native["dataset_checksum"]:
            raise PlatformError.precondition("native crypto dataset identity, universe or frequency is incompatible")
        as_of, available, cutoff, started = map(_timestamp, (native["as_of"], native["available_at"], shared["availability_cutoff"], run["started_at"]))
        if not as_of <= available <= cutoff <= started or as_of != _timestamp(run["slot"]) or not 0 <= (cutoff-as_of).total_seconds() <= 1200:
            raise PlatformError.precondition("native crypto input is stale, future available or mismatched to its slot")
        controlled = source.get("trigger") == "controlled_replay"
        if native["market_data_synthetic"] and not controlled:
            raise PlatformError.precondition("synthetic native prices cannot be forward evidence")
        if not controlled:
            provenance = native["sources"]
            if len(provenance) != 2 or {p.get("product") for p in provenance} != set(universe) or any(p.get("provider") != "coinbase_exchange_public" or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(p.get("response_checksum", ""))) or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(p.get("frozen_source_checksum", ""))) for p in provenance):
                raise PlatformError.precondition("native crypto provider provenance is unavailable")
        original_times = None
        for instrument in universe:
            rows = native["bars"][instrument]
            times = [_timestamp(row["start"]) for row in rows]
            if any(t.minute or t.second or t.microsecond for t in times) or times[-1] + timedelta(hours=1) != as_of or any(b-a != timedelta(hours=1) for a,b in zip(times,times[1:])) or (original_times is not None and times != original_times):
                raise PlatformError.precondition("native crypto source candles are unaligned or incomplete")
            if any(not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"] for row in rows):
                raise PlatformError.precondition("native crypto source prices are inconsistent")
            original_times = times
        identity = {"input_id": shared["input_id"], "checksum": shared["checksum"], "source_type": "platform_crypto_hourly", "dataset_id": native["dataset_id"], "dataset_checksum": native["dataset_checksum"], "as_of": _slot(native["as_of"]), "available_at": _slot(native["available_at"]), "granularity_seconds": 3600, "universe": universe, "market_data_synthetic": native["market_data_synthetic"]}
        require_valid(identity, "native-input-ref")
        return identity

    def commit_paper_step(self, run_id: str, expected_revision: int, state: Mapping[str, Any], fills: list[dict[str, Any]], execution: Mapping[str, Any], recorded_at: str) -> dict[str, Any]:
        """Commit common-evaluator output, NEVER issue orders or touch the user's book.

        Caller supplies evaluator identity, eligible quote time/provenance and explicit
        session validation. All fills must reconcile cost and cash before publication.
        A permanently committed run cannot produce a second revision on any retry.
        """
        recorded_at = _slot(recorded_at)
        old = self.repo.require("research_run", run_id).doc
        if old.get("execution_ref"):
            return json.loads(self.read_artifact_bytes(old["execution_ref"]))
        if old["status"] not in {"succeeded", "hold", "reused", "pending_execution"}:
            raise PlatformError.precondition("run has no applicable recommendation")
        book = self.get_portfolio(old["portfolio_id"])
        if (book["experiment_id"], book["approach_id"]) != (old["experiment_id"], old["approach_id"]):
            raise PlatformError.not_permitted("run does not own isolated book")
        if expected_revision != old["portfolio_revision"] or expected_revision != book["revision"]:
            raise PlatformError.conflict("isolated paper holdings revision changed")
        _safe(state); _safe(fills); _safe(execution)
        require_valid(dict(state), "paper-portfolio-state")
        cash = state.get("cash_balance")
        if isinstance(cash, bool) or not isinstance(cash, (int, float)) or not math.isfinite(cash) or cash < -1e-7:
            raise PlatformError.validation("paper cash must remain finite and nonnegative")
        if not fills:
            prior = {p["instrument_id"]: p["quantity"] for p in book["paper_state"]["positions"]}
            proposed = {p["instrument_id"]: p["quantity"] for p in state["positions"]}
            if not math.isclose(cash, book["paper_state"]["cash_balance"], rel_tol=1e-12, abs_tol=1e-7) or any(not math.isclose(prior.get(i, 0), proposed.get(i, 0), rel_tol=1e-12, abs_tol=1e-10) for i in prior.keys() | proposed.keys()) or execution.get("transaction_cost", 0) != 0:
                raise PlatformError.validation("no-op paper step cannot change cash or positions without fills")
        if fills and (execution.get("market_session_valid") is not True or not execution.get("evaluator_version") or not execution.get("quote_provenance")):
            raise PlatformError.precondition("fills require validated session, evaluator and eligible quote provenance")
        if fills:
            quote_at = execution.get("execution_at")
            if not quote_at or _timestamp(quote_at) < _timestamp(old["started_at"]) or _timestamp(quote_at) > _timestamp(recorded_at):
                raise PlatformError.precondition("paper fill time is not eligible for this decision")
            total_cost = execution.get("transaction_cost", 0)
            before_value, after_value = execution.get("before_value"), execution.get("after_value")
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (total_cost, before_value, after_value)) or total_cost < 0 or not math.isclose(before_value, after_value + total_cost, rel_tol=1e-8, abs_tol=1e-6):
                raise PlatformError.validation("paper evaluator wealth/cost conservation failed")
        status = "applied" if fills else "no_op"
        did = _public_id("pd", run_id)
        captured = self.read_input(old["input_id"])
        shared_ref = captured["metadata"].get("shared_source_ref")
        source = self.read_input(shared_ref["input_id"])["payload"] if shared_ref else captured["payload"]
        snapshot_id = source.get("input_snapshot_id")
        native_ref = self._native_input_identity(source, shared_ref, old, book) if snapshot_id is None else None
        native_fields = {"native_input_ref": native_ref} if native_ref is not None else {}
        outcome = json.loads(self.read_artifact_bytes(old["output_ref"]))
        targets = outcome.get("target_weights", {})
        if isinstance(targets, list):
            targets = {row["instrument_id"]: row["weight"] for row in targets}
        targets = {**targets, "USD_CASH": outcome.get("cash_weight", max(0, 1-sum(targets.values())))}
        numerical_execution = {"reference_date": execution.get("reference_date", state["as_of"]), "reference_prices": execution.get("reference_prices", {}), "target_weights": targets, "transaction_cost_bps": execution.get("transaction_cost_bps", 0)}
        public_fills = []
        for fill in fills:
            quantity = fill["quantity"]
            price = fill.get("reference_price", fill.get("price"))
            public_fills.append({"instrument_id": fill.get("instrument_id", fill.get("symbol")), "side": fill.get("side", "buy" if quantity >= 0 else "sell"), "quantity": abs(quantity), "reference_price": price, "notional": abs(fill.get("notional", quantity * price)), "transaction_cost": fill.get("transaction_cost", fill.get("cost", fill.get("fee", 0) + fill.get("spread_cost", 0) + fill.get("slippage_cost", 0)))})
        result = {"run_id": run_id, "input_id": old["input_id"], "decision_id": did, "portfolio_id": old["portfolio_id"], "before_revision": expected_revision, "after_revision": expected_revision + 1, "paper_state": dict(state), "simulated_fills": public_fills, "execution": dict(execution), "recorded_at": recorded_at, "status": "accepted", "execution_status": status, "action": "accept", "transaction_cost": execution.get("transaction_cost", 0), "paper_execution": True, "reference_date": numerical_execution["reference_date"], "reference_prices": numerical_execution["reference_prices"], "input_snapshot_id": snapshot_id, **native_fields, "synthetic": True, "contract_version": "1.13.0", "automatic_experiment_policy": True, "confirmed_by_user": False, "execution_mode": "automatic_isolated_experiment_common_evaluator", "caller": self._ctx(recorded_at).caller.audit_view()}
        require_valid(result, "api/resolve-portfolio-decision-response")
        ref = self._artifact("execution", run_id, result)
        decision = {"decision_id": did, "portfolio_id": old["portfolio_id"], "run_id": run_id, "input_id": old["input_id"], "input_snapshot_id": snapshot_id, **native_fields, "algorithm_family": old.get("metadata", {}).get("family", "control"), "algorithm": old["approach_id"], "portfolio_revision": expected_revision, "status": "accepted", "created_at": old["started_at"], "recommendation": outcome, "provenance": {"experiment_id": old["experiment_id"], "run_id": run_id, "input_checksum": old["input_checksum"]}, "execution": numerical_execution, "synthetic": True, "contract_version": "1.13.0", "automatic_experiment_policy": True}
        require_valid({**decision, "checksum": ref["checksum"]}, "portfolio-decision")
        proposal = self._artifact("decisions", did, decision)
        history_id = f"{old['portfolio_id']}:{expected_revision + 1:020d}"
        history = self._artifact("history", history_id + ":" + run_id, {"portfolio_id": old["portfolio_id"], "revision": expected_revision + 1, "paper_state": dict(state), "recorded_at": recorded_at, "reason": "automatic_isolated_experiment", "decision_id": did, "run_id": run_id, "input_id": old["input_id"], "input_snapshot_id": snapshot_id, **native_fields, "automatic_experiment_policy": True, "synthetic": True, "contract_version": "1.13.0"})
        new = {**old, "status": status, "execution_ref": ref}
        # Issue day and execution day are independent. Publish an immutable event in
        # the SAME ledger transaction, pointing at the original verified receipt.
        # Reports can query Monday fills from Friday decisions without scanning runs
        # or charging Friday for costs incurred on Monday. It is not another ledger.
        event_at = _slot(execution.get("execution_at", recorded_at))
        event_id = _id("mo", run_id, "paper_execution")
        execution_event = {"schema_version": "modeling-evidence/1", "environment": "beta", "synthetic": True, "observation_id": event_id, "kind": "paper_execution", "experiment_id": old["experiment_id"], "approach_id": old["approach_id"], "portfolio_id": old["portfolio_id"], "portfolio_revision": expected_revision + 1, "run_id": run_id, "input_id": old["input_id"], "decision_id": did, "decision_slot": old["slot"], "slot": event_at, "observed_at": event_at, "recorded_at": recorded_at, "observed_key": _index_time(event_at) + "#" + event_id, "execution_ref": ref, **ref, **self._keys(old["experiment_id"], old["approach_id"], event_at, event_id)}
        self.repo.commit([Transition("research_run", record_id=run_id, old_doc=old, new_doc=new), HeadMove("portfolio", record_id=old["portfolio_id"], expected_revision=expected_revision, revision_attr="paper_state_revision", set_attrs={"paper_state": dict(state)}), PutNew("portfolio_history", doc={"history_id": history_id, "portfolio_id": old["portfolio_id"], "revision": expected_revision + 1, **history}), PutNew("portfolio_decision", doc={"decision_id": did, "portfolio_id": old["portfolio_id"], "portfolio_revision": expected_revision, "status": "accepted", "algorithm_family": decision["algorithm_family"], "algorithm": old["approach_id"], "run_id": run_id, "input_id": old["input_id"], **proposal, "resolution_key": ref["artifact_key"], "resolution_checksum": ref["checksum"]}), PutNew("portfolio_observation", doc=execution_event, contract_version="1.13.0")], audit=[audit_event(self._ctx(recorded_at), record_id=old["portfolio_id"], record_type="portfolio", operation="apply_isolated_experiment_policy", prior={"revision": expected_revision}, new={"revision": expected_revision + 1}, run_id=run_id, decision_id=did)])
        return result

    def record_observation(self, experiment_id: str, approach_id: str, slot: str, portfolio_id: str, payload: Mapping[str, Any], observed_at: str, *, kind: str = "valuation") -> dict[str, Any]:
        slot = _slot(slot)
        book = self.get_portfolio(portfolio_id)
        if (book["experiment_id"], book["approach_id"]) != (experiment_id, approach_id):
            raise PlatformError.not_permitted("observation portfolio ownership mismatch")
        oid = _id("mo", experiment_id, approach_id, slot, kind)
        revision = payload.get("revision", payload.get("portfolio_revision", book["revision"]))
        if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= book["revision"]:
            raise PlatformError.validation("observation holdings revision must exist in this book")
        ref = self._artifact("observations", oid, payload)
        doc = {"schema_version": "modeling-evidence/1", "environment": "beta", "synthetic": True, "observation_id": oid, "experiment_id": experiment_id, "approach_id": approach_id, "slot": slot, "portfolio_id": portfolio_id, "portfolio_revision": revision, "observed_at": observed_at, "observed_key": _index_time(observed_at) + "#" + oid, "kind": kind, **ref, **self._keys(experiment_id, approach_id, slot, oid)}
        return self._put("portfolio_observation", doc, observed_at)

    def query_day(self, experiment_id: str, day: str, *, kind: str = "runs", page_size: int = 100, next_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        if kind not in KINDS or not 1 <= page_size <= 100 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise PlatformError.validation("invalid bounded modeling day query")
        table = KINDS[kind]
        # Scope tokens to their exact report/day/table so filters cannot select other data.
        cursor = None
        scope = f"{table}:{experiment_id}#{day}"
        if next_token:
            try:
                token = json.loads(base64.urlsafe_b64decode(next_token))
                if token["scope"] != scope:
                    raise ValueError()
                cursor = token["cursor"]
            except (ValueError, KeyError, TypeError):
                raise PlatformError.validation("modeling page token has wrong query scope") from None
        rows, token = self.repo.query_index(table, "day-index", f"{experiment_id}#{day}", newest_first=False, limit=page_size, page_token=cursor)
        values = []
        for row in rows:
            try:
                if table == "research_run":
                    values.append(self.read_run(row.id))
                elif table == "portfolio_observation":
                    values.append({**row.doc, "payload": json.loads(self.read_artifact_bytes(row.doc))})
                else:
                    values.append(row.doc)
            except PlatformError as exc:
                # A damaged/unavailable artifact must remain a visible coverage failure,
                # never disappear from a day's recommendation/accounting rows.
                values.append({**row.doc, "outcome": None, "execution": None, "payload": None, "evidence_error": {"code": exc.code, "message": str(exc)}})
        encoded = base64.urlsafe_b64encode(json.dumps({"scope": scope, "cursor": token}).encode()).decode() if token else None
        return values, encoded

    @classmethod
    def from_ec2(cls, *, region: str = "us-east-2") -> "ModelingEvidence":
        """Resolve the existing approved reports bucket/KMS through SSM and S3."""
        if region != "us-east-2":
            raise PlatformError.not_permitted("experiment backend is beta us-east-2 only")
        import boto3
        from .aws_clients import s3_client
        ssm = boto3.client("ssm", region_name=region)
        name = ssm.get_parameter(Name="/finplan/beta/financialplanning/config/bucket-reports")["Parameter"]["Value"]
        s3 = s3_client(region)
        encryption = s3.get_bucket_encryption(Bucket=name)["ServerSideEncryptionConfiguration"]["Rules"][0]["ApplyServerSideEncryptionByDefault"]
        principal = boto3.client("sts", region_name=region).get_caller_identity()["Arn"]
        return cls(MetadataRepository(boto3.client("dynamodb", region_name=region), "beta"), ArtifactStore(s3, {"reports": name}, kms_key_id=encryption.get("KMSMasterKeyID")), principal=principal)
