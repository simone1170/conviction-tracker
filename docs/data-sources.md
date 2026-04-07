# Data Sources

Reference for data source quirks, rate limits, and integration notes.
This file will be filled in as each scraper is implemented.

---

## SEC EDGAR (Form 4 — Primary Signal Source)

- **Library:** `edgartools`
- **Rate limit:** 10 requests/second (handled natively by edgartools)
- **Setup:** Call `set_identity("name email@example.com")` before any requests
- **Data freshness:** 1-3 days (Form 4 must be filed within 2 business days of transaction)
- **Key quirks:** TBD in Phase 2

---

## Congressional Disclosures (STOCK Act — Optional Confirmation Layer)

- **Source:** Third-party API (do NOT scrape Senate/House portals directly)
- **Data freshness:** 8-48 days (45-day reporting window + API lag)
- **Cost:** ~$20-50/month for reliable data
- **Key quirks:** TBD in Phase 5

---

## ETF Constituents (Sector Mapping)

- **Purpose:** Map sector ETFs to constituent tickers for Middle Ring cluster detection
- **Update frequency:** Monthly via `scripts/update_etf_constituents.py`
- **Staleness threshold:** 45 days (dashboard shows warning banner if exceeded)
- **Key quirks:** TBD in Phase 4
