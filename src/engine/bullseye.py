"""Bullseye orchestrator — routes trades to the appropriate ring and scores them.

Phase 2: Inner Ring detection and scoring.
Phase 4: Middle Ring cluster detection added (detect_and_score_clusters).

Middle Ring detection operates on the full DB window (not just the current
ingestion batch), so it is called separately from process_trades after trades
have been inserted.

Public API:
    enriched = process_trades(trades, conn)
    clusters  = detect_and_score_clusters(conn)
"""

from __future__ import annotations

import sqlite3

from src.alerts.formatters import format_anti_signal_alert, format_large_sell_alert
from src.alerts.telegram_bot import queue_for_digest, should_batch_alert
from src.engine.anti_signal import detect_large_sells, detect_sell_clusters, is_new_sell_cluster
from src.engine.inner_ring import check_inner_ring, load_watchlist
from src.engine.middle_ring import detect_clusters, is_new_cluster
from src.engine.scoring import compute_historical_avg, score_cluster, score_trade
from src.scrapers.etf_mapper import get_sector_for_ticker
from src.utils.logger import get_logger

log = get_logger(__name__)


def process_trades(
    trades: list[dict],
    conn: sqlite3.Connection,
) -> list[dict]:
    """Assign ring and confidence_score to each trade and return enriched list.

    Phase 2 + Phase 4 behaviour:
    - Planned trades (10b5-1) are kept in the output with ring=None
      and confidence_score=0 (stored for context, never alert).
    - Purchases on the Inner Ring watchlist above the per-ticker threshold
      get ring='inner'.
    - Everything else gets ring=None; Middle Ring detection runs separately
      via detect_and_score_clusters() after DB insertion.

    Args:
        trades: Raw trade dicts from the edgar scraper.
        conn: Open database connection used for watchlist lookup and scoring.

    Returns:
        The same list with 'ring' and 'confidence_score' populated in-place.
        Trades also receive a 'sector_name' key (None if not in any sector).
    """
    if not trades:
        return trades

    watchlist = load_watchlist(conn)
    log.info("Processing %d trades through Bullseye", len(trades))

    inner_count = 0
    planned_count = 0

    for trade in trades:
        # Planned trades: store with score=0, no ring assignment
        if trade.get("is_planned_trade"):
            trade["ring"] = None
            trade["confidence_score"] = 0
            trade["sector_name"] = None
            planned_count += 1
            log.info(
                "Planned trade skipped: %s by %s",
                trade.get("ticker"),
                trade.get("person_name"),
            )
            continue

        # Tag sector (metadata only — not a DB column)
        trade["sector_name"] = get_sector_for_ticker(conn, trade.get("ticker", ""))

        # Inner Ring check
        if check_inner_ring(trade, watchlist):
            trade["ring"] = "inner"
            inner_count += 1
        else:
            trade["ring"] = None  # Middle/Outer assigned later

        # Score the trade
        historical_avg = compute_historical_avg(conn, trade.get("ticker", ""))
        trade["confidence_score"] = score_trade(
            trade,
            ring=trade["ring"] or "outer",
            conn=conn,
            historical_avg=historical_avg,
        )

    log.info(
        "Bullseye results: %d Inner Ring, %d planned (excluded), %d unassigned",
        inner_count,
        planned_count,
        len(trades) - inner_count - planned_count,
    )
    return trades


def detect_and_score_clusters(
    conn: sqlite3.Connection,
    window_days: int = 7,
) -> list[dict]:
    """Detect Middle Ring clusters, score them, and update DB ring assignments.

    Runs on the full DB window (all trades inserted so far), not just the
    current batch. Called from run_ingestion.py after insert_trades().

    For each new cluster found:
    - Sets ring='middle' in the DB for all constituent trades that don't
      already have a ring assignment.
    - Computes the cluster confidence score.
    - Attaches confidence_score to the returned cluster dict for alerting.

    Args:
        conn: Open database connection.
        window_days: Rolling lookback window in days (default 7).

    Returns:
        List of NEW cluster dicts (already-alerted clusters filtered out).
        Each cluster has a 'confidence_score' key added.
    """
    clusters = detect_clusters(conn, window_days=window_days)
    new_clusters: list[dict] = []

    for cluster in clusters:
        sector_name = cluster["sector_name"]
        window_end = cluster["window_end"]

        if not is_new_cluster(conn, sector_name, window_end):
            log.info("Cluster '%s' already alerted — skipping", sector_name)
            continue

        # Score the cluster (re-scores constituent trades with cluster bonuses)
        cluster["confidence_score"] = score_cluster(cluster, conn)

        # Update DB: mark constituent trades as 'middle' ring (don't overwrite 'inner')
        for trade in cluster["trades"]:
            trade_id = trade.get("id")
            if trade_id and not trade.get("ring"):
                conn.execute(
                    """
                    UPDATE trades
                    SET ring = 'middle', confidence_score = ?
                    WHERE id = ? AND (ring IS NULL OR ring = '')
                    """,
                    (cluster["confidence_score"], trade_id),
                )
        conn.commit()
        log.info(
            "Cluster '%s' scored %d/100, %d trades updated to ring='middle'",
            sector_name,
            cluster["confidence_score"],
            len(cluster["trades"]),
        )

        new_clusters.append(cluster)

    return new_clusters


def detect_and_alert_sells(
    conn: sqlite3.Connection,
    alerter,  # TelegramAlerter | None
) -> tuple[int, int]:
    """Detect sell clusters and large watchlist sells, then send/queue alerts.

    Called from run_ingestion.py after buy-side processing.  Operates on the
    full DB window so patterns that formed over multiple ingestion runs are
    caught.

    Args:
        conn: Open database connection.
        alerter: TelegramAlerter instance, or None if Telegram is not configured.

    Returns:
        Tuple of (cluster_count, large_sell_count) — number of NEW events
        detected (not number of alerts sent, which may differ due to batching).
    """
    watchlist = load_watchlist(conn)

    # ── Sell clusters ─────────────────────────────────────────────────────────
    clusters = detect_sell_clusters(conn)
    n_clusters = 0

    for cluster in clusters:
        ticker = cluster["ticker"]
        if not is_new_sell_cluster(conn, ticker):
            log.info("Sell cluster for '%s' already alerted — skipping", ticker)
            continue

        n_clusters += 1
        message = format_anti_signal_alert(cluster)

        if alerter and should_batch_alert(conn, "middle"):
            queue_for_digest(conn, None, "middle", "anti_signal", message, 0)
            log.info("Sell cluster alert queued for digest: %s", ticker)
        elif alerter:
            alerter.send_alert(
                trade_id=None,
                ring="middle",
                alert_type="anti_signal",
                message=message,
                confidence_score=0,
                conn=conn,
            )
        else:
            # Telegram not configured — log to DB as pending so it's not lost
            queue_for_digest(conn, None, "middle", "anti_signal", message, 0)
            log.info("Sell cluster stored (Telegram not configured): %s", ticker)

    # ── Large single sells on watchlist tickers ───────────────────────────────
    large_sells = detect_large_sells(conn, watchlist)
    n_large = 0

    # Dedup: skip if we already alerted this specific trade
    already_alerted_ids = {
        row["trade_id"]
        for row in conn.execute(
            """
            SELECT trade_id FROM alerts_log
            WHERE alert_type = 'anti_signal'
              AND trade_id IS NOT NULL
              AND delivery_status IN ('sent', 'pending')
            """
        ).fetchall()
        if row["trade_id"] is not None
    }

    for trade in large_sells:
        trade_id = trade.get("id")
        if trade_id in already_alerted_ids:
            log.debug("Large sell trade %s already alerted — skipping", trade_id)
            continue

        n_large += 1
        message = format_large_sell_alert(trade)

        if alerter and should_batch_alert(conn, "middle"):
            queue_for_digest(conn, trade_id, "middle", "anti_signal", message, 0)
            log.info("Large sell alert queued for digest: %s $%.0f",
                     trade.get("ticker"), trade.get("total_value", 0))
        elif alerter:
            alerter.send_alert(
                trade_id=trade_id,
                ring="middle",
                alert_type="anti_signal",
                message=message,
                confidence_score=0,
                conn=conn,
            )
        else:
            queue_for_digest(conn, trade_id, "middle", "anti_signal", message, 0)

    log.info(
        "Anti-signal scan complete: %d new sell clusters, %d large watchlist sells",
        n_clusters, n_large,
    )
    return n_clusters, n_large
