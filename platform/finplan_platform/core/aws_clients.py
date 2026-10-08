"""AWS client construction shared by the Lambda entry points.

S3 clients must sign with SigV4 against the regional endpoint. A default ``boto3.client("s3")``
presigns with SigV2 on the global ``s3.amazonaws.com`` host, which S3 rejects for KMS-encrypted
objects and in SigV4-only regions; the first beta run's download grants failed with HTTP 403.
"""

from __future__ import annotations

import os
from typing import Any


def s3_client(region: str | None = None) -> Any:
    """An S3 client that presigns SigV4 URLs on the regional virtual-hosted endpoint."""
    import boto3
    from botocore.config import Config

    region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError("AWS_REGION is not set; S3 presigning needs the bucket's region")
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
