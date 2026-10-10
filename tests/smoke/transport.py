"""Transports and state for running the smoke suite against a deployed environment (task 10.4).

* :class:`SigV4Transport`: IAM-signed (SigV4, service ``execute-api``) HTTPS calls to the plan API
  endpoint, using the caller's credentials (in the pipeline: the environment's stage role, which the
  API admits as an operator principal). Download grants are presigned URLs fetched without signing.
* :class:`SsmSmokeState`: the smoke portfolio ID in ``/finplan/<env>/financialplanning/config/smoke-portfolio-id``.

The HTTP opener is injectable, so the unit suite checks request signing without any network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from finplan_contracts import ssm as contract_ssm

__all__ = ["SigV4Transport", "SsmSmokeState", "endpoint_parameter", "smoke_state_parameter"]


def endpoint_parameter(env: str) -> str:
    return contract_ssm.build(env, "financialplanning", "api", "plan-endpoint")


def smoke_state_parameter(env: str) -> str:
    return contract_ssm.build(env, "financialplanning", "config", "smoke-portfolio-id")


class SigV4Transport:
    def __init__(self, endpoint: str, region: str, credentials: Any, *, opener: Callable[..., Any] | None = None, timeout: float = 30.0, correlation_prefix: str = "cor_smoke") -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("the plan endpoint must be an https URL")
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.credentials = credentials
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.correlation_prefix = correlation_prefix
        self._n = 0

    def _url(self, path: str) -> str:
        base = self.endpoint
        url = base + path if not base.endswith("/v1") else base[: -len("/v1")] + path
        parts = urllib.parse.urlsplit(url)
        # Botocore signs an embedded query as already URI-encoded. Encode before
        # signing so slashes in dataset IDs match API Gateway's canonical query.
        query = urllib.parse.urlencode(
            urllib.parse.parse_qsl(parts.query, keep_blank_values=True),
            quote_via=urllib.parse.quote,
            safe="-_.~",
        )
        return urllib.parse.urlunsplit(parts._replace(query=query))

    def signed_request(self, method: str, path: str, body: Any = None) -> urllib.request.Request:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        self._n += 1
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Correlation-Id": f"{self.correlation_prefix}_{self._n:04d}"}
        req = AWSRequest(method=method, url=self._url(path), data=data, headers=headers)
        SigV4Auth(self.credentials, "execute-api", self.region).add_auth(req)
        return urllib.request.Request(req.url, data=data, headers=dict(req.headers.items()), method=method)

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any], dict[str, str]]:
        request = self.signed_request(method, path, body if method != "GET" else None)
        try:
            with self.opener(request, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), dict(resp.headers.items())
        except urllib.error.HTTPError as err:
            raw = err.read() or b"{}"
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"code": "INTERNAL", "message": "non-JSON error response"}
            return err.code, payload, dict(err.headers.items()) if err.headers else {}

    def download(self, url: str) -> bytes:
        if not url.startswith("https://"):
            raise ValueError("download grants are https URLs")
        with self.opener(urllib.request.Request(url, method="GET"), timeout=self.timeout) as resp:
            return resp.read()


class SsmSmokeState:
    def __init__(self, ssm: Any, env: str) -> None:
        self.ssm = ssm
        self.name = smoke_state_parameter(env)
        self.env = env

    def get(self) -> str | None:
        try:
            return self.ssm.get_parameter(Name=self.name)["Parameter"]["Value"]
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ParameterNotFound":
                return None
            raise

    def put(self, portfolio_id: str) -> None:
        decision = contract_ssm.check_write(self.name, contract_ssm.Writer("financialplanning", "pipeline", self.env))
        if not decision:
            raise PermissionError("; ".join(decision.reasons))
        self.ssm.put_parameter(Name=self.name, Value=portfolio_id, Type="String", Overwrite=False)
