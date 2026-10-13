"""Render or apply the separately authorized beta payment boundary; never edit old boundaries."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infra.research_payments import STACK_NAME, template


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    doc = template(); args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n")
    if not args.apply:
        print("Rendered isolated boundary; no AWS changes"); return
    import boto3
    c = boto3.client("cloudformation", region_name="us-east-2")
    kwargs = dict(StackName=STACK_NAME, TemplateBody=json.dumps(doc), Capabilities=["CAPABILITY_NAMED_IAM"],
                  Tags=[{"Key": k, "Value": v} for k, v in {"project": "finplan", "owner-repo": "financialplanning", "environment": "beta", "logical-role": "research-payment-boundary"}.items()])
    try: c.describe_stacks(StackName=STACK_NAME)
    except c.exceptions.ClientError as exc:
        if "does not exist" not in str(exc): raise
        c.create_stack(**kwargs); print("Isolated boundary creation requested"); return
    try: c.update_stack(**kwargs); print("Isolated boundary update requested")
    except c.exceptions.ClientError as exc:
        if "No updates are to be performed" not in str(exc): raise
        print("Isolated boundary already current")


if __name__ == "__main__": main()
