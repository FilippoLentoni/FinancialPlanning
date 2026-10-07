"""Live-financial permission scan (``finplan-conformance live-perm-scan``; ENV-05).

No role created by the four pipelines may hold permissions or credentials for live
trading, brokerage or exchange accounts (including Coinbase), AgentCore payments or
wallet spending (environment-promotion, "Live financial permissions are separated").

Input: synthesized CloudFormation templates (JSON or YAML, short-form tags allowed) or
bare IAM policy documents. Every object with a ``Statement`` member anywhere in the
document is treated as a policy (identity policies, managed policies, inline role
policies, permission boundaries, resource and key policies).

Findings:

``live-action``
    an ``Allow`` statement whose ``Action`` names a live-financial action: an action
    in a live-financial service (:data:`LIVE_SERVICES`), or an action whose name
    contains a live-financial keyword (:data:`LIVE_ACTION_KEYWORDS`, e.g. ``payment``,
    ``wallet``, ``trade``, order placement, ``withdraw``, fund transfers).
``live-wildcard``
    an ``Allow`` statement whose action pattern (``*``, ``service:*``, ``svc:Create*``)
    matches an entry of the representative live-financial action catalogue
    (:data:`LIVE_ACTION_CATALOGUE`), or an ``Allow`` with ``NotAction`` that does not
    exclude them; such a grant includes live-financial actions. Actions denied
    unconditionally on all resources in the same policy document (a permission
    boundary that allows ``*`` and denies the live-financial list) are not findings.
``live-secret``
    a reference to a live-financial credential: a secret ARN, secret name, SSM
    ``secret-ref`` name or ``{{resolve:secretsmanager:...}}`` reference containing a
    live-financial keyword (brokerage, exchange, trading, coinbase, payment, wallet ...),
    or an ``AWS::SecretsManager::Secret`` resource so named.

``Deny`` statements (the live-financial deny list in permission boundaries) are never
findings.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from .ownership import load_template

#: Services whose every action is live-financial.
LIVE_SERVICES = frozenset({"payments", "payment", "wallet", "wallets", "coinbase", "brokerage", "trading", "exchange", "crypto-exchange"})
#: Keywords that make an action name live-financial (matched case-insensitively against the action part).
LIVE_ACTION_KEYWORDS = (
    "payment",
    "wallet",
    "trade",
    "trading",
    "brokerage",
    "placeorder",
    "submitorder",
    "cancelorder",
    "withdraw",
    "transferfunds",
    "fundtransfer",
    "spend",
)
#: Ordinary AWS actions whose names contain a keyword but are not live-financial.
NON_LIVE_ACTIONS = frozenset({"s3:getbucketrequestpayment", "s3:putbucketrequestpayment"})
#: Representative live-financial actions used to evaluate wildcards (AgentCore payments and
#: wallet actions, brokerage/exchange trading, payment-processing services).
LIVE_ACTION_CATALOGUE = (
    "bedrock-agentcore:CreatePayment",
    "bedrock-agentcore:InvokePayment",
    "bedrock-agentcore:CreateWallet",
    "bedrock-agentcore:SpendFromWallet",
    "bedrock-agentcore:TransferFunds",
    "payments:CreatePayment",
    "payment-cryptography:CreateKey",
    "payment-cryptography-data:GenerateCardValidationData",
    "wallet:Spend",
    "coinbase:PlaceOrder",
    "brokerage:PlaceOrder",
    "trading:SubmitOrder",
)
#: Keywords that make a secret reference live-financial.
LIVE_SECRET_KEYWORDS = ("brokerage", "broker", "exchange", "trading", "trade", "coinbase", "payment", "wallet", "alpaca", "ibkr", "interactive-brokers", "robinhood", "binance", "kraken")
_SECRET_REF = re.compile(
    r"(?:arn:[^:\s\"']*:secretsmanager:[^\s\"']*:secret:[^\s\"']+|\{\{resolve:secretsmanager:[^}]+\}\}|/finplan/[^\s\"']*/secret-ref/[^\s\"']+|secret[-_ ]?(?:name|id|ref)?\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LivePermFinding:
    rule: str
    file: str
    location: str
    detail: str

    def __str__(self) -> str:
        return f"[ENV-05] {self.file}: {self.location}: [{self.rule}] {self.detail}"


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _action_strings(v: Any) -> list[str]:
    return [a for a in _as_list(v) if isinstance(a, str)]


def is_live_action(action: str) -> bool:
    if action == "*" or ":" not in action:
        return False
    if action.lower() in NON_LIVE_ACTIONS:
        return False
    service, name = action.split(":", 1)
    if service.lower() in LIVE_SERVICES or service.lower().startswith("payment"):
        return True
    if "*" in name or "?" in name:
        return False  # wildcards are evaluated against the catalogue
    low = name.lower()
    return any(k in low for k in LIVE_ACTION_KEYWORDS)


def wildcard_live_matches(pattern: str) -> list[str]:
    if "*" not in pattern and "?" not in pattern:
        return []
    pat = pattern.lower()
    return [a for a in LIVE_ACTION_CATALOGUE if fnmatch.fnmatchcase(a.lower(), pat)]


def iter_policies(node: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(node, dict):
        if "Statement" in node:
            yield path or "/", node
        for k, v in node.items():
            yield from iter_policies(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from iter_policies(v, f"{path}/{i}")


def _strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, str):
        yield path or "/", node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}/{i}")


def _live_secret_keyword(text: str) -> str | None:
    low = re.sub(r"token[-_]?exchange|exchange[-_]?token", "", text.lower())  # OAuth token exchange is not a trading exchange
    return next((k for k in LIVE_SECRET_KEYWORDS if re.search(rf"(?<![a-z]){re.escape(k)}", low)), None)


def _denied_patterns(policy: dict[str, Any]) -> list[str]:
    """Action patterns denied unconditionally on all resources within one policy document."""
    out: list[str] = []
    for st in _as_list(policy.get("Statement")):
        if isinstance(st, dict) and st.get("Effect") == "Deny" and not st.get("Condition") and "NotAction" not in st and "NotResource" not in st:
            if st.get("Resource", "*") in ("*", ["*"]):
                out += [a.lower() for a in _action_strings(st.get("Action"))]
    return out


def _is_denied(action: str, denied: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(action.lower(), d) for d in denied)


def scan_document(doc: Any, name: str = "<document>") -> list[LivePermFinding]:
    findings: list[LivePermFinding] = []
    for ppath, policy in iter_policies(doc):
        denied = _denied_patterns(policy)
        for i, st in enumerate(_as_list(policy.get("Statement"))):
            if not isinstance(st, dict) or st.get("Effect") != "Allow":
                continue
            where = f"{ppath.rstrip('/')}/Statement/{i}" + (f" ({st['Sid']})" if isinstance(st.get("Sid"), str) else "")
            for action in _action_strings(st.get("Action")):
                if is_live_action(action):
                    if not _is_denied(action, denied):
                        findings.append(LivePermFinding("live-action", name, where, f"Allow grants live-financial action {action}"))
                else:
                    hits = [a for a in wildcard_live_matches(action) if not _is_denied(a, denied)]
                    if hits:
                        findings.append(LivePermFinding("live-wildcard", name, where, f"Allow {action} includes live-financial actions such as {', '.join(hits[:3])}"))
            not_actions = _action_strings(st.get("NotAction"))
            if not_actions:
                uncovered = [
                    a for a in LIVE_ACTION_CATALOGUE if not any(fnmatch.fnmatchcase(a.lower(), n.lower()) for n in not_actions) and not _is_denied(a, denied)
                ]
                if uncovered:
                    findings.append(LivePermFinding("live-wildcard", name, where, f"Allow with NotAction grants live-financial actions such as {', '.join(uncovered[:3])}"))
            for res in _action_strings(st.get("Resource")):
                if ":secretsmanager:" in res or "secret-ref" in res:
                    kw = _live_secret_keyword(res)
                    if kw:
                        findings.append(LivePermFinding("live-secret", name, where, f"Allow on a live-financial secret ({kw})"))
    # Secret resources and secret references anywhere in the document.
    resources = doc.get("Resources") if isinstance(doc, dict) else None
    if isinstance(resources, dict):
        for lid, res in resources.items():
            if isinstance(res, dict) and res.get("Type") == "AWS::SecretsManager::Secret":
                label = " ".join(str(x) for x in (lid, (res.get("Properties") or {}).get("Name", ""), (res.get("Properties") or {}).get("Description", "")))
                kw = _live_secret_keyword(label)
                if kw:
                    findings.append(LivePermFinding("live-secret", name, f"/Resources/{lid}", f"declares a live-financial credential secret ({kw})"))
    seen = {(f.location, f.rule) for f in findings}
    for spath, s in _strings(doc):
        for m in _SECRET_REF.finditer(s):
            kw = _live_secret_keyword(m.group(0))
            if kw and not any(spath.startswith(loc.split(" ")[0]) and rule == "live-secret" for loc, rule in seen):
                findings.append(LivePermFinding("live-secret", name, spath, f"references a live-financial secret ({kw})"))
                seen.add((spath, "live-secret"))
    return findings


def scan_file(path: str | Path) -> list[LivePermFinding]:
    return scan_document(load_template(path), str(path))


def scan_paths(paths: list[Path]) -> tuple[int, list[LivePermFinding]]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.suffix in (".json", ".yaml", ".yml") and "node_modules" not in f.parts)
        else:
            files.append(p)
    findings: list[LivePermFinding] = []
    for f in files:
        try:
            findings.extend(scan_file(f))
        except (json.JSONDecodeError, ValueError) as exc:
            findings.append(LivePermFinding("unreadable", str(f), "/", f"cannot parse policy/template: {exc}"))
    return len(files), findings


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance live-perm-scan", description="Fail when a policy or secret reference grants live trading, payment or wallet access (ENV-05).")
    ap.add_argument("paths", nargs="+", type=Path, help="templates, policy documents or directories of them")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    count, findings = scan_paths(args.paths)
    if args.json:
        print(json.dumps({"files_scanned": count, "ok": not findings, "findings": [asdict(f) for f in findings]}, indent=2, sort_keys=True))
    else:
        for f in findings:
            print(f)
        print(f"{'PASS' if not findings else 'FAIL'}: live-permission scan, {count} files, {len(findings)} findings")
    return 0 if not findings else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
