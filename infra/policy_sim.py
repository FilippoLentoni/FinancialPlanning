"""Offline IAM policy simulation for platform resources (tasks 2.2, 3.1, 3.5; STO-03, STO-04, MDS-01, MDS-06, MDS-07).

Reuses the contract package's evaluator (:func:`finplan_contracts.iam.evaluate`) and adds
what it deliberately leaves out: **resource-based policies** with ``Principal`` matching,
and resolution of CloudFormation intrinsics in synthesized templates.

Evaluation model (same account, which is the single-account decision D5):

1. an explicit ``Deny`` in an identity policy, the permission boundary or a matching
   resource-policy statement wins;
2. otherwise the request is allowed when an identity policy allows it and the boundary (if
   any) allows it, or when a resource-policy statement allows it *naming the principal
   directly* (a statement naming the account root only delegates to IAM and grants nothing
   by itself);
3. otherwise it is implicitly denied.

``Principal`` matching: ``"*"``/``{"AWS": "*"}`` matches everyone; an ARN matches that
principal (wildcards allowed); the account root ARN matches every principal of the account
for ``Deny`` statements and is a delegation for ``Allow`` statements.

Template helpers resolve ``Ref``/``Fn::GetAtt``/``Fn::Join``/``Fn::Sub``/``Fn::ImportValue``
with placeholder pseudo parameters (account ``<account-id>``, partition ``aws``, region from
configuration), so simulated ARNs never contain a real account identifier.

This is a unit-test simulation, not a replacement for the IAM policy simulator in beta.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from finplan_contracts.boundaries import concrete_boundary
from finplan_contracts.iam import ALLOWED, EXPLICIT_DENY, IMPLICIT_DENY, EvalResult, Request, evaluate

__all__ = [
    "ACCOUNT",
    "PARTITION",
    "Principal",
    "SimResult",
    "simulate",
    "role_arn",
    "TemplateResolver",
    "load_template",
]

ACCOUNT = "<account-id>"
PARTITION = "aws"


def role_arn(name: str, account: str = ACCOUNT) -> str:
    return f"arn:{PARTITION}:iam::{account}:role/{name}"


@dataclass(frozen=True)
class Principal:
    """A simulated principal: its ARN, identity policies and permission boundary."""

    arn: str
    identity_policies: tuple[Mapping[str, Any], ...] = ()
    boundary: Mapping[str, Any] | None = None

    @property
    def account(self) -> str:
        parts = self.arn.split(":")
        return parts[4] if len(parts) > 4 else ""

    @classmethod
    def role(cls, name: str, *policies: Mapping[str, Any], boundary: Mapping[str, Any] | None = None) -> "Principal":
        return cls(role_arn(name), tuple(policies), boundary)


@dataclass
class SimResult:
    decision: str
    identity: EvalResult
    resource_allows: list[str] = field(default_factory=list)
    resource_denies: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOWED

    def __bool__(self) -> bool:
        return self.allowed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SimResult({self.decision}, identity={self.identity.decision}, rp_allow={self.resource_allows}, rp_deny={self.resource_denies + self.identity.matched_deny})"


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _glob(pattern: str, value: str) -> bool:
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, value) is not None


def _principal_match(spec: Any, principal: Principal) -> str | None:
    """'direct', 'account' or None."""
    if spec == "*":
        return "direct"
    if isinstance(spec, Mapping):
        values = []
        for k, v in spec.items():
            if k == "AWS":
                values += _as_list(v)
        for v in values:
            if v == "*":
                return "direct"
            if isinstance(v, str):
                if v.endswith(":root") and v.split(":")[4:5] == [principal.account]:
                    return "account"
                if _glob(v, principal.arn):
                    return "direct"
    return None


def simulate(
    action: str,
    resource: str,
    principal: Principal,
    *,
    resource_policy: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> SimResult:
    ctx = {"aws:PrincipalArn": principal.arn, "aws:SecureTransport": "true", **dict(context or {})}
    req = Request(action, resource, ctx)
    # Contract boundaries are CloudFormation-ready (Fn::Sub with pseudo parameters, contracts 0.2.0);
    # substitute the placeholder partition/region/account before evaluating.
    boundary = concrete_boundary(principal.boundary, account=ACCOUNT) if principal.boundary is not None else None
    identity = evaluate(req, list(principal.identity_policies), boundary)
    rp_allow: list[str] = []
    rp_deny: list[str] = []
    if resource_policy is not None:
        stmts = resource_policy.get("Statement", [])
        stmts = [stmts] if isinstance(stmts, Mapping) else stmts
        for i, stmt in enumerate(stmts):
            kind = _principal_match(stmt.get("Principal"), principal)
            if kind is None:
                continue
            res = evaluate(req, [{"Statement": [{k: v for k, v in stmt.items() if k != "Principal"}]}])
            sid = str(stmt.get("Sid", i))
            if stmt.get("Effect") == "Deny" and res.decision == EXPLICIT_DENY:
                rp_deny.append(sid)
            elif stmt.get("Effect") == "Allow" and res.decision == ALLOWED and kind == "direct":
                rp_allow.append(sid)
    if identity.decision == EXPLICIT_DENY or rp_deny:
        decision = EXPLICIT_DENY
    elif identity.decision == ALLOWED or rp_allow:
        decision = ALLOWED
    else:
        decision = IMPLICIT_DENY
    return SimResult(decision, identity, rp_allow, rp_deny)


# ===================================================================== templates
def load_template(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class TemplateResolver:
    """Resolve intrinsics of one or more synthesized templates into placeholder values."""

    def __init__(self, templates: Mapping[str, Mapping[str, Any]], *, region: str = "us-east-2", account: str = ACCOUNT) -> None:
        self.templates = dict(templates)
        self.region = region
        self.account = account
        self.exports: dict[str, Any] = {}
        for stack_name, t in self.templates.items():
            for out in (t.get("Outputs") or {}).values():
                exp = (out.get("Export") or {}).get("Name")
                if exp:
                    self.exports[exp] = (stack_name, out["Value"])

    def _pseudo(self, name: str) -> str:
        return {"AWS::AccountId": self.account, "AWS::Partition": PARTITION, "AWS::Region": self.region, "AWS::URLSuffix": "amazonaws.com", "AWS::StackName": "stack"}[name]

    def resolve(self, value: Any, stack: str) -> Any:
        if isinstance(value, list):
            return [self.resolve(v, stack) for v in value]
        if not isinstance(value, Mapping):
            return value
        if len(value) == 1:
            ((k, v),) = value.items()
            if k == "Ref":
                if v.startswith("AWS::"):
                    return self._pseudo(v)
                return self._ref(stack, v)
            if k == "Fn::GetAtt":
                lid, attr = (v if isinstance(v, list) else v.split(".", 1))
                return self._getatt(stack, lid, attr)
            if k == "Fn::Join":
                sep, parts = v
                return sep.join(str(self.resolve(p, stack)) for p in parts)
            if k == "Fn::Sub":
                text, mapping = (v, {}) if isinstance(v, str) else (v[0], v[1])
                def rep(m: re.Match[str]) -> str:
                    name = m.group(1)
                    if name in mapping:
                        return str(self.resolve(mapping[name], stack))
                    if name.startswith("AWS::"):
                        return self._pseudo(name)
                    if "." in name:
                        lid, attr = name.split(".", 1)
                        return str(self._getatt(stack, lid, attr))
                    return str(self._ref(stack, name))
                return re.sub(r"\$\{([^}!]+)\}", rep, text)
            if k == "Fn::ImportValue":
                name = self.resolve(v, stack)
                src_stack, val = self.exports[name]
                return self.resolve(val, src_stack)
            if k == "Fn::Select":
                idx, lst = v
                return self.resolve(lst, stack)[int(idx)]
            if k == "Fn::Split":
                sep, s = v
                return str(self.resolve(s, stack)).split(sep)
        return {k: self.resolve(x, stack) for k, x in value.items()}

    def _resource(self, stack: str, lid: str) -> Mapping[str, Any]:
        return self.templates[stack]["Resources"][lid]

    def _ref(self, stack: str, lid: str) -> str:
        if lid in (self.templates[stack].get("Parameters") or {}):
            return f"<param:{lid}>"
        r = self._resource(stack, lid)
        props = r.get("Properties", {})
        typ = r["Type"]
        if typ == "AWS::S3::Bucket":
            return str(self.resolve(props.get("BucketName", lid.lower()), stack))
        if typ == "AWS::DynamoDB::Table":
            return str(self.resolve(props.get("TableName", lid), stack))
        if typ == "AWS::IAM::Role":
            return str(self.resolve(props.get("RoleName", lid), stack))
        if typ == "AWS::KMS::Key":
            return f"key-{lid}"
        if typ == "AWS::Lambda::Function":
            return str(self.resolve(props.get("FunctionName", lid), stack))
        if typ == "AWS::SSM::Parameter":
            return str(self.resolve(props.get("Name", lid), stack))
        return lid

    def _getatt(self, stack: str, lid: str, attr: str) -> str:
        r = self._resource(stack, lid)
        typ = r["Type"]
        name = self._ref(stack, lid)
        region, account = self.region, self.account
        if attr == "Arn":
            if typ == "AWS::S3::Bucket":
                return f"arn:{PARTITION}:s3:::{name}"
            if typ == "AWS::DynamoDB::Table":
                return f"arn:{PARTITION}:dynamodb:{region}:{account}:table/{name}"
            if typ == "AWS::KMS::Key":
                return f"arn:{PARTITION}:kms:{region}:{account}:{name.replace('key-', 'key/')}"
            if typ == "AWS::IAM::Role":
                return role_arn(name, self.account)
            if typ == "AWS::Lambda::Function":
                return f"arn:{PARTITION}:lambda:{region}:{account}:function:{name}"
        return f"<{lid}.{attr}>"

    # ---------------------------------------------------------- extraction
    def resources(self, typ: str) -> Iterable[tuple[str, str, Mapping[str, Any]]]:
        for stack, t in self.templates.items():
            for lid, r in t["Resources"].items():
                if r["Type"] == typ:
                    yield stack, lid, r

    def bucket_policies(self) -> dict[str, dict[str, Any]]:
        """bucket name -> resolved bucket policy document."""
        out: dict[str, dict[str, Any]] = {}
        for stack, _lid, r in self.resources("AWS::S3::BucketPolicy"):
            props = r["Properties"]
            out[str(self.resolve(props["Bucket"], stack))] = self.resolve(props["PolicyDocument"], stack)
        return out

    def table_policies(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for stack, _lid, r in self.resources("AWS::DynamoDB::Table"):
            props = r["Properties"]
            rp = props.get("ResourcePolicy")
            if rp:
                out[str(self.resolve(props["TableName"], stack))] = self.resolve(rp["PolicyDocument"], stack)
        return out

    def role_policies(self, role_name: str) -> list[dict[str, Any]]:
        """Inline and attached (``AWS::IAM::Policy``) identity policies of a named role."""
        docs: list[dict[str, Any]] = []
        for stack, lid, r in self.resources("AWS::IAM::Role"):
            if self._ref(stack, lid) != role_name:
                continue
            for p in r["Properties"].get("Policies", []) or []:
                docs.append(self.resolve(p["PolicyDocument"], stack))
            for pstack, _plid, pol in self.resources("AWS::IAM::Policy"):
                roles = [self.resolve(x, pstack) for x in pol["Properties"].get("Roles", [])]
                if role_name in roles:
                    docs.append(self.resolve(pol["Properties"]["PolicyDocument"], pstack))
        return docs
