"""Run one full SEC scrape → Bullseye filter → database write → alert cycle.

Usage:
    python scripts/run_ingestion.py

Prints a summary on stdout.  All detailed logging goes to data/logs/conviction.log.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.alerts.formatters import format_inner_ring_alert, format_middle_ring_alert
from src.alerts.telegram_bot import TelegramAlerter, queue_for_digest, should_batch_alert
from src.config import DB_PATH, SECTORS_PATH, settings
from src.db.models import get_connection
from src.db import queries
from src.engine.bullseye import detect_and_score_clusters, process_trades
from src.scrapers.edgar_scraper import fetch_recent_form4s
from src.scrapers.etf_mapper import is_sectors_seeded, seed_sectors_from_json
from src.utils.logger import get_logger

log = get_logger(__name__)

_COMPONENT = "scraper_sec_form4"
_DEFAULT_LOOKBACK_DAYS = 3


def main() -> None:
    """Execute one scrape+filter+store+alert cycle and print a summary."""
    log.info("=== Ingestion cycle started ===")

    conn = get_connection(DB_PATH)
    errors = 0
    n_inserted = 0
    enriched: list[dict] = []

    # ── Seed sectors on first run ─────────────────────────────────────────────
    if not is_sectors_seeded(conn):
        log.info("Sectors table empty — seeding from %s", SECTORS_PATH)
        seed_sectors_from_json(conn, SECTORS_PATH)

    try:
        # ── Determine fetch window ────────────────────────────────────────────
        last_date = queries.get_last_scrape_date(conn, "sec_form4")
        if last_date is not None:
            since_date = last_date
            log.info("Resuming from last scrape: %s", since_date)
        else:
            since_date = date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
            log.info("No prior run found — fetching last %d days", _DEFAULT_LOOKBACK_DAYS)

        # ── Fetch ─────────────────────────────────────────────────────────────
        raw_trades = fetch_recent_form4s(since_date)

        if not raw_trades:
            log.info("0 new Form 4 transactions found")
        else:
            # ── Filter + score ────────────────────────────────────────────────
            enriched = process_trades(raw_trades, conn)

            # ── Persist ───────────────────────────────────────────────────────
            n_inserted = queries.insert_trades(conn, enriched)

    except Exception as exc:
        log.error("Ingestion cycle failed: %s", exc, exc_info=True)
        errors = 1
    finally:
        queries.log_health(
            conn,
            component=_COMPONENT,
            records_processed=n_inserted,
            errors=errors,
        )

    # ── Middle Ring cluster detection ─────────────────────────────────────────
    new_clusters = detect_and_score_clusters(conn)

    if new_clusters:
        sector_names = [c["sector_name"] for c in new_clusters]
        print(f"Detected {len(new_clusters)} sector cluster(s): {', '.join(sector_names)}")
    else:
        log.info("No new Middle Ring clusters detected")

    # ── Send alerts ───────────────────────────────────────────────────────────
    n_inner_sent = 0
    n_inner_failed = 0
    n_middle_sent = 0
    n_middle_queued = 0

    if not settings.telegram_enabled:
        log.info("Telegram not configured — skipping alerts")
    else:
        alerter = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)

        # Inner Ring alerts (always immediate, never batched)
        unsent_inner = queries.get_unsent_alerts(conn, ring="inner")
        log.info("Found %d unsent Inner Ring alerts", len(unsent_inner))
        for trade in unsent_inner:
            trade_id = trade["id"]
            message = format_inner_ring_alert(trade)
            success = alerter.send_alert(
                trade_id=trade_id,
                ring="inner",
                alert_type="single",
                message=message,
                confidence_score=trade.get("confidence_score", 0),
                conn=conn,
            )
            if success:
                queries.mark_alert_sent(conn, trade_id)
                n_inner_sent += 1
            else:
                n_inner_failed += 1

        # Middle Ring cluster alerts (subject to digest batching)
        for cluster in new_clusters:
            message = format_middle_ring_alert(cluster)
            confidence_score = cluster.get("confidence_score", 0)

            if should_batch_alert(conn, "middle"):
                queue_for_digest(conn, None, "middle", "cluster", message, confidence_score)
                n_middle_queued += 1
                log.info("Cluster alert queued for digest: %s", cluster["sector_name"])
            else:
                success = alerter.send_alert(
                    trade_id=None,
                    ring="middle",
                    alert_type="cluster",
                    message=message,
                    confidence_score=confidence_score,
                    conn=conn,
                )
                if success:
                    n_middle_sent += 1

        if n_inner_sent or n_inner_failed:
            print(f"Sent {n_inner_sent} Inner Ring alerts, {n_inner_failed} failed")
        if n_middle_sent or n_middle_queued:
            print(
                f"Middle Ring: {n_middle_sent} sent immediately, {n_middle_queued} queued for digest"
            )

    conn.close()

    inner_count = sum(1 for t in enriched if t.get("ring") == "inner")
    planned_count = sum(1 for t in enriched if t.get("is_planned_trade"))
    print(
        f"Ingested {n_inserted} trades "
        f"({inner_count} Inner Ring signals, {planned_count} planned/excluded)"
    )
    log.info("=== Ingestion cycle complete ===")


if __name__ == "__main__":
    main()
