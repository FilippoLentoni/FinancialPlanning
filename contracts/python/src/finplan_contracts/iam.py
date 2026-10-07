"""IAM policy fragments for the SSM convention and an offline policy evaluator.

Policy fragments (design D4; task 8.3)
--------------------------------------
:func:`ssm_access_policy` returns a policy document that

* allows ``ssm:PutParameter`` (and the matching tag/label/delete actions) only on
  the role's own repository segment, ``/finplan/<env>/<own-repo>/*`` (plus
  ``/finplan/shared/<own-repo>/*`` for bootstrap and contract-publish roles);
* allows reads (``ssm:GetParameter*``) only on ``/finplan/<same-env>/*`` and
  ``/finplan/shared/*``;
* explicitly denies writes anywhere else under ``/finplan/``.

:func:`budget_state_writer_policy` is the single runtime writer exception: it
allows ``ssm:PutParameter`` on ``/finplan/shared/financialplanning/config/budget-state``
only.

ARNs use the CloudFormation pseudo parameters ``${AWS::Partition}``,
``${AWS::Region}`` and ``${AWS::AccountId}`` by default, so no account identifier
is ever written to a repository file; :func:`to_cfn` wraps such strings in
``Fn::Sub``. Tests substitute placeholders such as ``<account-id>``.

Offline evaluator
-----------------
:func:`evaluate` implements the subset of the IAM evaluation logic the contract
tests need (it is a unit-test "policy simulation", not a replacement for the IAM
policy simulator used in beta):

* explicit ``Deny`` in any identity policy or the permission boundary wins;
* otherwise the request needs an ``Allow`` in an identity policy and, when a
  permission boundary is given, an ``Allow`` in the boundary too;
* ``Action``/``NotAction`` and ``Resource``/``NotResource`` with ``*`` and ``?``
  wildcards (actions case-insensitive, resources case-sensitive);
* ``Condition`` operators ``StringEquals``, ``StringNotEquals``,
  ``StringEqualsIgnoreCase``, ``StringNotEqualsIgnoreCase``, ``StringLike``,
  ``StringNotLike``, ``ArnEquals``, ``ArnLike``, ``ArnNotEquals``, ``ArnNotLike``,
  ``Bool``, ``Null``, ``NumericEquals``/``NumericLessThan``/``NumericGreaterThan``
  and friends, each with an optional ``...IfExists`` suffix, plus the
  ``ForAnyValue:``/``ForAllValues:`` set prefixes;
* missing condition keys: positive operators evaluate to false, negated operators
  to true, ``...IfExists`` to true;
* policy variables ``${aws:PrincipalTag/...}``, ``${aws:username}`` and other
  request context keys in ``Resource`` and condition values.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import ssm as _ssm

__all__ = [
    "PolicyDocument",
    "Request",
    "EvalResult",
    "ALLOWED",
    "EXPLICIT_DENY",
    "IMPLICIT_DENY",
    "evaluate",
    "is_allowed",
    "ssm_parameter_arn",
    "ssm_access_policy",
    "budget_state_writer_policy",
    "SSM_WRITE_ACTIONS",
    "SSM_READ_ACTIONS",
    "to_cfn",
    "substitute",
]

PolicyDocument = dict[str, Any]

PARTITION = "${AWS::Partition}"
REGION = "${AWS::Region}"
ACCOUNT = "${AWS::AccountId}"

SSM_WRITE_ACTIONS = ["ssm:PutParameter", "ssm:DeleteParameter", "ssm:DeleteParameters", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource", "ssm:LabelParameterVersion"]
SSM_READ_ACTIONS = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory", "ssm:DescribeParameters"]


# ===================================================================== generators
def ssm_parameter_arn(path_glob: str, *, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT) -> str:
    """ARN of an SSM parameter (or wildcard) given its name, e.g. ``/finplan/beta/financemodel/*``."""
    return f"arn:{partition}:ssm:{region}:{account}:parameter{path_glob}"


def ssm_access_policy(
    repo: str,
    environment: str,
    *,
    shared_writes: bool = False,
    partition: str = PARTITION,
    region: str = REGION,
    account: str = ACCOUNT,
) -> PolicyDocument:
    """SSM policy for a role of ``repo`` deploying to ``environment`` (D4, ENV-07).

    ``shared_writes`` adds ``/finplan/shared/<repo>/*`` writes, for the bootstrap
    and contract-publish roles only (deploy roles never write ``shared``).
    """
    if repo not in _ssm.REPOS:
        raise ValueError(f"unknown repo {repo!r}")
    if environment not in _ssm.ENVIRONMENTS:
        raise ValueError(f"environment must be one of {_ssm.ENVIRONMENTS}")
    arn = lambda p: ssm_parameter_arn(p, partition=partition, region=region, account=account)  # noqa: E731
    own = [arn(f"/finplan/{environment}/{repo}/*")]
    if shared_writes:
        own.append(arn(f"/finplan/shared/{repo}/*"))
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "WriteOwnRepoSegment",
                "Effect": "Allow",
                "Action": list(SSM_WRITE_ACTIONS),
                "Resource": own,
            },
            {
                "Sid": "ReadSameEnvironmentAndShared",
                "Effect": "Allow",
                "Action": list(SSM_READ_ACTIONS),
                "Resource": [
                    arn(f"/finplan/{environment}"),
                    arn(f"/finplan/{environment}/*"),
                    arn("/finplan/shared"),
                    arn("/finplan/shared/*"),
                ],
            },
            {
                "Sid": "DenyWritesOutsideOwnSegment",
                "Effect": "Deny",
                "Action": list(SSM_WRITE_ACTIONS),
                "NotResource": own,
            },
        ],
    }


def budget_state_writer_policy(*, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT) -> PolicyDocument:
    """The only runtime writer under ``shared`` (D4 runtime writer exception)."""
    target = ssm_parameter_arn(_ssm.BUDGET_STATE_PARAMETER, partition=partition, region=region, account=account)
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "WriteBudgetStateOnly", "Effect": "Allow", "Action": ["ssm:PutParameter"], "Resource": [target]},
            {
                "Sid": "ReadBudgetInputs",
                "Effect": "Allow",
                "Action": ["ssm:GetParameter", "ssm:GetParameters"],
                "Resource": [
                    ssm_parameter_arn("/finplan/shared/financialplanning/config/*", partition=partition, region=region, account=account),
                    ssm_parameter_arn("/finplan/*/*/config/budget-enforced-role-names", partition=partition, region=region, account=account),
                ],
            },
            {"Sid": "DenyAnyOtherSsmWrite", "Effect": "Deny", "Action": list(SSM_WRITE_ACTIONS), "NotResource": [target]},
        ],
    }


def _contains_sub(value: str) -> bool:
    return "${AWS::" in value


def to_cfn(value: Any) -> Any:
    """Wrap strings that reference CloudFormation pseudo parameters in ``Fn::Sub``."""
    if isinstance(value, str):
        return {"Fn::Sub": value} if _contains_sub(value) else value
    if isinstance(value, list):
        return [to_cfn(v) for v in value]
    if isinstance(value, dict):
        return {k: to_cfn(v) for k, v in value.items()}
    return value


def substitute(value: Any, mapping: Mapping[str, str]) -> Any:
    """Replace ``${AWS::...}`` pseudo parameters (and unwrap ``Fn::Sub``) for offline evaluation."""
    if isinstance(value, dict) and set(value) == {"Fn::Sub"} and isinstance(value["Fn::Sub"], str):
        return substitute(value["Fn::Sub"], mapping)
    if isinstance(value, str):
        for k, v in mapping.items():
            value = value.replace("${" + k + "}", v)
        return value
    if isinstance(value, list):
        return [substitute(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, mapping) for k, v in value.items()}
    return value


# ===================================================================== evaluator
ALLOWED = "allowed"
EXPLICIT_DENY = "explicitDeny"
IMPLICIT_DENY = "implicitDeny"


@dataclass(frozen=True)
class Request:
    action: str
    resource: str = "*"
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class EvalResult:
    decision: str
    matched_allow: list[str] = field(default_factory=list)
    matched_deny: list[str] = field(default_factory=list)
    boundary_allows: bool | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOWED

    def __bool__(self) -> bool:
        return self.allowed


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


_VAR = re.compile(r"\$\{([^}]+)\}")


def _resolve_vars(pattern: str, ctx: Mapping[str, Any]) -> str | None:
    """Substitute ``${key}`` policy variables from the request context.

    ``${*}``, ``${?}``, ``${$}`` are the IAM literal escapes. Returns None when a
    referenced key is missing (the element then does not match).
    """
    missing = False

    def rep(m: re.Match[str]) -> str:
        nonlocal missing
        key = m.group(1)
        if key in ("*", "?", "$"):
            return "\x00" + key  # literal marker
        val = _ctx_get(ctx, key)
        if val is None or isinstance(val, (list, tuple)):
            missing = True
            return ""
        return str(val)

    out = _VAR.sub(rep, pattern)
    return None if missing else out


def _ctx_get(ctx: Mapping[str, Any], key: str) -> Any:
    # condition keys are case-insensitive (tag keys after '/' are case-sensitive in IAM; we accept exact or lower match)
    if key in ctx:
        return ctx[key]
    lk = key.lower()
    for k, v in ctx.items():
        if k.lower() == lk:
            return v
    return None


def _glob(pattern: str, value: str, *, ignore_case: bool = False) -> bool:
    # translate IAM wildcards: * any sequence (including ':' and '/'), ? one char; literal markers from ${*}
    regex = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\x00" and i + 1 < len(pattern):
            regex.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "*":
            regex.append(".*")
        elif ch == "?":
            regex.append(".")
        else:
            regex.append(re.escape(ch))
        i += 1
    flags = re.DOTALL | (re.IGNORECASE if ignore_case else 0)
    return re.fullmatch("".join(regex), value, flags) is not None


def _arn_match(pattern: str, arn: str) -> bool:
    """ARN matching: the six colon-separated segments are matched separately; the
    resource segment (which may itself contain ':') is matched as a whole."""
    if pattern == "*":
        return True
    pp = pattern.split(":", 5)
    ap = arn.split(":", 5)
    if len(pp) != 6 or len(ap) != 6:
        return _glob(pattern, arn)
    return all(_glob(p, a) for p, a in zip(pp, ap, strict=True))


def _action_match(pattern: str, action: str) -> bool:
    return _glob(pattern, action, ignore_case=True)


def _resource_match(patterns: Iterable[Any], resource: str, ctx: Mapping[str, Any]) -> bool:
    for p in patterns:
        if not isinstance(p, str):
            continue
        rp = _resolve_vars(p, ctx)
        if rp is None:
            continue
        if _arn_match(rp, resource):
            return True
    return False


_NEGATED = {"StringNotEquals", "StringNotEqualsIgnoreCase", "StringNotLike", "ArnNotEquals", "ArnNotLike", "NotIpAddress", "NumericNotEquals", "DateNotEquals"}


def _cmp_values(op: str, actual: Any, expected: str, ctx: Mapping[str, Any]) -> bool:
    exp = _resolve_vars(str(expected), ctx) if isinstance(expected, str) else expected
    if exp is None:
        return False
    a = actual
    if op in ("StringEquals", "StringNotEquals"):
        return str(a) == str(exp).replace("\x00", "")
    if op in ("StringEqualsIgnoreCase", "StringNotEqualsIgnoreCase"):
        return str(a).lower() == str(exp).replace("\x00", "").lower()
    if op in ("StringLike", "StringNotLike"):
        return _glob(str(exp), str(a))
    if op in ("ArnEquals", "ArnNotEquals", "ArnLike", "ArnNotLike"):
        return _arn_match(str(exp), str(a))
    if op == "Bool":
        return str(a).lower() == str(exp).lower()
    if op.startswith("Numeric"):
        try:
            x, y = float(a), float(exp)
        except (TypeError, ValueError):
            return False
        return {
            "NumericEquals": x == y,
            "NumericNotEquals": x == y,
            "NumericLessThan": x < y,
            "NumericLessThanEquals": x <= y,
            "NumericGreaterThan": x > y,
            "NumericGreaterThanEquals": x >= y,
        }[op]
    raise ValueError(f"unsupported condition operator {op!r}")


def _condition_block_matches(op_full: str, conds: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    set_prefix = None
    op = op_full
    if ":" in op:
        set_prefix, op = op.split(":", 1)
    if_exists = op.endswith("IfExists")
    if if_exists:
        op = op[: -len("IfExists")]
    for key, expected in conds.items():
        exp_values = _as_list(expected)
        actual = _ctx_get(ctx, key)
        if op == "Null":
            want_null = str(exp_values[0]).lower() == "true"
            if (actual is None) != want_null:
                return False
            continue
        negated = op in _NEGATED
        if actual is None:
            if if_exists or negated:
                continue
            if set_prefix == "ForAllValues":
                continue  # vacuously true for an empty/missing set
            return False
        actual_values = _as_list(actual)
        if set_prefix == "ForAllValues":
            ok = all(any(_cmp_values(op, a, e, ctx) for e in exp_values) for a in actual_values)
            if negated:
                ok = all(not any(_cmp_values(op, a, e, ctx) for e in exp_values) for a in actual_values)
        elif set_prefix == "ForAnyValue":
            hits = [any(_cmp_values(op, a, e, ctx) for e in exp_values) for a in actual_values]
            ok = (not all(hits)) if negated else any(hits)
        else:
            # single-valued: for multiple expected values, positive ops OR them; negated ops require no match
            a = actual_values[0] if len(actual_values) == 1 else actual_values
            if isinstance(a, list):
                matched = any(_cmp_values(op, x, e, ctx) for x in a for e in exp_values)
            else:
                matched = any(_cmp_values(op, a, e, ctx) for e in exp_values)
            ok = (not matched) if negated else matched
        if not ok:
            return False
    return True


def _statement_applies(stmt: Mapping[str, Any], req: Request) -> bool:
    ctx = req.context
    if "Action" in stmt:
        if not any(_action_match(a, req.action) for a in _as_list(stmt["Action"])):
            return False
    elif "NotAction" in stmt:
        if any(_action_match(a, req.action) for a in _as_list(stmt["NotAction"])):
            return False
    else:
        return False
    if "Resource" in stmt:
        if not _resource_match(_as_list(stmt["Resource"]), req.resource, ctx):
            return False
    elif "NotResource" in stmt:
        if _resource_match(_as_list(stmt["NotResource"]), req.resource, ctx):
            return False
    else:
        return False
    for op_full, conds in (stmt.get("Condition") or {}).items():
        if not _condition_block_matches(op_full, conds, ctx):
            return False
    return True


def _statements(policy: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    st = policy.get("Statement", [])
    return [st] if isinstance(st, dict) else list(st)


def _sid(policy_name: str, stmt: Mapping[str, Any], i: int) -> str:
    return f"{policy_name}:{stmt.get('Sid', i)}"


def evaluate(
    request: Request,
    identity_policies: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    permission_boundary: Mapping[str, Any] | None = None,
) -> EvalResult:
    """Evaluate one request against identity policies and an optional permission boundary."""
    if isinstance(identity_policies, Mapping):
        named = list(identity_policies.items())
    else:
        named = [(f"policy{i}", p) for i, p in enumerate(identity_policies)]
    result = EvalResult(IMPLICIT_DENY)
    for name, pol in named:
        for i, stmt in enumerate(_statements(pol)):
            if _statement_applies(stmt, request):
                (result.matched_deny if stmt.get("Effect") == "Deny" else result.matched_allow).append(_sid(name, stmt, i))
    boundary_allow = None
    if permission_boundary is not None:
        boundary_allow = False
        for i, stmt in enumerate(_statements(permission_boundary)):
            if _statement_applies(stmt, request):
                if stmt.get("Effect") == "Deny":
                    result.matched_deny.append(_sid("boundary", stmt, i))
                else:
                    boundary_allow = True
        result.boundary_allows = boundary_allow
    if result.matched_deny:
        result.decision = EXPLICIT_DENY
    elif result.matched_allow and (boundary_allow is None or boundary_allow):
        result.decision = ALLOWED
    return result


def is_allowed(action: str, resource: str, identity_policies: Any, permission_boundary: Mapping[str, Any] | None = None, **context: Any) -> bool:
    return evaluate(Request(action, resource, context), identity_policies, permission_boundary).allowed


def policy_glob_matches(pattern: str, value: str) -> bool:
    """Public helper: IAM-style wildcard match (``*``/``?``), used by static checks."""
    return _glob(pattern, value) or fnmatch.fnmatchcase(value, pattern)
