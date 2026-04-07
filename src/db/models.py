"""SQLite schema definitions and connection factory.

Every connection created here applies the mandatory PRAGMAs:
  PRAGMA journal_mode=WAL
  PRAGMA busy_timeout=5000
  PRAGMA foreign_keys=ON
"""

import sqlite3
from pathlib import Path


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT    NOT NULL CHECK(source IN ('sec_form4', 'congress_senate', 'congress_house')),
    ticker              TEXT    NOT NULL,
    company_name        TEXT,
    person_name         TEXT    NOT NULL,
    person_title        TEXT,
    transaction_type    TEXT    NOT NULL CHECK(transaction_type IN ('purchase', 'sale')),
    transaction_code    TEXT,
    ownership_type      TEXT,
    shares              REAL,
    price_per_share     REAL,
    total_value         REAL    NOT NULL,
    amount_range        TEXT,
    transaction_date    TEXT    NOT NULL,
    filing_date         TEXT    NOT NULL,
    report_lag_days     INTEGER,
    filing_url          TEXT,
    is_planned_trade    BOOLEAN DEFAULT FALSE,
    ring                TEXT    CHECK(ring IN ('inner', 'middle', 'outer', 'confirmation')),
    confirmation_of     INTEGER REFERENCES trades(id),
    confidence_score    INTEGER,
    alert_sent          BOOLEAN DEFAULT FALSE,
    created_at          TEXT    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, ticker, person_name, transaction_date, total_value, transaction_type)
        ON CONFLICT REPLACE
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker           ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_transaction_date ON trades(transaction_date);
CREATE INDEX IF NOT EXISTS idx_trades_source           ON trades(source);
CREATE INDEX IF NOT EXISTS idx_trades_ring             ON trades(ring);
CREATE INDEX IF NOT EXISTS idx_trades_alert            ON trades(alert_sent);

CREATE TABLE IF NOT EXISTS watchlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT    UNIQUE NOT NULL,
    threshold_usd REAL    NOT NULL,
    notes         TEXT,
    active        BOOLEAN DEFAULT TRUE,
    created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sectors (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sector_name         TEXT    NOT NULL,
    etf_ticker          TEXT    NOT NULL,
    constituent_ticker  TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE(etf_ticker, constituent_ticker)
);

CREATE TABLE IF NOT EXISTS alerts_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            INTEGER REFERENCES trades(id),
    ring                TEXT    NOT NULL,
    alert_type          TEXT    NOT NULL CHECK(alert_type IN ('single', 'cluster', 'anomaly', 'confirmation', 'anti_signal', 'digest')),
    message             TEXT    NOT NULL,
    confidence_score    INTEGER,
    sent_at             TEXT,
    telegram_message_id TEXT,
    delivery_status     TEXT    DEFAULT 'pending' CHECK(delivery_status IN ('pending', 'sent', 'failed')),
    retry_count         INTEGER DEFAULT 0,
    created_at          TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS failed_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_log_id    INTEGER REFERENCES alerts_log(id),
    error_message   TEXT,
    last_retry_at   TEXT,
    resolved        BOOLEAN DEFAULT FALSE,
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_health (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    component            TEXT    NOT NULL,
    last_successful_run  TEXT    NOT NULL,
    records_processed    INTEGER DEFAULT 0,
    errors               INTEGER DEFAULT 0,
    notes                TEXT,
    created_at           TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS politician_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    politician_name  TEXT    NOT NULL,
    sector_name      TEXT    NOT NULL,
    first_trade_date TEXT    NOT NULL,
    trade_count      INTEGER DEFAULT 1,
    UNIQUE(politician_name, sector_name) ON CONFLICT IGNORE
);
"""

_PRAGMAS = (
    "PRAGMA journal_mode=WAL;"
    "PRAGMA busy_timeout=5000;"
    "PRAGMA foreign_keys=ON;"
)


# ── Connection factory ────────────────────────────────────────────────────────

def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with required PRAGMAs applied.

    Args:
        db_path: Absolute path to the SQLite database file.

    Returns:
        A configured sqlite3.Connection with WAL mode and foreign keys on.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_PRAGMAS)
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they do not already exist.

    Args:
        conn: An open database connection (PRAGMAs already applied).
    """
    conn.executescript(_DDL)
    conn.commit()


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create the database, apply PRAGMAs, and create all tables.

    Convenience entry point used by scripts/setup_db.py and tests.

    Args:
        db_path: Absolute path to the SQLite database file.

    Returns:
        An open, fully initialised sqlite3.Connection.
    """
    conn = get_connection(db_path)
    create_tables(conn)
    return conn
