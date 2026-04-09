"""Confidence scoring engine (SPEC §6).

Assigns a 0-100 confidence score to each trade based on its ring, the
insider's title, ownership type, purchase size vs. historical average,
repeat-buyer history, and planned-trade / low-value penalties.

Congressional hard caps (Phase 5) are enforced here but not exercised until
the congressional module is enabled.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from src.db import queries
from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Base scores by ring (SPEC §6.1) ──────────────────────────────────────────

_BASE_SCORES: dict[str, int] = {
    "inner": 60,
    "middle": 40,
    "outer_sec": 50,
    "outer_congress": 35,
}

# Congressional Outer Ring hard cap (SPEC §6.4)
_CONGRESS_OUTER_CAP = 50

# Minimum data points needed for a meaningful historical average
_MIN_AVG_DATAPOINTS = 3

# Lookback window for repeat-buyer bonus
_REPEAT_BUYER_DAYS = 180


# ── Title bonus (SPEC §6.2) ───────────────────────────────────────────────────

def _title_bonus(title: str | None) -> int:
    """Return the confidence bonus for an insider's title.

    Uses word-boundary regex to avoid false matches (e.g. "CTO" inside
    "DIRECTOR", or "PRESIDENT" inside "VICE PRESIDENT").  Only the highest
    applicable tier is applied (if/elif chain, ordered highest → lowest).

    Args:
        title: The person_title field from the trade dict.

    Returns:
        Integer bonus: +15, +10, +8, +3, or 0.
    """
    if not title:
        return 0
    t = title.upper()

    # +15: CEO / CFO / Chief Executive / Chief Financial
    if re.search(r"\bCEO\b|\bCFO\b|CHIEF EXECUTIVE|CHIEF FINANCIAL", t):
        return 15

    # +10: COO / CTO (word-bounded), Chief Operating/Technology, standalone President
    # Check VP/VICE PRESIDENT BEFORE PRESIDENT so "Vice President" scores +3 not +10.
    if re.search(r"\bCOO\b|\bCTO\b|CHIEF OPERATING|CHIEF TECHNOLOGY", t):
        return 10

    # +3: VP / Vice President — checked before standalone PRESIDENT to prevent
    # "Vice President" from matching the +10 PRESIDENT rule below.
    if re.search(r"\bVP\b|VICE.?PRESIDENT", t):
        return 3

    # +10: standalone President (not Vice President — already caught above)
    if re.search(r"\bPRESIDENT\b", t):
        return 10

    # +8: Director/Directors (word-bounded avoids matching inside e.g. "Administrator")
    if re.search(r"\bDIRECTORS?\b", t):
        return 8

    return 0


# ── Historical average ────────────────────────────────────────────────────────

def compute_historical_avg(
    conn: sqlite3.Connection,
    ticker: str,
    days: int = 90,
) -> float | None:
    """Return the average purchase value for ticker over the last N days.

    Returns None if fewer than _MIN_AVG_DATAPOINTS data points exist (not
    enough history for a statistically meaningful average).

    Args:
        conn: Open database connection.
        ticker: Company ticker symbol.
        days: Rolling lookback window in days.

    Returns:
        Average total_value as a float, or None.
    """
    since_date = (date.today() - timedelta(days=days)).isoformat()

    # Check we have enough data points
    count_row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM trades
        WHERE ticker = :ticker
          AND transaction_type = 'purchase'
          AND is_planned_trade = FALSE
          AND transaction_date >= :since_date
        """,
        {"ticker": ticker, "since_date": since_date},
    ).fetchone()

    if count_row is None or count_row["cnt"] < _MIN_AVG_DATAPOINTS:
        return None

    avg_row = conn.execute(
        queries.SELECT_ROLLING_AVG_BY_TICKER,
        {"ticker": ticker, "since_date": since_date},
    ).fetchone()

    if avg_row is None or avg_row["avg_value"] is None:
        return None

    return float(avg_row["avg_value"])


# ── Main scoring function ─────────────────────────────────────────────────────

def score_trade(
    trade: dict,
    ring: str,
    conn: sqlite3.Connection,
    historical_avg: float | None = None,
    cluster_company_count: int = 0,
) -> int:
    """Compute a 0-100 confidence score for a single trade.

    Applies base score, title bonus, ownership penalty, purchase-size bonus,
    repeat-buyer bonus, cluster strength bonus (Middle Ring only), planned-trade
    and low-value penalties, then clamps to [0, 100].  Congressional Outer Ring
    signals are further capped at 50.

    Args:
        trade: Trade dict with at least: is_planned_trade, person_title,
            ownership_type, total_value, transaction_type, source,
            person_name, ticker, transaction_date.
        ring: Ring assignment string ('inner', 'middle', 'outer', or None).
        conn: Open database connection (needed for repeat-buyer lookup).
        historical_avg: 90-day rolling average purchase value for this ticker,
            or None if insufficient history.
        cluster_company_count: Number of distinct companies in the cluster
            (Middle Ring only).  0 means no cluster strength bonus.

    Returns:
        Integer confidence score in [0, 100].
    """
    # Planned trades always score 0 and are excluded from all triggers
    if trade.get("is_planned_trade"):
        return 0

    # ── Base score ────────────────────────────────────────────────────────────
    source = trade.get("source", "")
    if ring == "inner":
        score = _BASE_SCORES["inner"]
    elif ring == "middle":
        score = _BASE_SCORES["middle"]
    elif ring == "outer":
        if "congress" in source:
            score = _BASE_SCORES["outer_congress"]
        else:
            score = _BASE_SCORES["outer_sec"]
    else:
        # Unassigned trades (e.g. sells stored for anti-signal): use outer_sec base
        score = _BASE_SCORES["outer_sec"]

    # ── Title bonus ───────────────────────────────────────────────────────────
    score += _title_bonus(trade.get("person_title"))

    # ── Ownership type penalty ────────────────────────────────────────────────
    if trade.get("ownership_type") == "I":
        score -= 15

    # ── Purchase size bonus (only for buys with valid history) ───────────────
    if trade.get("transaction_type") == "purchase" and historical_avg and historical_avg > 0:
        try:
            ratio = float(trade["total_value"]) / historical_avg
            if ratio > 5.0:
                score += 20
            elif ratio > 2.0:
                score += 10
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # ── Repeat buyer bonus ────────────────────────────────────────────────────
    if trade.get("transaction_type") == "purchase":
        since = (date.today() - timedelta(days=_REPEAT_BUYER_DAYS)).isoformat()
        if queries.check_repeat_buyer(
            conn,
            person_name=trade.get("person_name", ""),
            ticker=trade.get("ticker", ""),
            since_date=since,
        ):
            score += 5

    # ── Cluster strength bonus (Middle Ring only, SPEC §6.2) ─────────────────
    if ring == "middle" and cluster_company_count >= 5:
        score += 20
    elif ring == "middle" and cluster_company_count >= 4:
        score += 10

    # ── Low-value penalty ─────────────────────────────────────────────────────
    try:
        if float(trade.get("total_value", 0)) < 10_000:
            score -= 20
    except (TypeError, ValueError):
        pass

    # ── Hard caps and clamping ────────────────────────────────────────────────
    if ring == "outer" and "congress" in source:
        score = min(score, _CONGRESS_OUTER_CAP)

    return max(0, min(100, score))


def score_cluster(cluster: dict, conn: sqlite3.Connection) -> int:
    """Compute the confidence score for a Middle Ring cluster.

    Re-scores each constituent trade with ring='middle' and the cluster's
    company_count (which applies the cluster-strength bonus), then returns
    the rounded average.  The cluster must contain at least one trade.

    Args:
        cluster: Cluster dict from detect_clusters(), with 'trades', 'company_count'.
        conn: Open database connection (needed for historical avg + repeat-buyer).

    Returns:
        Integer average confidence score in [0, 100].
    """
    trades = cluster.get("trades", [])
    if not trades:
        return _BASE_SCORES["middle"]

    company_count = cluster.get("company_count", 0)
    scored: list[int] = []
    for trade in trades:
        historical_avg = compute_historical_avg(conn, trade.get("ticker", ""))
        individual = score_trade(
            trade,
            ring="middle",
            conn=conn,
            historical_avg=historical_avg,
            cluster_company_count=company_count,
        )
        scored.append(individual)

    avg = sum(scored) / len(scored)
    return max(0, min(100, round(avg)))
