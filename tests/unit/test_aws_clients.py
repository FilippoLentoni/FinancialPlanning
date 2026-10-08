"""S3 presigning uses SigV4 on the regional endpoint (regression for the first beta run).

A default ``boto3.client("s3")`` presigned SigV2 URLs on ``s3.amazonaws.com``; S3 answered HTTP 403
to the download grants for KMS-encrypted plan artifacts.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from finplan_platform.core.aws_clients import s3_client

ROOT = Path(__file__).resolve().parents[2]


def test_presigned_get_is_sigv4_on_the_regional_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    url = s3_client().generate_presigned_url("get_object", Params={"Bucket": "example-bucket", "Key": "plans/k.json"}, ExpiresIn=60)
    parts = urlsplit(url)
    assert parts.netloc == "example-bucket.s3.us-east-2.amazonaws.com"
    query = parse_qs(parts.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "/us-east-2/s3/aws4_request" in query["X-Amz-Credential"][0]
    assert "Signature" not in query and "AWSAccessKeyId" not in query


def test_presigned_post_is_sigv4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    post = s3_client().generate_presigned_post(Bucket="example-bucket", Key="uploads/incoming/x.xlsx", ExpiresIn=60)
    assert post["url"].startswith("https://example-bucket.s3.us-east-2.amazonaws.com")
    assert post["fields"]["x-amz-algorithm"] == "AWS4-HMAC-SHA256"


def test_region_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(RuntimeError):
        s3_client()


def test_no_entry_point_builds_a_default_s3_client() -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "platform").rglob("*.py")
        if p.name != "aws_clients.py"
        if re.search(r"""boto3\.client\(\s*["']s3["']""", p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"use finplan_platform.core.aws_clients.s3_client(): {offenders}"
