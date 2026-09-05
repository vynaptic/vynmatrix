"""Data handling and processing utilities for the vynmatrix platform.

Modules:
    bars          - Lightweight OHLCV bar representation
    consolidation - Minute-to-N-minute bar consolidation (LEAN-compatible)
    pg_notify     - PostgreSQL LISTEN/NOTIFY client with reconnect
    watermark     - Per-worker/symbol/timeframe watermark tracking
"""

__version__ = "0.1.0"
