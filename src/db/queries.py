"""All SQL queries and database helper functions.

SQL constants are plain strings consumed by conn.execute().
Python helpers wrap common multi-step operations and are the only place
that should call conn.execute() with these constants.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# ── Trades ────────────────────────────────────────────────────────────────────

UPSERT_TRADE = """
INSERT INTO trades (
    source, ticker, company_name, person_name, person_title,
    transaction_type, transaction_code, ownership_type,
    shares, price_per_share, total_value, amount_range,
    transaction_date, filing_date, report_lag_days, filing_url,
    is_planned_trade, ring, confidence_score, alert_sent
) VALUES (
    :source, :ticker, :company_name, :person_name, :person_title,
    :transaction_type, :transaction_code, :ownership_type,
    :shares, :price_per_share, :total_value, :amount_range,
    :transaction_date, :filing_date, :report_lag_days, :filing_url,
    :is_planned_trade, :ring, :confidence_score, :alert_sent
)
ON CONFLICT(source, ticker, person_name, transaction_date, total_value, transaction_type)
DO UPDATE SET
    company_name     = excluded.company_name,
    person_title     = excluded.person_title,
    ownership_type   = excluded.ownership_type,
    shares           = excluded.shares,
    price_per_share  = excluded.price_per_share,
    amount_range     = excluded.amount_range,
    filing_date      = excluded.filing_date,
    report_lag_days  = excluded.report_lag_days,
    filing_url       = excluded.filing_url,
    is_planned_trade = excluded.is_planned_trade,
    ring             = excluded.ring,
    confidence_score = excluded.confidence_score
"""

SELECT_UNSENT_TRADES = """
SELECT * FROM trades
WHERE alert_sent = FALSE
  AND is_planned_trade = FALSE
  AND transaction_type = 'purchase'
ORDER BY transaction_date DESC
"""

MARK_ALERT_SENT = """
UPDATE trades SET alert_sent = TRUE WHERE id = :id
"""

SELECT_TRADES_BY_TICKER_WINDOW = """
SELECT * FROM trades
WHERE ticker = :ticker
  AND transaction_type = 'purchase'
  AND is_planned_trade = FALSE
  AND transaction_date BETWEEN :start_date AND :end_date
ORDER BY transaction_date DESC
"""

SELECT_ROLLING_AVG_BY_TICKER = """
SELECT AVG(total_value) AS avg_value
FROM trades
WHERE ticker = :ticker
  AND transaction_type = 'purchase'
  AND is_planned_trade = FALSE
  AND transaction_date >= :since_date
"""

# ── Watchlist ─────────────────────────────────────────────────────────────────

SELECT_ACTIVE_WATCHLIST = """
SELECT * FROM watchlist WHERE active = TRUE
"""

UPSERT_WATCHLIST = """
INSERT INTO watchlist (ticker, threshold_usd, notes, active)
VALUES (:ticker, :threshold_usd, :notes, :active)
ON CONFLICT(ticker) DO UPDATE SET
    threshold_usd = excluded.threshold_usd,
    notes         = excluded.notes,
    active        = excluded.active
"""

# ── Sectors ───────────────────────────────────────────────────────────────────

SELECT_SECTOR_TICKERS = """
SELECT DISTINCT constituent_ticker FROM sectors WHERE etf_ticker = :etf_ticker
"""

SELECT_ALL_SECTORS = """
SELECT DISTINCT sector_name, etf_ticker, MAX(updated_at) AS updated_at
FROM sectors
GROUP BY sector_name, etf_ticker
"""

SELECT_SECTOR_FOR_TICKER = """
SELECT DISTINCT sector_name, etf_ticker
FROM sectors
WHERE constituent_ticker = :ticker
"""

DELETE_SECTOR_ETF = """
DELETE FROM sectors WHERE etf_ticker = :etf_ticker
"""

INSERT_SECTOR_CONSTITUENT = """
INSERT OR REPLACE INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at)
VALUES (:sector_name, :etf_ticker, :constituent_ticker, :updated_at)
"""

# ── Cluster detection (Middle Ring) ───────────────────────────────────────────

SELECT_SECTOR_CLUSTER_CANDIDATES = """
SELECT t.*
FROM trades t
JOIN sectors s ON t.ticker = s.constituent_ticker
WHERE s.etf_ticker = :etf_ticker
  AND t.transaction_type = 'purchase'
  AND t.is_planned_trade = FALSE
  AND t.transaction_date BETWEEN :start_date AND :end_date
ORDER BY t.transaction_date DESC
"""

# ── Anti-signal (sell detection) ──────────────────────────────────────────────

SELECT_SELL_CLUSTER_CANDIDATES = """
SELECT ticker, person_name, person_title, total_value, transaction_date
FROM trades
WHERE ticker = :ticker
  AND transaction_type = 'sale'
  AND is_planned_trade = FALSE
  AND transaction_date BETWEEN :start_date AND :end_date
ORDER BY transaction_date DESC
"""

# ── Alerts log ────────────────────────────────────────────────────────────────

INSERT_ALERT_LOG = """
INSERT INTO alerts_log (trade_id, ring, alert_type, message, confidence_score)
VALUES (:trade_id, :ring, :alert_type, :message, :confidence_score)
"""

UPDATE_ALERT_SENT = """
UPDATE alerts_log
SET delivery_status = :delivery_status,
    sent_at = :sent_at,
    telegram_message_id = :telegram_message_id,
    retry_count = :retry_count
WHERE id = :id
"""

INSERT_FAILED_ALERT = """
INSERT INTO failed_alerts (alert_log_id, error_message, last_retry_at)
VALUES (:alert_log_id, :error_message, :last_retry_at)
"""

SELECT_PENDING_ALERTS = """
SELECT al.*, t.ticker, t.person_name, t.total_value
FROM alerts_log al
LEFT JOIN trades t ON al.trade_id = t.id
WHERE al.delivery_status = 'pending'
ORDER BY al.created_at ASC
"""

SELECT_UNRESOLVED_FAILED_ALERTS = """
SELECT fa.*, al.message, al.ring, al.alert_type
FROM failed_alerts fa
JOIN alerts_log al ON fa.alert_log_id = al.id
WHERE fa.resolved = FALSE
"""

SELECT_UNSENT_ALERTS = """
SELECT * FROM trades
WHERE alert_sent = FALSE
  AND ring IS NOT NULL
  AND confidence_score > 0
  AND is_planned_trade = FALSE
ORDER BY transaction_date DESC
"""

SELECT_UNSENT_ALERTS_BY_RING = """
SELECT * FROM trades
WHERE alert_sent = FALSE
  AND ring = :ring
  AND confidence_score > 0
  AND is_planned_trade = FALSE
ORDER BY transaction_date DESC
"""

COUNT_ALERTS_SENT_TODAY = """
SELECT COUNT(*) AS cnt
FROM alerts_log
WHERE ring != 'inner'
  AND delivery_status = 'sent'
  AND DATE(sent_at) = DATE('now')
"""

SELECT_FAILED_ALERTS = """
SELECT fa.*, al.message, al.ring, al.alert_type, al.trade_id
FROM failed_alerts fa
JOIN alerts_log al ON fa.alert_log_id = al.id
WHERE fa.resolved = :resolved
ORDER BY fa.created_at DESC
"""

# ── System health ─────────────────────────────────────────────────────────────

UPSERT_HEALTH = """
INSERT INTO system_health (component, last_successful_run, records_processed, errors, notes)
VALUES (:component, :last_successful_run, :records_processed, :errors, :notes)
"""

SELECT_HEALTH_ALL = """
SELECT component, MAX(last_successful_run) AS last_successful_run,
       SUM(records_processed) AS records_processed, SUM(errors) AS errors
FROM system_health
GROUP BY component
"""

# ── Politician history (Outer Ring) ───────────────────────────────────────────

UPSERT_POLITICIAN_HISTORY = """
INSERT INTO politician_history (politician_name, sector_name, first_trade_date, trade_count)
VALUES (:politician_name, :sector_name, :first_trade_date, 1)
ON CONFLICT(politician_name, sector_name) DO UPDATE SET
    trade_count = trade_count + 1
"""

SELECT_POLITICIAN_SECTORS = """
SELECT sector_name FROM politician_history WHERE politician_name = :politician_name
"""

_SELECT_LAST_HEALTH = """
SELECT MAX(last_successful_run) AS last_run
FROM system_health
WHERE component = :component
"""

_SELECT_REPEAT_BUYER = """
SELECT 1 FROM trades
WHERE person_name = :person_name
  AND ticker = :ticker
  AND transaction_type = 'purchase'
  AND is_planned_trade = FALSE
  AND transaction_date >= :since_date
LIMIT 1
"""

_COUNT_TICKER_PURCHASES = """
SELECT COUNT(*) AS cnt FROM trades
WHERE ticker = :ticker
  AND transaction_type = 'purchase'
  AND is_planned_trade = FALSE
  AND transaction_date >= :since_date
"""


# ── Python helpers ────────────────────────────────────────────────────────────

def insert_trades(conn: sqlite3.Connection, trades: list[dict]) -> int:
    """Upsert a list of trade dicts into the trades table.

    Uses ON CONFLICT REPLACE so re-ingesting the same trade is idempotent.

    Args:
        conn: Open database connection with PRAGMAs applied.
        trades: List of trade dicts matching the trades table schema.

    Returns:
        Number of rows processed (not necessarily net new rows).
    """
    for trade in trades:
        conn.execute(UPSERT_TRADE, trade)
    conn.commit()
    return len(trades)


def get_last_scrape_date(conn: sqlite3.Connection, source: str) -> date | None:
    """Return the date of the last successful scrape for a given source.

    Args:
        conn: Open database connection.
        source: Source identifier, e.g. 'sec_form4'.

    Returns:
        The last successful run date, or None if the component has never run.
    """
    component = f"scraper_{source}"
    row = conn.execute(_SELECT_LAST_HEALTH, {"component": component}).fetchone()
    if row is None or row["last_run"] is None:
        return None
    try:
        return date.fromisoformat(str(row["last_run"])[:10])
    except (ValueError, TypeError):
        return None


def log_health(
    conn: sqlite3.Connection,
    component: str,
    records_processed: int,
    errors: int,
    notes: str = "",
) -> None:
    """Insert a health record for a pipeline component.

    Args:
        conn: Open database connection.
        component: Component name (e.g. 'scraper_sec_form4').
        records_processed: Number of records handled in this run.
        errors: Number of errors encountered.
        notes: Optional free-text notes about the run.
    """
    conn.execute(
        UPSERT_HEALTH,
        {
            "component": component,
            "last_successful_run": datetime.utcnow().isoformat(),
            "records_processed": records_processed,
            "errors": errors,
            "notes": notes,
        },
    )
    conn.commit()


def check_repeat_buyer(
    conn: sqlite3.Connection,
    person_name: str,
    ticker: str,
    since_date: str,
) -> bool:
    """Return True if this person has bought this ticker since since_date.

    Used by the scoring engine to award the +5 repeat buyer bonus.

    Args:
        conn: Open database connection.
        person_name: Insider's full name.
        ticker: Company ticker symbol.
        since_date: ISO date string (earliest transaction_date to consider).

    Returns:
        True if at least one qualifying purchase exists, False otherwise.
    """
    row = conn.execute(
        _SELECT_REPEAT_BUYER,
        {"person_name": person_name, "ticker": ticker, "since_date": since_date},
    ).fetchone()
    return row is not None


def get_unsent_alerts(
    conn: sqlite3.Connection,
    ring: str | None = None,
) -> list[dict]:
    """Return trades that need alerts sent.

    Filters: alert_sent=FALSE, ring IS NOT NULL, confidence_score > 0,
    is_planned_trade=FALSE.  Optionally filter to a specific ring.

    Args:
        conn: Open database connection.
        ring: Optional ring name to filter by ('inner', 'middle', etc.).

    Returns:
        List of trade dicts.
    """
    if ring is not None:
        rows = conn.execute(SELECT_UNSENT_ALERTS_BY_RING, {"ring": ring}).fetchall()
    else:
        rows = conn.execute(SELECT_UNSENT_ALERTS).fetchall()
    return [dict(row) for row in rows]


def mark_alert_sent(conn: sqlite3.Connection, trade_id: int) -> None:
    """Set alert_sent=TRUE on a trade.

    Args:
        conn: Open database connection.
        trade_id: Primary key of the trade to mark.
    """
    conn.execute(MARK_ALERT_SENT, {"id": trade_id})
    conn.commit()


def get_alerts_sent_today(conn: sqlite3.Connection) -> int:
    """Count non-Inner Ring alerts delivered today.

    Used for digest batching threshold checks (Phase 4).

    Args:
        conn: Open database connection.

    Returns:
        Number of non-Inner alerts sent today.
    """
    row = conn.execute(COUNT_ALERTS_SENT_TODAY).fetchone()
    return row["cnt"] if row else 0


def get_failed_alerts(
    conn: sqlite3.Connection,
    resolved: bool = False,
) -> list[dict]:
    """Return failed alert records.

    Args:
        conn: Open database connection.
        resolved: If False (default), return only unresolved failures.

    Returns:
        List of failed_alerts dicts joined with alerts_log fields.
    """
    rows = conn.execute(
        SELECT_FAILED_ALERTS, {"resolved": 1 if resolved else 0}
    ).fetchall()
    return [dict(row) for row in rows]
