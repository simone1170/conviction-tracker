"""Phase 2 tests: Inner Ring watchlist filter."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.db.models import init_db
from src.engine.inner_ring import check_inner_ring, load_watchlist


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = init_db(db_path)
    # Seed a small watchlist
    conn.execute(
        "INSERT INTO watchlist (ticker, threshold_usd, active) VALUES ('NVDA', 100000, 1)"
    )
    conn.execute(
        "INSERT INTO watchlist (ticker, threshold_usd, active) VALUES ('AAPL', 200000, 1)"
    )
    conn.execute(
        "INSERT INTO watchlist (ticker, threshold_usd, active) VALUES ('GOOGL', 150000, 0)"  # inactive
    )
    conn.commit()
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    for extra in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        extra.unlink(missing_ok=True)


def _trade(
    ticker: str = "NVDA",
    total_value: float = 150_000.0,
    source: str = "sec_form4",
    tx_type: str = "purchase",
    is_planned: bool = False,
) -> dict:
    return {
        "source": source,
        "ticker": ticker,
        "transaction_type": tx_type,
        "total_value": total_value,
        "is_planned_trade": is_planned,
    }


# ── load_watchlist ────────────────────────────────────────────────────────────

def test_load_watchlist_returns_active_only(db_conn):
    watchlist = load_watchlist(db_conn)
    assert "NVDA" in watchlist
    assert "AAPL" in watchlist
    # GOOGL is inactive — must NOT appear
    assert "GOOGL" not in watchlist


def test_load_watchlist_correct_thresholds(db_conn):
    watchlist = load_watchlist(db_conn)
    assert watchlist["NVDA"] == pytest.approx(100_000.0)
    assert watchlist["AAPL"] == pytest.approx(200_000.0)


def test_load_watchlist_empty_db():
    """load_watchlist on a fresh DB returns an empty dict (no crash)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = init_db(db_path)
    watchlist = load_watchlist(conn)
    assert watchlist == {}
    conn.close()
    db_path.unlink(missing_ok=True)


# ── check_inner_ring ──────────────────────────────────────────────────────────

def test_inner_ring_match():
    watchlist = {"NVDA": 100_000.0}
    assert check_inner_ring(_trade("NVDA", 150_000.0), watchlist) is True


def test_inner_ring_exactly_at_threshold():
    watchlist = {"NVDA": 100_000.0}
    assert check_inner_ring(_trade("NVDA", 100_000.0), watchlist) is True


def test_inner_ring_below_threshold():
    watchlist = {"NVDA": 100_000.0}
    assert check_inner_ring(_trade("NVDA", 50_000.0), watchlist) is False


def test_inner_ring_not_in_watchlist():
    watchlist = {"NVDA": 100_000.0}
    assert check_inner_ring(_trade("TSLA", 500_000.0), watchlist) is False


def test_inner_ring_planned_trade_excluded():
    watchlist = {"NVDA": 100_000.0}
    t = _trade("NVDA", 150_000.0, is_planned=True)
    assert check_inner_ring(t, watchlist) is False


def test_inner_ring_sale_excluded():
    watchlist = {"NVDA": 100_000.0}
    t = _trade("NVDA", 150_000.0, tx_type="sale")
    assert check_inner_ring(t, watchlist) is False


def test_inner_ring_congress_senate_excluded():
    watchlist = {"NVDA": 100_000.0}
    t = _trade("NVDA", 500_000.0, source="congress_senate")
    assert check_inner_ring(t, watchlist) is False


def test_inner_ring_congress_house_excluded():
    watchlist = {"NVDA": 100_000.0}
    t = _trade("NVDA", 500_000.0, source="congress_house")
    assert check_inner_ring(t, watchlist) is False


def test_inner_ring_empty_watchlist():
    """With no watchlist, nothing qualifies."""
    assert check_inner_ring(_trade("NVDA", 150_000.0), {}) is False
