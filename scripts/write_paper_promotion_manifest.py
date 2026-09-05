#!/usr/bin/env python
"""Write the exact evidence manifest consumed by the paper-strategy gate."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / "lib_common"))

from lib_common.paper_promotion import (  # noqa: E402
    PAPER_PROMOTION_EVIDENCE_NAMES,
    PAPER_PROMOTION_MODEL_SCOPES,
    build_paper_promotion_manifest,
)

DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / ".artifacts"
DEFAULT_OUTPUT = DEFAULT_ARTIFACT_ROOT / "paper_strategy_promotion.json"
DEFAULT_CONFIG = PROJECT_ROOT / "strategies" / "indicator" / "SwingHighLowPMO" / "config.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--broker-account-id", required=True, type=int)
    parser.add_argument("--strategy-binding-id", required=True, type=int)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--model-scope",
        choices=sorted(PAPER_PROMOTION_MODEL_SCOPES),
        help="Optional override; normally inferred from the strategy config.",
    )
    parser.add_argument(
        "--broker-code",
        help="Optional override; normally read from deployment.paper_candidate.",
    )
    parser.add_argument(
        "--model-configuration-sha256",
        help=(
            "Required for synchronized_portfolio: pre-start model configuration "
            "digest carried by each admitted rebalance."
        ),
    )
    parser.add_argument(
        "--instrument-set-artifact",
        type=Path,
        help=(
            "Required for synchronized_portfolio: reviewed JSON allowlist artifact "
            "below --artifact-root."
        ),
    )
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "Required evidence artifact below --artifact-root. Repeat once for each of: "
            + ", ".join(sorted(PAPER_PROMOTION_EVIDENCE_NAMES))
        ),
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Manifest path below --artifact-root, or '-' for stdout.",
    )
    return parser.parse_args()


def _parse_evidence(values: list[str], *, artifact_root: Path) -> dict[str, Path]:
    evidence: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        name = name.strip()
        raw_path = raw_path.strip()
        if separator != "=" or not name or not raw_path:
            msg = f"invalid --evidence {value!r}; expected NAME=PATH"
            raise ValueError(msg)
        if name in evidence:
            msg = f"duplicate --evidence name: {name}"
            raise ValueError(msg)
        path = Path(raw_path)
        evidence[name] = path if path.is_absolute() else artifact_root / path
    return evidence


def _write_atomic_json(
    payload: dict[str, object],
    *,
    output: str,
    artifact_root: Path,
) -> Path | None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output == "-":
        sys.stdout.write(rendered)
        return None

    artifact_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    resolved_root = artifact_root.resolve(strict=True)
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = artifact_root / output_path
    resolved_output = output_path.resolve()
    try:
        resolved_output.relative_to(resolved_root)
    except ValueError as exc:
        msg = f"--output must remain below --artifact-root ({resolved_root})"
        raise ValueError(msg) from exc
    resolved_output.parent.mkdir(mode=0o750, parents=True, exist_ok=True)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=resolved_output.parent,
            prefix=f".{resolved_output.name}.",
            delete=False,
        ) as temporary:
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())
            temp_path = Path(temporary.name)
        temp_path.chmod(0o640)
        temp_path.replace(resolved_output)
    finally:
        if temp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()
    return resolved_output


def main() -> None:
    args = parse_args()
    try:
        manifest = build_paper_promotion_manifest(
            config_path=args.config,
            artifact_root=args.artifact_root,
            evidence_paths=_parse_evidence(
                args.evidence,
                artifact_root=args.artifact_root,
            ),
            user_id=args.user_id,
            broker_account_id=args.broker_account_id,
            strategy_binding_id=args.strategy_binding_id,
            image_tag=args.image_tag,
            operator=args.operator,
            model_scope=args.model_scope,
            broker_code=args.broker_code,
            model_configuration_sha256=args.model_configuration_sha256,
            instrument_set_artifact=args.instrument_set_artifact,
        )
        output_path = _write_atomic_json(
            manifest,
            output=args.output,
            artifact_root=args.artifact_root,
        )
    except (OSError, TypeError, ValueError) as exc:
        msg = f"paper promotion manifest refused: {exc}"
        raise SystemExit(msg) from exc
    if output_path is not None:
        print(output_path)


if __name__ == "__main__":
    main()
