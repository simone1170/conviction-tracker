"""Refresh sector/ETF constituent mappings from the financial data API. Stub — Phase 4."""

# TODO Phase 4: call ETF API, truncate old rows, insert fresh constituents

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.logger import get_logger

log = get_logger(__name__)


def main() -> None:
    log.info("ETF constituent update started (stub — not yet implemented)")


if __name__ == "__main__":
    main()
