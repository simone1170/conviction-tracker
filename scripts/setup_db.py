"""Create the SQLite database, apply WAL mode, and seed watchlist/sectors from JSON files.

Run once before first use:
    python scripts/setup_db.py
"""

import json
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import DB_PATH, WATCHLIST_PATH, SECTORS_PATH
from src.db.models import get_connection, create_tables
from src.utils.logger import get_logger

log = get_logger(__name__)


def seed_watchlist(conn) -> int:
    """Load watchlist.json into the watchlist table.

    Args:
        conn: Open database connection.

    Returns:
        Number of rows upserted.
    """
    entries = json.loads(WATCHLIST_PATH.read_text())
    from src.db import queries
    rows = 0
    for entry in entries:
        conn.execute(queries.UPSERT_WATCHLIST, {
            "ticker": entry["ticker"],
            "threshold_usd": entry["threshold_usd"],
            "notes": entry.get("notes"),
            "active": entry.get("active", True),
        })
        rows += 1
    conn.commit()
    return rows


def seed_sectors(conn) -> int:
    """Load sectors.json into the sectors table.

    Args:
        conn: Open database connection.

    Returns:
        Number of rows inserted.
    """
    sectors = json.loads(SECTORS_PATH.read_text())
    from src.db import queries
    rows = 0
    for sector in sectors:
        conn.execute(queries.DELETE_SECTOR_ETF, {"etf_ticker": sector["etf"]})
        for ticker in sector["tickers"]:
            conn.execute(queries.INSERT_SECTOR_CONSTITUENT, {
                "sector_name": sector["sector"],
                "etf_ticker": sector["etf"],
                "constituent_ticker": ticker,
                "updated_at": sector["updated_at"],
            })
            rows += 1
    conn.commit()
    return rows


def main() -> None:
    log.info("Setting up database at %s", DB_PATH)
    conn = get_connection(DB_PATH)
    create_tables(conn)
    log.info("Tables created (WAL mode active)")

    wl_rows = seed_watchlist(conn)
    log.info("Watchlist seeded: %d tickers", wl_rows)

    sec_rows = seed_sectors(conn)
    log.info("Sectors seeded: %d constituent rows", sec_rows)

    conn.close()
    log.info("Database setup complete: %s", DB_PATH)


if __name__ == "__main__":
    main()
