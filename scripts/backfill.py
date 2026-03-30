"""Load 6 months of historical Form 4 data without sending alerts. Stub — Phase 8."""

# TODO Phase 8: edgartools historical pull, alert_sent=TRUE on all inserts

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.logger import get_logger

log = get_logger(__name__)


def main() -> None:
    log.info("Backfill started (stub — not yet implemented)")


if __name__ == "__main__":
    main()
