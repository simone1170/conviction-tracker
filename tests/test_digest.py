"""Phase 4 tests: digest batching logic (SPEC §7.3)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.alerts.telegram_bot import queue_for_digest, should_batch_alert
from src.db.models import init_db


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = init_db(db_path)
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    for extra in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        extra.unlink(missing_ok=True)


def _log_sent_alert(conn, ring: str = "middle", count: int = 1) -> None:
    """Insert N 'sent' alerts_log rows for the given ring, all sent today."""
    for _ in range(count):
        conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score,
                 delivery_status, sent_at)
            VALUES (NULL, ?, 'cluster', 'test msg', 60, 'sent', datetime('now'))
            """,
            (ring,),
        )
    conn.commit()


# ── should_batch_alert ────────────────────────────────────────────────────────

class TestShouldBatchAlert:
    def test_under_threshold_no_batch(self, db_conn):
        """0 alerts sent today with threshold=5 → Middle Ring should NOT batch."""
        with patch("src.alerts.telegram_bot.settings") as mock_settings:
            mock_settings.daily_alert_digest_threshold = 5
            result = should_batch_alert(db_conn, "middle")
        assert result is False

    def test_at_threshold_batch(self, db_conn):
        """5 non-Inner alerts sent today with threshold=5 → should batch."""
        _log_sent_alert(db_conn, ring="middle", count=5)
        with patch("src.alerts.telegram_bot.settings") as mock_settings:
            mock_settings.daily_alert_digest_threshold = 5
            result = should_batch_alert(db_conn, "middle")
        assert result is True

    def test_over_threshold_batch(self, db_conn):
        """7 non-Inner alerts sent today → should batch."""
        _log_sent_alert(db_conn, ring="middle", count=7)
        with patch("src.alerts.telegram_bot.settings") as mock_settings:
            mock_settings.daily_alert_digest_threshold = 5
            result = should_batch_alert(db_conn, "middle")
        assert result is True

    def test_inner_ring_never_batched_zero_alerts(self, db_conn):
        """Inner Ring is never batched, even with 0 alerts sent."""
        result = should_batch_alert(db_conn, "inner")
        assert result is False

    def test_inner_ring_never_batched_over_threshold(self, db_conn):
        """Inner Ring is never batched even when threshold is exceeded."""
        _log_sent_alert(db_conn, ring="middle", count=10)
        with patch("src.alerts.telegram_bot.settings") as mock_settings:
            mock_settings.daily_alert_digest_threshold = 5
            result = should_batch_alert(db_conn, "inner")
        assert result is False

    def test_inner_ring_alerts_excluded_from_count(self, db_conn):
        """Inner Ring sent alerts don't count toward the digest threshold."""
        # 5 inner ring alerts — these should NOT push middle ring into batching
        _log_sent_alert(db_conn, ring="inner", count=5)
        with patch("src.alerts.telegram_bot.settings") as mock_settings:
            mock_settings.daily_alert_digest_threshold = 5
            result = should_batch_alert(db_conn, "middle")
        assert result is False

    def test_outer_ring_subject_to_batching(self, db_conn):
        """Outer Ring follows same batching rules as Middle Ring."""
        _log_sent_alert(db_conn, ring="middle", count=5)
        with patch("src.alerts.telegram_bot.settings") as mock_settings:
            mock_settings.daily_alert_digest_threshold = 5
            result = should_batch_alert(db_conn, "outer")
        assert result is True

    def test_threshold_of_one(self, db_conn):
        """Threshold=1 means first non-inner alert sends, second batches."""
        with patch("src.alerts.telegram_bot.settings") as mock_settings:
            mock_settings.daily_alert_digest_threshold = 1
            result_before = should_batch_alert(db_conn, "middle")
        assert result_before is False

        _log_sent_alert(db_conn, ring="middle", count=1)
        with patch("src.alerts.telegram_bot.settings") as mock_settings:
            mock_settings.daily_alert_digest_threshold = 1
            result_after = should_batch_alert(db_conn, "middle")
        assert result_after is True


# ── queue_for_digest ──────────────────────────────────────────────────────────

class TestQueueForDigest:
    def test_creates_pending_alerts_log_row(self, db_conn):
        """queue_for_digest inserts an alerts_log row with delivery_status='pending'."""
        queue_for_digest(db_conn, None, "middle", "cluster", "test message", 65)
        row = db_conn.execute("SELECT * FROM alerts_log WHERE ring='middle'").fetchone()
        assert row is not None
        assert row["delivery_status"] == "pending"

    def test_pending_row_not_counted_as_sent(self, db_conn):
        """Pending rows do not count toward the daily sent threshold."""
        queue_for_digest(db_conn, None, "middle", "cluster", "msg", 60)
        with patch("src.alerts.telegram_bot.settings") as mock_settings:
            mock_settings.daily_alert_digest_threshold = 5
            count = should_batch_alert(db_conn, "middle")
        # 0 sent alerts today → should not batch
        assert count is False

    def test_queue_preserves_message(self, db_conn):
        """Queued message is stored verbatim in alerts_log."""
        msg = "🔵 SECTOR CLUSTER — Semiconductors\nConfidence: 70/100"
        queue_for_digest(db_conn, None, "middle", "cluster", msg, 70)
        row = db_conn.execute("SELECT message FROM alerts_log").fetchone()
        assert row["message"] == msg

    def test_queue_stores_confidence_score(self, db_conn):
        """Queued alert stores the confidence score."""
        queue_for_digest(db_conn, None, "middle", "cluster", "msg", 72)
        row = db_conn.execute("SELECT confidence_score FROM alerts_log").fetchone()
        assert row["confidence_score"] == 72

    def test_queue_with_trade_id(self, db_conn):
        """queue_for_digest works with a non-None trade_id."""
        # Insert a trade first
        db_conn.execute(
            """
            INSERT INTO trades (
                source, ticker, person_name, transaction_type, total_value,
                transaction_date, filing_date, is_planned_trade, alert_sent
            ) VALUES ('sec_form4', 'NVDA', 'Test', 'purchase', 100000,
                      '2026-04-01', '2026-04-01', 0, 0)
            """
        )
        db_conn.commit()
        trade_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        queue_for_digest(db_conn, trade_id, "middle", "cluster", "msg", 60)
        row = db_conn.execute("SELECT trade_id FROM alerts_log").fetchone()
        assert row["trade_id"] == trade_id
