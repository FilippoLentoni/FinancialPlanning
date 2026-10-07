# Budget-state writer (task 9.2; design P9; contract D4 shared runtime writer). Standalone
# (stdlib + boto3): the tooling stack inlines this file. See docs/pipeline.md.
import json
import os
import re
from datetime import UTC, datetime

PARAM = "/finplan/shared/financialplanning/config/budget-state"
_AMOUNT = r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)"


def _num(text, label):
    m = re.search(label + r"\s*:\s*>?\s*" + _AMOUNT, text, re.IGNORECASE)
    return float(m.group(1).replace(",", "")) if m else None


def classify(message):
    """'enforced' for an ACTUAL alert at or above the budgeted amount or an executed
    budget action, 'alert' for any other budget notification, None otherwise."""
    text = message if isinstance(message, str) else json.dumps(message)
    if re.search(r"budget\s+action", text, re.IGNORECASE) and re.search(r"\bexecuted\b", text, re.IGNORECASE):
        return "enforced"
    kind = re.search(r"alert\s+type\s*:\s*(ACTUAL|FORECASTED)", text, re.IGNORECASE)
    if not kind:
        return "alert" if re.search(r"budget", text, re.IGNORECASE) else None
    if kind.group(1).upper() != "ACTUAL":
        return "alert"
    pct = re.search(r"alert\s+threshold\s*:\s*>?\s*([0-9.]+)\s*%", text, re.IGNORECASE)
    if pct:
        return "enforced" if float(pct.group(1)) >= 100 else "alert"
    budgeted = _num(text, r"budgeted\s+amount")
    threshold = _num(text, r"alert\s+threshold")
    if budgeted and threshold is not None and threshold >= budgeted:
        return "enforced"
    return "alert"


def handler(event, context=None, ssm=None, now=None):
    """SNS -> budget-state flag. Never clears the flag: only a human resets it."""
    name = os.environ.get("FINPLAN_BUDGET_STATE_PARAMETER", PARAM)
    if name != PARAM:
        raise ValueError("the budget-state writer writes only " + PARAM)
    results = []
    for rec in (event or {}).get("Records", []):
        sns = rec.get("Sns") or {}
        verdict = classify(str(sns.get("Subject") or "") + "\n" + str(sns.get("Message") or ""))
        results.append(verdict)
        if verdict != "enforced":
            continue
        ts = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
        value = {"state": "enforced", "enforced": True, "since": ts, "source": "aws-budgets-notification"}
        if ssm is None:
            import boto3

            ssm = boto3.client("ssm")
        ssm.put_parameter(Name=PARAM, Value=json.dumps(value, sort_keys=True), Type="String", Overwrite=True)
    return {"results": results}
