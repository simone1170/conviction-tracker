"""Run one full SEC scrape → Bullseye filter → database write cycle.

Usage:
    python scripts/run_ingestion.py

Prints a summary on stdout.  All detailed logging goes to data/logs/conviction.log.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import DB_PATH
from src.db.models import get_connection
from src.db import queries
from src.engine.bullseye import process_trades
from src.scrapers.edgar_scraper import fetch_recent_form4s
from src.utils.logger import get_logger

log = get_logger(__name__)

_COMPONENT = "scraper_sec_form4"
_DEFAULT_LOOKBACK_DAYS = 3


def main() -> None:
    """Execute one scrape+filter+store cycle and print a summary."""
    log.info("=== Ingestion cycle started ===")

    conn = get_connection(DB_PATH)
    errors = 0
    n_inserted = 0
    enriched: list[dict] = []

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
