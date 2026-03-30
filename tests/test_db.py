"""Phase 1 tests: database creation, WAL mode, schema validation."""

import sqlite3
from pathlib import Path
import tempfile

import pytest

from src.db.models import get_connection, create_tables


@pytest.fixture()
def db_conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = get_connection(db_path)
    create_tables(conn)
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    for extra in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        extra.unlink(missing_ok=True)


def test_wal_mode(db_conn):
    row = db_conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_busy_timeout(db_conn):
    row = db_conn.execute("PRAGMA busy_timeout").fetchone()
    assert row[0] == 5000


def test_foreign_keys_on(db_conn):
    row = db_conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


def test_all_tables_exist(db_conn):
    expected = {
        "trades", "watchlist", "sectors",
        "alerts_log", "failed_alerts", "system_health", "politician_history",
    }
    rows = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    actual = {r[0] for r in rows}
    assert expected.issubset(actual)


def test_trades_indexes_exist(db_conn):
    rows = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'"
    ).fetchall()
    index_names = {r[0] for r in rows}
    for idx in ("idx_trades_ticker", "idx_trades_transaction_date",
                "idx_trades_source", "idx_trades_ring", "idx_trades_alert"):
        assert idx in index_names, f"Missing index: {idx}"


def test_trades_schema_columns(db_conn):
    info = db_conn.execute("PRAGMA table_info(trades)").fetchall()
    columns = {row[1] for row in info}
    required = {
        "id", "source", "ticker", "person_name", "transaction_type",
        "total_value", "transaction_date", "filing_date", "is_planned_trade",
        "alert_sent", "confidence_score", "ring",
    }
    assert required.issubset(columns)


def test_trades_unique_constraint(db_conn):
    """Inserting the same trade twice should silently replace (ON CONFLICT REPLACE)."""
    trade = {
        "source": "sec_form4", "ticker": "NVDA", "company_name": "NVIDIA",
        "person_name": "Jensen Huang", "person_title": "CEO",
        "transaction_type": "purchase", "transaction_code": "P",
        "ownership_type": "D", "shares": 1000.0, "price_per_share": 800.0,
        "total_value": 800000.0, "amount_range": None,
        "transaction_date": "2026-03-15", "filing_date": "2026-03-17",
        "report_lag_days": 2, "filing_url": "https://example.com",
        "is_planned_trade": False, "ring": "inner",
        "confidence_score": 85, "alert_sent": False,
    }
    db_conn.execute(
        """INSERT INTO trades (source,ticker,company_name,person_name,person_title,
           transaction_type,transaction_code,ownership_type,shares,price_per_share,
           total_value,amount_range,transaction_date,filing_date,report_lag_days,
           filing_url,is_planned_trade,ring,confidence_score,alert_sent)
           VALUES (:source,:ticker,:company_name,:person_name,:person_title,
           :transaction_type,:transaction_code,:ownership_type,:shares,:price_per_share,
           :total_value,:amount_range,:transaction_date,:filing_date,:report_lag_days,
           :filing_url,:is_planned_trade,:ring,:confidence_score,:alert_sent)""",
        trade,
    )
    db_conn.execute(
        """INSERT INTO trades (source,ticker,company_name,person_name,person_title,
           transaction_type,transaction_code,ownership_type,shares,price_per_share,
           total_value,amount_range,transaction_date,filing_date,report_lag_days,
           filing_url,is_planned_trade,ring,confidence_score,alert_sent)
           VALUES (:source,:ticker,:company_name,:person_name,:person_title,
           :transaction_type,:transaction_code,:ownership_type,:shares,:price_per_share,
           :total_value,:amount_range,:transaction_date,:filing_date,:report_lag_days,
           :filing_url,:is_planned_trade,:ring,:confidence_score,:alert_sent)""",
        trade,
    )
    db_conn.commit()
    count = db_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert count == 1


def test_watchlist_schema(db_conn):
    info = db_conn.execute("PRAGMA table_info(watchlist)").fetchall()
    columns = {row[1] for row in info}
    assert {"ticker", "threshold_usd", "active"}.issubset(columns)


def test_sectors_unique_constraint(db_conn):
    db_conn.execute(
        "INSERT INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Semis', 'SMH', 'NVDA', '2026-03-30')"
    )
    # Second insert of same etf+ticker should silently replace
    db_conn.execute(
        "INSERT OR REPLACE INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Semis', 'SMH', 'NVDA', '2026-03-30')"
    )
    db_conn.commit()
    count = db_conn.execute("SELECT COUNT(*) FROM sectors").fetchone()[0]
    assert count == 1
