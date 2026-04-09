"""Alert message formatters for all ring types.

All formatters return HTML-formatted strings for use with Telegram's HTML
parse mode (<b>, <i>, <code>, <a href>).
"""

from __future__ import annotations


def format_inner_ring_alert(trade: dict) -> str:
    """Format an Inner Ring alert message per SPEC §7.2.

    Args:
        trade: Trade dict from the database (all fields optional except ticker).

    Returns:
        HTML-formatted Telegram message string.
    """
    ticker = trade.get("ticker", "UNKNOWN")
    score = trade.get("confidence_score", 0)
    person_name = trade.get("person_name", "Unknown")
    person_title = trade.get("person_title")
    shares = trade.get("shares")
    price = trade.get("price_per_share")
    total_value = trade.get("total_value", 0)
    ownership_type = trade.get("ownership_type", "")
    transaction_date = trade.get("transaction_date", "Unknown")
    filing_url = trade.get("filing_url")

    # Build person line
    if person_title:
        person_line = f"{person_name} ({person_title})"
    else:
        person_line = person_name

    # Build shares/price line
    if shares is not None and price is not None:
        trade_line = f"Bought {shares:,.0f} shares @ ${price:.2f}"
    elif shares is not None:
        trade_line = f"Bought {shares:,.0f} shares"
    else:
        trade_line = "Bought (shares/price not reported)"

    # Ownership label
    if ownership_type == "D":
        ownership_label = "Direct"
    elif ownership_type == "I":
        ownership_label = "Indirect"
    else:
        ownership_label = ownership_type or "Unknown"

    # Filing link
    if filing_url:
        filing_line = f'📄 <a href="{filing_url}">View SEC Filing</a>'
    else:
        filing_line = "📄 Filing URL not available"

    return (
        f"🎯 <b>INNER RING — {ticker}</b>\n"
        f"Confidence: {score}/100\n"
        f"\n"
        f"{person_line}\n"
        f"{trade_line}\n"
        f"Total: ${total_value:,.0f}\n"
        f"Ownership: {ownership_label}\n"
        f"\n"
        f"📅 Traded: {transaction_date}\n"
        f"{filing_line}"
    )


def format_middle_ring_alert(cluster: dict) -> str:
    """Format a Middle Ring cluster alert message per SPEC §7.2.

    Stub — full implementation in Phase 4.

    Args:
        cluster: Cluster dict with sector name and constituent trades.

    Returns:
        HTML-formatted Telegram message string.
    """
    sector_name = cluster.get("sector_name", "Unknown Sector")
    return f"🔵 <b>SECTOR CLUSTER — {sector_name}</b>\n[Phase 4 implementation pending]"


def format_outer_ring_alert(trade: dict) -> str:
    """Format an Outer Ring anomaly alert per SPEC §7.2.

    Stub — full implementation in Phase 5.

    Args:
        trade: Trade dict with anomaly_reason field.

    Returns:
        HTML-formatted Telegram message string.
    """
    ticker = trade.get("ticker", "UNKNOWN")
    return f"⚡ <b>ANOMALY — {ticker}</b>\n[Phase 5 implementation pending]"


def format_anti_signal_alert(trades: list[dict]) -> str:
    """Format an anti-signal (sell cluster) alert per SPEC §7.2.

    Stub — full implementation in Phase 6.

    Args:
        trades: List of sell trade dicts forming the cluster.

    Returns:
        HTML-formatted Telegram message string.
    """
    ticker = trades[0].get("ticker", "UNKNOWN") if trades else "UNKNOWN"
    return f"🔴 <b>SELL CLUSTER — {ticker}</b>\n[Phase 6 implementation pending]"


def format_daily_digest(signals: dict) -> str:
    """Format the daily digest batch message per SPEC §7.2.

    Stub — full implementation in Phase 4 (digest batching).

    Args:
        signals: Dict with counts and lists by ring type.

    Returns:
        HTML-formatted Telegram message string.
    """
    date_str = signals.get("date", "Unknown")
    total = signals.get("total_signals", 0)
    return f"📊 <b>DAILY DIGEST — {date_str}</b>\n{total} signals detected today\n[Full digest: Phase 4]"
