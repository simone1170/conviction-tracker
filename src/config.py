"""Load environment variables and define project-wide constants."""

from pathlib import Path

from dotenv import load_dotenv
import os

# Resolve project root regardless of where the process is launched from
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = Path(os.getenv("DB_PATH", "data/conviction.db"))
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_ROOT / DB_PATH

# ── SEC EDGAR ─────────────────────────────────────────────────────────────────
SEC_USER_AGENT: str = os.environ["SEC_USER_AGENT"]

# ── Congressional API ─────────────────────────────────────────────────────────
CONGRESS_API_PROVIDER: str = os.getenv("CONGRESS_API_PROVIDER", "quiverquant")
CONGRESS_API_KEY: str = os.getenv("CONGRESS_API_KEY", "")

# ── ETF data ──────────────────────────────────────────────────────────────────
ETF_API_PROVIDER: str = os.getenv("ETF_API_PROVIDER", "fmp")
ETF_API_KEY: str = os.getenv("ETF_API_KEY", "")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Scheduling ────────────────────────────────────────────────────────────────
CRON_SCHEDULE: str = os.getenv("CRON_SCHEDULE", "0 18 * * 1-5")
HEARTBEAT_TIME: str = os.getenv("HEARTBEAT_TIME", "07:00")

# ── Alert tuning ──────────────────────────────────────────────────────────────
DAILY_ALERT_DIGEST_THRESHOLD: int = int(
    os.getenv("DAILY_ALERT_DIGEST_THRESHOLD", "5")
)
DIGEST_SEND_TIME: str = os.getenv("DIGEST_SEND_TIME", "20:00")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: Path = PROJECT_ROOT / "data" / "logs" / "conviction.log"

# ── Data files ────────────────────────────────────────────────────────────────
WATCHLIST_PATH: Path = PROJECT_ROOT / "data" / "watchlist.json"
SECTORS_PATH: Path = PROJECT_ROOT / "data" / "sectors.json"

# ── Bullseye thresholds ───────────────────────────────────────────────────────
MIDDLE_RING_CLUSTER_MIN: int = 3          # distinct companies to trigger cluster
MIDDLE_RING_WINDOW_DAYS: int = 7          # rolling window for cluster detection
OUTER_RING_CONGRESS_MIN_USD: int = 100_000  # lower bound for congressional anomaly
OUTER_RING_SIZE_MULTIPLE: float = 5.0     # x rolling avg to trigger size anomaly
ANTI_SIGNAL_MIN_SELLERS: int = 2          # distinct sellers for sell-cluster alert
ANTI_SIGNAL_WINDOW_DAYS: int = 14
ANTI_SIGNAL_SINGLE_SELL_USD: int = 500_000  # single large sell on watchlist ticker
SECTOR_STALENESS_DAYS: int = 45           # warn if ETF mapping older than this

# ── Transaction code constants ────────────────────────────────────────────────
TX_BUY = "P"
TX_SELL = "S"
TX_DISCARD = frozenset({"A", "M", "F", "G"})
