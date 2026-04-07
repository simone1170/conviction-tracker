# Conviction Tracker — Full Specification (v3)

## 1. Core Philosophy

Track the objective financial commitments of "smart money" — primarily corporate insiders (C-suite, directors, officers) via SEC Form 4 filings. If insiders are buying heavily during a dip, that is your entry signal. The system follows money, not media — it scores conviction by combining who is buying, how much, and whether the signal clusters across a sector.

Congressional STOCK Act disclosures serve as a **confirmation layer**, not a primary signal source. Due to structural reporting delays (up to 45 days), political trades are treated as retroactive macro validation and pattern research, never as tactical entry triggers.

---

## 2. The Bullseye Architecture

### 2.1 Inner Ring (Maximum Sensitivity)

**Purpose:** Immediate alerts on your highest-conviction tickers.

**Data source:** SEC Form 4 ONLY. Congressional data is excluded from Inner Ring entirely — stale data contradicts the purpose of immediate, high-conviction signals.

- Hardcoded watchlist stored in `data/watchlist.json`
- Config format:
  ```json
  {
    "ticker": "NVDA",
    "threshold_usd": 100000,
    "notes": "GPU monopoly",
    "active": true
  }
  ```
- **Trigger:** Any insider open-market purchase (Form 4, transaction code `P`, ownership type `D` preferred) above the per-ticker USD threshold
- **Exclusions:** 10b5-1 planned trades (see §5.4), congressional trades
- **Alert:** Immediate Telegram push — always instant, never batched
- **Confidence:** Starts at base 60, adds bonuses per §6. Can reach 100.

### 2.2 Middle Ring (Cluster Detection)

**Purpose:** Detect coordinated insider buying across a sector, signaling a macro or geopolitical shift.

**Data source:** SEC Form 4 for cluster detection. Congressional data can retroactively confirm an existing cluster (see §2.4) but cannot trigger one.

- Sectors defined via ETF constituent mappings in `data/sectors.json`
- Format:
  ```json
  {
    "sector": "Semiconductors",
    "etf": "SMH",
    "tickers": ["NVDA", "AMD", "INTC", "..."],
    "updated_at": "2026-03-15"
  }
  ```
- **Trigger:** 3+ distinct SEC insider purchases from different companies in the same sector within a 7-day rolling window
- **Critical:** Use `transaction_date` (when the trade happened), never `filing_date` (when it was reported to SEC), to avoid false clusters from weekend filing surges
- **Exclusions:** Same-company buys don't count toward the cluster threshold. 10b5-1 planned trades are excluded. Congressional trades do not count toward the 3-company threshold.
- **Alert:** Telegram push with sector name, company list, aggregate volume. Subject to daily digest batching during high-activity periods (see §7.3).

### 2.3 Outer Ring (Anomaly Hunting)

**Purpose:** Surface extreme statistical outliers in the rest of the market.

**Data source:** SEC Form 4 (primary) and congressional data (for political anomalies only).

- Covers all tickers not in Inner Ring watchlist or Middle Ring sector maps
- **SEC-sourced trigger conditions** (any one is sufficient):
  1. An insider purchase exceeds 5x the rolling 90-day average purchase size for that ticker (requires historical baseline — see §5.5)
- **Congress-sourced trigger conditions** (political anomalies — any one is sufficient):
  1. A congressional member purchases a stock where the upper range bound exceeds $100K in a micro/small-cap company
  2. A first-time purchase by a politician in a sector they have never previously traded
- **Exclusions:** 10b5-1 planned trades
- **Confidence cap:** Congressional-only Outer Ring signals are hard-capped at 50 (see §6). SEC-sourced Outer Ring signals follow standard scoring.
- **Alert:** Telegram push marked as "ANOMALY." Subject to daily digest batching.

### 2.4 Congressional Confirmation Layer (Enhancement Module)

**Purpose:** Retroactively validate existing signals when congressional trades align with prior corporate insider activity. Not a standalone signal source.

**Data source:** Congressional STOCK Act disclosures via third-party API.

**This entire module is OPTIONAL.** The system is fully functional without it. See §11 phasing — the core product ships after Phase 4 (Middle Ring). Congressional data is Phase 5.

**How it works:**
1. When a congressional trade is ingested, the system checks: does this ticker or sector already have an active signal (Inner or Middle Ring alert fired in the last 30 days)?
2. **If YES (confirmation match):** Boost the existing signal's confidence by +10 (capped at 100). Send a follow-up Telegram alert:
   ```
   🏛️ CONGRESSIONAL CONFIRMATION
   Senator/Rep {name} also bought {TICKER}
   ${amount_range} — traded {transaction_date} (reported {report_lag_days}d later)
   
   Originally flagged: {original_ring} Ring on {original_alert_date}
   Updated confidence: {new_score}/100
   📄 {filing_url}
   ```
3. **If NO (no prior signal):** The trade is logged silently to the database for pattern research. No alert is sent UNLESS it qualifies as an Outer Ring anomaly (§2.3 congress-sourced triggers).
4. **Never triggers Inner Ring.** Congressional trades cannot fire Inner Ring alerts under any circumstance.
5. **Never counts toward Middle Ring clusters.** Congressional trades do not contribute to the 3-company cluster threshold. They can only confirm an already-detected cluster.

**Long-term research value:**
- The `politician_history` table accumulates sector trading patterns over months
- The Streamlit dashboard exposes this as a research view: "which politicians trade which sectors"
- This is informational, not actionable — it builds context for interpreting future anomalies

---

## 3. Data Sources

### 3.1 SEC EDGAR (Form 4 — Corporate Insiders) — PRIMARY SOURCE

- **Library:** `edgartools` (`pip install edgartools`)
- **Setup:** Call `set_identity("your.email@example.com")` before any requests (SEC compliance requirement)
- **Rate limit:** 10 requests/second, handled natively by edgartools
- **Data freshness:** 1-3 days (Form 4 must be filed within 2 business days of transaction)
- **Key fields per transaction:**
  - `issuer_ticker` — company ticker
  - `reporting_person_name` — insider's full name
  - `reporting_person_title` — CEO, CFO, Director, VP, etc.
  - `transaction_code` — P (purchase), S (sale), A (grant), M (exercise), etc.
  - `ownership_type` — D (direct) or I (indirect)
  - `shares` — number of shares
  - `price_per_share` — exact price
  - `transaction_date` — date the trade actually occurred
  - `filing_date` — date the Form 4 was filed with SEC
  - `filing_url` — direct link to SEC filing
  - `footnotes` — raw footnote text (used for 10b5-1 detection)

- **Transaction code filter (STRICT):**
  - `P` (Open Market Purchase) → **BUY signal — process through Bullseye**
  - `S` (Open Market Sale) → **SELL signal — process for anti-signal layer only (§8.1)**
  - `A` (Grant/Award) → discard
  - `M` (Exercise of derivative) → discard
  - `F` (Tax payment via shares) → discard
  - `G` (Gift) → discard
  - All others → discard

- **Ownership type weighting:**
  - `D` (Direct) → full weight in confidence scoring
  - `I` (Indirect, e.g. trust, LLC, family member) → reduced weight (0.5x multiplier in confidence scoring)

### 3.2 Congressional Disclosures (STOCK Act) — CONFIRMATION SOURCE (OPTIONAL)

- **Source:** Third-party API — do NOT scrape Senate/House portals directly
- **Recommended providers (evaluate in order):**
  1. QuiverQuant API (most comprehensive, paid tier ~$30/month for granular data)
  2. CapitolTrades (community data, free tier available, less reliable update frequency)
  3. Finnhub congressional endpoint (premium tier)
  4. FMP Senate/House trading API (paid, structured JSON)
- **Data freshness:** 8-48 days (STOCK Act allows 45 days to report + API processing lag). Structurally inferior to SEC Form 4.
- **Cost:** $20-50/month for reliable data. The system works fully without it.
- **Key fields per transaction:**
  - `politician_name`
  - `chamber` — Senate or House
  - `ticker`
  - `transaction_type` — Purchase or Sale
  - `amount_range` — e.g. "$15,001–$50,000"
  - `transaction_date` — when the trade happened
  - `report_date` — when the disclosure was filed (up to 45 days later)
  - `filing_url`

- **Amount handling:**
  - Congressional amounts are reported as ranges, not exact values
  - Standard ranges: $1,001–$15,000 | $15,001–$50,000 | $50,001–$100,000 | $100,001–$250,000 | $250,001–$500,000 | $500,001–$1,000,000 | $1,000,001–$5,000,000 | $5,000,001–$25,000,000 | $25,000,001–$50,000,000 | Over $50,000,000
  - Store both `amount_lower` (numeric, lower bound) and `amount_range` (original string)
  - Use `amount_lower` for all threshold comparisons

- **Lag handling (CRITICAL):**
  - In Telegram alerts and the dashboard, always display both `transaction_date` and `report_date` with a clear label: "Trade executed X days ago"
  - The `report_lag_days` field (computed: `report_date - transaction_date`) must be displayed prominently
  - Congressional trades NEVER generate Inner Ring alerts
  - Congressional trades NEVER count toward Middle Ring cluster thresholds
  - Congressional trades are either confirmation matches (§2.4) or Outer Ring anomalies (§2.3) — nothing else

### 3.3 ETF Constituents (Sector Mapping)

- **Purpose:** Map sector ETFs to their constituent tickers for Middle Ring cluster detection
- **Source:** Financial API (FMP, Finnhub, or similar ETF holdings endpoint)
- **Key ETFs to track:**
  - `SMH` — Semiconductors
  - `XLE` — Energy
  - `XLF` — Financials
  - `ITA` — Defense/Aerospace
  - `XBI` — Biotech
  - `XLK` — Technology
  - `XLV` — Healthcare
  - `XLI` — Industrials
  - `XLP` — Consumer Staples
  - `XLU` — Utilities
  - (User-configurable — add/remove ETFs via `data/sectors.json`)
- **Update frequency:** Monthly via `scripts/update_etf_constituents.py` (not quarterly — catches mid-quarter changes from M&A, delistings, IPO additions)
- **Staleness warning:** If any sector mapping is older than 45 days, the Streamlit dashboard displays a yellow warning banner

---

## 4. Database Schema (SQLite + WAL)

### 4.1 Database initialization

On first connection and at start of every process (daemon and dashboard):
```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

### 4.2 Tables

#### trades
The central table. Every ingested transaction lands here.
```sql
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK(source IN ('sec_form4', 'congress_senate', 'congress_house')),
    ticker TEXT NOT NULL,
    company_name TEXT,
    person_name TEXT NOT NULL,
    person_title TEXT,
    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('purchase', 'sale')),
    transaction_code TEXT,               -- P, S, etc. (SEC only, NULL for congress)
    ownership_type TEXT,                 -- D or I (SEC only, NULL for congress)
    shares REAL,                         -- exact (SEC), NULL for congress
    price_per_share REAL,                -- exact (SEC), NULL for congress
    total_value REAL NOT NULL,           -- exact (SEC), lower bound (congress)
    amount_range TEXT,                   -- original range string (congress only)
    transaction_date TEXT NOT NULL,      -- ISO 8601, when trade happened
    filing_date TEXT NOT NULL,           -- ISO 8601, when filed/reported
    report_lag_days INTEGER,             -- computed: filing_date - transaction_date
    filing_url TEXT,
    is_planned_trade BOOLEAN DEFAULT FALSE,  -- 10b5-1 flag (SEC only)
    ring TEXT CHECK(ring IN ('inner', 'middle', 'outer', 'confirmation')),
    confirmation_of INTEGER REFERENCES trades(id),  -- links congress trade to the SEC trade it confirms
    confidence_score INTEGER,            -- 0-100, computed by scoring engine
    alert_sent BOOLEAN DEFAULT FALSE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, ticker, person_name, transaction_date, total_value, transaction_type)
        ON CONFLICT REPLACE
);

CREATE INDEX idx_trades_ticker ON trades(ticker);
CREATE INDEX idx_trades_transaction_date ON trades(transaction_date);
CREATE INDEX idx_trades_source ON trades(source);
CREATE INDEX idx_trades_ring ON trades(ring);
CREATE INDEX idx_trades_alert ON trades(alert_sent);
```

#### watchlist (Inner Ring configuration)
```sql
CREATE TABLE watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT UNIQUE NOT NULL,
    threshold_usd REAL NOT NULL,
    notes TEXT,
    active BOOLEAN DEFAULT TRUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

#### sectors (Middle Ring configuration)
```sql
CREATE TABLE sectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector_name TEXT NOT NULL,
    etf_ticker TEXT NOT NULL,
    constituent_ticker TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(etf_ticker, constituent_ticker)
);
```

#### alerts_log
```sql
CREATE TABLE alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES trades(id),
    ring TEXT NOT NULL,
    alert_type TEXT NOT NULL CHECK(alert_type IN ('single', 'cluster', 'anomaly', 'confirmation', 'anti_signal', 'digest')),
    message TEXT NOT NULL,
    confidence_score INTEGER,
    sent_at TEXT,
    telegram_message_id TEXT,
    delivery_status TEXT DEFAULT 'pending' CHECK(delivery_status IN ('pending', 'sent', 'failed')),
    retry_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

#### failed_alerts
```sql
CREATE TABLE failed_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_log_id INTEGER REFERENCES alerts_log(id),
    error_message TEXT,
    last_retry_at TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

#### system_health
```sql
CREATE TABLE system_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    last_successful_run TEXT NOT NULL,
    records_processed INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

#### politician_history (Outer Ring + research context)
```sql
CREATE TABLE politician_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    politician_name TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    first_trade_date TEXT NOT NULL,
    trade_count INTEGER DEFAULT 1,
    UNIQUE(politician_name, sector_name) ON CONFLICT IGNORE
);
```

---

## 5. Data Ingestion Pipeline

### 5.1 SEC Form 4 ingestion (`src/scrapers/edgar_scraper.py`)

1. Call edgartools to fetch recent Form 4 filings since last successful run
2. For each filing, extract all non-derivative transactions
3. Filter: keep only `transaction_code IN ('P', 'S')`
4. For purchases (`P`): check footnotes for "10b5-1" / "trading plan" → set `is_planned_trade = TRUE`
5. Compute `total_value = shares * price_per_share`
6. Compute `report_lag_days = filing_date - transaction_date`
7. Insert into `trades` table (UPSERT via unique constraint handles amendments/duplicates)
8. Log run to `system_health`

### 5.2 Congressional ingestion (`src/scrapers/congress_scraper.py`) — OPTIONAL MODULE

This entire scraper is optional. The system runs fully on SEC data alone.

1. Call the configured congressional API for trades since last successful run
2. Parse `amount_range` string → extract `amount_lower` as numeric
3. Compute `report_lag_days`
4. Insert into `trades` table with `ring = NULL` initially (ring assignment happens in §5.6)
5. Update `politician_history` table with new sector entries
6. Log run to `system_health`

### 5.3 ETF constituent update (`src/scrapers/etf_mapper.py`)

1. For each ETF in config, fetch current holdings from financial API
2. Truncate old entries for that ETF in `sectors` table
3. Insert fresh constituent list with `updated_at = now`
4. Log run to `system_health`

### 5.4 10b5-1 planned trade detection

Form 4 footnotes often contain language such as:
- "pursuant to a Rule 10b5-1 trading plan"
- "10b5-1 plan adopted on [date]"
- "pre-arranged trading plan"

**Implementation:**
```python
PLANNED_TRADE_PATTERNS = [
    r"10b5-1",
    r"10b5[-\s]1",
    r"rule\s*10b",
    r"trading\s+plan",
    r"pre-arranged\s+plan",
]
```
Run a case-insensitive regex search across all footnote text for the filing. If any pattern matches, set `is_planned_trade = TRUE`. These trades are stored for context but are excluded from all Bullseye trigger logic and receive a confidence score of 0.

### 5.5 Cold start and backfill (`scripts/backfill.py`)

The Outer Ring requires historical baselines (90-day rolling averages) to detect anomalies. Without backfill, anomaly detection is blind for 3 months.

**Backfill procedure:**
1. Use edgartools to pull 6 months of historical Form 4 filings
2. Process through the same ingestion pipeline (transaction code filter, 10b5-1 detection, deduplication)
3. Do NOT send alerts for historical trades — set `alert_sent = TRUE` on insert
4. For congressional data (if module is enabled), pull maximum available history from the API provider
5. Populate `politician_history` from backfilled congressional trades
6. Log backfill completion to `system_health`

**When to run:** Once before going live, and optionally again if the database is ever reset.

### 5.6 Congressional confirmation matching (`src/engine/confirmation.py`) — OPTIONAL MODULE

Runs after each congressional ingestion cycle. For each newly ingested congressional purchase:

1. **Check for ticker match:** Query `trades` table for SEC-sourced signals on the same ticker where an alert was sent in the last 30 days.
2. **Check for sector match:** If no ticker match, check if the congressional trade's ticker belongs to a sector where a Middle Ring cluster was detected in the last 30 days.
3. **If match found:**
   - Set the congressional trade's `ring = 'confirmation'`
   - Set `confirmation_of = {matched_trade_id}` (or the first trade in the matched cluster)
   - Boost the matched SEC trade's `confidence_score` by +10 (capped at 100)
   - Send a confirmation alert (§7.2)
4. **If no match found:**
   - Check Outer Ring anomaly rules (§2.3 congress-sourced triggers)
   - If anomaly: set `ring = 'outer'`, score at max 50, send anomaly alert
   - If not anomaly: set `ring = NULL`, `alert_sent = FALSE`. Logged silently for research.

---

## 6. Confidence Scoring Engine (`src/engine/scoring.py`)

Every trade that passes through the Bullseye filter receives a confidence score from 0 to 100.

### 6.1 Base scores by ring and source
- Inner Ring (SEC only): 60
- Middle Ring (SEC only): 40 (per individual trade in the cluster)
- Outer Ring (SEC-sourced): 50
- Outer Ring (Congress-sourced): 35
- Confirmation (Congress confirming SEC signal): no base — adds +10 to the existing signal

### 6.2 Bonus modifiers (additive, capped at 100)

| Factor | Condition | Bonus | Applies to |
|---|---|---|---|
| Title weight | CEO or CFO | +15 | SEC only |
| Title weight | President, COO, CTO | +10 | SEC only |
| Title weight | Director (board member) | +8 | SEC only |
| Title weight | VP or other officer | +3 | SEC only |
| Ownership type | Direct (D) | +0 (baseline) | SEC only |
| Ownership type | Indirect (I) | -15 | SEC only |
| Purchase size | Above 2x the ticker's 90-day avg | +10 | SEC only |
| Purchase size | Above 5x the ticker's 90-day avg | +20 (replaces +10) | SEC only |
| Cluster strength | 4+ companies in Middle Ring cluster | +10 | Middle Ring |
| Cluster strength | 5+ companies in Middle Ring cluster | +20 (replaces +10) | Middle Ring |
| Repeat buyer | Same person bought this ticker in last 180 days | +5 | SEC only |
| Congressional size | Upper range bound >= $250K | +10 | Congress Outer Ring |
| Congressional lag | Report lag <= 15 days (unusually fast) | +5 | Congress only |

### 6.3 Penalty modifiers (subtractive)

| Factor | Condition | Penalty | Applies to |
|---|---|---|---|
| Planned trade | 10b5-1 flagged | set score to 0, exclude from alerts | SEC only |
| Indirect ownership | Via trust or LLC | -15 (already in bonus table) | SEC only |
| Low absolute value | SEC purchase < $10,000 | -20 | SEC only |

### 6.4 Hard caps

| Source | Maximum possible confidence | Rationale |
|---|---|---|
| SEC Inner Ring | 100 | Primary signal, freshest data |
| SEC Middle Ring | 100 | Primary signal, freshest data |
| SEC Outer Ring | 100 | Primary signal, freshest data |
| Congress Outer Ring | **50** | Stale data, imprecise amounts |
| Congress Confirmation | N/A (adds +10 to existing) | Enhancement, not standalone |

### 6.5 Display
- Telegram alerts include the confidence score: e.g. "Confidence: 78/100"
- Streamlit dashboard color-codes by confidence: green (>=70), amber (40-69), gray (<40)
- Congressional-sourced signals always display `report_lag_days` prominently

---

## 7. Alert System

### 7.1 Telegram bot setup (`src/alerts/telegram_bot.py`)

- Library: `python-telegram-bot`
- Token stored in `.env` as `TELEGRAM_BOT_TOKEN`
- Chat ID stored in `.env` as `TELEGRAM_CHAT_ID`
- All `send_message()` calls wrapped in retry decorator: 3 attempts, exponential backoff (1s, 2s, 4s)
- On successful delivery: log `telegram_message_id` and set `delivery_status = 'sent'`
- On all retries failed: write to `failed_alerts` table, set `delivery_status = 'failed'`

### 7.2 Alert message formats (`src/alerts/formatters.py`)

#### Inner Ring alert (SEC only, always immediate)
```
🎯 INNER RING — {TICKER}
Confidence: {score}/100

{person_name} ({person_title})
Bought {shares:,.0f} shares @ ${price:.2f}
Total: ${total_value:,.0f}
Ownership: {Direct|Indirect}

📅 Traded: {transaction_date}
📄 {filing_url}
```

#### Middle Ring alert (SEC only)
```
🔵 SECTOR CLUSTER — {sector_name}
Confidence: {avg_score}/100

{count} insider buys across {count} companies (7-day window):
• {ticker1}: {person1} ({title1}) — ${value1:,.0f}
• {ticker2}: {person2} ({title2}) — ${value2:,.0f}
• {ticker3}: {person3} ({title3}) — ${value3:,.0f}

Aggregate: ${total:,.0f}
Window: {start_date} → {end_date}
```

#### Outer Ring alert (SEC-sourced)
```
⚡ ANOMALY — {TICKER}
Confidence: {score}/100

{person_name} ({person_title})
Bought {shares:,.0f} shares @ ${price:.2f}
Total: ${total_value:,.0f}
Reason: {anomaly_reason}

📅 Traded: {transaction_date}
📄 {filing_url}
```

#### Outer Ring alert (Congress-sourced)
```
⚡ POLITICAL ANOMALY — {TICKER}
Confidence: {score}/100 (capped — congressional data)

{politician_name} ({chamber})
${amount_range}
Reason: {anomaly_reason}

📅 Traded: {transaction_date} (reported {report_lag_days}d later)
⚠️ Stale data — macro context only, not a tactical entry signal
📄 {filing_url}
```

#### Congressional confirmation alert
```
🏛️ CONGRESSIONAL CONFIRMATION
{politician_name} ({chamber}) also bought {TICKER}
${amount_range} — traded {transaction_date} (reported {report_lag_days}d later)

Originally flagged: {original_ring} Ring on {original_alert_date}
Updated confidence: {old_score} → {new_score}/100
📄 {filing_url}
```

#### Anti-signal alert (see §8.1)
```
🔴 SELL CLUSTER — {TICKER}
{count} insiders selling in {days}-day window:
• {person1} ({title1}) — ${value1:,.0f}
• {person2} ({title2}) — ${value2:,.0f}

⚠️ Review your position.
```

#### Daily digest (batched, see §7.3)
```
📊 DAILY DIGEST — {date}
{total_signals} signals detected today

Inner Ring: {inner_count}
  {list of ticker: person — $value, one per line}

Middle Ring: {middle_count} clusters
  {list of sector: company_count companies — $aggregate}

Outer Ring: {outer_count} anomalies
  {list of ticker: person — $value — reason}

Confirmations: {confirm_count}
  {list if any}

Anti-signals: {anti_count}
  {list if any}

Full details on dashboard → localhost:8501
```

### 7.3 Alert batching / digest mode

**Problem:** During earnings season or broad selloffs, insider buying spikes and your phone gets 15+ pings in a day.

**Rules:**
- Inner Ring alerts are ALWAYS sent immediately, regardless of volume.
- Middle Ring and Outer Ring alerts are sent immediately UNLESS more than 5 total non-Inner alerts have already been sent today.
- Confirmation and anti-signal alerts follow the same batching rules as Middle/Outer.
- Once the daily threshold is crossed, remaining alerts are queued.
- At a configurable time (default: 8:00 PM local), queued alerts are sent as a single daily digest message.
- The digest threshold (default: 5) is configurable in `.env` as `DAILY_ALERT_DIGEST_THRESHOLD`.

### 7.4 Heartbeat

The daemon sends a short status message to Telegram once daily at a fixed time (default: 7:00 AM):
```
💚 System alive — {date}
Last SEC scrape: {timestamp} ({records} new trades)
Last Congress scrape: {timestamp} ({records} new) [or "Module disabled"]
DB size: {size_mb} MB | Pending alerts: {count}
```

If the daemon itself fails to run, no heartbeat is sent — the absence of the message IS the alert.

---

## 8. Upgrade Layers

### 8.1 Anti-signal layer (Sell detection)

**Purpose:** Detect heavy insider selling in stocks you hold, as a defensive warning.

**Data source:** SEC Form 4 only (transaction code `S`). Congressional sells are logged but do not trigger anti-signal alerts (same staleness problem — a 40-day-old sell is not actionable).

**Implementation:**
- Sells (transaction code `S`) are ingested and stored in the `trades` table alongside purchases
- Sells do NOT trigger Bullseye buy signals
- A separate function in `src/engine/anti_signal.py` runs after each SEC ingestion cycle
- **Trigger:** 2+ distinct insiders selling the same ticker within a 14-day rolling window (using `transaction_date`)
- **Additional trigger:** Any single insider sell exceeding $500K in a ticker on your Inner Ring watchlist
- Alert type: `anti_signal` — uses the red format from §7.2
- These alerts are subject to the daily digest batching rules

### 8.2 Monitoring and health (`src/monitoring/health.py`)

**Dashboard health indicators (shown in Streamlit sidebar):**
- Green: component ran successfully within expected interval
- Yellow: component has not run in >24 hours
- Red: component has not run in >36 hours OR last run had errors
- Sector staleness: yellow warning if any ETF mapping is >45 days old
- Congressional module: shows "Disabled" gracefully if not configured

**Components tracked in `system_health` table:**
- `scraper_sec` — expected interval: every cron cycle
- `scraper_congress` — expected interval: every cron cycle (if enabled)
- `etf_updater` — expected interval: monthly
- `alerter` — expected interval: every cron cycle

**Failed alerts banner:**
- If `failed_alerts` table has any `resolved = FALSE` rows, the Streamlit dashboard shows a persistent red banner: "N alerts failed to deliver — review failed_alerts"

### 8.3 Logging (`src/utils/logger.py`)

- Use Python `logging` module with `RotatingFileHandler`
- Log file: `data/logs/conviction.log`
- Max file size: 10 MB, keep 3 rotated backups
- Log level: INFO for normal operations, DEBUG available via `.env` flag
- **What to log:**
  - Every API call (endpoint, status code, response time)
  - Every database write (table, row count)
  - Every alert sent (trade_id, ring, delivery status)
  - Every error with full traceback
  - Every skipped transaction (reason: wrong code, planned trade, duplicate, etc.)
  - Every congressional confirmation match (or non-match)
- Format: `%(asctime)s | %(levelname)s | %(module)s | %(message)s`

---

## 9. Streamlit Dashboard (`src/dashboard/`)

### 9.1 Entry point: `app.py`

- Runs on `localhost:8501`
- Sidebar: health indicators (§8.2), quick filters (date range, ring, ticker search)
- Zero scraping logic — reads only from SQLite
- Congressional features hidden/disabled gracefully if the module is not configured

### 9.2 Page: Overview (`pages/overview.py`)

- **Signal feed:** Chronological list of recent signals (last 30 days), color-coded by ring and confidence score
- **Source badge:** Each signal shows its data source (SEC / Senate / House) with visual distinction
- **Confirmation links:** Congressional confirmations display with a reference to the original SEC signal they confirmed
- **Summary cards:** Total signals this week (by ring), highest confidence signal, most active sector
- **Quick filters:** Ring type, confidence threshold slider, date range, ticker search, source filter

### 9.3 Page: Sectors (`pages/sectors.py`)

- **Sector heatmap:** Grid showing insider buying intensity by sector (Plotly heatmap) — SEC data only
- **Cluster timeline:** When clusters were detected, which companies, aggregate volumes
- **Congressional overlay (optional):** If module is enabled, show congressional trades in the same sectors as secondary markers on the timeline
- **Sector drill-down:** Click a sector → see all individual trades within it
- **Staleness indicator:** Sectors with old ETF mappings shown with warning icon

### 9.4 Page: Filings (`pages/filings.py`)

- **Full trade table:** Searchable, sortable table of all ingested trades
- **Columns:** Date, ticker, person, title, type, value, source, confidence, ring, 10b5-1 flag, report lag, filing link
- **Filters:** Source (SEC/Senate/House), transaction type (buy/sell), date range, min value, ring
- **Export:** Download filtered results as CSV
- **Filing links:** Every row has a clickable link to the original SEC/Congress filing for manual verification

### 9.5 Page: Health (`pages/health.py`)

- **System status:** Table of all components with last run time, records processed, error count
- **Failed alerts:** List of undelivered alerts with error messages and retry status
- **Database stats:** Total trades by source, DB file size, oldest/newest record
- **ETF freshness:** Table of all sector mappings with `updated_at` dates and staleness warnings

### 9.6 Page: Politicians (`pages/politicians.py`) — OPTIONAL, requires congressional module

- **Research-only view** — no actionable signals, informational context
- **Politician leaderboard:** Sorted by trade frequency or total volume
- **Sector heatmap per politician:** Which sectors does each politician trade?
- **First-time trades:** Highlight when a politician enters a new sector for the first time
- **Lag distribution:** Histogram of `report_lag_days` — how fast does each politician report?

---

## 10. Configuration

### 10.1 Environment variables (`.env`)

```env
# SEC EDGAR (required)
SEC_USER_AGENT=your.name your.email@example.com

# Congressional API (optional — leave blank to disable module)
CONGRESS_API_PROVIDER=                     # quiverquant | capitoltrades | finnhub | fmp
CONGRESS_API_KEY=

# ETF data (required for Middle Ring)
ETF_API_PROVIDER=fmp
ETF_API_KEY=your_api_key_here

# Telegram (required)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Scheduling
CRON_SCHEDULE=0 18 * * 1-5
HEARTBEAT_TIME=07:00

# Alert tuning
DAILY_ALERT_DIGEST_THRESHOLD=5
DIGEST_SEND_TIME=20:00

# Congressional confirmation (only if module enabled)
CONFIRMATION_LOOKBACK_DAYS=30              # how far back to check for matching SEC signals

# Logging
LOG_LEVEL=INFO

# Database
DB_PATH=data/conviction.db
```

### 10.2 Watchlist (`data/watchlist.json`)

```json
[
  {"ticker": "NVDA", "threshold_usd": 100000, "notes": "GPU/AI monopoly", "active": true},
  {"ticker": "AAPL", "threshold_usd": 200000, "notes": "Core holding", "active": true},
  {"ticker": "GOOGL", "threshold_usd": 150000, "notes": "Search + Cloud", "active": true}
]
```

### 10.3 Sector mappings (`data/sectors.json`)

```json
[
  {
    "sector": "Semiconductors",
    "etf": "SMH",
    "tickers": ["NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "MU"],
    "updated_at": "2026-03-15"
  },
  {
    "sector": "Defense",
    "etf": "ITA",
    "tickers": ["LMT", "RTX", "NOC", "GD", "BA", "LHX"],
    "updated_at": "2026-03-15"
  }
]
```

---

## 11. Development Phases

The system is fully functional after Phase 4. Congressional data is Phase 5 — an optional enhancement module that can be added later or never.

### Phase 1: Foundation (1-2 sessions)
- Create full directory structure, `pyproject.toml`, `requirements.txt`, `.gitignore`, `.env.example`
- Implement `src/config.py` (loads `.env`, defines constants, detects which optional modules are enabled)
- Implement `src/db/models.py` (full schema from §4)
- Implement `scripts/setup_db.py` (creates tables, sets WAL mode)
- Write tests: database creation, WAL mode verification, table schema validation

### Phase 2: SEC Ingestion + Inner Ring (2-3 sessions)
- Implement `src/scrapers/edgar_scraper.py` using edgartools
- Implement transaction code filtering and 10b5-1 detection
- Implement deduplication via UPSERT
- Implement `src/engine/inner_ring.py` (watchlist threshold check)
- Implement `src/engine/scoring.py` (confidence scoring — SEC portion)
- Write tests: mock Form 4 data → correct filtering → correct scoring

### Phase 3: Telegram Alerts (1 session)
- Implement `src/alerts/telegram_bot.py` with retry logic
- Implement `src/alerts/formatters.py` (SEC message templates)
- Implement alert logging and failed_alerts tracking
- End-to-end test: scrape → filter → score → alert → log

### Phase 4: Middle Ring (1-2 sessions) — **CORE PRODUCT COMPLETE AFTER THIS PHASE**
- Implement `src/scrapers/etf_mapper.py`
- Implement `src/engine/middle_ring.py` (cluster detection with rolling window on transaction_date)
- Implement cluster alert formatting
- Implement daily digest batching
- Write tests: mock multi-company buys → cluster detection → alert

### Phase 5: Congressional Module (2 sessions) — OPTIONAL
- Implement `src/scrapers/congress_scraper.py` (API integration)
- Implement amount range parsing
- Implement `src/engine/confirmation.py` (confirmation matching logic from §5.6)
- Implement `src/engine/outer_ring.py` (SEC anomalies + congress anomaly rules)
- Implement congressional alert formats (confirmation + political anomaly)
- Implement confidence hard cap (50 max for congress-only)
- Update config.py to gracefully disable if API keys are empty
- Write tests: mock congressional data → confirmation matching → anomaly detection

### Phase 6: Anti-Signal Layer (1 session)
- Implement `src/engine/anti_signal.py` (sell cluster detection — SEC only)
- Add sell ingestion path to edgar_scraper
- Wire anti-signal alerts through the alert system
- Write tests: mock sell clusters → alert firing

### Phase 7: Dashboard (2-3 sessions)
- Implement `src/dashboard/app.py` (Streamlit shell, sidebar, health indicators)
- Implement `pages/overview.py` (signal feed, summary cards, source badges)
- Implement `pages/sectors.py` (heatmap, cluster timeline)
- Implement `pages/filings.py` (searchable table, CSV export)
- Implement `pages/health.py` (system status, failed alerts)
- Implement `pages/politicians.py` (optional, only if congressional module enabled)
- Gracefully hide congressional UI elements if module is disabled

### Phase 8: Operations + Polish (1-2 sessions)
- Implement `scripts/backfill.py` (6-month historical load)
- Implement `src/monitoring/health.py` (heartbeat, staleness checks)
- Implement `src/utils/logger.py` (rotating file handler)
- Set up cron schedule
- Write `docs/deployment.md` (full setup instructions)
- Final integration test: full cycle from scrape to dashboard

---

## 12. .gitignore

```
# Secrets
.env

# Database
data/conviction.db
data/conviction.db-wal
data/conviction.db-shm

# Logs
data/logs/

# Python
__pycache__/
*.pyc
*.pyo
.venv/
venv/
*.egg-info/
dist/
build/

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db
```
