"""Tests for the pure SEC filing event rules."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from lib_strategy.sec_filing_rules import (
    GATE_RELEVANT_FORMS,
    SEC_FILING_COVERAGE_TOLERANCE,
    SEC_FILING_EVIDENCE_SCOPE,
    SEC_FILING_FETCH_LOOKBACK,
    SEC_FILING_RULES,
    SEC_FILING_RULES_VERSION,
    SecFilingCoverage,
    SecFilingEvent,
    SecFilingGateReason,
    parse_sec_items,
    sec_filing_gate_reason,
)

T = datetime(2026, 12, 31, 22, 0, tzinfo=UTC)
_N = iter(range(1, 10_000))


def _event(form: str, items: Sequence[str], when: datetime) -> SecFilingEvent:
    n = next(_N)
    return SecFilingEvent(
        accession=f"0000000000-26-{n:06d}",
        form=form,
        items=tuple(items),
        acceptance_time=when,
        observation_id=f"obs-{n}",
    )


def _cov(
    events: Sequence[SecFilingEvent] = (),
    *,
    window_end: datetime | None = None,
    has_older_page: bool = False,
    oldest: object = "default",
) -> SecFilingCoverage:
    return SecFilingCoverage(
        window_end=window_end if window_end is not None else T - timedelta(hours=1),
        oldest_listed_filing_date=(T - timedelta(days=800)).date()
        if oldest == "default"
        else oldest,  # type: ignore[arg-type]
        has_older_page=has_older_page,
        listed_accessions=frozenset(e.accession for e in events),
        observation_id="obs-cov",
    )


def _run(events: Sequence[SecFilingEvent], coverage: SecFilingCoverage | None = None):
    cov = coverage if coverage is not None else _cov(events)
    return sec_filing_gate_reason(events=events, coverage=cov, cutoff=T)


def test_constants():
    assert SEC_FILING_RULES_VERSION == "sec-filing-rules-v1"
    assert timedelta(days=730) == SEC_FILING_EVIDENCE_SCOPE
    assert timedelta(hours=6) == SEC_FILING_COVERAGE_TOLERANCE
    assert timedelta(days=760) == SEC_FILING_FETCH_LOOKBACK
    assert {"8-K", "8-K/A", "NT 10-K", "NT 10-K/A", "NT 10-Q", "NT 10-Q/A"} == GATE_RELEVANT_FORMS


def test_rule_table_matches_spec():
    table = {(r.item, r.window.days, r.reason.value) for r in SEC_FILING_RULES}
    assert table == {
        ("4.02", 365, "sec_non_reliance"),
        ("1.03", 730, "sec_bankruptcy_or_receivership"),
        (None, 180, "sec_late_periodic_report"),
        ("2.06", 120, "sec_material_impairment"),
    }
    assert all(r.window >= timedelta(days=100) for r in SEC_FILING_RULES)


def test_reason_precedence_order():
    assert [r.value for r in SecFilingGateReason] == [
        "sec_filing_coverage_unavailable",
        "sec_filing_evidence_invalid",
        "sec_bankruptcy_or_receivership",
        "sec_non_reliance",
        "sec_late_periodic_report",
        "sec_material_impairment",
    ]


@pytest.mark.parametrize(("item", "days"), [("4.02", 365), ("1.03", 730), ("2.06", 120)])
def test_window_edges(item, days):
    inside = _event("8-K", (item,), T - timedelta(days=days) + timedelta(seconds=1))
    edge = _event("8-K", (item,), T - timedelta(days=days))
    assert _run([inside]).reason is not None
    assert _run([edge]).reason is None


def test_late_periodic_window_edges():
    inside = _event("NT 10-K", (), T - timedelta(days=180) + timedelta(seconds=1))
    edge = _event("NT 10-K", (), T - timedelta(days=180))
    assert _run([inside]).reason is SecFilingGateReason.LATE_PERIODIC_REPORT
    assert _run([edge]).reason is None


def test_event_at_cutoff_fires():
    assert _run([_event("8-K", ("4.02",), T)]).reason is SecFilingGateReason.NON_RELIANCE


def test_amendment_counts():
    assert _run([_event("8-K/A", ("4.02",), T - timedelta(days=5))]).reason is (
        SecFilingGateReason.NON_RELIANCE
    )


def test_nt_form_fires_without_items():
    result = _run([_event("NT 10-Q/A", (), T - timedelta(days=5))])
    assert result.reason is SecFilingGateReason.LATE_PERIODIC_REPORT


def test_unrelated_item_does_not_fire():
    assert _run([_event("8-K", ("5.02",), T - timedelta(days=5))]).reason is None


def test_malformed_item_in_scope_is_invalid():
    result = _run([_event("8-K", ("Item 4.02",), T - timedelta(days=700))])
    assert result.reason is SecFilingGateReason.EVIDENCE_INVALID


def test_malformed_item_out_of_scope_ignored():
    assert _run([_event("8-K", ("Item 4.02",), T - timedelta(days=731))]).reason is None


def test_empty_items_8k_is_invalid():
    assert _run([_event("8-K", (), T - timedelta(days=3))]).reason is (
        SecFilingGateReason.EVIDENCE_INVALID
    )


def test_missing_coverage():
    result = sec_filing_gate_reason(events=[], coverage=None, cutoff=T)
    assert result.reason is SecFilingGateReason.COVERAGE_UNAVAILABLE


def test_stale_coverage():
    cov = _cov(window_end=T - timedelta(hours=6, seconds=1))
    assert _run([], cov).reason is SecFilingGateReason.COVERAGE_UNAVAILABLE
    assert _run([], _cov(window_end=T - timedelta(hours=6))).reason is None


def test_coverage_after_cutoff_rejected():
    cov = _cov(window_end=T + timedelta(seconds=1))
    assert _run([], cov).reason is SecFilingGateReason.COVERAGE_UNAVAILABLE


def test_shallow_pages():
    cov = _cov(oldest=(T - timedelta(days=700)).date(), has_older_page=True)
    assert _run([], cov).reason is SecFilingGateReason.COVERAGE_UNAVAILABLE


def test_shallow_pages_no_older_page_ok():
    cov = _cov(oldest=(T - timedelta(days=700)).date(), has_older_page=False)
    assert _run([], cov).reason is None


def test_older_page_with_no_listed_filings_is_unavailable():
    cov = _cov(oldest=None, has_older_page=True)
    assert _run([], cov).reason is SecFilingGateReason.COVERAGE_UNAVAILABLE


def test_listed_accession_without_event():
    cov = SecFilingCoverage(
        window_end=T - timedelta(hours=1),
        oldest_listed_filing_date=(T - timedelta(days=800)).date(),
        has_older_page=False,
        listed_accessions=frozenset({"0000000000-26-999999"}),
        observation_id="obs-cov",
    )
    assert _run([], cov).reason is SecFilingGateReason.EVIDENCE_INVALID


def test_precedence_and_fired_list():
    events = [
        _event("8-K", ("2.06",), T - timedelta(days=10)),
        _event("8-K", ("1.03",), T - timedelta(days=20)),
    ]
    result = _run(events)
    assert result.reason is SecFilingGateReason.BANKRUPTCY_OR_RECEIVERSHIP
    assert result.fired == (
        SecFilingGateReason.BANKRUPTCY_OR_RECEIVERSHIP,
        SecFilingGateReason.MATERIAL_IMPAIRMENT,
    )
    assert result.accessions == tuple(sorted(e.accession for e in events))
    assert result.observation_ids == tuple(sorted(["obs-cov", *(e.observation_id for e in events)]))


def test_fired_deduplicated():
    events = [_event("8-K", ("4.02",), T - timedelta(days=d)) for d in (3, 4)]
    assert _run(events).fired == (SecFilingGateReason.NON_RELIANCE,)


def test_no_events_clean():
    result = _run([])
    assert result.reason is None
    assert result.fired == ()
    assert result.accessions == ()
    assert result.observation_ids == ("obs-cov",)


def test_naive_cutoff_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        sec_filing_gate_reason(events=[], coverage=_cov(), cutoff=datetime(2026, 12, 31, 22, 0))


def test_naive_event_time_rejected():
    ev = _event("8-K", ("4.02",), datetime(2026, 12, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        _run([ev])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["4.02"], ("4.02",)),
        ([" 1.03 ", "9.01"], ("1.03", "9.01")),
        ([], None),
        (["Item 4.02"], None),
        (["4.2"], None),
        (["4.02", ""], None),
        (["\uff14.\uff10\uff12"], None),
    ],
)
def test_parse_sec_items(raw, expected):
    assert parse_sec_items(raw) == expected
