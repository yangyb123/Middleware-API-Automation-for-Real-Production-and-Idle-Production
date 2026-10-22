#!/usr/bin/env python3
"""Validate the exact JMX before execution and its JTL after execution."""

import argparse
import hashlib
from pathlib import Path
import sys

import jtl_fail_report as report


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_transactions(jmx, jtl=None, minimum=1):
    if minimum < 0:
        raise ValueError("minimum must not be negative")
    plan = report.read_transaction_plan(jmx)
    result = {
        "jmx": plan["path"],
        "jmx_sha256": file_sha256(jmx),
        "enabled_controllers": len(plan["enabled_labels"]),
        "minimum": minimum,
        "errors": [],
    }
    if result["enabled_controllers"] < minimum:
        result["errors"].append(
            f"JMX enables {result['enabled_controllers']} transaction controllers; expected at least {minimum}. "
            "Check the exact -t file and disabled ancestor nodes."
        )
    if jtl is not None:
        rows = report.parse_jtl(jtl)
        report.apply_transaction_plan(rows, plan)
        summary = report.summarize([], rows)
        result.update({
            "jtl": str(Path(jtl).resolve()),
            "jtl_sha256": file_sha256(jtl),
            "sample_total": summary["sample_total"],
            "transaction_total": summary["transaction_total"],
            "transaction_failed": summary["transaction_failed"],
            "diagnostics": summary["transaction_diagnostics"],
        })
        if result["transaction_total"] < minimum:
            result["errors"].append(
                f"JTL contains {result['transaction_total']} transaction executions; expected at least {minimum}. "
                "Check the executed JMX, result saving settings and the exact report input. "
                "JMX definition counts cannot replace missing execution records."
            )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jmx", required=True, help="Exact JMeter -t file; never a wildcard.")
    parser.add_argument("--jtl", help="Exact JMeter -l file to check after execution.")
    parser.add_argument("--minimum-transactions", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        result = check_transactions(args.jmx, args.jtl, args.minimum_transactions)
    except (OSError, ValueError, report.ET.ParseError) as exc:
        print(f"Transaction validation failed: {exc}", file=sys.stderr)
        return 2
    for key, value in result.items():
        if key != "errors":
            print(f"{key}: {value}")
    for message in result["errors"]:
        print(f"ERROR: {message}", file=sys.stderr)
    return 3 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
