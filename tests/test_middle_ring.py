"""Phase 4 tests: Middle Ring sector cluster detection."""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.db.models import init_db
from src.engine.middle_ring import detect_clusters, is_new_cluster
from src.scrapers.etf_mapper import seed_sectors_from_json


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = init_db(db_path)
    # Seed sectors: Semiconductors and Defense
    conn.execute(
        "INSERT INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Semiconductors', 'SMH', 'NVDA', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Semiconductors', 'SMH', 'AMD', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Semiconductors', 'SMH', 'INTC', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Semiconductors', 'SMH', 'AVGO', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Semiconductors', 'SMH', 'QCOM', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Defense', 'ITA', 'LMT', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Defense', 'ITA', 'RTX', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO sectors (sector_name, etf_ticker, constituent_ticker, updated_at) "
        "VALUES ('Defense', 'ITA', 'NOC', '2026-01-01')"
    )
    conn.commit()
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    for extra in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        extra.unlink(missing_ok=True)


def _insert_trade(
    conn,
    ticker: str,
    person: str = "Test Person",
    title: str = "CEO",
    total_value: float = 500_000.0,
    tx_date: str | None = None,
    source: str = "sec_form4",
    is_planned: bool = False,
    tx_type: str = "purchase",
) -> int:
    if tx_date is None:
        tx_date = date.today().isoformat()
    conn.execute(
        """
        INSERT INTO trades (
            source, ticker, company_name, person_name, person_title,
            transaction_type, transaction_code, ownership_type,
            shares, price_per_share, total_value,
            transaction_date, filing_date, report_lag_days,
            filing_url, is_planned_trade, ring, confidence_score, alert_sent
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, 'P', 'D',
            1000, 500.0, ?,
            ?, ?, 1,
            'https://sec.gov/test', ?, NULL, 0, 0
        )
        """,
        (
            source, ticker, f"{ticker} Corp", person, title,
            tx_type, total_value,
            tx_date, date.today().isoformat(),
            1 if is_planned else 0,
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── detect_clusters ───────────────────────────────────────────────────────────

class TestDetectClusters:
    def test_basic_cluster_three_companies(self, db_conn):
        """3 SEC purchases from 3 different Semiconductor companies → 1 cluster."""
        _insert_trade(db_conn, "NVDA", "Jensen Huang")
        _insert_trade(db_conn, "AMD", "Lisa Su")
        _insert_trade(db_conn, "INTC", "Pat Gelsinger")
        clusters = detect_clusters(db_conn, window_days=7)
        assert len(clusters) == 1
        assert clusters[0]["sector_name"] == "Semiconductors"
        assert clusters[0]["company_count"] == 3

    def test_below_threshold_two_companies(self, db_conn):
        """2 companies in same sector → no cluster (need 3+)."""
        _insert_trade(db_conn, "NVDA", "Jensen Huang")
        _insert_trade(db_conn, "AMD", "Lisa Su")
        clusters = detect_clusters(db_conn, window_days=7)
        assert clusters == []

    def test_same_company_multiple_insiders_counts_as_one(self, db_conn):
        """5 NVDA insiders buying ≠ cluster: all same company."""
        for i in range(5):
            _insert_trade(db_conn, "NVDA", f"Person {i}")
        clusters = detect_clusters(db_conn, window_days=7)
        assert clusters == []

    def test_multi_sector_cluster(self, db_conn):
        """3 semi trades + 3 defense trades → 2 separate clusters."""
        _insert_trade(db_conn, "NVDA", "Person A")
        _insert_trade(db_conn, "AMD", "Person B")
        _insert_trade(db_conn, "INTC", "Person C")
        _insert_trade(db_conn, "LMT", "Person D")
        _insert_trade(db_conn, "RTX", "Person E")
        _insert_trade(db_conn, "NOC", "Person F")
        clusters = detect_clusters(db_conn, window_days=7)
        sector_names = {c["sector_name"] for c in clusters}
        assert len(clusters) == 2
        assert "Semiconductors" in sector_names
        assert "Defense" in sector_names

    def test_window_boundary_within_window(self, db_conn):
        """2 trades from 6 days ago + 1 from today → cluster (within 7-day window)."""
        six_days_ago = (date.today() - timedelta(days=6)).isoformat()
        _insert_trade(db_conn, "NVDA", "Person A", tx_date=six_days_ago)
        _insert_trade(db_conn, "AMD", "Person B", tx_date=six_days_ago)
        _insert_trade(db_conn, "INTC", "Person C", tx_date=date.today().isoformat())
        clusters = detect_clusters(db_conn, window_days=7)
        assert len(clusters) == 1

    def test_window_boundary_outside_window(self, db_conn):
        """2 trades from 8 days ago + 1 from today → no cluster (old trades outside window)."""
        eight_days_ago = (date.today() - timedelta(days=8)).isoformat()
        _insert_trade(db_conn, "NVDA", "Person A", tx_date=eight_days_ago)
        _insert_trade(db_conn, "AMD", "Person B", tx_date=eight_days_ago)
        _insert_trade(db_conn, "INTC", "Person C", tx_date=date.today().isoformat())
        clusters = detect_clusters(db_conn, window_days=7)
        assert clusters == []

    def test_planned_trades_excluded(self, db_conn):
        """3 trades in same sector, one is planned → only 2 valid, no cluster."""
        _insert_trade(db_conn, "NVDA", "Person A")
        _insert_trade(db_conn, "AMD", "Person B")
        _insert_trade(db_conn, "INTC", "Person C", is_planned=True)
        clusters = detect_clusters(db_conn, window_days=7)
        assert clusters == []

    def test_congressional_trades_excluded(self, db_conn):
        """2 SEC trades + 1 congressional trade in Semiconductors → no cluster."""
        _insert_trade(db_conn, "NVDA", "Person A", source="sec_form4")
        _insert_trade(db_conn, "AMD", "Person B", source="sec_form4")
        _insert_trade(db_conn, "INTC", "Senator X", source="congress_senate")
        clusters = detect_clusters(db_conn, window_days=7)
        assert clusters == []

    def test_cluster_contains_aggregate_value(self, db_conn):
        """Cluster aggregate_value = sum of all constituent trade values."""
        _insert_trade(db_conn, "NVDA", "Person A", total_value=1_000_000.0)
        _insert_trade(db_conn, "AMD", "Person B", total_value=500_000.0)
        _insert_trade(db_conn, "INTC", "Person C", total_value=250_000.0)
        clusters = detect_clusters(db_conn, window_days=7)
        assert len(clusters) == 1
        assert clusters[0]["aggregate_value"] == pytest.approx(1_750_000.0)

    def test_cluster_contains_tickers_list(self, db_conn):
        """Cluster tickers list contains all distinct companies."""
        _insert_trade(db_conn, "NVDA", "Person A")
        _insert_trade(db_conn, "AMD", "Person B")
        _insert_trade(db_conn, "INTC", "Person C")
        clusters = detect_clusters(db_conn, window_days=7)
        assert set(clusters[0]["tickers"]) == {"NVDA", "AMD", "INTC"}

    def test_cluster_excludes_sales(self, db_conn):
        """Sales do not count toward cluster threshold."""
        _insert_trade(db_conn, "NVDA", "Person A")
        _insert_trade(db_conn, "AMD", "Person B")
        # Third entry is a sale, not a purchase
        _insert_trade(db_conn, "INTC", "Person C", tx_type="sale")
        clusters = detect_clusters(db_conn, window_days=7)
        assert clusters == []


# ── is_new_cluster ────────────────────────────────────────────────────────────

class TestIsNewCluster:
    def test_returns_true_when_no_prior_alert(self, db_conn):
        """No prior cluster alert → is_new_cluster returns True."""
        assert is_new_cluster(db_conn, "Semiconductors", date.today()) is True

    def test_returns_false_after_cluster_alert_sent(self, db_conn):
        """After a cluster alert is logged, is_new_cluster returns False."""
        msg = "🔵 SECTOR CLUSTER — Semiconductors\nConfidence: 65/100"
        db_conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score, delivery_status, sent_at)
            VALUES (NULL, 'middle', 'cluster', ?, 65, 'sent', datetime('now'))
            """,
            (msg,),
        )
        db_conn.commit()
        assert is_new_cluster(db_conn, "Semiconductors", date.today()) is False

    def test_returns_false_for_pending_cluster(self, db_conn):
        """Pending (queued) cluster alert also counts as already alerted."""
        msg = "🔵 SECTOR CLUSTER — Semiconductors\nConfidence: 65/100"
        db_conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score, delivery_status)
            VALUES (NULL, 'middle', 'cluster', ?, 65, 'pending')
            """,
            (msg,),
        )
        db_conn.commit()
        assert is_new_cluster(db_conn, "Semiconductors", date.today()) is False

    def test_different_sector_is_new(self, db_conn):
        """Alert for 'Defense' does not suppress a new 'Semiconductors' cluster."""
        msg = "🔵 SECTOR CLUSTER — Defense\nConfidence: 55/100"
        db_conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score, delivery_status, sent_at)
            VALUES (NULL, 'middle', 'cluster', ?, 55, 'sent', datetime('now'))
            """,
            (msg,),
        )
        db_conn.commit()
        assert is_new_cluster(db_conn, "Semiconductors", date.today()) is True

    def test_accepts_string_window_end(self, db_conn):
        """is_new_cluster accepts an ISO date string for window_end."""
        assert is_new_cluster(db_conn, "Semiconductors", date.today().isoformat()) is True
