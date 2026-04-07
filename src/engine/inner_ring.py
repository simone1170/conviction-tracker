"""Inner Ring watchlist threshold filter (SPEC §2.1).

The Inner Ring fires an immediate alert when a corporate insider makes an
open-market purchase above the per-ticker USD threshold defined in the
watchlist.  Congressional trades are explicitly excluded.

Public API:
    watchlist = load_watchlist(conn)          # {ticker: threshold_usd}
    hit = check_inner_ring(trade, watchlist)  # True / False
"""

from __future__ import annotations

import sqlite3

from src.db import queries
from src.utils.logger import get_logger

log = get_logger(__name__)


def load_watchlist(conn: sqlite3.Connection) -> dict[str, float]:
    """Load the active Inner Ring watchlist from the database.

    Args:
        conn: Open database connection with PRAGMAs applied.

    Returns:
        Dict mapping ticker symbol → USD threshold for all active entries.
        Returns an empty dict if the watchlist table is empty.
    """
    rows = conn.execute(queries.SELECT_ACTIVE_WATCHLIST).fetchall()
    watchlist = {row["ticker"]: float(row["threshold_usd"]) for row in rows}
    log.debug("Loaded %d active watchlist tickers", len(watchlist))
    return watchlist


def check_inner_ring(trade: dict, watchlist: dict[str, float]) -> bool:
    """Return True if a trade qualifies as an Inner Ring signal.

    All five conditions must hold (SPEC §2.1):
      1. Source is SEC Form 4 (congressional trades never trigger Inner Ring)
      2. Transaction type is a purchase
      3. Trade is not a 10b5-1 planned trade
      4. Ticker is on the active watchlist
      5. Total value meets or exceeds the per-ticker threshold

    Args:
        trade: Trade dict with at least: source, transaction_type,
            is_planned_trade, ticker, total_value.
        watchlist: Dict mapping ticker → threshold_usd (from load_watchlist).

    Returns:
        True if all five conditions are satisfied, False otherwise.
    """
    if trade.get("source") != "sec_form4":
        return False
    if trade.get("transaction_type") != "purchase":
        return False
    if trade.get("is_planned_trade", False):
        return False
    ticker = trade.get("ticker", "")
    if ticker not in watchlist:
        return False
    try:
        if float(trade.get("total_value", 0)) < watchlist[ticker]:
            return False
    except (TypeError, ValueError):
        return False
    return True
