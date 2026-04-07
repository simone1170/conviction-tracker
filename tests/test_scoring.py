"""Phase 2 tests: confidence scoring engine (SPEC §6)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.db.models import init_db
from src.engine.scoring import _title_bonus, compute_historical_avg, score_trade


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_conn():
    """In-memory DB connection for scoring tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = init_db(db_path)
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    for extra in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        extra.unlink(missing_ok=True)


def _trade(
    total_value: float = 150_000.0,
    title: str | None = None,
    ownership: str = "D",
    is_planned: bool = False,
    source: str = "sec_form4",
    tx_type: str = "purchase",
    ticker: str = "NVDA",
    person: str = "Test Person",
    tx_date: str = "2026-04-01",
) -> dict:
    return {
        "source": source,
        "ticker": ticker,
        "person_name": person,
        "person_title": title,
        "transaction_type": tx_type,
        "ownership_type": ownership,
        "total_value": total_value,
        "transaction_date": tx_date,
        "is_planned_trade": is_planned,
    }


# ── _title_bonus ──────────────────────────────────────────────────────────────

def test_title_bonus_ceo():
    assert _title_bonus("CEO") == 15


def test_title_bonus_cfo():
    assert _title_bonus("CFO") == 15


def test_title_bonus_chief_executive():
    assert _title_bonus("Chief Executive Officer") == 15


def test_title_bonus_chief_financial():
    assert _title_bonus("Chief Financial Officer") == 15


def test_title_bonus_president():
    assert _title_bonus("President") == 10


def test_title_bonus_coo():
    assert _title_bonus("COO") == 10


def test_title_bonus_cto():
    assert _title_bonus("CTO") == 10


def test_title_bonus_director():
    assert _title_bonus("Director") == 8


def test_title_bonus_board_director():
    assert _title_bonus("Board of Directors") == 8


def test_title_bonus_vp():
    assert _title_bonus("VP Finance") == 3


def test_title_bonus_vice_president():
    assert _title_bonus("Vice President, Engineering") == 3


def test_title_bonus_other():
    assert _title_bonus("Secretary") == 0


def test_title_bonus_none():
    assert _title_bonus(None) == 0


def test_title_bonus_empty():
    assert _title_bonus("") == 0


# CEO trumps Director when both match
def test_title_bonus_ceo_trumps_director():
    # "CEO and Director" — should match CEO tier (highest)
    assert _title_bonus("CEO and Director") == 15


# ── score_trade ───────────────────────────────────────────────────────────────

def test_planned_trade_always_zero(db_conn):
    t = _trade(is_planned=True, title="CEO")
    assert score_trade(t, "inner", db_conn) == 0


def test_base_score_inner_no_bonus(db_conn):
    t = _trade(title=None)
    assert score_trade(t, "inner", db_conn) == 60


def test_ceo_bonus_inner(db_conn):
    t = _trade(title="CEO")
    assert score_trade(t, "inner", db_conn) == 75  # 60 + 15


def test_cfo_bonus_inner(db_conn):
    t = _trade(title="CFO")
    assert score_trade(t, "inner", db_conn) == 75


def test_director_bonus_inner(db_conn):
    t = _trade(title="Director")
    assert score_trade(t, "inner", db_conn) == 68  # 60 + 8


def test_indirect_penalty(db_conn):
    t = _trade(title="CEO", ownership="I")
    # 60 + 15 (CEO) - 15 (indirect) = 60
    assert score_trade(t, "inner", db_conn) == 60


def test_low_value_penalty(db_conn):
    t = _trade(total_value=5_000.0, title=None)
    # 60 - 20 = 40
    assert score_trade(t, "inner", db_conn) == 40


def test_purchase_size_5x_bonus(db_conn):
    # historical avg = 10_000; total = 55_000 → ratio 5.5x → +20
    t = _trade(total_value=55_000.0, title=None)
    result = score_trade(t, "inner", db_conn, historical_avg=10_000.0)
    assert result == 80  # 60 + 20


def test_purchase_size_2x_bonus(db_conn):
    # historical avg = 10_000; total = 25_000 → ratio 2.5x → +10
    t = _trade(total_value=25_000.0, title=None)
    result = score_trade(t, "inner", db_conn, historical_avg=10_000.0)
    assert result == 70  # 60 + 10


def test_no_size_bonus_when_avg_none(db_conn):
    t = _trade(total_value=100_000.0, title=None)
    assert score_trade(t, "inner", db_conn, historical_avg=None) == 60


def test_score_clamped_at_100(db_conn):
    # CEO (+15) + 5x bonus (+20) + repeat buyer would push over 100
    t = _trade(title="CEO", total_value=1_000_000.0)
    result = score_trade(t, "inner", db_conn, historical_avg=100.0)
    assert result <= 100


def test_score_clamped_at_zero(db_conn):
    # Indirect (-15) + low value (-20) + no base should not go negative
    t = _trade(total_value=500.0, title=None, ownership="I")
    result = score_trade(t, "inner", db_conn, historical_avg=None)
    assert result >= 0


def test_congress_outer_capped_at_50(db_conn):
    # Congress-sourced Outer Ring is hard-capped at 50 (SPEC §6.4)
    t = _trade(
        title="CEO",
        total_value=500_000.0,
        source="congress_senate",
        ownership="D",
    )
    result = score_trade(t, "outer", db_conn, historical_avg=1_000.0)
    assert result <= 50


def test_base_score_middle(db_conn):
    t = _trade(title=None)
    assert score_trade(t, "middle", db_conn) == 40


def test_base_score_outer_sec(db_conn):
    t = _trade(title=None)
    assert score_trade(t, "outer", db_conn) == 50


# ── compute_historical_avg ────────────────────────────────────────────────────

def test_historical_avg_returns_none_no_data(db_conn):
    assert compute_historical_avg(db_conn, "NVDA") is None


def test_historical_avg_returns_none_insufficient_data(db_conn):
    # Insert 2 trades — below the minimum of 3
    for i in range(2):
        db_conn.execute(
            """
            INSERT INTO trades (source, ticker, person_name, transaction_type,
                total_value, transaction_date, filing_date, is_planned_trade, alert_sent)
            VALUES ('sec_form4', 'NVDA', 'Person A', 'purchase',
                100000, '2026-01-15', '2026-01-17', 0, 0)
            """
        )
    db_conn.commit()
    assert compute_historical_avg(db_conn, "NVDA") is None


def test_historical_avg_computed_with_sufficient_data(db_conn):
    # Insert 3 trades with known values
    for val in (100_000.0, 200_000.0, 300_000.0):
        db_conn.execute(
            """
            INSERT INTO trades (source, ticker, person_name, transaction_type,
                total_value, transaction_date, filing_date, is_planned_trade, alert_sent)
            VALUES ('sec_form4', 'AAPL', 'Person B', 'purchase',
                ?, '2026-01-15', '2026-01-17', 0, 0)
            """,
            (val,),
        )
    db_conn.commit()
    avg = compute_historical_avg(db_conn, "AAPL")
    assert avg == pytest.approx(200_000.0)
