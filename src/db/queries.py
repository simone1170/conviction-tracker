"""All SQL queries used by the application. No queries should live outside this file."""

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
