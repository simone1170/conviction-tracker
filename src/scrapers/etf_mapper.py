"""ETF constituent mapper for Middle Ring sector detection.

Loads sector/ETF constituent data from the local JSON file into the SQLite
`sectors` table. An API-based live updater can be layered on top in a later
phase — this module handles the local-file seed path.

Usage:
    from src.scrapers.etf_mapper import seed_sectors_from_json, get_sector_for_ticker
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.utils.logger import get_logger

log = get_logger(__name__)

# Module-level cache: populated by get_all_sectors() on first call.
# Maps sector_name -> list[ticker].  Stays valid for the duration of one run.
_sector_cache: dict[str, list[str]] | None = None


def seed_sectors_from_json(conn: sqlite3.Connection, json_path: Path) -> int:
    """Load sector/ETF constituent data from a JSON file into the sectors table.

    Uses INSERT OR REPLACE so re-seeding is safe and idempotent.

    Args:
        conn: Open database connection with PRAGMAs applied.
        json_path: Path to sectors.json.

    Returns:
        Total number of constituent rows inserted/replaced.
    """
    global _sector_cache
    _sector_cache = None  # invalidate cache on re-seed

    try:
        sectors_data = json.loads(json_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.error("Failed to read sectors JSON from %s: %s", json_path, exc)
        return 0

    total = 0
    for entry in sectors_data:
        sector_name = entry.get("sector", "")
        etf_ticker = entry.get("etf", "")
        tickers = entry.get("tickers", [])
        updated_at = entry.get("updated_at", "")

        if not sector_name or not etf_ticker:
            log.warning("Skipping sector entry with missing sector/etf: %r", entry)
            continue

        for ticker in tickers:
            conn.execute(
                """
                INSERT OR REPLACE INTO sectors
                    (sector_name, etf_ticker, constituent_ticker, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (sector_name, etf_ticker, ticker.strip().upper(), updated_at),
            )
            total += 1

    conn.commit()
    log.info("Seeded %d sector constituent rows from %s", total, json_path)
    return total


def get_sector_for_ticker(conn: sqlite3.Connection, ticker: str) -> str | None:
    """Return the sector name for a given ticker, or None if not tracked.

    When a ticker belongs to multiple sectors (e.g. NVDA in both
    Semiconductors and Technology), returns the first match (lowest rowid).

    Args:
        conn: Open database connection.
        ticker: Company ticker symbol (case-insensitive).

    Returns:
        Sector name string, or None.
    """
    row = conn.execute(
        "SELECT sector_name FROM sectors WHERE constituent_ticker = ? LIMIT 1",
        (ticker.strip().upper(),),
    ).fetchone()
    return row["sector_name"] if row else None


def get_sector_tickers(conn: sqlite3.Connection, sector_name: str) -> list[str]:
    """Return all constituent tickers for a given sector.

    Args:
        conn: Open database connection.
        sector_name: Exact sector name as stored in the sectors table.

    Returns:
        Sorted list of ticker strings.
    """
    rows = conn.execute(
        "SELECT constituent_ticker FROM sectors WHERE sector_name = ? ORDER BY constituent_ticker",
        (sector_name,),
    ).fetchall()
    return [row["constituent_ticker"] for row in rows]


def get_all_sectors(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return a mapping of sector_name → list[ticker].

    Result is cached in memory for the duration of the process — sectors do
    not change between ingestion cycles.

    Args:
        conn: Open database connection.

    Returns:
        Dict mapping sector names to their constituent tickers.
    """
    global _sector_cache
    if _sector_cache is not None:
        return _sector_cache

    rows = conn.execute(
        "SELECT sector_name, constituent_ticker FROM sectors ORDER BY sector_name, constituent_ticker"
    ).fetchall()

    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row["sector_name"], []).append(row["constituent_ticker"])

    _sector_cache = result
    return result


def is_sectors_seeded(conn: sqlite3.Connection) -> bool:
    """Return True if the sectors table has at least one row.

    Used by run_ingestion.py to decide whether a first-time seed is needed.

    Args:
        conn: Open database connection.

    Returns:
        True if sectors data exists.
    """
    row = conn.execute("SELECT COUNT(*) AS cnt FROM sectors").fetchone()
    return (row["cnt"] if row else 0) > 0
