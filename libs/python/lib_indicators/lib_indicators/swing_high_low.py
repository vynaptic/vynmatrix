"""
Swing High/Low Indicator

Identifies swing highs and swing lows using pivot point detection. A swing high
occurs when the center bar's high is higher than all surrounding bars within the
specified lookback period. A swing low occurs when the center bar's low is lower
than all surrounding bars.

These swing points are commonly used as support/resistance levels and for setting
stop losses in trading strategies.

Author: vynmatrix Team
"""

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class SwingPoint:
    """
    Represents a swing high or swing low point.

    Attributes:
    -----------
    price : float
        Price level of the swing point
    timestamp : datetime
        When the swing point was identified
    bar_index : int
        Index of the bar where swing occurred
    """

    price: float
    timestamp: datetime
    bar_index: int


class SwingHighLow:
    """
    Swing High/Low indicator using pivot point detection.

    A swing high is identified when the center bar's high is higher than all
    surrounding bars (both left and right) within the swing_length window.
    Similarly, a swing low is identified when the center bar's low is lower
    than all surrounding bars.

    Parameters:
    -----------
    swing_length : int, default=5
        Number of bars on each side of center bar to check for swing points.
        Total window size = (swing_length * 2) + 1
    max_history : int, default=1024
        Maximum confirmed swing highs and lows retained for diagnostics. Latest
        swing values remain available while process memory stays bounded.

    Attributes:
    -----------
    latest_swing_high : Optional[SwingPoint]
        Most recent swing high identified
    latest_swing_low : Optional[SwingPoint]
        Most recent swing low identified
    is_ready : bool
        True when enough bars collected to identify swings

    Usage:
    ------
    >>> swing = SwingHighLow(swing_length=5)
    >>> swing.update(high=102.5, low=100.0, timestamp=datetime.now(UTC))
    >>> if swing.is_ready:
    >>>     if swing.latest_swing_high:
    >>>         print(f"Swing high at {swing.latest_swing_high.price}")
    >>>     if swing.latest_swing_low:
    >>>         print(f"Swing low at {swing.latest_swing_low.price}")
    """

    def __init__(self, swing_length: int = 5, max_history: int = 1024):
        """Initialize the SwingHighLow indicator."""
        if swing_length < 1:
            msg = "swing_length must be positive"
            raise ValueError(msg)
        if max_history < 1:
            msg = "max_history must be positive"
            raise ValueError(msg)
        self.swing_length = swing_length
        self.window_size = (swing_length * 2) + 1  # Total bars needed

        # Storage for price data
        self.highs: deque = deque(maxlen=self.window_size)
        self.lows: deque = deque(maxlen=self.window_size)
        self.timestamps: deque = deque(maxlen=self.window_size)

        # Track swing points
        self.swing_highs: deque[SwingPoint] = deque(maxlen=max_history)
        self.swing_lows: deque[SwingPoint] = deque(maxlen=max_history)

        # Most recent swing points
        self.latest_swing_high: SwingPoint | None = None
        self.latest_swing_low: SwingPoint | None = None

        # Bar counting
        self.bar_count = 0
        self._last_swing_high_bar = -1
        self._last_swing_low_bar = -1

    @property
    def is_ready(self) -> bool:
        """
        Check if indicator has enough data to identify swing points.

        Returns:
        --------
        bool
            True if enough bars collected
        """
        return len(self.highs) == self.window_size

    def update(
        self, high: float, low: float, timestamp: datetime | None = None
    ) -> tuple[SwingPoint | None, SwingPoint | None]:
        """
        Update the indicator with new bar data.

        Parameters:
        -----------
        high : float
            High price of the bar
        low : float
            Low price of the bar
        timestamp : Optional[datetime]
            Timestamp of the bar (defaults to now if not provided)

        Returns:
        --------
        Tuple[Optional[SwingPoint], Optional[SwingPoint]]
            (new_swing_high, new_swing_low) or (None, None) if no new swings
        """
        if timestamp is None:
            timestamp = datetime.now(UTC)

        self.highs.append(high)
        self.lows.append(low)
        self.timestamps.append(timestamp)
        self.bar_count += 1

        new_swing_high = None
        new_swing_low = None

        if self.is_ready:
            swing_high, swing_low = self._calculate_swing_points()

            if swing_high is not None:
                self.swing_highs.append(swing_high)
                self.latest_swing_high = swing_high
                new_swing_high = swing_high

            if swing_low is not None:
                self.swing_lows.append(swing_low)
                self.latest_swing_low = swing_low
                new_swing_low = swing_low

        return new_swing_high, new_swing_low

    def _calculate_swing_points(self) -> tuple[SwingPoint | None, SwingPoint | None]:
        """
        Calculate swing high and low for the center bar.

        Returns:
        --------
        Tuple[Optional[SwingPoint], Optional[SwingPoint]]
            (swing_high, swing_low) or (None, None)
        """
        center_idx = self.swing_length
        center_high = self.highs[center_idx]
        center_low = self.lows[center_idx]
        center_timestamp = self.timestamps[center_idx]

        # Calculate the bar index for the center bar
        center_bar_index = self.bar_count - self.swing_length - 1

        swing_high = None
        swing_low = None

        # Prevent duplicate detection of same swing point
        if center_bar_index <= self._last_swing_high_bar:
            pass  # Already processed this bar for swing high
        else:
            # Check for swing high: center high > all other highs
            is_swing_high = all(
                center_high > self.highs[i] for i in range(len(self.highs)) if i != center_idx
            )

            if is_swing_high:
                swing_high = SwingPoint(
                    price=center_high, timestamp=center_timestamp, bar_index=center_bar_index
                )
                self._last_swing_high_bar = center_bar_index

        if center_bar_index <= self._last_swing_low_bar:
            pass  # Already processed this bar for swing low
        else:
            # Check for swing low: center low < all other lows
            is_swing_low = all(
                center_low < self.lows[i] for i in range(len(self.lows)) if i != center_idx
            )

            if is_swing_low:
                swing_low = SwingPoint(
                    price=center_low, timestamp=center_timestamp, bar_index=center_bar_index
                )
                self._last_swing_low_bar = center_bar_index

        return swing_high, swing_low

    def get_latest_swing_high(self) -> float | None:
        """
        Get the price level of the most recent swing high.

        Returns:
        --------
        Optional[float]
            Swing high price or None if not available
        """
        return self.latest_swing_high.price if self.latest_swing_high else None

    def get_latest_swing_low(self) -> float | None:
        """
        Get the price level of the most recent swing low.

        Returns:
        --------
        Optional[float]
            Swing low price or None if not available
        """
        return self.latest_swing_low.price if self.latest_swing_low else None

    def get_swing_high_age(self) -> int | None:
        """
        Get number of bars since last swing high was identified.

        Returns:
        --------
        Optional[int]
            Number of bars or None if no swing high
        """
        if self.latest_swing_high:
            return self.bar_count - self.latest_swing_high.bar_index
        return None

    def get_swing_low_age(self) -> int | None:
        """
        Get number of bars since last swing low was identified.

        Returns:
        --------
        Optional[int]
            Number of bars or None if no swing low
        """
        if self.latest_swing_low:
            return self.bar_count - self.latest_swing_low.bar_index
        return None

    def get_all_swing_highs(self, limit: int | None = None) -> list[SwingPoint]:
        """
        Get all swing highs, optionally limited to most recent N.

        Parameters:
        -----------
        limit : Optional[int]
            Maximum number of swing highs to return (most recent first)

        Returns:
        --------
        List[SwingPoint]
            List of swing high points
        """
        history = list(self.swing_highs)
        if limit is None:
            return list(reversed(history))
        return list(reversed(history[-limit:]))

    def get_all_swing_lows(self, limit: int | None = None) -> list[SwingPoint]:
        """
        Get all swing lows, optionally limited to most recent N.

        Parameters:
        -----------
        limit : Optional[int]
            Maximum number of swing lows to return (most recent first)

        Returns:
        --------
        List[SwingPoint]
            List of swing low points
        """
        history = list(self.swing_lows)
        if limit is None:
            return list(reversed(history))
        return list(reversed(history[-limit:]))

    def reset(self) -> None:
        """Reset the indicator to initial state."""
        self.highs.clear()
        self.lows.clear()
        self.timestamps.clear()
        self.swing_highs.clear()
        self.swing_lows.clear()

        self.latest_swing_high = None
        self.latest_swing_low = None
        self.bar_count = 0
        self._last_swing_high_bar = -1
        self._last_swing_low_bar = -1

    def __str__(self) -> str:
        """String representation of the indicator."""
        if not self.is_ready:
            return (
                f"SwingHighLow(length={self.swing_length}): "
                f"Not Ready ({len(self.highs)}/{self.window_size} bars)"
            )

        high_str = f"{self.latest_swing_high.price:.2f}" if self.latest_swing_high else "None"
        low_str = f"{self.latest_swing_low.price:.2f}" if self.latest_swing_low else "None"

        return (
            f"SwingHighLow(length={self.swing_length}): "
            f"High={high_str}, Low={low_str}, "
            f"Total Highs={len(self.swing_highs)}, Total Lows={len(self.swing_lows)}"
        )
