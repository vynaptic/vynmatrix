"""Write the Coinbase sandbox certification marker consumed by live gating."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

MIN_PAPER_SOAK_DAYS = 14


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write sandbox certification marker")
    parser.add_argument("--status", choices=["passed", "failed"], default="passed")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--symbols", default="BTC-USDC,ETH-USDC,SOL-USDC")
    parser.add_argument("--paper-window-days", type=int, default=14)
    parser.add_argument("--duplicate-submission-count", type=int, default=0)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--sandbox-smoke-evidence", required=True)
    parser.add_argument("--paper-soak-evidence", required=True)
    parser.add_argument("--reconciliation-summary", required=True)
    parser.add_argument(
        "--acceptance-report",
        default=None,
        help=(
            "Path to the JSON report from scripts/check_soak_acceptance.py. "
            "Required (and must be passing) for a 'passed' marker."
        ),
    )
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--output",
        default=".artifacts/coinbase_sandbox_certified.json",
        help="Marker output path",
    )
    return parser.parse_args()


def _load_passing_acceptance_report(path_str: str | None) -> dict:
    """Load + validate the soak acceptance report; raise unless it passed."""
    if not path_str:
        msg = (
            "--acceptance-report is required for a passed marker "
            "(run scripts/check_soak_acceptance.py --json --output <path>)"
        )
        raise SystemExit(msg)
    report_path = Path(path_str)
    if not report_path.is_file():
        msg = f"acceptance report not found: {report_path}"
        raise SystemExit(msg)
    report: dict = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("passed"):
        failed = [c["name"] for c in report.get("checks", []) if not c.get("passed")]
        msg = f"soak acceptance report did not pass; failing checks: {failed or 'unknown'}"
        raise SystemExit(msg)
    return report


def main() -> None:
    args = parse_args()
    acceptance_report: dict | None = None
    if args.status == "passed":
        if args.paper_window_days < MIN_PAPER_SOAK_DAYS:
            msg = "--paper-window-days must be at least 14 for a passed marker"
            raise SystemExit(msg)
        if args.duplicate_submission_count != 0:
            msg = "--duplicate-submission-count must be 0 for a passed marker"
            raise SystemExit(msg)
        acceptance_report = _load_passing_acceptance_report(args.acceptance_report)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": args.status,
        "completed_at": datetime.now(tz=timezone.utc).isoformat(),
        "commit": args.commit,
        "symbols": [item.strip() for item in args.symbols.split(",") if item.strip()],
        "paper_window_days": args.paper_window_days,
        "duplicate_submission_count": args.duplicate_submission_count,
        "operator": args.operator,
        "sandbox_smoke_evidence": args.sandbox_smoke_evidence,
        "paper_soak_evidence": args.paper_soak_evidence,
        "reconciliation_summary": args.reconciliation_summary,
        "acceptance_report": acceptance_report,
        "notes": args.notes,
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
