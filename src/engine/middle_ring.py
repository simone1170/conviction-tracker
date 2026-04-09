"""Middle Ring cluster detection engine (SPEC §2.2).

Detects coordinated insider buying across 3+ distinct companies in the same
sector within a rolling 7-day window. Uses transaction_date (not filing_date)
to avoid false clusters from weekend filing surges.

Congressional trades are excluded — only SEC Form 4 purchases count toward
the cluster threshold.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from src.scrapers.etf_mapper import get_sector_for_ticker
from src.utils.logger import get_logger

log = get_logger(__name__)

_CLUSTER_MIN_COMPANIES = 3  # minimum distinct tickers to form a cluster


def detect_clusters(
    conn: sqlite3.Connection,
    window_days: int = 7,
) -> list[dict]:
    """Find all active Middle Ring sector clusters in the rolling window.

    Queries the full database window (not just the current ingestion batch),
    so clusters can form across multiple days of ingestion.

    Cluster rules (SPEC §2.2):
    - SEC Form 4 purchases only (source='sec_form4', transaction_type='purchase')
    - Exclude is_planned_trade=TRUE
    - Congressional trades do NOT count
    - 3+ distinct company tickers in the same sector within window_days
    - Same company's multiple insiders count as ONE company

    Args:
        conn: Open database connection.
        window_days: Rolling lookback window in days (default 7).

    Returns:
        List of cluster dicts. Each dict has:
            sector_name (str), company_count (int), trades (list[dict]),
            aggregate_value (float), window_start (str), window_end (str),
            tickers (list[str]).
    """
    window_start = (date.today() - timedelta(days=window_days)).isoformat()
    window_end = date.today().isoformat()

    rows = conn.execute(
        """
        SELECT * FROM trades
        WHERE transaction_type = 'purchase'
          AND source = 'sec_form4'
          AND is_planned_trade = FALSE
          AND transaction_date >= ?
          AND transaction_date <= ?
        ORDER BY transaction_date DESC
        """,
        (window_start, window_end),
    ).fetchall()

    if not rows:
        log.debug("No SEC purchases in window %s → %s", window_start, window_end)
        return []

    # Group trades by sector
    sector_groups: dict[str, list[dict]] = {}
    for row in rows:
        trade = dict(row)
        sector = get_sector_for_ticker(conn, trade["ticker"])
        if sector:
            sector_groups.setdefault(sector, []).append(trade)

    clusters: list[dict] = []
    for sector_name, trades_in_sector in sector_groups.items():
        # Count distinct companies (a company = a ticker, not a person)
        distinct_tickers = list({t["ticker"] for t in trades_in_sector})
        if len(distinct_tickers) < _CLUSTER_MIN_COMPANIES:
            log.debug(
                "Sector %s: %d distinct companies (below threshold of %d)",
                sector_name,
                len(distinct_tickers),
                _CLUSTER_MIN_COMPANIES,
            )
            continue

        dates = [t["transaction_date"] for t in trades_in_sector if t["transaction_date"]]
        clusters.append(
            {
                "sector_name": sector_name,
                "company_count": len(distinct_tickers),
                "trades": trades_in_sector,
                "aggregate_value": sum(
                    float(t.get("total_value") or 0) for t in trades_in_sector
                ),
                "window_start": min(dates) if dates else window_start,
                "window_end": max(dates) if dates else window_end,
                "tickers": distinct_tickers,
            }
        )
        log.info(
            "Cluster detected: %s — %d companies, %d trades, $%.0f aggregate",
            sector_name,
            len(distinct_tickers),
            len(trades_in_sector),
            clusters[-1]["aggregate_value"],
        )

    return clusters


def is_new_cluster(
    conn: sqlite3.Connection,
    sector_name: str,
    window_end: date | str,
) -> bool:
    """Return True if no cluster alert has been sent for this sector in the last 7 days.

    Prevents re-alerting when the same sector cluster persists across multiple
    days of ingestion (e.g. a 5-day buying streak should only alert once).

    Args:
        conn: Open database connection.
        sector_name: Sector name to check (e.g. "Semiconductors").
        window_end: Latest transaction_date in the cluster — used to anchor the
            7-day lookback window. Accepts ISO date string or date object.

    Returns:
        True if this is a new cluster (no recent alert), False if already alerted.
    """
    if isinstance(window_end, str):
        try:
            window_end = date.fromisoformat(window_end)
        except ValueError:
            window_end = date.today()

    since = (window_end - timedelta(days=7)).isoformat()

    row = conn.execute(
        """
        SELECT 1 FROM alerts_log
        WHERE alert_type = 'cluster'
          AND delivery_status IN ('sent', 'pending')
          AND created_at >= ?
          AND message LIKE ?
        LIMIT 1
        """,
        (since, f"%{sector_name}%"),
    ).fetchone()

    is_new = row is None
    if not is_new:
        log.debug("Cluster for '%s' already alerted within 7 days — suppressing", sector_name)
    return is_new
