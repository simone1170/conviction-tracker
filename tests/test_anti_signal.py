"""Phase 6 tests: anti-signal sell cluster detection and formatters."""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.alerts.formatters import format_anti_signal_alert, format_large_sell_alert
from src.db.models import init_db
from src.engine.anti_signal import (
    detect_large_sells,
    detect_sell_clusters,
    is_new_sell_cluster,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = init_db(db_path)
    # Seed a minimal watchlist
    conn.execute(
        "INSERT INTO watchlist (ticker, threshold_usd, active) VALUES ('NVDA', 100000, 1)"
    )
    conn.execute(
        "INSERT INTO watchlist (ticker, threshold_usd, active) VALUES ('AAPL', 200000, 1)"
    )
    conn.commit()
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    for extra in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        extra.unlink(missing_ok=True)


def _insert_sell(
    conn,
    ticker: str = "NVDA",
    person: str = "Jensen Huang",
    title: str = "CEO",
    total_value: float = 600_000.0,
    tx_date: str | None = None,
    source: str = "sec_form4",
    is_planned: bool = False,
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
            'sale', 'S', 'D',
            1000, ?, ?,
            ?, ?, 1,
            'https://sec.gov/test', ?, NULL, 0, 0
        )
        """,
        (
            source, ticker, f"{ticker} Corp", person, title,
            total_value / 1000, total_value,
            tx_date, date.today().isoformat(),
            1 if is_planned else 0,
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── detect_sell_clusters ──────────────────────────────────────────────────────

class TestDetectSellClusters:
    def test_basic_cluster_two_sellers(self, db_conn):
        """2 distinct sellers for the same ticker within 14 days → 1 cluster."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang")
        _insert_sell(db_conn, "NVDA", "Colette Kress")
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert len(clusters) == 1
        assert clusters[0]["ticker"] == "NVDA"
        assert clusters[0]["seller_count"] == 2

    def test_below_threshold_one_seller(self, db_conn):
        """1 seller only → no cluster (need 2+)."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang")
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert clusters == []

    def test_same_person_does_not_count(self, db_conn):
        """3 sells from the same person → still only 1 distinct seller, no cluster."""
        for i in range(3):
            _insert_sell(db_conn, "NVDA", "Jensen Huang", total_value=float(600_000 + i))
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert clusters == []

    def test_window_boundary_inside(self, db_conn):
        """Sell from 13 days ago + sell from today → cluster (both within 14-day window)."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang",
                     tx_date=(date.today() - timedelta(days=13)).isoformat())
        _insert_sell(db_conn, "NVDA", "Colette Kress",
                     tx_date=date.today().isoformat())
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert len(clusters) == 1

    def test_window_boundary_outside(self, db_conn):
        """Sell from 15 days ago + sell from today → no cluster (older one is outside window)."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang",
                     tx_date=(date.today() - timedelta(days=15)).isoformat())
        _insert_sell(db_conn, "NVDA", "Colette Kress",
                     tx_date=date.today().isoformat())
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert clusters == []

    def test_planned_trades_excluded(self, db_conn):
        """2 sells in window but one is planned → only 1 valid, no cluster."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang")
        _insert_sell(db_conn, "NVDA", "Colette Kress", is_planned=True)
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert clusters == []

    def test_congressional_excluded(self, db_conn):
        """Congressional sells do not count toward the cluster threshold."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang", source="sec_form4")
        _insert_sell(db_conn, "NVDA", "Nancy Pelosi", source="congress_senate")
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert clusters == []

    def test_different_tickers_separate_clusters(self, db_conn):
        """Sells in NVDA and AAPL each form their own cluster."""
        _insert_sell(db_conn, "NVDA", "Person A")
        _insert_sell(db_conn, "NVDA", "Person B")
        _insert_sell(db_conn, "AAPL", "Person C")
        _insert_sell(db_conn, "AAPL", "Person D")
        clusters = detect_sell_clusters(db_conn, window_days=14)
        tickers = {c["ticker"] for c in clusters}
        assert len(clusters) == 2
        assert "NVDA" in tickers
        assert "AAPL" in tickers

    def test_aggregate_value(self, db_conn):
        """Cluster aggregate_value is the sum of all constituent sells."""
        _insert_sell(db_conn, "NVDA", "Person A", total_value=1_000_000.0)
        _insert_sell(db_conn, "NVDA", "Person B", total_value=500_000.0)
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert len(clusters) == 1
        assert clusters[0]["aggregate_value"] == pytest.approx(1_500_000.0)

    def test_sellers_list_populated(self, db_conn):
        """Cluster sellers list contains all distinct person names."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang")
        _insert_sell(db_conn, "NVDA", "Colette Kress")
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert set(clusters[0]["sellers"]) == {"Jensen Huang", "Colette Kress"}


# ── detect_large_sells ────────────────────────────────────────────────────────

class TestDetectLargeSells:
    def test_large_sell_on_watchlist_ticker(self, db_conn):
        """$600K sell on NVDA (watchlist) → returned."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang", total_value=600_000.0)
        watchlist = {"NVDA": 100_000.0}
        result = detect_large_sells(db_conn, watchlist, threshold_usd=500_000.0)
        assert len(result) == 1
        assert result[0]["ticker"] == "NVDA"

    def test_below_threshold_not_returned(self, db_conn):
        """$400K sell on NVDA (watchlist) → not returned (below $500K threshold)."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang", total_value=400_000.0)
        watchlist = {"NVDA": 100_000.0}
        result = detect_large_sells(db_conn, watchlist, threshold_usd=500_000.0)
        assert result == []

    def test_non_watchlist_ticker_not_returned(self, db_conn):
        """$600K sell on AMD (not on watchlist) → not returned."""
        _insert_sell(db_conn, "AMD", "Lisa Su", total_value=600_000.0)
        watchlist = {"NVDA": 100_000.0}
        result = detect_large_sells(db_conn, watchlist, threshold_usd=500_000.0)
        assert result == []

    def test_exactly_at_threshold(self, db_conn):
        """$500K sell at exactly the threshold → returned."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang", total_value=500_000.0)
        watchlist = {"NVDA": 100_000.0}
        result = detect_large_sells(db_conn, watchlist, threshold_usd=500_000.0)
        assert len(result) == 1

    def test_empty_watchlist_returns_empty(self, db_conn):
        """Empty watchlist → no results regardless of sell size."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang", total_value=1_000_000.0)
        result = detect_large_sells(db_conn, {}, threshold_usd=500_000.0)
        assert result == []

    def test_planned_sell_excluded(self, db_conn):
        """Planned 10b5-1 sells are excluded even if above threshold."""
        _insert_sell(db_conn, "NVDA", "Jensen Huang", total_value=800_000.0, is_planned=True)
        watchlist = {"NVDA": 100_000.0}
        result = detect_large_sells(db_conn, watchlist, threshold_usd=500_000.0)
        assert result == []


# ── is_new_sell_cluster ───────────────────────────────────────────────────────

class TestIsNewSellCluster:
    def test_true_when_no_prior_alert(self, db_conn):
        assert is_new_sell_cluster(db_conn, "NVDA") is True

    def test_false_after_anti_signal_sent(self, db_conn):
        db_conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score,
                 delivery_status, sent_at)
            VALUES (NULL, 'middle', 'anti_signal', '🔴 SELL CLUSTER — NVDA', 0,
                    'sent', datetime('now'))
            """
        )
        db_conn.commit()
        assert is_new_sell_cluster(db_conn, "NVDA") is False

    def test_false_for_pending_alert(self, db_conn):
        db_conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score, delivery_status)
            VALUES (NULL, 'middle', 'anti_signal', '🔴 SELL CLUSTER — NVDA', 0, 'pending')
            """
        )
        db_conn.commit()
        assert is_new_sell_cluster(db_conn, "NVDA") is False

    def test_different_ticker_is_new(self, db_conn):
        db_conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score,
                 delivery_status, sent_at)
            VALUES (NULL, 'middle', 'anti_signal', '🔴 SELL CLUSTER — AAPL', 0,
                    'sent', datetime('now'))
            """
        )
        db_conn.commit()
        assert is_new_sell_cluster(db_conn, "NVDA") is True

    def test_duplicate_suppression_integration(self, db_conn):
        """Detect cluster → log alert → re-run → suppressed."""
        _insert_sell(db_conn, "NVDA", "Person A")
        _insert_sell(db_conn, "NVDA", "Person B")
        clusters = detect_sell_clusters(db_conn, window_days=14)
        assert len(clusters) == 1

        # Simulate alert being sent
        msg = format_anti_signal_alert(clusters[0])
        db_conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score,
                 delivery_status, sent_at)
            VALUES (NULL, 'middle', 'anti_signal', ?, 0, 'sent', datetime('now'))
            """,
            (msg,),
        )
        db_conn.commit()

        assert is_new_sell_cluster(db_conn, "NVDA") is False


# ── Formatter tests ───────────────────────────────────────────────────────────

class TestFormatAntiSignalAlert:
    def _cluster(self) -> dict:
        return {
            "ticker": "NVDA",
            "seller_count": 2,
            "aggregate_value": 1_500_000.0,
            "window_start": "2026-04-01",
            "window_end": "2026-04-08",
            "trades": [
                {
                    "person_name": "Jensen Huang",
                    "person_title": "CEO",
                    "total_value": 1_000_000.0,
                    "transaction_date": "2026-04-08",
                },
                {
                    "person_name": "Colette Kress",
                    "person_title": "CFO",
                    "total_value": 500_000.0,
                    "transaction_date": "2026-04-01",
                },
            ],
        }

    def test_contains_ticker(self):
        assert "NVDA" in format_anti_signal_alert(self._cluster())

    def test_contains_seller_count(self):
        assert "2" in format_anti_signal_alert(self._cluster())

    def test_contains_person_names(self):
        msg = format_anti_signal_alert(self._cluster())
        assert "Jensen Huang" in msg
        assert "Colette Kress" in msg

    def test_contains_aggregate_value(self):
        assert "1,500,000" in format_anti_signal_alert(self._cluster())

    def test_contains_review_warning(self):
        assert "Review your position" in format_anti_signal_alert(self._cluster())

    def test_red_emoji(self):
        assert "🔴" in format_anti_signal_alert(self._cluster())


class TestFormatLargeSellAlert:
    def _trade(self) -> dict:
        return {
            "ticker": "NVDA",
            "person_name": "Jensen Huang",
            "person_title": "CEO",
            "shares": 2000.0,
            "price_per_share": 400.0,
            "total_value": 800_000.0,
            "transaction_date": "2026-04-08",
            "filing_url": "https://sec.gov/test/nvda",
        }

    def test_contains_ticker(self):
        assert "NVDA" in format_large_sell_alert(self._trade())

    def test_contains_watchlist_warning(self):
        assert "Watchlist stock" in format_large_sell_alert(self._trade())

    def test_contains_person_name(self):
        assert "Jensen Huang" in format_large_sell_alert(self._trade())

    def test_contains_total_value(self):
        assert "800,000" in format_large_sell_alert(self._trade())

    def test_contains_filing_url(self):
        assert "https://sec.gov/test/nvda" in format_large_sell_alert(self._trade())

    def test_contains_review_warning(self):
        assert "Review your position" in format_large_sell_alert(self._trade())

    def test_missing_filing_url_graceful(self):
        trade = self._trade()
        trade["filing_url"] = None
        msg = format_large_sell_alert(trade)
        assert "NVDA" in msg
        assert "None" not in msg

    def test_missing_title_graceful(self):
        trade = self._trade()
        trade["person_title"] = None
        msg = format_large_sell_alert(trade)
        assert "Jensen Huang" in msg
