#!/usr/bin/env python
"""Verify the go-live soak acceptance criteria against the live database.

Encodes the ``docs/DEPLOYMENT.md`` *Promotion acceptance criteria* 14-day soak
signals as a single pass/fail check so the certification has real teeth instead
of being eyeballed off dashboards. Run it at the end of the paper soak; feed its
JSON to ``write_sandbox_certification_marker.py`` so a ``passed`` marker cannot
be written while any signal is red.

Usage
-----
    DATABASE_URL=postgresql://trader:<pw>@localhost:5432/vm_trading \\
        python scripts/check_soak_acceptance.py

    # Machine-readable report for the certification marker:
    DATABASE_URL=... python scripts/check_soak_acceptance.py \\
        --json --output soak_acceptance.json

Exit code is 0 when every check passes, 1 otherwise. ``ALERT_*`` environment
variables determine the alert-sink check (same vars the services read).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _rel in ("lib_common", "lib_strategy", "lib_application"):
    sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / _rel))

from lib_application.db.session import (  # noqa: E402 - must follow sys.path.insert
    create_engine_for_env,
    dispose_engine,
    get_session_factory,
)
from lib_application.services.soak_acceptance import (  # noqa: E402
    SoakReport,
    SoakThresholds,
    check_soak_acceptance,
)
from lib_common.alerting import (  # noqa: E402
    Alert,
    build_publisher_from_env,
    build_sinks_from_env,
)
from lib_common.env_utils import parse_bool_env  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat-max-age-s", type=int, default=None)
    parser.add_argument("--market-data-max-age-s", type=int, default=None)
    parser.add_argument("--signal-max-age-s", type=int, default=None)
    parser.add_argument("--outbox-pending-max", type=int, default=None)
    parser.add_argument("--min-executions", type=int, default=None)
    parser.add_argument("--nav-max-age-days", type=int, default=None)
    parser.add_argument("--feedback-service", type=str, default=None)
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--output", type=str, default=None, help="write the JSON report to a file")
    parser.add_argument(
        "--interval-s",
        type=int,
        default=None,
        help="run continuously every N seconds as a soak monitor (alerting on every "
        "failing iteration via ALERT_* sinks); default is a single one-shot check",
    )
    return parser.parse_args()


def _thresholds(args: argparse.Namespace) -> SoakThresholds:
    defaults = SoakThresholds()
    return SoakThresholds(
        heartbeat_max_age_s=args.heartbeat_max_age_s or defaults.heartbeat_max_age_s,
        market_data_max_age_s=args.market_data_max_age_s or defaults.market_data_max_age_s,
        signal_max_age_s=args.signal_max_age_s or defaults.signal_max_age_s,
        outbox_pending_max=args.outbox_pending_max or defaults.outbox_pending_max,
        min_executions=args.min_executions or defaults.min_executions,
        nav_max_age_days=args.nav_max_age_days or defaults.nav_max_age_days,
        feedback_service=args.feedback_service or defaults.feedback_service,
    )


def _emit(report: SoakReport, args: argparse.Namespace) -> None:
    payload = report.to_dict()
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2))
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        verdict = "PASS" if report.passed else "FAIL"
        sys.stdout.write(f"Soak acceptance: {verdict}\n")
        for check in report.checks:
            mark = "✓" if check.passed else "✗"
            sys.stdout.write(f"  {mark} {check.name}: {check.detail}\n")
    sys.stdout.flush()


def _run_once(
    session_factory: Any, *, thresholds: SoakThresholds, alerts_deliverable: bool
) -> SoakReport:
    with session_factory() as session:
        return check_soak_acceptance(
            session,
            now=datetime.now(UTC),
            alerts_deliverable=alerts_deliverable,
            thresholds=thresholds,
        )


def _run_loop(
    session_factory: Any,
    args: argparse.Namespace,
    thresholds: SoakThresholds,
    alerts_deliverable: bool,
) -> int:
    """Continuous soak monitor: re-check every --interval-s, alert on failure.

    Runs as a long-lived sidecar so a stall/dead-letter/missing-execution at
    hour 3 is caught mid-soak instead of only by an end-of-soak one-shot.
    """
    publisher = build_publisher_from_env(
        enabled=parse_bool_env("EXECUTION_ALERTS_ENABLED", default=False)
    )
    sys.stdout.write(f"[soak-monitor] continuous mode, interval={args.interval_s}s\n")
    sys.stdout.flush()
    try:
        while True:
            report = _run_once(
                session_factory, thresholds=thresholds, alerts_deliverable=alerts_deliverable
            )
            _emit(report, args)
            if not report.passed:
                failed = {c.name: c.detail for c in report.checks if not c.passed}
                publisher.publish(
                    Alert(
                        event_type="soak_check_failed",
                        severity="critical",
                        message=f"Soak acceptance failing: {', '.join(failed)}",
                        payload=failed,
                        source="soak_monitor",
                    )
                )
            time.sleep(args.interval_s)
    except KeyboardInterrupt:
        sys.stdout.write("[soak-monitor] stopped\n")
        return 0


def main() -> int:
    args = parse_args()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.stderr.write("DATABASE_URL is required\n")
        return 2

    engine = create_engine_for_env(db_url=db_url)
    try:
        # Alerts only reach a human when a sink is configured AND alerting is
        # enabled — a configured-but-disabled sink delivers nothing.
        alerts_deliverable = bool(build_sinks_from_env()) and parse_bool_env(
            "EXECUTION_ALERTS_ENABLED", default=False
        )
        session_factory = get_session_factory(engine=engine)
        thresholds = _thresholds(args)
        if args.interval_s:
            return _run_loop(session_factory, args, thresholds, alerts_deliverable)
        report = _run_once(
            session_factory, thresholds=thresholds, alerts_deliverable=alerts_deliverable
        )
    finally:
        dispose_engine(engine)

    _emit(report, args)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
