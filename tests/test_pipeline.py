"""End-to-end pipeline integration test.

Exercises the complete conviction-tracker pipeline against a temporary
SQLite database and sends real Telegram messages so you can confirm the
full system works on your phone.

Usage:
    python tests/test_pipeline.py

What fires:
    1. [TEST] Inner Ring alert  — NVDA CEO buy above watchlist threshold
    2. [TEST] Middle Ring alert — 3-company Semiconductor cluster
    3. Planned trade exclusion  — 10b5-1 trade silently stored, no alert
    4. Dedup suppression        — re-running cluster detection sends nothing extra
    5. Digest batching          — 6th non-inner alert queues instead of sends
    6. Summary Telegram message — pass/fail count so you see the final result

Problems and workarounds are written to:
    data/logs/pipeline_test_<YYYYMMDD_HHMMSS>.log
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Windows console UTF-8 fix ─────────────────────────────────────────────────
# cp1252 (default Windows console encoding) can't print ✓ ✗ ─ etc.
# Reconfigure stdout/stderr to UTF-8 with replacement fallback so the script
# doesn't crash on special characters.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Path setup ────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.alerts.formatters import format_inner_ring_alert, format_middle_ring_alert
from src.alerts.telegram_bot import (
    TelegramAlerter,
    queue_for_digest,
    should_batch_alert,
)
from src.config import SECTORS_PATH, settings
from src.db import queries
from src.db.models import init_db
from src.engine.bullseye import detect_and_score_clusters, process_trades
from src.engine.middle_ring import is_new_cluster
from src.engine.scoring import score_cluster, score_trade
from src.scrapers.etf_mapper import (
    get_sector_for_ticker,
    is_sectors_seeded,
    seed_sectors_from_json,
)


# ── Log setup ─────────────────────────────────────────────────────────────────

_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_DIR = _PROJECT_ROOT / "data" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_PATH = _LOG_DIR / f"pipeline_test_{_RUN_TS}.log"

_PASSED = "PASS"
_FAILED = "FAIL"
_WARN = "WARN"


class PipelineLog:
    """Writes structured test results to console and a persistent log file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._results: list[dict] = []
        self._problems: list[dict] = []
        self._fh = path.open("w", encoding="utf-8")
        self._write_header()

    def _write_header(self) -> None:
        self._fh.write(f"CONVICTION TRACKER — PIPELINE TEST LOG\n")
        self._fh.write(f"Run started : {datetime.now().isoformat()}\n")
        self._fh.write(f"Python      : {sys.version.split()[0]}\n")
        self._fh.write(f"Telegram    : {'ENABLED' if settings.telegram_enabled else 'DISABLED'}\n")
        self._fh.write("=" * 70 + "\n\n")
        self._fh.flush()

    def record(
        self,
        test_name: str,
        status: str,
        detail: str = "",
        workaround: str = "",
    ) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        icon = "✓" if status == _PASSED else ("⚠" if status == _WARN else "✗")
        console_line = f"  {icon} [{status}] {test_name}"
        if detail:
            console_line += f" — {detail}"
        print(console_line)

        entry = {
            "ts": ts,
            "test": test_name,
            "status": status,
            "detail": detail,
            "workaround": workaround,
        }
        self._results.append(entry)

        log_line = f"[{ts}] {status:4s} | {test_name}"
        if detail:
            log_line += f"\n       detail     : {detail}"
        if workaround:
            log_line += f"\n       workaround : {workaround}"
            self._problems.append(entry)
        self._fh.write(log_line + "\n")
        self._fh.flush()

    def problem(self, test_name: str, exc: Exception, workaround: str = "") -> None:
        tb = traceback.format_exc()
        self.record(test_name, _FAILED, str(exc), workaround)
        self._fh.write(f"       traceback  :\n")
        for line in tb.strip().split("\n"):
            self._fh.write(f"           {line}\n")
        self._fh.write("\n")
        self._fh.flush()

    def section(self, title: str) -> None:
        print(f"\n{'─' * 60}")
        print(f"  {title}")
        print(f"{'─' * 60}")
        self._fh.write(f"\n{'─' * 60}\n{title}\n{'─' * 60}\n")
        self._fh.flush()

    def summary(self) -> tuple[int, int]:
        """Print and log the pass/fail summary. Does NOT close the file."""
        passed = sum(1 for r in self._results if r["status"] == _PASSED)
        total = len(self._results)
        failed = sum(1 for r in self._results if r["status"] == _FAILED)

        text = (
            f"\n{'=' * 70}\n"
            f"SUMMARY : {passed}/{total} passed"
            + (f", {failed} failed" if failed else "")
            + f"\nLog file : {self._path}\n"
        )
        if self._problems:
            text += f"\nProblems logged ({len(self._problems)}):\n"
            for p in self._problems:
                text += f"  • {p['test']}: {p['detail']}\n"
                if p["workaround"]:
                    text += f"    Workaround: {p['workaround']}\n"

        print(text)
        self._fh.write(text)
        self._fh.flush()
        return passed, total

    def close(self) -> None:
        """Flush and close the log file."""
        self._fh.flush()
        self._fh.close()


# ── Database helpers ──────────────────────────────────────────────────────────

def _build_trade(
    ticker: str,
    person: str,
    title: str = "CEO",
    total_value: float = 500_000.0,
    shares: float = 1_000.0,
    price: float = 500.0,
    tx_date: str | None = None,
    source: str = "sec_form4",
    is_planned: bool = False,
    tx_type: str = "purchase",
    ownership: str = "D",
    filing_url: str | None = None,
) -> dict:
    if tx_date is None:
        tx_date = date.today().isoformat()
    return {
        "source": source,
        "ticker": ticker,
        "company_name": f"{ticker} Corp",
        "person_name": person,
        "person_title": title,
        "transaction_type": tx_type,
        "transaction_code": "P" if tx_type == "purchase" else "S",
        "ownership_type": ownership,
        "shares": shares,
        "price_per_share": price,
        "total_value": total_value,
        "amount_range": None,
        "transaction_date": tx_date,
        "filing_date": tx_date,
        "report_lag_days": 0,
        "filing_url": filing_url or f"https://sec.gov/test/{ticker.lower()}",
        "is_planned_trade": is_planned,
        "ring": None,
        "confidence_score": None,
        "alert_sent": False,
    }


def _seed_watchlist(conn) -> None:
    """Seed the watchlist from data/watchlist.json."""
    wl_path = _PROJECT_ROOT / "data" / "watchlist.json"
    entries = json.loads(wl_path.read_text())
    for entry in entries:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (ticker, threshold_usd, notes, active) "
            "VALUES (?, ?, ?, ?)",
            (entry["ticker"], entry["threshold_usd"], entry.get("notes", ""), entry["active"]),
        )
    conn.commit()


# ── Individual test functions ─────────────────────────────────────────────────

def test_db_and_sectors(conn, log: PipelineLog) -> None:
    log.section("1. DATABASE + SECTOR SEEDING")

    # WAL mode
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal", f"Expected wal, got {row[0]}"
        log.record("WAL mode active", _PASSED)
    except Exception as exc:
        log.problem("WAL mode active", exc)

    # Sector seeding
    try:
        n = seed_sectors_from_json(conn, SECTORS_PATH)
        assert n > 0, "0 sectors seeded"
        log.record("Sector seeding from JSON", _PASSED, f"{n} constituent rows inserted")
    except Exception as exc:
        log.problem("Sector seeding from JSON", exc)

    # Sector lookup
    try:
        sector = get_sector_for_ticker(conn, "NVDA")
        assert sector is not None, "NVDA sector not found"
        log.record("Sector lookup (NVDA)", _PASSED, f"sector={sector}")
    except Exception as exc:
        log.problem("Sector lookup (NVDA)", exc)

    try:
        sector = get_sector_for_ticker(conn, "ZZZZZ")
        assert sector is None, f"Expected None for unknown ticker, got {sector}"
        log.record("Sector lookup (unknown ticker)", _PASSED, "returns None as expected")
    except Exception as exc:
        log.problem("Sector lookup (unknown ticker)", exc)

    # Watchlist seeding
    try:
        _seed_watchlist(conn)
        rows = conn.execute("SELECT COUNT(*) AS cnt FROM watchlist WHERE active=1").fetchone()
        assert rows["cnt"] > 0, "Watchlist empty after seeding"
        log.record("Watchlist seeding", _PASSED, f"{rows['cnt']} active tickers")
    except Exception as exc:
        log.problem("Watchlist seeding", exc)


def test_inner_ring(conn, log: PipelineLog, alerter: TelegramAlerter | None) -> dict | None:
    log.section("2. INNER RING — NVDA CEO BUY")

    # Build an NVDA buy above the $100k threshold
    trade = _build_trade(
        ticker="NVDA",
        person="Jensen Huang",
        title="Chief Executive Officer",
        total_value=250_000.0,
        shares=500,
        price=500.0,
        filing_url="https://sec.gov/test/nvda-test",
    )

    enriched_trade = None
    try:
        enriched = process_trades([trade], conn)
        assert enriched, "process_trades returned empty list"
        enriched_trade = enriched[0]
        assert enriched_trade["ring"] == "inner", (
            f"Expected ring='inner', got {enriched_trade['ring']!r}"
        )
        log.record(
            "Inner Ring detection (NVDA, $250k)",
            _PASSED,
            f"ring={enriched_trade['ring']}, score={enriched_trade['confidence_score']}",
        )
    except Exception as exc:
        log.problem("Inner Ring detection", exc)
        return None

    # Scoring sanity: CEO + direct → should be at least 60+15=75
    try:
        score = enriched_trade.get("confidence_score", 0)
        assert score >= 70, f"Score {score} lower than expected (≥70)"
        log.record("Inner Ring confidence score", _PASSED, f"score={score}/100")
    except Exception as exc:
        log.problem("Inner Ring confidence score", exc,
                    workaround="Score may be lower if no historical avg data in test DB — acceptable")

    # Insert and verify
    try:
        queries.insert_trades(conn, [enriched_trade])
        row = conn.execute(
            "SELECT * FROM trades WHERE ticker='NVDA' AND ring='inner'"
        ).fetchone()
        assert row is not None, "Inserted trade not found with ring='inner'"
        log.record("Inner Ring trade persisted to DB", _PASSED)
    except Exception as exc:
        log.problem("Inner Ring DB persistence", exc)
        return enriched_trade

    # Send Telegram alert
    if alerter is None:
        log.record("Telegram Inner Ring send", _WARN, "Telegram not configured — skipped")
        return enriched_trade

    try:
        msg = "[PIPELINE TEST]\n\n" + format_inner_ring_alert(enriched_trade)
        trade_id = conn.execute(
            "SELECT id FROM trades WHERE ticker='NVDA' AND ring='inner'"
        ).fetchone()["id"]
        success = alerter.send_alert(
            trade_id=trade_id,
            ring="inner",
            alert_type="single",
            message=msg,
            confidence_score=enriched_trade.get("confidence_score", 0),
            conn=conn,
        )
        if success:
            queries.mark_alert_sent(conn, trade_id)
            log.record("Telegram Inner Ring alert sent", _PASSED, "check your Telegram")
        else:
            log.record(
                "Telegram Inner Ring alert sent",
                _FAILED,
                "send_alert returned False",
                workaround="Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env",
            )
    except Exception as exc:
        log.problem("Telegram Inner Ring send", exc,
                    workaround="Verify .env Telegram credentials")

    return enriched_trade


def test_planned_trade_exclusion(conn, log: PipelineLog) -> None:
    log.section("3. PLANNED TRADE EXCLUSION (10b5-1)")

    trade = _build_trade(
        ticker="NVDA",
        person="Jensen Huang",
        title="CEO",
        total_value=500_000.0,
        is_planned=True,
    )

    try:
        enriched = process_trades([trade], conn)
        t = enriched[0]
        assert t["ring"] is None, f"Expected ring=None, got {t['ring']!r}"
        assert t["confidence_score"] == 0, f"Expected score=0, got {t['confidence_score']}"
        log.record(
            "Planned trade excluded from ring assignment",
            _PASSED,
            f"ring={t['ring']}, score={t['confidence_score']}",
        )
    except Exception as exc:
        log.problem("Planned trade exclusion", exc)

    # Insert and verify no alert_sent flag set
    try:
        queries.insert_trades(conn, [trade])
        # Planned trade should not appear in unsent alerts
        unsent = queries.get_unsent_alerts(conn, ring="inner")
        planned_in_unsent = [u for u in unsent if u.get("is_planned_trade")]
        assert len(planned_in_unsent) == 0, (
            f"{len(planned_in_unsent)} planned trades appeared in unsent alert queue"
        )
        log.record("Planned trade not in alert queue", _PASSED)
    except Exception as exc:
        log.problem("Planned trade alert queue check", exc)


def test_middle_ring_cluster(conn, log: PipelineLog, alerter: TelegramAlerter | None) -> None:
    log.section("4. MIDDLE RING — SEMICONDUCTOR CLUSTER")

    # Insert 3 trades from different semi companies — use unique people/values
    # to avoid UNIQUE constraint conflicts with the NVDA trade already inserted
    cluster_trades = [
        _build_trade("NVDA", "Jen-Hsun Huang", "Executive Chairman",
                     total_value=300_000.0, shares=600, price=500.0),
        _build_trade("AMD",  "Lisa Su",         "CEO",
                     total_value=200_000.0, shares=800, price=250.0),
        _build_trade("INTC", "Patrick Gelsinger", "President & CEO",
                     total_value=150_000.0, shares=1_000, price=150.0),
    ]

    try:
        enriched = process_trades(cluster_trades, conn)
        queries.insert_trades(conn, enriched)
        log.record(
            "Cluster trades inserted",
            _PASSED,
            f"{len(cluster_trades)} trades (NVDA, AMD, INTC)",
        )
    except Exception as exc:
        log.problem("Cluster trades insertion", exc)
        return

    # Run cluster detection
    try:
        new_clusters = detect_and_score_clusters(conn, window_days=7)
        semi_clusters = [c for c in new_clusters if c["sector_name"] == "Semiconductors"]
        assert len(semi_clusters) >= 1, (
            f"Expected ≥1 Semiconductor cluster, detected: {[c['sector_name'] for c in new_clusters]}"
        )
        cluster = semi_clusters[0]
        log.record(
            "Semiconductor cluster detected",
            _PASSED,
            f"company_count={cluster['company_count']}, "
            f"tickers={cluster['tickers']}, "
            f"score={cluster['confidence_score']}/100",
        )
    except Exception as exc:
        log.problem(
            "Semiconductor cluster detection",
            exc,
            workaround=(
                "Sectors must be seeded before this test. "
                "Also check that test trades use transaction_date=today and source='sec_form4'."
            ),
        )
        return

    # Verify DB ring update
    try:
        rows = conn.execute(
            "SELECT ticker FROM trades WHERE ring='middle' ORDER BY ticker"
        ).fetchall()
        middle_tickers = {r["ticker"] for r in rows}
        # AMD and INTC should be 'middle'; NVDA may be 'inner' (not overwritten)
        assert "AMD" in middle_tickers, "AMD not tagged as middle ring"
        assert "INTC" in middle_tickers, "INTC not tagged as middle ring"
        log.record(
            "Cluster trades tagged as ring='middle' in DB",
            _PASSED,
            f"middle tickers: {sorted(middle_tickers)}",
        )
    except Exception as exc:
        log.problem("Middle ring DB ring update", exc)

    # Send Telegram alert
    if alerter is None:
        log.record("Telegram Middle Ring send", _WARN, "Telegram not configured — skipped")
        return

    try:
        cluster_with_prefix = {**cluster}
        msg = "[PIPELINE TEST]\n\n" + format_middle_ring_alert(cluster_with_prefix)
        success = alerter.send_alert(
            trade_id=None,
            ring="middle",
            alert_type="cluster",
            message=msg,
            confidence_score=cluster.get("confidence_score", 0),
            conn=conn,
        )
        if success:
            log.record("Telegram Middle Ring cluster alert sent", _PASSED, "check your Telegram")
        else:
            log.record(
                "Telegram Middle Ring cluster alert sent",
                _FAILED,
                "send_alert returned False",
                workaround="Check Telegram credentials",
            )
    except Exception as exc:
        log.problem("Telegram Middle Ring send", exc,
                    workaround="Verify .env Telegram credentials")


def test_cluster_dedup(conn, log: PipelineLog) -> None:
    log.section("5. CLUSTER DEDUP SUPPRESSION")

    # Running detect_and_score_clusters again should find 0 new clusters
    # (the Semiconductor cluster was already alerted in the previous test)
    try:
        new_clusters = detect_and_score_clusters(conn, window_days=7)
        semi_new = [c for c in new_clusters if c["sector_name"] == "Semiconductors"]
        assert len(semi_new) == 0, (
            f"Expected 0 new Semiconductor clusters (dedup), got {len(semi_new)}"
        )
        log.record(
            "Duplicate cluster suppressed",
            _PASSED,
            "second detection run finds no new Semiconductor cluster",
        )
    except Exception as exc:
        log.problem(
            "Cluster dedup suppression",
            exc,
            workaround=(
                "is_new_cluster checks alerts_log for a recent cluster entry. "
                "If the previous cluster alert failed to insert, dedup won't work."
            ),
        )


def test_digest_batching(conn, log: PipelineLog) -> None:
    log.section("6. DIGEST BATCHING")

    try:
        # Inject 5 'sent' middle ring alerts to simulate hitting threshold
        for i in range(5):
            conn.execute(
                """
                INSERT INTO alerts_log
                    (trade_id, ring, alert_type, message, confidence_score,
                     delivery_status, sent_at)
                VALUES (NULL, 'middle', 'cluster', 'synthetic test alert', 55,
                        'sent', datetime('now'))
                """
            )
        conn.commit()

        with_threshold = should_batch_alert(conn, "middle")
        assert with_threshold is True, (
            "should_batch_alert should return True after 5 sent non-inner alerts"
        )
        log.record(
            "Digest batching triggered at threshold",
            _PASSED,
            f"threshold={settings.daily_alert_digest_threshold}, should_batch=True",
        )

        never_batch_inner = should_batch_alert(conn, "inner")
        assert never_batch_inner is False, "Inner Ring must never be batched"
        log.record(
            "Inner Ring never batched regardless of count",
            _PASSED,
            "should_batch_alert('inner') = False",
        )

        # Queue a digest entry and verify it's pending
        queue_for_digest(conn, None, "middle", "cluster", "[TEST] digest queue check", 60)
        row = conn.execute(
            "SELECT delivery_status FROM alerts_log WHERE message LIKE '%digest queue check%'"
        ).fetchone()
        assert row and row["delivery_status"] == "pending", (
            f"Expected 'pending', got {row['delivery_status'] if row else 'None'}"
        )
        log.record("queue_for_digest creates pending row", _PASSED)

    except Exception as exc:
        log.problem("Digest batching", exc)


def test_failed_alert_tracking(conn, log: PipelineLog) -> None:
    log.section("7. FAILED ALERT TRACKING")

    try:
        # Simulate a failed send using a bad token
        bad_alerter = TelegramAlerter(bot_token="bad-token", chat_id="0")
        trade_id = conn.execute(
            "SELECT id FROM trades WHERE ring='inner' LIMIT 1"
        ).fetchone()
        if trade_id is None:
            log.record(
                "Failed alert tracking",
                _WARN,
                "No inner ring trade in DB — inserting synthetic one",
            )
            conn.execute(
                """
                INSERT INTO trades (
                    source, ticker, person_name, transaction_type, total_value,
                    transaction_date, filing_date, is_planned_trade, ring, confidence_score, alert_sent
                ) VALUES ('sec_form4', 'AAPL', 'Tim Cook', 'purchase', 300000,
                          ?, ?, 0, 'inner', 80, 0)
                """,
                (date.today().isoformat(), date.today().isoformat()),
            )
            conn.commit()
            trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            trade_id = trade_id["id"]

        import unittest.mock as mock
        with mock.patch("src.alerts.telegram_bot.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "ok": False,
                "description": "Unauthorized",
            }
            mock_post.return_value.raise_for_status = mock.MagicMock()
            with mock.patch("src.alerts.telegram_bot.time.sleep"):
                success = bad_alerter.send_alert(
                    trade_id=trade_id,
                    ring="inner",
                    alert_type="single",
                    message="[TEST] failure tracking test",
                    confidence_score=80,
                    conn=conn,
                )

        assert not success, "Expected failure but got success"

        failed = queries.get_failed_alerts(conn, resolved=False)
        assert len(failed) >= 1, "No entry in failed_alerts table"
        log.record(
            "Failed alert written to failed_alerts table",
            _PASSED,
            f"{len(failed)} unresolved failure(s) in DB",
        )

        log_row = conn.execute(
            "SELECT delivery_status FROM alerts_log WHERE message LIKE '%failure tracking test%'"
        ).fetchone()
        assert log_row and log_row["delivery_status"] == "failed"
        log.record("alerts_log delivery_status='failed'", _PASSED)

    except Exception as exc:
        log.problem("Failed alert tracking", exc)


def test_send_summary_telegram(
    conn,
    log: PipelineLog,
    alerter: TelegramAlerter | None,
    passed: int,
    total: int,
) -> None:
    log.section("8. SUMMARY TELEGRAM MESSAGE")

    if alerter is None:
        log.record("Summary Telegram send", _WARN, "Telegram not configured — skipped")
        return

    failed = total - passed
    status_icon = "✅" if failed == 0 else "⚠️"
    msg = (
        f"{status_icon} <b>PIPELINE TEST COMPLETE</b>\n"
        f"\n"
        f"Result: {passed}/{total} tests passed"
        + (f" ({failed} failed)" if failed else "")
        + "\n"
        f"Run: {_RUN_TS}\n"
        f"\n"
        f"<i>All components exercised:\n"
        f"• DB init + WAL mode\n"
        f"• Sector seeding + ticker lookup\n"
        f"• Inner Ring detection + scoring\n"
        f"• Planned trade (10b5-1) exclusion\n"
        f"• Middle Ring cluster detection\n"
        f"• Cluster dedup suppression\n"
        f"• Digest batching threshold\n"
        f"• Failed alert tracking</i>\n"
        f"\n"
        f"Log: {_LOG_PATH.name}"
    )

    try:
        result = alerter.send_message(msg)
        if result:
            log.record("Summary message sent to Telegram", _PASSED, f"message_id={result}")
        else:
            log.record(
                "Summary message sent to Telegram",
                _FAILED,
                "send_message returned None",
                workaround="Telegram reachability issue — check credentials and network",
            )
    except Exception as exc:
        log.problem("Summary Telegram send", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print("\n" + "=" * 60)
    print("  CONVICTION TRACKER — PIPELINE TEST")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    log = PipelineLog(_LOG_PATH)
    print(f"\n  Log file: {_LOG_PATH}\n")

    # ── Set up isolated test database ─────────────────────────────────────────
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=_LOG_DIR) as f:
        test_db_path = Path(f.name)

    print(f"  Test DB : {test_db_path.name}\n")
    conn = init_db(test_db_path)

    # ── Telegram client (real credentials) ───────────────────────────────────
    alerter: TelegramAlerter | None = None
    if settings.telegram_enabled:
        alerter = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
        print("  Telegram: ENABLED (real alerts will be sent)\n")
    else:
        print("  Telegram: DISABLED (no .env credentials — alert tests will be skipped)\n")

    # ── Run tests ─────────────────────────────────────────────────────────────
    try:
        test_db_and_sectors(conn, log)
        test_inner_ring(conn, log, alerter)
        test_planned_trade_exclusion(conn, log)
        test_middle_ring_cluster(conn, log, alerter)
        test_cluster_dedup(conn, log)
        test_digest_batching(conn, log)
        test_failed_alert_tracking(conn, log)
    except Exception as exc:
        log.problem("Unexpected top-level failure", exc)
    finally:
        passed, total = log.summary()
        test_send_summary_telegram(conn, log, alerter, passed, total)
        log.close()
        conn.close()

        # Clean up test DB
        for suffix in (".db", ".db-wal", ".db-shm"):
            Path(str(test_db_path) + (suffix[3:] if test_db_path.suffix == ".db" else suffix)).unlink(missing_ok=True)
        test_db_path.unlink(missing_ok=True)

    print(f"\n  Full log written to:\n  {_LOG_PATH}\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
