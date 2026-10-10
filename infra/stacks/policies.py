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

Consumer grants
---------------
* snapshots: the FinanceModel job role may ``GetObject`` only objects tagged
  ``snapshot-status=approved``; reads of untagged/unapproved objects and all writes are
  denied (STO-03, ING-12).
* outputs: the FinanceModel job role may only ``PutObject`` under ``staging/run_*/``; it
  has no read, list or delete anywhere in the bucket (STO-03 "writes outside staging",
  STG-01 "cross-run reads denied").
* raw: ``uploads/*`` is readable only by the plan-API (import) role and operators (STO-07).

Metadata table resource policy (3.1, 3.5)
-----------------------------------------
* ``DenyDataAccessOutsidePlatform``: item-level actions by any principal that is not a
  platform role of this environment (MDS-01: FinanceLambdasTool and FinanceModel roles
  are denied direct access; MDS-07: other environments are denied).
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
    "raw": ("uploads/excel/",),
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
) -> list[dict]:
    """All bucket policy statements for one bucket.

    ``consumers`` maps consumer keys (``financemodel_job``, ``operator``, ...) to role-name
    patterns from the environment configuration. ``arn_for(pattern)`` turns a role-name
    pattern into a role ARN pattern (tokens in CDK, placeholders in tests).
    """
    if env not in ENVIRONMENTS:
        raise ValueError(env)
    objects = f"{bucket_arn}/*"
    others = [arn_for(f"finplan-{o}-*") for o in ENVIRONMENTS if o != env]
    platform = arn_for(_platform(env))
    operator = arn_for(consumers["operator"])
    fm_job = arn_for(consumers["financemodel_job"])
    sweeper = arn_for(_role(env, "metadata-sweeper"))
    importer = arn_for(_role(env, "plan-api-handler"))

    allowed = [platform, operator]
    if role in ("snapshots", "outputs"):
        allowed.append(fm_job)

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
        {
            "Sid": "DenyObjectAccessOutsideEnvironment",
            "Effect": "Deny",
            "Principal": "*",
            "Action": list(OBJECT_ACTIONS),
            "Resource": objects,
            "Condition": {"ArnNotLike": {"aws:PrincipalArn": allowed}},
        },
    ]

    prefixes = WRITE_ONCE.get(role, ())
    if prefixes:
        resources = [f"{bucket_arn}/{p}*" for p in prefixes]
        st.append(
            {
                "Sid": "DenyPutWithoutIfNoneMatch",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": resources,
                "Condition": {"Null": {"s3:if-none-match": "true"}},
            }
        )
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
                "Sid": "DenyFinanceModelUnapprovedSnapshotRead",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:GetObject*",
                "Resource": objects,
                "Condition": {"ArnLike": {"aws:PrincipalArn": [fm_job]}, "StringNotEquals": {f"s3:ExistingObjectTag/{SNAPSHOT_STATUS_TAG}": "approved"}},
            },
            {
                "Sid": "DenyFinanceModelSnapshotWrites",
                "Effect": "Deny",
                "Principal": "*",
                "Action": ["s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject", "s3:AbortMultipartUpload"],
                "Resource": objects,
                "Condition": {"ArnLike": {"aws:PrincipalArn": [fm_job]}},
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
            {
                "Sid": "DenyFinanceModelWritesOutsideStaging",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject*",
                "NotResource": staging,
                "Condition": {"ArnLike": {"aws:PrincipalArn": [fm_job]}},
            },
            {
                "Sid": "DenyFinanceModelReadListDelete",
                "Effect": "Deny",
                "Principal": "*",
                "Action": ["s3:GetObject*", "s3:ListBucket*", "s3:DeleteObject*", "s3:RestoreObject"],
                "Resource": [bucket_arn, objects],
                "Condition": {"ArnLike": {"aws:PrincipalArn": [fm_job]}},
            },
        ]
    if role == "raw":
        st.append(
            {
                "Sid": "DenyUploadReadsExceptImportAndOperators",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:GetObject*",
                "Resource": f"{bucket_arn}/uploads/*",
                "Condition": {"ArnNotLike": {"aws:PrincipalArn": [importer, operator]}},
            }
        )
    return st


def table_policy_statements(*, env: str, logical: str, table_arn: str, arn_for: ArnFor) -> list[dict]:
    """Resource policy statements for one metadata table (plus its indexes)."""
    resources = [table_arn, f"{table_arn}/index/*"]
    st: list[dict] = [
        {
            "Sid": "DenyDataAccessOutsidePlatform",
            "Effect": "Deny",
            "Principal": "*",
            "Action": list(DYNAMODB_DATA_ACTIONS),
            "Resource": resources,
            "Condition": {"ArnNotLike": {"aws:PrincipalArn": [arn_for(_platform(env))]}},
        }
    ]
    if logical in ("audit_event", "portfolio_history", "activity_event"):
        st.append({"Sid": "DenyAuditMutation", "Effect": "Deny", "Principal": "*", "Action": list(AUDIT_MUTATION_ACTIONS), "Resource": resources})
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
