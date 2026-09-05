#!/usr/bin/env python
"""Reconcile the full pipeline after a local paper soak (e2e-verification utility).

Companion to ``scripts/check_soak_acceptance.py`` (the programmatic go-live gate):
this prints a per-stage activity + reconciliation report over a recent window so a
human can confirm signals flowed end to end — signals → scores → decisions →
executions → outbox → positions → P&L → feedback — and flags the invariant
violations that matter (duplicate scores/venue trade identities, dead-lettered
events, no attributed canonical fills, stale feedback heartbeat).

Usage
-----
    DATABASE_URL=postgresql://trader:<pw>@localhost:5432/vm_trading \\
        python scripts/verify_pipeline_soak.py --hours 24

    # Machine-readable:
    DATABASE_URL=... python scripts/verify_pipeline_soak.py --hours 24 --json \\
        --output soak_report.json

Exit code is 0 when every invariant section holds, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _rel in ("lib_common", "lib_data", "lib_strategy", "lib_application"):
    sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / _rel))

from lib_application.db.session import (  # noqa: E402 - must follow sys.path.insert
    create_engine_for_env,
    dispose_engine,
    get_session_factory,
)
from lib_application.services.soak_report import (  # noqa: E402
    build_soak_report,
    render_markdown,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0, help="lookback window in hours")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--output", type=str, default=None, help="write the JSON report to a file")
    parser.add_argument(
        "--no-require-feedback",
        dest="require_feedback",
        action="store_false",
        help="treat a missing/stale feedback heartbeat as informational, not a failure "
        "(for soaks run without the feedback-loop-engine service)",
    )
    parser.set_defaults(require_feedback=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.stderr.write("DATABASE_URL is required\n")
        return 2

    now = datetime.now(UTC)
    since = now - timedelta(hours=args.hours)
    engine = create_engine_for_env(db_url=db_url)
    try:
        session_factory = get_session_factory(engine=engine)
        with session_factory() as session:
            report = build_soak_report(
                session, now=now, since=since, require_feedback=args.require_feedback
            )
    finally:
        dispose_engine(engine)

    payload = report.to_dict()
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2, default=str))
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(render_markdown(report) + "\n")

    # A degraded report (a stage could not run, e.g. schema behind head) is not a
    # trustworthy pass — fail closed.
    return 0 if (report.passed and not report.degraded) else 1


if __name__ == "__main__":
    raise SystemExit(main())
