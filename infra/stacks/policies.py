"""Pure policy builders for platform resource policies (tasks 2.2, 2.5, 3.1, 3.5).

These functions return plain IAM JSON (dicts). The CDK stacks call them with CDK tokens
for ARNs; the unit tests call them with placeholder ARNs and evaluate the result with
the contract package's offline evaluator (``infra/policy_sim.py``). The same builders
therefore define what is deployed and what is simulated.

Bucket policy (every bucket)
----------------------------
* ``DenyInsecureTransport``: any request without TLS (STO-02).
* ``DenyNonKmsEncryption`` / ``DenyOtherKmsKey``: uploads declaring another encryption
  mode or another KMS key (STO-02). Uploads that declare nothing get the bucket default
  (SSE-KMS with the environment key, Bucket Keys on).
* ``DenyOtherEnvironmentPrincipals``: any action by a ``finplan-<other-env>-*`` role
  (STO-03 cross-environment).
* ``DenyObjectAccessOutsideEnvironment``: object actions by any principal that is not a
  platform role of this environment, an operator, or a consumer granted on this bucket
  (STO-03 least privilege).
* write-once buckets/prefixes (snapshots, plans, reports, ``outputs/accepted/``,
  ``raw/uploads/excel/``): ``DenyPutWithoutIfNoneMatch`` (every PutObject must be a
  conditional create) and ``DenyDeleteImmutableArtifacts`` (only the orphan sweeper may
  delete, and only in ``plans``/``snapshots``) (STO-04).

Consumer access (experimental project, 2026-10-18)
-------------------------------------------------
Per-role allowlists were removed at the owner's request: any role of the SAME
environment (FinanceModel, FinanceLambdasTool, FinanceAgent included) may use these
buckets and tables, subject only to its own IAM policy. ``AllowFinanceModel*`` grants
remain so the FinanceModel job role keeps working without its own bucket grant;
its read of unapproved snapshots stays denied (data-quality gate, ING-12).
Environment isolation, TLS, KMS, write-once and append-only rules are unchanged.

Metadata table resource policy (3.1, 3.5)
-----------------------------------------
* ``DenyOtherEnvironmentPrincipals``: item-level actions by roles of another environment
  (MDS-07). Roles of this environment are not restricted by the table policy.
* audit table: ``DenyAuditMutation`` of ``UpdateItem``/``DeleteItem``/``BatchWriteItem``
  and PartiQL writes for every principal (MDS-06 append-only).
"""

from __future__ import annotations

from typing import Callable, Iterable, Mapping

__all__ = [
    "ENVIRONMENTS",
    "OBJECT_ACTIONS",
    "WRITE_ONCE",
    "DYNAMODB_DATA_ACTIONS",
    "AUDIT_MUTATION_ACTIONS",
    "SNAPSHOT_STATUS_TAG",
    "bucket_policy_statements",
    "table_policy_statements",
    "kms_key_policy_statements",
]

ENVIRONMENTS = ("beta", "gamma", "prod")
REPO = "financialplanning"
SNAPSHOT_STATUS_TAG = "snapshot-status"
OBJECT_ACTIONS = ["s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject", "s3:ReplicateObject", "s3:AbortMultipartUpload"]
#: bucket role -> write-once key prefixes ("" = whole bucket); mirrors core.artifacts.WRITE_ONCE_PREFIXES
WRITE_ONCE: dict[str, tuple[str, ...]] = {
    "snapshots": ("",),
    "plans": ("",),
    "reports": ("",),
    "outputs": ("accepted/",),
    "raw": ("uploads/excel/", "research-news/v1/"),
    "curated": ("research-news/v1/",),
}
#: buckets where the orphan sweeper may delete orphans (design P3)
SWEEPABLE = ("snapshots", "plans")
DYNAMODB_DATA_ACTIONS = [
    "dynamodb:GetItem",
    "dynamodb:BatchGetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:PartiQLSelect",
    "dynamodb:PartiQLInsert",
    "dynamodb:PartiQLUpdate",
    "dynamodb:PartiQLDelete",
    # No stream actions (GetRecords, GetShardIterator): the tables have no streams, and a table
    # resource policy rejects them ("Invalid policy document"), which failed the first beta deploy.
]
AUDIT_MUTATION_ACTIONS = ["dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete"]

ArnFor = Callable[[str], str]


def _platform(env: str) -> str:
    return f"finplan-{env}-{REPO}-*"


def _role(env: str, logical: str) -> str:
    return f"finplan-{env}-{REPO}-{logical}-role"


def bucket_policy_statements(
    *,
    env: str,
    role: str,
    bucket_arn: str,
    key_arn: str,
    account_root: str,
    arn_for: ArnFor,
    consumers: Mapping[str, str],
    dev_desktop_roles: Iterable[str] = (),
    mutable_prefixes: Iterable[str] = (),
) -> list[dict]:
    """All bucket policy statements for one bucket.

    ``mutable_prefixes`` (beta reports bucket only, ``core.artifacts.MUTABLE_PREFIXES``): key
    prefixes exempt from ``DenyPutWithoutIfNoneMatch`` (stable modeling report views); the
    statement then uses ``NotResource``. Deletes stay denied everywhere.

    ``consumers`` maps consumer keys (``financemodel_job``, ``operator``, ...) to role-name
    patterns from the environment configuration. ``arn_for(pattern)`` turns a role-name
    pattern into a role ARN pattern (tokens in CDK, placeholders in tests).
    """
    if env not in ENVIRONMENTS:
        raise ValueError(env)
    objects = f"{bucket_arn}/*"
    others = [arn_for(f"finplan-{o}-*") for o in ENVIRONMENTS if o != env]
    fm_job = arn_for(consumers["financemodel_job"])
    sweeper = arn_for(_role(env, "metadata-sweeper"))

    st: list[dict] = [
        {
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [bucket_arn, objects],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        },
        {
            "Sid": "DenyNonKmsEncryption",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:PutObject",
            "Resource": objects,
            # present AND different (an absent header gets the bucket default, the environment key)
            "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"}, "Null": {"s3:x-amz-server-side-encryption": "false"}},
        },
        {
            "Sid": "DenyOtherKmsKey",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:PutObject",
            "Resource": objects,
            "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption-aws-kms-key-id": key_arn}, "Null": {"s3:x-amz-server-side-encryption-aws-kms-key-id": "false"}},
        },
        {
            "Sid": "DenyOtherEnvironmentPrincipals",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [bucket_arn, objects],
            "Condition": {"ArnLike": {"aws:PrincipalArn": others}},
        },
    ]

    prefixes = WRITE_ONCE.get(role, ())
    mutable = sorted(set(mutable_prefixes))
    if mutable and prefixes != ("",):
        raise ValueError("mutable prefixes are only supported on a wholly write-once bucket")
    if prefixes:
        resources = [f"{bucket_arn}/{p}*" for p in prefixes]
        put_deny: dict = {
            "Sid": "DenyPutWithoutIfNoneMatch",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:PutObject",
            "Resource": resources,
            "Condition": {"Null": {"s3:if-none-match": "true"}},
        }
        if mutable:
            del put_deny["Resource"]
            put_deny["NotResource"] = [f"{bucket_arn}/{p}*" for p in mutable]
        st.append(put_deny)
        delete = {
            "Sid": "DenyDeleteImmutableArtifacts",
            "Effect": "Deny",
            "Principal": "*",
            "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
            "Resource": resources,
        }
        if role in SWEEPABLE:
            delete["Condition"] = {"ArnNotLike": {"aws:PrincipalArn": [sweeper]}}
        st.append(delete)

    if role == "snapshots":
        st += [
            {
                "Sid": "AllowFinanceModelApprovedSnapshotRead",
                "Effect": "Allow",
                "Principal": {"AWS": account_root},
                "Action": "s3:GetObject",
                "Resource": objects,
                "Condition": {"ArnLike": {"aws:PrincipalArn": [fm_job]}, "StringEquals": {f"s3:ExistingObjectTag/{SNAPSHOT_STATUS_TAG}": "approved"}},
            },
            {
                # data-quality gate, not access control: models only ever read approved snapshots
                "Sid": "DenyFinanceModelUnapprovedSnapshotRead",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:GetObject*",
                "Resource": objects,
                "Condition": {"ArnLike": {"aws:PrincipalArn": [fm_job]}, "StringNotEquals": {f"s3:ExistingObjectTag/{SNAPSHOT_STATUS_TAG}": "approved"}},
            },
        ]
    if role == "outputs":
        staging = f"{bucket_arn}/staging/run_*/*"
        st += [
            {
                "Sid": "AllowFinanceModelStagingWrite",
                "Effect": "Allow",
                "Principal": {"AWS": account_root},
                "Action": "s3:PutObject",
                "Resource": staging,
                "Condition": {"ArnLike": {"aws:PrincipalArn": [fm_job]}},
            },
        ]
    return st


def table_policy_statements(*, env: str, logical: str, table_arn: str, arn_for: ArnFor, dev_desktop_roles: Iterable[str] = ()) -> list[dict]:
    """Resource policy statements for one metadata table (plus its indexes)."""
    resources = [table_arn, f"{table_arn}/index/*"]
    others = [arn_for(f"finplan-{o}-*") for o in ENVIRONMENTS if o != env]
    st: list[dict] = [
        {
            "Sid": "DenyOtherEnvironmentPrincipals",
            "Effect": "Deny",
            "Principal": "*",
            "Action": list(DYNAMODB_DATA_ACTIONS),
            "Resource": resources,
            "Condition": {"ArnLike": {"aws:PrincipalArn": others}},
        }
    ]
    if logical in ("audit_event", "portfolio_history", "activity_event", "research_input", "portfolio_observation"):
        st.append({"Sid": "DenyAuditMutation", "Effect": "Deny", "Principal": "*", "Action": list(AUDIT_MUTATION_ACTIONS), "Resource": resources})
    if logical == "research_run":
        st.append({"Sid": "DenyPermanentSlotDeletion", "Effect": "Deny", "Principal": "*", "Action": ["dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:PartiQLDelete"], "Resource": resources})
    return st


def kms_key_policy_statements(*, env: str, arn_for: ArnFor) -> list[dict]:
    others = [arn_for(f"finplan-{o}-*") for o in ENVIRONMENTS if o != env]
    return [
        {
            "Sid": "DenyOtherEnvironmentPrincipals",
            "Effect": "Deny",
            "Principal": {"AWS": "*"},
            "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:ReEncrypt*"],
            "Resource": "*",
            "Condition": {"ArnLike": {"aws:PrincipalArn": others}},
        }
    ]


def _unique(items: Iterable[str]) -> list[str]:  # pragma: no cover - helper for callers
    seen: list[str] = []
    for i in items:
        if i not in seen:
            seen.append(i)
    return seen
