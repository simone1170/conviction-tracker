"""Project-wide configuration loaded from .env.

Usage:
    from src.config import settings          # Pydantic Settings object
    from src.config import DB_PATH, LOG_FILE # flat module-level aliases (legacy)

The module-level aliases are kept so existing imports don't break.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root is two levels up from this file (src/config.py → src/ → project/)
_PROJECT_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    """Typed configuration for the Conviction Tracker.

    All values are read from the .env file at project root (or from the
    environment).  Required fields raise a clear ValidationError on startup
    if they are absent or empty.
    """

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── SEC EDGAR ─────────────────────────────────────────────────────────────
    sec_user_agent: str

    # ── Congressional API (optional) ─────────────────────────────────────────
    congress_api_provider: str = ""
    congress_api_key: str = ""

    # ── ETF data ─────────────────────────────────────────────────────────────
    etf_api_provider: str = "fmp"
    etf_api_key: str = ""

    # ── Telegram (optional — leave blank to disable alerting) ────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Scheduling ───────────────────────────────────────────────────────────
    cron_schedule: str = "0 18 * * 1-5"
    heartbeat_time: str = "07:00"

    # ── Alert tuning ─────────────────────────────────────────────────────────
    daily_alert_digest_threshold: int = 5
    digest_send_time: str = "20:00"
    confirmation_lookback_days: int = 30

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ── Database ─────────────────────────────────────────────────────────────
    db_path: str = "data/conviction.db"

    # ── Derived / computed ────────────────────────────────────────────────────

    @field_validator("sec_user_agent", mode="before")
    @classmethod
    def must_not_be_empty(cls, v: str, info) -> str:  # noqa: ANN001
        """Raise a clear error for required fields that are blank."""
        if not v or not v.strip():
            raise ValueError(
                f"{info.field_name} is required but was empty. "
                "Check your .env file."
            )
        return v

    @property
    def telegram_enabled(self) -> bool:
        """True only when both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def congress_enabled(self) -> bool:
        """True only when both CONGRESS_API_PROVIDER and CONGRESS_API_KEY are set."""
        return bool(self.congress_api_provider and self.congress_api_key)

    @property
    def db_path_resolved(self) -> Path:
        """Absolute Path to the SQLite database file."""
        p = Path(self.db_path)
        return p if p.is_absolute() else _PROJECT_ROOT / p

    @property
    def log_file(self) -> Path:
        """Absolute Path to the rotating log file."""
        return _PROJECT_ROOT / "data" / "logs" / "conviction.log"

    @property
    def watchlist_path(self) -> Path:
        """Absolute Path to watchlist.json."""
        return _PROJECT_ROOT / "data" / "watchlist.json"

    @property
    def sectors_path(self) -> Path:
        """Absolute Path to sectors.json."""
        return _PROJECT_ROOT / "data" / "sectors.json"

    @property
    def project_root(self) -> Path:
        """Absolute Path to the project root directory."""
        return _PROJECT_ROOT


# ── Singleton ─────────────────────────────────────────────────────────────────

settings = Settings()

# ── Module-level aliases (for backward-compatible imports) ────────────────────

DB_PATH: Path = settings.db_path_resolved
LOG_FILE: Path = settings.log_file
LOG_LEVEL: str = settings.log_level
WATCHLIST_PATH: Path = settings.watchlist_path
SECTORS_PATH: Path = settings.sectors_path

# ── Bullseye thresholds (constants, not env-controlled) ───────────────────────

MIDDLE_RING_CLUSTER_MIN: int = 3
MIDDLE_RING_WINDOW_DAYS: int = 7
OUTER_RING_CONGRESS_MIN_USD: int = 100_000
OUTER_RING_SIZE_MULTIPLE: float = 5.0
ANTI_SIGNAL_MIN_SELLERS: int = 2
ANTI_SIGNAL_WINDOW_DAYS: int = 14
ANTI_SIGNAL_SINGLE_SELL_USD: int = 500_000
SECTOR_STALENESS_DAYS: int = 45

# ── Transaction code constants ────────────────────────────────────────────────

TX_BUY = "P"
TX_SELL = "S"
TX_DISCARD = frozenset({"A", "M", "F", "G"})
