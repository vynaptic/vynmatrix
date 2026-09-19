"""Exact per-symbol source coverage and replay for checkpointed native bar cores."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, cast

from lib_data.bars import Bar
from lib_data.consolidation import BarConsolidator
from lib_data.dataset import timeframe_interval
from lib_data.watermark import Watermark, WatermarkState
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState, ModelStateContractError
from lib_strategy.signals.utils import ensure_utc

RangeReader = Callable[[str, datetime, datetime], list[Bar]]


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        msg = "Source checkpoint timestamp must be an ISO string"
        raise TypeError(msg)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = "Source checkpoint timestamp must have a timezone"
        raise ValueError(msg)
    return ensure_utc(parsed)


def _anchor(bar: Bar) -> dict[str, Any]:
    result = {
        "timestamp": ensure_utc(bar.timestamp).isoformat(),
        "price_id": bar.metadata.get("price_id"),
        "content_revision": bar.metadata.get("content_revision"),
    }
    _validate_anchor(result)
    return result


def _validate_anchor(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) != {
        "timestamp",
        "price_id",
        "content_revision",
    }:
        msg = "Malformed source checkpoint anchor"
        raise ValueError(msg)
    _timestamp(value["timestamp"])
    if any(
        type(value[key]) is not int or value[key] < 1 for key in ("price_id", "content_revision")
    ):
        msg = "Source checkpoint anchor requires positive row identity and revision"
        raise ValueError(msg)


class BarRecovery:
    """Worker-owned source boundaries; strategy state remains in its native core."""

    def __init__(
        self,
        *,
        strategy: BarSignalStrategy,
        symbols: list[str],
        source: str,
        timeframe: str,
        consolidators: dict[str, BarConsolidator],
        read_range: RangeReader,
    ) -> None:
        self.strategy = strategy
        self.symbols = tuple(sorted(symbols))
        self.source = source
        self.timeframe = timeframe
        self.consolidators = consolidators
        self.read_range = read_range
        self.feeds: dict[str, dict[str, Any]] = {
            symbol: {"inception": None, "bootstrap_through": None, "acknowledged": None}
            for symbol in self.symbols
        }

    def seed(self, symbol: str, bars: list[Bar]) -> None:
        """Record the exact cold-start interval, including an explicit empty feed."""
        self.feeds[symbol] = {
            "inception": _anchor(bars[0]) if bars else None,
            "bootstrap_through": _anchor(bars[-1]) if bars else None,
            "acknowledged": _anchor(bars[-1]) if bars else None,
        }

    def advance(self, bar: Bar) -> None:
        feed = self.feeds[bar.symbol]
        anchor = _anchor(bar)
        previous = feed["acknowledged"]
        if previous is not None and _timestamp(anchor["timestamp"]) <= _timestamp(
            previous["timestamp"]
        ):
            msg = "Source checkpoint cannot acknowledge a repeated or older bar"
            raise ModelStateContractError(msg)
        if feed["inception"] is None:
            feed["inception"] = anchor
        feed["acknowledged"] = anchor

    def snapshot(self) -> dict[str, Any]:
        streams = self.strategy.serialize_model_state()["streams"]["symbols"]
        payload = {
            "version": 1,
            "source": self.source,
            "timeframe": self.timeframe,
            "feeds": {
                symbol: {
                    **self.feeds[symbol],
                    "strategy_through": streams.get(symbol, {}).get("last_timestamp"),
                    "consolidator": self.consolidators[symbol].snapshot_state()
                    if symbol in self.consolidators
                    else None,
                }
                for symbol in self.symbols
            },
        }
        return cast(dict[str, Any], json.loads(json.dumps(payload, allow_nan=False)))

    def _parse(self, payload: Any, states: Mapping[str, WatermarkState]) -> dict[str, Any]:
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"version", "source", "timeframe", "feeds"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
            or payload["source"] != self.source
            or payload["timeframe"] != self.timeframe
            or not isinstance(payload["feeds"], Mapping)
            or set(payload["feeds"]) != set(self.symbols)
            or set(states) != set(self.symbols)
        ):
            msg = "Incompatible complete bar-runtime checkpoint"
            raise ModelStateContractError(msg)
        feeds = cast(dict[str, Any], json.loads(json.dumps(payload["feeds"], allow_nan=False)))
        for symbol, feed in feeds.items():
            if not isinstance(feed, dict) or set(feed) != {
                "inception",
                "bootstrap_through",
                "acknowledged",
                "consolidator",
                "strategy_through",
            }:
                msg = "Bar-runtime checkpoint has incomplete symbol coverage"
                raise ModelStateContractError(msg)
            for key in ("inception", "bootstrap_through", "acknowledged"):
                _validate_anchor(feed[key])
            if feed["strategy_through"] is not None:
                _timestamp(feed["strategy_through"])
            origin, boundary, acknowledged = (
                feed["inception"],
                feed["bootstrap_through"],
                feed["acknowledged"],
            )
            if (origin is None) != (acknowledged is None):
                msg = "Source checkpoint inception and acknowledgement disagree"
                raise ModelStateContractError(msg)
            if acknowledged is None:
                if boundary is not None or not Watermark.is_initial(states[symbol].last_ts):
                    msg = "Empty source checkpoint disagrees with its watermark"
                    raise ModelStateContractError(msg)
            else:
                first, last = _timestamp(origin["timestamp"]), _timestamp(acknowledged["timestamp"])
                if first > last or last != ensure_utc(states[symbol].last_ts):
                    msg = f"Source checkpoint/watermark mismatch for {symbol}"
                    raise ModelStateContractError(msg)
                if boundary is not None and not first <= _timestamp(boundary["timestamp"]) <= last:
                    msg = "Source checkpoint bootstrap boundary is outside its observed history"
                    raise ModelStateContractError(msg)
            self._validate_bucket(symbol, feed)
        return feeds

    def _validate_bucket(self, symbol: str, feed: dict[str, Any]) -> None:
        bucket = feed["consolidator"]
        consolidator = self.consolidators.get(symbol)
        if consolidator is None:
            if bucket is not None:
                msg = "Unexpected consolidator checkpoint for raw-bar strategy"
                raise ModelStateContractError(msg)
            return
        config = consolidator.snapshot_state()
        candidate = BarConsolidator(config["period_minutes"], min_coverage=config["min_coverage"])
        candidate.restore_state(bucket)
        working = bucket["working_bar"]
        acknowledged = feed["acknowledged"]
        if working is None:
            if acknowledged is not None:
                close = _timestamp(acknowledged["timestamp"]) + timeframe_interval(self.timeframe)
                if (
                    close.second
                    or close.microsecond
                    or (close.hour * 60 + close.minute) % consolidator.period_minutes
                ):
                    msg = "Source checkpoint requires a partial bucket before its period close"
                    raise ModelStateContractError(msg)
            return
        metadata = working["metadata"]
        if (
            acknowledged is None
            or working["symbol"] != symbol
            or working["source"] != self.source
            or metadata.get("source_timeframe") != self.timeframe
            or metadata.get("price_id") != acknowledged["price_id"]
            or metadata.get("content_revision") != acknowledged["content_revision"]
            or _timestamp(metadata.get("source_price_ts")) != _timestamp(acknowledged["timestamp"])
        ):
            msg = "Partial bucket disagrees with its acknowledged source provenance"
            raise ModelStateContractError(msg)

    def _verify_anchor(self, symbol: str, anchor: dict[str, Any], *, corrected: bool) -> Bar:
        timestamp = _timestamp(anchor["timestamp"])
        rows = self.read_range(symbol, timestamp, timestamp)
        if len(rows) != 1:
            msg = f"Missing authoritative checkpoint anchor for {symbol} at {timestamp}"
            raise ModelStateContractError(msg)
        actual = _anchor(rows[0])
        if (
            actual["timestamp"] != anchor["timestamp"]
            or actual["price_id"] != anchor["price_id"]
            or (not corrected and actual["content_revision"] != anchor["content_revision"])
        ):
            msg = f"Authoritative checkpoint anchor changed for {symbol} at {timestamp}"
            raise ModelStateContractError(msg)
        return rows[0]

    def restore(self, snapshot: Mapping[str, Any], states: Mapping[str, WatermarkState]) -> None:
        feeds = self._parse(snapshot.get("bar_runtime"), states)
        for symbol, feed in feeds.items():
            for key in ("inception", "bootstrap_through", "acknowledged"):
                if feed[key] is not None:
                    self._verify_anchor(symbol, feed[key], corrected=False)
        self._validate_stream_coverage(snapshot, feeds)
        self.strategy.restore_model_state(snapshot)
        for symbol, consolidator in self.consolidators.items():
            consolidator.restore_state(feeds[symbol]["consolidator"])
        self.feeds = {
            symbol: {
                key: value
                for key, value in feed.items()
                if key not in {"consolidator", "strategy_through"}
            }
            for symbol, feed in feeds.items()
        }

    def _validate_stream_coverage(self, snapshot: Mapping[str, Any], feeds: dict[str, Any]) -> None:
        streams = snapshot["streams"]["symbols"]
        models = snapshot["symbol_states"]
        if not set(streams) <= set(self.symbols) or not set(models) <= set(self.symbols):
            msg = "Strategy checkpoint contains an unsubscribed stream"
            raise ModelStateContractError(msg)
        for symbol, feed in feeds.items():
            expected = feed["strategy_through"]
            stream = streams.get(symbol)
            actual = stream.get("last_timestamp") if stream is not None else None
            if actual != expected or (stream is not None and expected is None):
                msg = f"Strategy stream coverage disagrees with its source checkpoint for {symbol}"
                raise ModelStateContractError(msg)
            acknowledged = feed["acknowledged"]
            if expected is not None and (
                acknowledged is None
                or _timestamp(expected)
                > (_timestamp(acknowledged["timestamp"]) + timeframe_interval(self.timeframe))
            ):
                msg = "Strategy stream is newer than its acknowledged source checkpoint"
                raise ModelStateContractError(msg)
            if (
                symbol not in self.consolidators
                and acknowledged is not None
                and (
                    expected is None
                    or _timestamp(expected) != _timestamp(acknowledged["timestamp"])
                )
            ):
                msg = "Raw strategy stream does not cover its acknowledged source bar"
                raise ModelStateContractError(msg)

    def rebuild(
        self,
        snapshot: Mapping[str, Any],
        states: Mapping[str, WatermarkState],
        targets: Mapping[str, datetime],
        recent: Callable[[str, datetime], list[Bar]],
        to_market_state: Callable[[Bar], MarketState],
    ) -> Bar | None:
        """Replay raw bootstrap and decision phases without journaling old decisions."""
        feeds = self._parse(snapshot.get("bar_runtime"), states)
        self._validate_stream_coverage(snapshot, feeds)
        latest: Bar | None = None
        with self.strategy.suppress_all_emissions():
            for symbol, feed in feeds.items():
                origin = feed["inception"]
                if origin is None:
                    bars = (
                        []
                        if Watermark.is_initial(targets[symbol])
                        else recent(symbol, targets[symbol])
                    )
                    self.seed(symbol, bars)
                    boundary = self.feeds[symbol]["bootstrap_through"]
                else:
                    for key in ("inception", "bootstrap_through", "acknowledged"):
                        if feed[key] is not None:
                            self._verify_anchor(symbol, feed[key], corrected=True)
                    bars = self.read_range(symbol, _timestamp(origin["timestamp"]), targets[symbol])
                    if not bars or ensure_utc(bars[-1].timestamp) != ensure_utc(targets[symbol]):
                        msg = f"Historical rebuild lacks the acknowledged endpoint for {symbol}"
                        raise ModelStateContractError(msg)
                    boundary = feed["bootstrap_through"]
                    self.feeds[symbol] = {
                        "inception": _anchor(bars[0]),
                        "bootstrap_through": None,
                        "acknowledged": _anchor(bars[-1]),
                    }
                    if boundary is not None:
                        matches = [
                            bar
                            for bar in bars
                            if ensure_utc(bar.timestamp) == _timestamp(boundary["timestamp"])
                        ]
                        if len(matches) != 1:
                            msg = "Historical rebuild lacks its original flat boundary"
                            raise ModelStateContractError(msg)
                        self.feeds[symbol]["bootstrap_through"] = _anchor(matches[0])
                self._replay_symbol(symbol, bars, boundary, to_market_state)
                if bars and (
                    latest is None
                    or (bars[-1].timestamp, symbol) > (latest.timestamp, latest.symbol)
                ):
                    latest = bars[-1]
        return latest

    def _replay_symbol(
        self,
        symbol: str,
        bars: list[Bar],
        boundary: dict[str, Any] | None,
        to_market_state: Callable[[Bar], MarketState],
    ) -> None:
        consolidator = self.consolidators.get(symbol)
        callback = consolidator.replace_callback(None) if consolidator else None
        try:
            for raw in bars:
                emitted = consolidator.update(raw) if consolidator else raw
                if emitted is None:
                    continue
                market_state = to_market_state(emitted)
                # The raw bar that triggers an emission determines its phase.
                # A gap can flush an older constituent after the flat boundary.
                if boundary is not None and ensure_utc(raw.timestamp) <= _timestamp(
                    boundary["timestamp"]
                ):
                    self.strategy.bootstrap_history([market_state])
                else:
                    self.strategy.record_bar(symbol)
                    self.strategy.on_data(market_state)
                    self.strategy.flush()
        finally:
            if consolidator:
                consolidator.replace_callback(callback)
