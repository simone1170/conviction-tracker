# Conviction Tracker — Insider & Congressional Trade Signal System

## What this is
A personal Python tool that monitors SEC Form 4 insider filings and congressional STOCK Act disclosures, filters them through a three-ring "Bullseye" sensitivity framework with confidence scoring, and delivers actionable alerts via Telegram + a Streamlit dashboard. Not a commercial product — no auth, no cloud infra, no user management.

## Stack
- Python 3.11+, SQLite (WAL mode), Streamlit, python-telegram-bot, Plotly
- `edgartools` for SEC EDGAR data (free, no API key, respects SEC 10 req/s limit)
- Congressional data via third-party API (QuiverQuant/FMP/Finnhub) — never scrape Congress portals directly
- Scheduling via cron (Linux/Mac) or APScheduler

## Project structure
- `src/scrapers/` — data ingestion from SEC, Congress API, and ETF constituent sources
- `src/engine/` — Bullseye filter (inner_ring, middle_ring, outer_ring), scoring engine, anti-signal sell detection
- `src/db/` — SQLite schema, queries, seed data
- `src/alerts/` — Telegram bot with retry logic, message formatters, digest batching
- `src/dashboard/` — Streamlit app (overview, sectors, filings, health pages). Zero scraping logic — reads only from SQLite.
- `src/monitoring/` — system health checks, heartbeat
- `src/utils/` — logging setup, shared helpers
- `data/` — watchlist.json, sectors.json, conviction.db (gitignored), logs/
- `scripts/` — cron entry points, DB setup, backfill, ETF updater
- `tests/` — pytest tests for each module

## Key rules
- Filter Form 4 by transaction code: `P` for buy signals, `S` for anti-signal sell detection only. Discard A, M, F, G, and all others.
- Detect 10b5-1 planned trades via footnote regex. Flag them `is_planned_trade=TRUE` and exclude from all Bullseye triggers.
- Always use `transaction_date` (when trade happened) for rolling windows and cluster detection. Never use `filing_date` (when reported to SEC).
- Congressional data is lagged (up to 45 days). Treat as macro confirmation, not tactical trigger. Always display `report_lag_days`.
- Deduplication: UPSERT via composite unique key `(source, ticker, person_name, transaction_date, total_value, transaction_type)`. Handles Form 4/A amendments.
- SQLite: always set `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on every connection.
- Daemon (scraper) and Viewer (Streamlit) are fully decoupled processes. Dashboard has zero internet-facing logic.
- Never commit .env, conviction.db, or any API keys.

## Build commands
- `pip install -r requirements.txt` — install deps
- `python scripts/setup_db.py` — create SQLite tables with WAL mode
- `python scripts/backfill.py` — load 6 months of historical data (run once before go-live)
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

## Architecture notes
- For full specification, see @SPEC.md (§1-12 cover everything)
- For data source quirks and rate limits, see @docs/data-sources.md
- For database schema, see SPEC.md §4 or @docs/schema.md
- Development is phased (§11): Foundation → SEC+Inner → Alerts → Middle → Congress+Outer → Anti-Signal → Dashboard → Polish
