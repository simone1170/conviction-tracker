# Conviction Tracker — Insider & Congressional Trade Signal System

## What this is
A personal Python tool that monitors SEC Form 4 insider filings, filters them through a three-ring "Bullseye" sensitivity framework with confidence scoring, and delivers actionable alerts via Telegram + a Streamlit dashboard. Congressional STOCK Act data is an optional confirmation layer — not a primary signal source. Not a commercial product — no auth, no cloud infra, no user management.

## Stack
- Python 3.11+, SQLite (WAL mode), Streamlit, python-telegram-bot, Plotly
- `edgartools` for SEC EDGAR data (free, no API key, respects SEC 10 req/s limit)
- Congressional data via third-party API (optional, costs ~$30/month) — never scrape Congress portals directly
- Scheduling via cron (Linux/Mac) or APScheduler

## Project structure
- `src/scrapers/` — data ingestion from SEC, Congress API (optional), and ETF constituent sources
- `src/engine/` — Bullseye filter (inner_ring, middle_ring, outer_ring), confirmation matching (optional), scoring engine, anti-signal sell detection
- `src/db/` — SQLite schema, queries, seed data
- `src/alerts/` — Telegram bot with retry logic, message formatters, digest batching
- `src/dashboard/` — Streamlit app (overview, sectors, filings, health, politicians). Zero scraping logic — reads only from SQLite. Congressional pages hidden if module disabled.
- `src/monitoring/` — system health checks, heartbeat
- `src/utils/` — logging setup, shared helpers
- `data/` — watchlist.json, sectors.json, conviction.db (gitignored), logs/
- `scripts/` — cron entry points, DB setup, backfill, ETF updater
- `tests/` — pytest tests for each module

## Key architectural rules
- **SEC Form 4 is the primary signal source.** Congressional data is a confirmation layer only.
- **Congressional trades NEVER trigger Inner Ring alerts.** They NEVER count toward Middle Ring cluster thresholds. They can only: (1) confirm an existing SEC signal (+10 confidence boost), or (2) trigger an Outer Ring anomaly (hard-capped at 50 confidence).
- **Congressional module is fully optional.** Leave `CONGRESS_API_KEY` blank in .env to disable. All code must handle this gracefully — no crashes, hide UI elements.
- **Core product is complete after Phase 4 (Middle Ring).** Congressional data is Phase 5.

## Key data rules
- Filter Form 4 by transaction code: `P` for buy signals, `S` for anti-signal sell detection only. Discard A, M, F, G.
- Detect 10b5-1 planned trades via footnote regex. Flag `is_planned_trade=TRUE`, exclude from all triggers, score = 0.
- Always use `transaction_date` for rolling windows and cluster detection. Never use `filing_date`.
- Congressional amounts are ranges — store `amount_lower` (numeric) and `amount_range` (original string). Use lower bound for thresholds.
- Deduplication: UPSERT via composite unique key `(source, ticker, person_name, transaction_date, total_value, transaction_type)`.
- SQLite: always set `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on every connection.
- Daemon (scraper) and Viewer (Streamlit) are fully decoupled processes.

## Build commands
- `pip install -r requirements.txt` — install deps
- `python scripts/setup_db.py` — create SQLite tables with WAL mode
- `python scripts/backfill.py` — load 6 months of historical SEC data (run once before go-live)
- `python scripts/run_ingestion.py` — run one scrape+filter+alert cycle
- `python scripts/update_etf_constituents.py` — refresh sector mappings
- `streamlit run src/dashboard/app.py` — launch dashboard
- `pytest tests/` — run tests

## Code style
- Type hints on all function signatures
- Docstrings on public functions (Google style)
- Use pathlib for file paths, not os.path
- Prefer dataclasses or Pydantic models over raw dicts
- SQL queries go in src/db/queries.py, not scattered in business logic
- All API calls and DB writes must be logged via src/utils/logger.py
- Wrap Telegram sends in retry decorator (3 attempts, exponential backoff)
- Check `config.congress_enabled` before any congressional code path

## Architecture notes
- For full specification, see @SPEC.md (§1-12 cover everything)
- For data source quirks and rate limits, see @docs/data-sources.md
- For database schema, see SPEC.md §4 or @docs/schema.md
- Development is phased (§11): Foundation → SEC+Inner → Alerts → Middle Ring (CORE DONE) → Congress (optional) → Anti-Signal → Dashboard → Polish
