"""Anti-signal sell cluster detection engine (SPEC §8.1).

Detects two defensive patterns in SEC Form 4 sale transactions:

1. Sell clusters — 2+ distinct insiders selling the same ticker within a
   rolling 14-day window.  Suggests coordinated distribution.

2. Large single sells — any insider sell exceeding $500K on a ticker that
   is on the Inner Ring watchlist.  Catches a single executive dumping a
   massive position in a stock you hold.

Congressional sells are intentionally excluded from both triggers: a 40-day-
old sell is not actionable as a defensive warning.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from src.config import ANTI_SIGNAL_SINGLE_SELL_USD, ANTI_SIGNAL_WINDOW_DAYS
from src.utils.logger import get_logger

log = get_logger(__name__)

_MIN_SELLERS = 2  # distinct insiders needed to form a sell cluster


def detect_sell_clusters(
    conn: sqlite3.Connection,
    window_days: int = ANTI_SIGNAL_WINDOW_DAYS,
) -> list[dict]:
    """Find tickers where 2+ distinct insiders have sold within the rolling window.

    Args:
        conn: Open database connection.
        window_days: Rolling lookback in days (default 14, SPEC §8.1).

    Returns:
        List of sell-cluster dicts, one per qualifying ticker.  Each dict has:
            ticker (str), company_name (str), seller_count (int),
            trades (list[dict]), aggregate_value (float),
            window_start (str), window_end (str), sellers (list[str]).
    """
    window_start = (date.today() - timedelta(days=window_days)).isoformat()
    window_end = date.today().isoformat()

    rows = conn.execute(
        """
        SELECT * FROM trades
        WHERE transaction_type = 'sale'
          AND source = 'sec_form4'
          AND is_planned_trade = FALSE
          AND transaction_date >= ?
          AND transaction_date <= ?
        ORDER BY ticker, transaction_date DESC
        """,
        (window_start, window_end),
    ).fetchall()

    if not rows:
        log.debug("No SEC sales in window %s → %s", window_start, window_end)
        return []

    # Group by ticker
    by_ticker: dict[str, list[dict]] = {}
    for row in rows:
        trade = dict(row)
        by_ticker.setdefault(trade["ticker"], []).append(trade)

    clusters: list[dict] = []
    for ticker, trades in by_ticker.items():
        distinct_sellers = list({t["person_name"] for t in trades})
        if len(distinct_sellers) < _MIN_SELLERS:
            continue

        dates = [t["transaction_date"] for t in trades if t.get("transaction_date")]
        company_name = next(
            (t.get("company_name") for t in trades if t.get("company_name")), ticker
        )
        clusters.append(
            {
                "ticker": ticker,
                "company_name": company_name,
                "seller_count": len(distinct_sellers),
                "trades": trades,
                "aggregate_value": sum(float(t.get("total_value") or 0) for t in trades),
                "window_start": min(dates) if dates else window_start,
                "window_end": max(dates) if dates else window_end,
                "sellers": distinct_sellers,
            }
        )
        log.info(
            "Sell cluster: %s — %d sellers, $%.0f aggregate",
            ticker,
            len(distinct_sellers),
            clusters[-1]["aggregate_value"],
        )

    return clusters


def detect_large_sells(
    conn: sqlite3.Connection,
    watchlist: dict[str, float],
    window_days: int = ANTI_SIGNAL_WINDOW_DAYS,
    threshold_usd: float = ANTI_SIGNAL_SINGLE_SELL_USD,
) -> list[dict]:
    """Find individual insider sells above the threshold on watchlist tickers.

    Args:
        conn: Open database connection.
        watchlist: Dict of {ticker: threshold_usd} from load_watchlist().
        window_days: Lookback window in days (default 14).
        threshold_usd: Minimum sell value to trigger (default $500K).

    Returns:
        List of trade dicts (each is one qualifying sell).
    """
    if not watchlist:
        return []

    window_start = (date.today() - timedelta(days=window_days)).isoformat()
    placeholders = ",".join("?" * len(watchlist))
    tickers = list(watchlist.keys())

    rows = conn.execute(
        f"""
        SELECT * FROM trades
        WHERE transaction_type = 'sale'
          AND source = 'sec_form4'
          AND is_planned_trade = FALSE
          AND transaction_date >= ?
          AND ticker IN ({placeholders})
          AND total_value >= ?
        ORDER BY total_value DESC
        """,
        [window_start, *tickers, threshold_usd],
    ).fetchall()

    result = [dict(row) for row in rows]
    if result:
        log.info(
            "Large watchlist sells detected: %d trades >= $%.0f",
            len(result),
            threshold_usd,
        )
    return result


def is_new_sell_cluster(
    conn: sqlite3.Connection,
    ticker: str,
    window_days: int = ANTI_SIGNAL_WINDOW_DAYS,
) -> bool:
    """Return True if no anti_signal alert has been sent for this ticker recently.

    Prevents re-alerting on the same ongoing sell cluster each day.

    Args:
        conn: Open database connection.
        ticker: Company ticker symbol.
        window_days: Lookback in days (default 14).

    Returns:
        True if this is a new event (no recent alert), False otherwise.
    """
    since = (date.today() - timedelta(days=window_days)).isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM alerts_log
        WHERE alert_type = 'anti_signal'
          AND delivery_status IN ('sent', 'pending')
          AND created_at >= ?
          AND message LIKE ?
        LIMIT 1
        """,
        (since, f"%{ticker}%"),
    ).fetchone()
    is_new = row is None
    if not is_new:
        log.debug("Sell cluster for '%s' already alerted — suppressing", ticker)
    return is_new
