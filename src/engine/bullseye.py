"""Bullseye orchestrator — routes trades to the appropriate ring and scores them.

Phase 2: Inner Ring only.  Middle Ring (Phase 4), Outer Ring, and congressional
confirmation (Phase 5) will be wired in their respective phases.

Public API:
    enriched = process_trades(trades, conn)
"""

from __future__ import annotations

import sqlite3

from src.engine.inner_ring import check_inner_ring, load_watchlist
from src.engine.scoring import compute_historical_avg, score_trade
from src.utils.logger import get_logger

log = get_logger(__name__)


def process_trades(
    trades: list[dict],
    conn: sqlite3.Connection,
) -> list[dict]:
    """Assign ring and confidence_score to each trade and return enriched list.

    Phase 2 behaviour:
    - Planned trades (10b5-1) are logged and kept in the output with ring=None
      and confidence_score=0 (they are stored for context but never alert).
    - Purchases on the Inner Ring watchlist above the per-ticker threshold
      get ring='inner'.
    - Everything else gets ring=None (Middle/Outer assignment: Phase 4+).

    Args:
        trades: Raw trade dicts from the edgar scraper.
        conn: Open database connection used for watchlist lookup and scoring.

    Returns:
        The same list with 'ring' and 'confidence_score' populated in-place.
    """
    if not trades:
        return trades

    watchlist = load_watchlist(conn)
    log.info("Processing %d trades through Bullseye (Phase 2: Inner Ring only)", len(trades))

    inner_count = 0
    planned_count = 0

    for trade in trades:
        # Planned trades: store with score=0, no ring assignment
        if trade.get("is_planned_trade"):
            trade["ring"] = None
            trade["confidence_score"] = 0
            planned_count += 1
            log.info(
                "Planned trade skipped: %s by %s",
                trade.get("ticker"),
                trade.get("person_name"),
            )
            continue

        # Inner Ring check
        if check_inner_ring(trade, watchlist):
            trade["ring"] = "inner"
            inner_count += 1
        else:
            trade["ring"] = None  # Middle/Outer: Phase 4+

        # Score the trade (uses DB for historical avg + repeat-buyer lookup)
        historical_avg = compute_historical_avg(conn, trade.get("ticker", ""))
        trade["confidence_score"] = score_trade(
            trade,
            ring=trade["ring"] or "outer",
            conn=conn,
            historical_avg=historical_avg,
        )

    log.info(
        "Bullseye results: %d Inner Ring, %d planned (excluded), %d other",
        inner_count,
        planned_count,
        len(trades) - inner_count - planned_count,
    )
    return trades
