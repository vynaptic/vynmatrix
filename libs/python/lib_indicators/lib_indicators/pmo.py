"""
Price Momentum Oscillator (PMO) Indicator

The PMO is a double-smoothed momentum indicator that uses a Rate of Change (ROC)
followed by two exponential moving averages. It provides early signals of momentum
shifts and trend changes.

Formula:
1. Calculate 1-period ROC: (current_price - previous_price) / previous_price * 100
2. First EMA of ROC (scaled by 10)
3. Second EMA of the first EMA result (this is the PMO line)
4. Signal line: EMA of the PMO line

Author: vynmatrix Team
"""

from collections import deque

MIN_ROC_SAMPLES = 2


class PriceMomentumOscillator:
    """
    Price Momentum Oscillator (PMO) - A double-smoothed momentum indicator.

    The PMO provides early warning of momentum shifts by applying two levels of
    exponential smoothing to the Rate of Change (ROC).

    Parameters:
    -----------
    first_length : int, default=35
        Length for the first EMA applied to ROC
    second_length : int, default=20
        Length for the second EMA (creates the PMO line)
    signal_length : int, default=10
        Length for the signal line EMA

    Attributes:
    -----------
    pmo : float
        Current PMO value (second EMA)
    signal : float
        Current signal line value (EMA of PMO)
    is_ready : bool
        True when indicator has enough data to produce valid signals

    Usage:
    ------
    >>> pmo = PriceMomentumOscillator(first_length=35, second_length=20, signal_length=10)
    >>> pmo.update(100.5)
    >>> pmo.update(101.2)
    >>> if pmo.is_ready:
    >>>     print(f"PMO: {pmo.pmo}, Signal: {pmo.signal}")
    >>>     if pmo.pmo > pmo.signal:
    >>>         print("Bullish crossover")
    """

    def __init__(self, first_length: int = 35, second_length: int = 20, signal_length: int = 10):
        """Initialize the PMO indicator."""
        if min(first_length, second_length, signal_length) < 1:
            msg = "PMO smoothing lengths must all be positive"
            raise ValueError(msg)
        self.first_length = first_length
        self.second_length = second_length
        self.signal_length = signal_length

        # Each smoothing stage needs only its seed window. Keeping full process-
        # lifetime histories leaked memory in long-running signal workers.
        self.prices: deque = deque(maxlen=2)  # Only need current and previous
        self.roc_values: deque[float] = deque(maxlen=first_length)
        self.first_ema_values: deque[float] = deque(maxlen=second_length)
        self.pmo_values: deque[float] = deque(maxlen=signal_length)
        # Retain a bounded diagnostic window without coupling runtime memory to
        # process age. Signal calculation uses only ``self.signal``.
        self.signal_values: deque[float] = deque(maxlen=signal_length)

        # Current indicator values
        self.first_ema: float | None = None
        self.pmo: float | None = None
        self.signal: float | None = None

        # Warmup tracking
        self.sample_count = 0
        self._warmup_period = first_length + second_length + signal_length

    @property
    def is_ready(self) -> bool:
        """
        Check if the indicator has enough data to produce valid signals.

        Returns:
        --------
        bool
            True if indicator is warmed up and ready
        """
        return (
            self.sample_count >= self._warmup_period
            and self.pmo is not None
            and self.signal is not None
        )

    def update(self, price: float) -> tuple[float | None, float | None]:
        """
        Update the indicator with a new price.

        Parameters:
        -----------
        price : float
            New price data point

        Returns:
        --------
        tuple[Optional[float], Optional[float]]
            (pmo_value, signal_value) or (None, None) if not ready
        """
        self.prices.append(price)
        self.sample_count += 1

        # Need at least minimal prices to calculate ROC
        if len(self.prices) < MIN_ROC_SAMPLES:
            return None, None

        # Calculate 1-period ROC
        current_price = self.prices[-1]
        previous_price = self.prices[-2]

        if previous_price == 0:
            return None, None

        roc = (current_price - previous_price) / previous_price * 100
        self.roc_values.append(roc)

        # Calculate first EMA of ROC (scaled by 10)
        if self.first_ema is None:
            # Initialize with simple average
            if len(self.roc_values) >= self.first_length:
                self.first_ema = sum(self.roc_values) / self.first_length
        else:
            # DecisionPoint custom smoothing (2/period) for the PMO-line EMAs.
            self.first_ema = self._custom_ema(self.first_ema, roc, self.first_length)

        if self.first_ema is not None:
            first_ema_scaled = self.first_ema * 10
            self.first_ema_values.append(first_ema_scaled)

            # Calculate PMO (second EMA)
            if self.pmo is None:
                # Initialize with simple average
                if len(self.first_ema_values) >= self.second_length:
                    self.pmo = sum(self.first_ema_values) / self.second_length
            else:
                # DecisionPoint custom smoothing (2/period) for the PMO line.
                self.pmo = self._custom_ema(self.pmo, first_ema_scaled, self.second_length)

            if self.pmo is not None:
                self.pmo_values.append(self.pmo)

                # Calculate Signal line (EMA of PMO)
                if self.signal is None:
                    # Initialize with simple average
                    if len(self.pmo_values) >= self.signal_length:
                        self.signal = sum(self.pmo_values) / self.signal_length
                else:
                    # Signal line is a standard EMA of the PMO (2/(period+1)),
                    # per the referenced TradingView Pine (LazyBear) convention.
                    self.signal = self._standard_ema(self.signal, self.pmo, self.signal_length)

                if self.signal is not None:
                    self.signal_values.append(self.signal)

        return self.pmo, self.signal

    @staticmethod
    def _custom_ema(previous_ema: float, new_value: float, period: int) -> float:
        """DecisionPoint/StockCharts *custom* smoothing: multiplier = 2/period.

        This is the canonical PMO smoothing (NOT a standard EMA's 2/(period+1));
        the two PMO-line stages use it so crossovers match industry charts.
        """
        multiplier = 2.0 / period
        return (new_value - previous_ema) * multiplier + previous_ema

    @staticmethod
    def _standard_ema(previous_ema: float, new_value: float, period: int) -> float:
        """Standard EMA smoothing: multiplier = 2/(period+1). Used for the PMO
        signal line (a plain EMA of the PMO, per the TradingView Pine convention)."""
        multiplier = 2.0 / (period + 1)
        return (new_value - previous_ema) * multiplier + previous_ema

    def reset(self) -> None:
        """Reset the indicator to initial state."""
        self.prices.clear()
        self.roc_values.clear()
        self.first_ema_values.clear()
        self.pmo_values.clear()
        self.signal_values.clear()

        self.first_ema = None
        self.pmo = None
        self.signal = None
        self.sample_count = 0

    def get_histogram(self) -> float | None:
        """
        Get the PMO histogram (PMO - Signal).

        Returns:
        --------
        Optional[float]
            Histogram value or None if not ready
        """
        if self.pmo is not None and self.signal is not None:
            return self.pmo - self.signal
        return None

    def __str__(self) -> str:
        """String representation of the indicator."""
        if self.is_ready:
            return (
                "PMO(first="
                f"{self.first_length}, second={self.second_length}, signal={self.signal_length}"
                f"): PMO={self.pmo:.4f}, Signal={self.signal:.4f}"
            )
        return (
            "PMO(first="
            f"{self.first_length}, second={self.second_length}, signal={self.signal_length}"
            "): Not Ready"
        )
