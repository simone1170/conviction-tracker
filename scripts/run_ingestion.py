"""Run one scrape + filter + alert cycle. Stub — implementation in Phase 2/3."""

# TODO Phase 2: wire edgar_scraper → inner/middle/outer engines → alerts

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.logger import get_logger

log = get_logger(__name__)


def main() -> None:
    log.info("Ingestion cycle started (stub — no scrapers implemented yet)")


if __name__ == "__main__":
    main()
