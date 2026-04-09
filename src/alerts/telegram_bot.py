"""Telegram bot client with retry logic.

Uses direct HTTP POST to the Telegram Bot API so no async runtime is needed.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime

import requests

from src.config import settings
from src.db import queries
from src.utils.logger import get_logger

log = get_logger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
# Delays between retry attempts: wait 1 s, then 2 s (3 total attempts)
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0)


class TelegramAlerter:
    """Send Telegram messages with automatic retry and database logging.

    Args:
        bot_token: Telegram bot token from BotFather.
        chat_id: Target chat or user ID.
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        if not self._enabled:
            log.warning("TelegramAlerter: credentials missing — alerting disabled")

    # ── Public API ────────────────────────────────────────────────────────────

    def send_message(self, text: str, parse_mode: str = "HTML") -> str | None:
        """Send a message via Telegram Bot API.

        Retries up to 3 total attempts with exponential backoff (1 s, 2 s, 4 s).

        Args:
            text: Message text (HTML or Markdown depending on parse_mode).
            parse_mode: Telegram parse mode — "HTML" or "MarkdownV2".

        Returns:
            The Telegram message_id string on success, None on all failures.
        """
        if not self._enabled:
            log.warning("Telegram not configured — skipping message send")
            return None

        url = _API_URL.format(token=self.bot_token)
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}

        last_error = "unknown error"
        # 3 attempts: first try + 2 retries
        delays = (0.0,) + _RETRY_DELAYS  # first attempt has no pre-delay
        for attempt, pre_delay in enumerate(delays, start=1):
            if pre_delay > 0:
                time.sleep(pre_delay)
            try:
                resp = requests.post(url, json=payload, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if data.get("ok"):
                    msg_id = str(data["result"]["message_id"])
                    log.info("Telegram message sent: message_id=%s", msg_id)
                    return msg_id
                last_error = data.get("description", "Telegram API returned ok=false")
                log.warning("Attempt %d/3 failed: %s", attempt, last_error)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                log.warning("Attempt %d/3 failed: %s", attempt, last_error)

        log.error("All 3 Telegram send attempts failed: %s", last_error)
        return None

    def send_alert(
        self,
        trade_id: int,
        ring: str,
        alert_type: str,
        message: str,
        confidence_score: int,
        conn: sqlite3.Connection,
    ) -> bool:
        """Send an alert and record the delivery attempt in the database.

        On success: inserts an alerts_log row with delivery_status='sent' and
        stores the telegram_message_id.

        On failure: inserts an alerts_log row with delivery_status='failed' and
        creates a failed_alerts row for retry tracking.

        Args:
            trade_id: Primary key of the trade being alerted.
            ring: Ring identifier ('inner', 'middle', 'outer', 'confirmation').
            alert_type: Alert type ('single', 'cluster', 'anomaly', etc.).
            message: Formatted alert text.
            confidence_score: Computed confidence score for this signal.
            conn: Open SQLite connection with PRAGMAs applied.

        Returns:
            True if the message was delivered successfully, False otherwise.
        """
        # Create a pending log entry first so we always have a record
        cursor = conn.execute(
            """
            INSERT INTO alerts_log
                (trade_id, ring, alert_type, message, confidence_score)
            VALUES (?, ?, ?, ?, ?)
            """,
            (trade_id, ring, alert_type, message, confidence_score),
        )
        alert_log_id = cursor.lastrowid
        conn.commit()

        message_id = self.send_message(message)
        now = datetime.utcnow().isoformat()

        if message_id is not None:
            conn.execute(
                """
                UPDATE alerts_log
                SET delivery_status = 'sent',
                    sent_at = ?,
                    telegram_message_id = ?,
                    retry_count = 0
                WHERE id = ?
                """,
                (now, message_id, alert_log_id),
            )
            conn.commit()
            log.info(
                "Alert sent: alert_log_id=%d trade_id=%s ring=%s",
                alert_log_id,
                trade_id,
                ring,
            )
            return True

        # Delivery failed after all retries
        conn.execute(
            """
            UPDATE alerts_log
            SET delivery_status = 'failed',
                retry_count = 3
            WHERE id = ?
            """,
            (alert_log_id,),
        )
        conn.execute(
            """
            INSERT INTO failed_alerts (alert_log_id, error_message, last_retry_at)
            VALUES (?, ?, ?)
            """,
            (alert_log_id, "All 3 Telegram retry attempts failed", now),
        )
        conn.commit()
        log.error(
            "Alert failed after retries: alert_log_id=%d trade_id=%d",
            alert_log_id,
            trade_id,
        )
        return False


# ── Digest batching (SPEC §7.3) ───────────────────────────────────────────────


def should_batch_alert(conn: sqlite3.Connection, ring: str) -> bool:
    """Return True if this alert should be queued for the daily digest.

    Inner Ring alerts are NEVER batched — they always return False.
    All other rings check the count of non-Inner alerts sent today against
    the DAILY_ALERT_DIGEST_THRESHOLD from config.

    Args:
        conn: Open database connection.
        ring: Ring identifier ('inner', 'middle', 'outer', 'confirmation').

    Returns:
        True if the alert should be queued (not sent immediately).
    """
    if ring == "inner":
        return False
    count = queries.get_alerts_sent_today(conn)
    threshold = settings.daily_alert_digest_threshold
    should_batch = count >= threshold
    if should_batch:
        log.debug(
            "Batching %s alert — %d non-inner alerts sent today (threshold=%d)",
            ring,
            count,
            threshold,
        )
    return should_batch


def queue_for_digest(
    conn: sqlite3.Connection,
    trade_id: int | None,
    ring: str,
    alert_type: str,
    message: str,
    confidence_score: int,
) -> None:
    """Queue an alert for the daily digest batch.

    Inserts an alerts_log row with delivery_status='pending'. These rows are
    collected and sent as a single digest message (full digest sending: Phase 8).

    Args:
        conn: Open database connection.
        trade_id: Primary key of the associated trade, or None for cluster alerts.
        ring: Ring identifier.
        alert_type: Alert type string.
        message: Formatted alert text.
        confidence_score: Computed confidence score.
    """
    conn.execute(
        """
        INSERT INTO alerts_log
            (trade_id, ring, alert_type, message, confidence_score, delivery_status)
        VALUES (?, ?, ?, ?, ?, 'pending')
        """,
        (trade_id, ring, alert_type, message, confidence_score),
    )
    conn.commit()
    log.info("Alert queued for digest: ring=%s alert_type=%s", ring, alert_type)
