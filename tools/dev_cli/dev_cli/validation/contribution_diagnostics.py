"""Complete-ledger contribution attribution and removal diagnostics."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime

from dev_cli.validation._diagnostic_common import (
    invalid,
    require_finite,
    require_nonempty,
    require_utc,
)


@dataclass(frozen=True, slots=True)
class TradeContribution:
    """One additive net contribution used for removal diagnostics."""

    trade_id: str
    asset: str
    exit_ts: datetime
    net_contribution: float

    def __post_init__(self) -> None:
        require_nonempty(self.trade_id, field_name="trade_id")
        require_nonempty(self.asset, field_name="asset")
        require_utc(self.exit_ts, field_name="exit_ts")
        require_finite(self.net_contribution, field_name="net_contribution")

    def to_dict(self) -> dict[str, object]:
        return {
            "trade_id": self.trade_id,
            "asset": self.asset,
            "exit_ts": self.exit_ts.isoformat(),
            "net_contribution": self.net_contribution,
        }


@dataclass(frozen=True, slots=True)
class TerminalPositionContribution:
    """One open terminal position's exact daily-rebalanced contribution."""

    position_id: str
    asset: str
    entry_ts: datetime
    final_economic_ts: datetime
    net_contribution: float

    def __post_init__(self) -> None:
        require_nonempty(self.position_id, field_name="position_id")
        require_nonempty(self.asset, field_name="asset")
        require_utc(self.entry_ts, field_name="entry_ts")
        require_utc(self.final_economic_ts, field_name="final_economic_ts")
        if self.final_economic_ts < self.entry_ts:
            invalid("terminal final economic timestamp must not precede entry")
        require_finite(self.net_contribution, field_name="net_contribution")

    def to_dict(self) -> dict[str, object]:
        return {
            "position_id": self.position_id,
            "asset": self.asset,
            "entry_ts": self.entry_ts.isoformat(),
            "final_economic_ts": self.final_economic_ts.isoformat(),
            "net_contribution": self.net_contribution,
        }


@dataclass(frozen=True, slots=True)
class AttributedContribution:
    """One complete additive contribution attributed to an asset and timestamp."""

    contribution_id: str
    asset: str
    timestamp: datetime
    net_contribution: float

    def __post_init__(self) -> None:
        require_nonempty(self.contribution_id, field_name="contribution_id")
        require_nonempty(self.asset, field_name="asset")
        require_utc(self.timestamp, field_name="timestamp")
        require_finite(self.net_contribution, field_name="net_contribution")

    def to_dict(self) -> dict[str, object]:
        return {
            "contribution_id": self.contribution_id,
            "asset": self.asset,
            "timestamp": self.timestamp.isoformat(),
            "net_contribution": self.net_contribution,
        }


@dataclass(frozen=True, slots=True)
class RemovalDiagnostic:
    """Additive result after removing one pre-defined contribution bucket."""

    dimension: str
    removed_bucket: str
    removed_contribution: float
    contribution_after_removal: float
    share_of_positive_contribution: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContributionRobustnessSummary:
    """Best-trade/year/asset removal and concentration evidence."""

    attributed_observations: int
    closed_trades: int
    total_contribution: float
    total_positive_contribution: float
    best_closed_trade: RemovalDiagnostic
    best_asset_year: RemovalDiagnostic
    best_calendar_year: RemovalDiagnostic
    best_asset: RemovalDiagnostic
    all_asset_removals: tuple[RemovalDiagnostic, ...]
    maximum_asset_positive_share: float
    maximum_calendar_year_positive_share: float
    maximum_asset_or_year_positive_share: float
    evidence_role: str = field(default="retrospective_robustness_diagnostic", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "attributed_observations": self.attributed_observations,
            "closed_trades": self.closed_trades,
            "total_contribution": self.total_contribution,
            "total_positive_contribution": self.total_positive_contribution,
            "best_closed_trade": self.best_closed_trade.to_dict(),
            "best_asset_year": self.best_asset_year.to_dict(),
            "best_calendar_year": self.best_calendar_year.to_dict(),
            "best_asset": self.best_asset.to_dict(),
            "all_asset_removals": [item.to_dict() for item in self.all_asset_removals],
            "maximum_asset_positive_share": self.maximum_asset_positive_share,
            "maximum_calendar_year_positive_share": self.maximum_calendar_year_positive_share,
            "maximum_asset_or_year_positive_share": self.maximum_asset_or_year_positive_share,
            "evidence_role": self.evidence_role,
        }


def _group_contributions(
    contributions: Sequence[AttributedContribution],
    *,
    dimension: str,
) -> tuple[tuple[str, float], ...]:
    grouped: dict[str, float] = {}
    for contribution in contributions:
        if dimension == "asset_year":
            key = f"{contribution.asset}:{contribution.timestamp.year}"
        elif dimension == "calendar_year":
            key = str(contribution.timestamp.year)
        elif dimension == "asset":
            key = contribution.asset
        else:
            invalid("unsupported contribution grouping dimension")
        grouped[key] = grouped.get(key, 0.0) + contribution.net_contribution
    return tuple(grouped.items())


def _removal(
    grouped: Sequence[tuple[str, float]],
    *,
    dimension: str,
    total: float,
) -> RemovalDiagnostic:
    if not grouped:
        invalid("removal diagnostic requires at least one contribution bucket")
    _best_index, (bucket, contribution) = max(
        enumerate(grouped),
        key=lambda item: (item[1][1], -item[0]),
    )
    positive_total = sum(max(0.0, value) for _label, value in grouped)
    share = max(0.0, contribution) / positive_total if positive_total > 0.0 else 0.0
    return RemovalDiagnostic(
        dimension=dimension,
        removed_bucket=bucket,
        removed_contribution=contribution,
        contribution_after_removal=total - contribution,
        share_of_positive_contribution=share,
    )


def summarize_contribution_robustness(
    closed_trade_contributions: Sequence[TradeContribution],
    *,
    attributed_contributions: Sequence[AttributedContribution],
) -> ContributionRobustnessSummary:
    """Summarize four removals over closed-trade and complete P&L ledgers."""

    trades = tuple(
        sorted(closed_trade_contributions, key=lambda item: (item.exit_ts, item.trade_id))
    )
    if not trades:
        invalid("contribution robustness requires at least one closed trade")
    if len({item.trade_id for item in trades}) != len(trades):
        invalid("trade contribution ids must be unique")
    attributed = tuple(
        sorted(
            attributed_contributions,
            key=lambda item: (item.timestamp, item.contribution_id),
        )
    )
    if not attributed:
        invalid("complete attributed contributions must not be empty")
    if len({item.contribution_id for item in attributed}) != len(attributed):
        invalid("attributed contribution ids must be unique")
    total = sum(item.net_contribution for item in attributed)
    positive_total = sum(max(0.0, item.net_contribution) for item in attributed)
    grouped_trade = tuple((item.trade_id, item.net_contribution) for item in trades)
    grouped_asset_year = _group_contributions(attributed, dimension="asset_year")
    grouped_year = _group_contributions(attributed, dimension="calendar_year")
    grouped_asset = _group_contributions(attributed, dimension="asset")
    asset_removals = tuple(
        RemovalDiagnostic(
            dimension="asset",
            removed_bucket=asset,
            removed_contribution=value,
            contribution_after_removal=total - value,
            share_of_positive_contribution=(
                max(0.0, value) / sum(max(0.0, item) for _key, item in grouped_asset)
                if any(item > 0.0 for _key, item in grouped_asset)
                else 0.0
            ),
        )
        for asset, value in grouped_asset
    )
    best_asset = _removal(grouped_asset, dimension="asset", total=total)
    best_year = _removal(grouped_year, dimension="calendar_year", total=total)
    return ContributionRobustnessSummary(
        attributed_observations=len(attributed),
        closed_trades=len(trades),
        total_contribution=total,
        total_positive_contribution=positive_total,
        best_closed_trade=_removal(grouped_trade, dimension="trade", total=total),
        best_asset_year=_removal(grouped_asset_year, dimension="asset_year", total=total),
        best_calendar_year=best_year,
        best_asset=best_asset,
        all_asset_removals=asset_removals,
        maximum_asset_positive_share=best_asset.share_of_positive_contribution,
        maximum_calendar_year_positive_share=best_year.share_of_positive_contribution,
        maximum_asset_or_year_positive_share=max(
            best_asset.share_of_positive_contribution,
            best_year.share_of_positive_contribution,
        ),
    )
