"""Phase 3 tests: Telegram alert formatters, bot client, and alerting queries."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.alerts.formatters import format_inner_ring_alert
from src.alerts.telegram_bot import TelegramAlerter
from src.db.models import init_db
from src.db import queries


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


def _insert_trade(conn, ring="inner", alert_sent=False, confidence_score=75):
    conn.execute(
        """
        INSERT INTO trades (
            source, ticker, company_name, person_name, person_title,
            transaction_type, transaction_code, ownership_type,
            shares, price_per_share, total_value,
            transaction_date, filing_date, report_lag_days,
            filing_url, is_planned_trade, ring, confidence_score, alert_sent
        ) VALUES (
            'sec_form4', 'NVDA', 'Nvidia Corp', 'Jensen Huang', 'CEO',
            'purchase', 'P', 'D',
            1000, 500.0, 500000.0,
            '2026-04-01', '2026-04-02', 1,
            'https://sec.gov/test', 0, ?, ?, ?
        )
        """,
        (ring, confidence_score, 1 if alert_sent else 0),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── Formatter tests ───────────────────────────────────────────────────────────

class TestFormatInnerRingAlert:
    def _full_trade(self) -> dict:
        return {
            "ticker": "NVDA",
            "confidence_score": 85,
            "person_name": "Jensen Huang",
            "person_title": "CEO",
            "shares": 1000,
            "price_per_share": 500.0,
            "total_value": 500_000.0,
            "ownership_type": "D",
            "transaction_date": "2026-04-01",
            "filing_url": "https://sec.gov/test/filing",
        }

    def test_contains_ticker(self):
        msg = format_inner_ring_alert(self._full_trade())
        assert "NVDA" in msg

    def test_contains_person_name(self):
        msg = format_inner_ring_alert(self._full_trade())
        assert "Jensen Huang" in msg

    def test_contains_confidence_score(self):
        msg = format_inner_ring_alert(self._full_trade())
        assert "85/100" in msg

    def test_contains_filing_url(self):
        msg = format_inner_ring_alert(self._full_trade())
        assert "https://sec.gov/test/filing" in msg

    def test_contains_total_value(self):
        msg = format_inner_ring_alert(self._full_trade())
        assert "500,000" in msg

    def test_missing_person_title_no_crash(self):
        trade = self._full_trade()
        trade["person_title"] = None
        msg = format_inner_ring_alert(trade)
        assert "Jensen Huang" in msg
        assert "None" not in msg

    def test_missing_filing_url_no_crash(self):
        trade = self._full_trade()
        trade["filing_url"] = None
        msg = format_inner_ring_alert(trade)
        assert "NVDA" in msg
        assert "None" not in msg

    def test_missing_filing_url_fallback_text(self):
        trade = self._full_trade()
        trade["filing_url"] = None
        msg = format_inner_ring_alert(trade)
        assert "not available" in msg

    def test_indirect_ownership_label(self):
        trade = self._full_trade()
        trade["ownership_type"] = "I"
        msg = format_inner_ring_alert(trade)
        assert "Indirect" in msg

    def test_direct_ownership_label(self):
        msg = format_inner_ring_alert(self._full_trade())
        assert "Direct" in msg

    def test_missing_shares_and_price_no_crash(self):
        trade = self._full_trade()
        trade["shares"] = None
        trade["price_per_share"] = None
        msg = format_inner_ring_alert(trade)
        assert "NVDA" in msg


# ── TelegramAlerter tests (mocked) ────────────────────────────────────────────

class TestTelegramAlerter:
    def _alerter(self) -> TelegramAlerter:
        return TelegramAlerter(bot_token="fake-token", chat_id="12345")

    # ── send_message ──────────────────────────────────────────────────────────

    def test_send_message_returns_message_id_on_success(self):
        alerter = self._alerter()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 42}}
        mock_resp.raise_for_status = MagicMock()

        with patch("src.alerts.telegram_bot.requests.post", return_value=mock_resp):
            result = alerter.send_message("hello")

        assert result == "42"

    def test_send_message_retries_3_times_on_failure(self):
        alerter = self._alerter()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "description": "Bad Request"}
        mock_resp.raise_for_status = MagicMock()

        with patch("src.alerts.telegram_bot.requests.post", return_value=mock_resp) as mock_post:
            with patch("src.alerts.telegram_bot.time.sleep"):
                result = alerter.send_message("hello")

        assert result is None
        assert mock_post.call_count == 3

    def test_send_message_returns_none_when_disabled(self):
        alerter = TelegramAlerter(bot_token="", chat_id="")
        with patch("src.alerts.telegram_bot.requests.post") as mock_post:
            result = alerter.send_message("hello")
        assert result is None
        mock_post.assert_not_called()

    def test_send_message_retries_on_exception(self):
        alerter = self._alerter()
        with patch(
            "src.alerts.telegram_bot.requests.post", side_effect=ConnectionError("timeout")
        ) as mock_post:
            with patch("src.alerts.telegram_bot.time.sleep"):
                result = alerter.send_message("hello")
        assert result is None
        assert mock_post.call_count == 3

    # ── send_alert — success path ─────────────────────────────────────────────

    def test_send_alert_writes_sent_to_alerts_log(self, db_conn):
        alerter = self._alerter()
        trade_id = _insert_trade(db_conn)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 99}}
        mock_resp.raise_for_status = MagicMock()

        with patch("src.alerts.telegram_bot.requests.post", return_value=mock_resp):
            success = alerter.send_alert(trade_id, "inner", "single", "msg", 80, db_conn)

        assert success is True
        row = db_conn.execute("SELECT * FROM alerts_log WHERE trade_id = ?", (trade_id,)).fetchone()
        assert row is not None
        assert row["delivery_status"] == "sent"
        assert row["telegram_message_id"] == "99"

    def test_send_alert_writes_failed_to_alerts_log(self, db_conn):
        alerter = self._alerter()
        trade_id = _insert_trade(db_conn)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "description": "Forbidden"}
        mock_resp.raise_for_status = MagicMock()

        with patch("src.alerts.telegram_bot.requests.post", return_value=mock_resp):
            with patch("src.alerts.telegram_bot.time.sleep"):
                success = alerter.send_alert(trade_id, "inner", "single", "msg", 80, db_conn)

        assert success is False
        row = db_conn.execute("SELECT * FROM alerts_log WHERE trade_id = ?", (trade_id,)).fetchone()
        assert row is not None
        assert row["delivery_status"] == "failed"

    def test_send_alert_writes_to_failed_alerts_on_failure(self, db_conn):
        alerter = self._alerter()
        trade_id = _insert_trade(db_conn)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "description": "Forbidden"}
        mock_resp.raise_for_status = MagicMock()

        with patch("src.alerts.telegram_bot.requests.post", return_value=mock_resp):
            with patch("src.alerts.telegram_bot.time.sleep"):
                alerter.send_alert(trade_id, "inner", "single", "msg", 80, db_conn)

        failed = db_conn.execute("SELECT * FROM failed_alerts").fetchall()
        assert len(failed) == 1
        assert failed[0]["resolved"] == 0

    def test_send_alert_no_failed_alerts_row_on_success(self, db_conn):
        alerter = self._alerter()
        trade_id = _insert_trade(db_conn)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 7}}
        mock_resp.raise_for_status = MagicMock()

        with patch("src.alerts.telegram_bot.requests.post", return_value=mock_resp):
            alerter.send_alert(trade_id, "inner", "single", "msg", 80, db_conn)

        failed = db_conn.execute("SELECT * FROM failed_alerts").fetchall()
        assert len(failed) == 0


# ── Query tests ───────────────────────────────────────────────────────────────

class TestAlertQueries:
    def test_get_unsent_alerts_returns_pending_inner(self, db_conn):
        _insert_trade(db_conn, ring="inner", alert_sent=False)
        results = queries.get_unsent_alerts(db_conn, ring="inner")
        assert len(results) == 1
        assert results[0]["ticker"] == "NVDA"

    def test_get_unsent_alerts_excludes_already_sent(self, db_conn):
        _insert_trade(db_conn, ring="inner", alert_sent=True)
        results = queries.get_unsent_alerts(db_conn, ring="inner")
        assert len(results) == 0

    def test_get_unsent_alerts_excludes_zero_score(self, db_conn):
        _insert_trade(db_conn, ring="inner", alert_sent=False, confidence_score=0)
        results = queries.get_unsent_alerts(db_conn, ring="inner")
        assert len(results) == 0

    def test_get_unsent_alerts_no_ring_filter_returns_all_rings(self, db_conn):
        _insert_trade(db_conn, ring="inner", alert_sent=False)
        # Insert a 'middle' ring trade with a unique value to avoid UNIQUE conflict
        db_conn.execute(
            """
            INSERT INTO trades (
                source, ticker, company_name, person_name, person_title,
                transaction_type, transaction_code, ownership_type,
                shares, price_per_share, total_value,
                transaction_date, filing_date, report_lag_days,
                filing_url, is_planned_trade, ring, confidence_score, alert_sent
            ) VALUES (
                'sec_form4', 'AMD', 'AMD Inc', 'Lisa Su', 'CEO',
                'purchase', 'P', 'D',
                500, 200.0, 100000.0,
                '2026-04-01', '2026-04-02', 1,
                'https://sec.gov/amd', 0, 'middle', 55, 0
            )
            """
        )
        db_conn.commit()
        results = queries.get_unsent_alerts(db_conn)
        assert len(results) == 2

    def test_mark_alert_sent_updates_flag(self, db_conn):
        trade_id = _insert_trade(db_conn, ring="inner", alert_sent=False)
        queries.mark_alert_sent(db_conn, trade_id)
        results = queries.get_unsent_alerts(db_conn, ring="inner")
        assert len(results) == 0

    def test_get_alerts_sent_today_excludes_inner(self, db_conn):
        trade_id = _insert_trade(db_conn)
        # Insert an inner alert_log row (should NOT be counted)
        db_conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score,
                 delivery_status, sent_at)
            VALUES (?, 'inner', 'single', 'msg', 80, 'sent', datetime('now'))
            """,
            (trade_id,),
        )
        db_conn.commit()
        count = queries.get_alerts_sent_today(db_conn)
        assert count == 0

    def test_get_alerts_sent_today_counts_non_inner(self, db_conn):
        trade_id = _insert_trade(db_conn)
        db_conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score,
                 delivery_status, sent_at)
            VALUES (?, 'middle', 'cluster', 'msg', 60, 'sent', datetime('now'))
            """,
            (trade_id,),
        )
        db_conn.commit()
        count = queries.get_alerts_sent_today(db_conn)
        assert count == 1

    def test_get_failed_alerts_returns_unresolved(self, db_conn):
        trade_id = _insert_trade(db_conn)
        cursor = db_conn.execute(
            """
            INSERT INTO alerts_log (trade_id, ring, alert_type, message, confidence_score)
            VALUES (?, 'inner', 'single', 'msg', 80)
            """,
            (trade_id,),
        )
        alert_log_id = cursor.lastrowid
        db_conn.execute(
            """
            INSERT INTO failed_alerts (alert_log_id, error_message, last_retry_at)
            VALUES (?, 'timeout', datetime('now'))
            """,
            (alert_log_id,),
        )
        db_conn.commit()

        results = queries.get_failed_alerts(db_conn, resolved=False)
        assert len(results) == 1
        assert results[0]["error_message"] == "timeout"

    def test_get_failed_alerts_resolved_false_excludes_resolved(self, db_conn):
        trade_id = _insert_trade(db_conn)
        cursor = db_conn.execute(
            """
            INSERT INTO alerts_log (trade_id, ring, alert_type, message, confidence_score)
            VALUES (?, 'inner', 'single', 'msg', 80)
            """,
            (trade_id,),
        )
        alert_log_id = cursor.lastrowid
        db_conn.execute(
            """
            INSERT INTO failed_alerts (alert_log_id, error_message, last_retry_at, resolved)
            VALUES (?, 'timeout', datetime('now'), 1)
            """,
            (alert_log_id,),
        )
        db_conn.commit()

        results = queries.get_failed_alerts(db_conn, resolved=False)
        assert len(results) == 0
