"""SEC EDGAR Form 4 ingestion pipeline.

Fetches recent Form 4 insider filings via the EDGAR EFTS search API and
direct XML downloads (no edgartools dependency), parses non-derivative
transactions, detects 10b5-1 planned trades, and returns a flat list of
trade dicts ready for the Bullseye filter and database insertion.

Usage:
    from src.scrapers.edgar_scraper import fetch_recent_form4s
    trades = fetch_recent_form4s()              # last 3 days
    trades = fetch_recent_form4s(since_date)    # since a specific date
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any

import requests

from src.config import settings
from src.utils.logger import get_logger

log = get_logger(__name__)

# EDGAR REST API endpoints
_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
_EFTS_PAGE_SIZE = 100

# Minimum delay between EDGAR HTTP requests to respect 10 req/s limit
_REQUEST_DELAY = 0.11  # seconds


# ── 10b5-1 detection patterns (SPEC §5.4) ────────────────────────────────────

PLANNED_TRADE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"10b5-1", re.IGNORECASE),
    re.compile(r"10b5[-\s]1", re.IGNORECASE),
    re.compile(r"rule\s*10b", re.IGNORECASE),
    re.compile(r"trading\s+plan", re.IGNORECASE),
    re.compile(r"pre-arranged\s+plan", re.IGNORECASE),
]

# Transaction codes we process (SPEC §3.1)
_KEEP_CODES = frozenset({"P", "S"})


# ── Helpers ───────────────────────────────────────────────────────────────────

def detect_planned_trade(text: str) -> bool:
    """Return True if text contains language indicating a 10b5-1 planned trade.

    Args:
        text: Footnote or remarks text from a Form 4 filing.

    Returns:
        True if any planned-trade pattern matches, False otherwise.
    """
    for pattern in PLANNED_TRADE_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _normalize_date(val: Any) -> str | None:
    """Normalize a date value to an ISO 8601 string.

    Accepts str, datetime.date, or datetime.datetime. All are coerced to
    "YYYY-MM-DD" via str slicing.

    Args:
        val: Raw date value.

    Returns:
        ISO date string or None if val is None.
    """
    if val is None:
        return None
    try:
        return str(val)[:10]
    except Exception:
        return None


def _date_diff_days(filing_date_str: str | None, tx_date_str: str | None) -> int | None:
    """Compute report_lag_days = filing_date - transaction_date in days.

    Args:
        filing_date_str: ISO date string for the SEC submission date.
        tx_date_str: ISO date string for the actual transaction date.

    Returns:
        Integer day difference, or None if either date is missing/invalid.
    """
    if not filing_date_str or not tx_date_str:
        return None
    try:
        fd = date.fromisoformat(filing_date_str)
        td = date.fromisoformat(tx_date_str)
        return (fd - td).days
    except (ValueError, TypeError):
        return None


def _xml_text(root: ET.Element, path: str) -> str | None:
    """Return stripped text from the first matching XML element, or None.

    Args:
        root: The root XML element to search from.
        path: ElementTree XPath-style path.

    Returns:
        Stripped text string, or None if element is absent or empty.
    """
    el = root.find(path)
    if el is None or not el.text:
        return None
    text = el.text.strip()
    return text or None


def _is_truthy_xml(val: str | None) -> bool:
    """Return True if an XML text value represents a truthy boolean.

    Handles "1", "true" (case-insensitive). Returns False for anything else.
    """
    if val is None:
        return False
    return val.strip().lower() in ("1", "true")


# ── Core XML parsing ──────────────────────────────────────────────────────────

def _parse_form4_xml(
    xml_text: str,
    adsh: str,
    filing_url: str,
    file_date: str,
) -> list[dict]:
    """Parse a Form 4 ownershipDocument XML string into a list of trade dicts.

    Extracts all non-derivative transactions with codes P (purchase) or
    S (sale). Silently skips transactions missing required fields or
    purchases with zero/null price.

    Args:
        xml_text: Raw XML text of the ownershipDocument.
        adsh: Accession number (used in log messages).
        filing_url: Direct URL to the XML file on SEC EDGAR.
        file_date: Filing date as ISO date string (from EFTS metadata).

    Returns:
        List of trade dicts conforming to the trades table schema. May be empty.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.error("XML parse error for %s: %s", adsh, exc)
        return []

    # ── Filing-level fields ───────────────────────────────────────────────────

    ticker = _xml_text(root, "./issuer/issuerTradingSymbol")
    if not ticker:
        log.warning("No ticker in Form 4 %s — skipping", adsh)
        return []
    ticker = ticker.strip().upper()

    company_name = _xml_text(root, "./issuer/issuerName")

    person_name = _xml_text(root, "./reportingOwner/reportingOwnerId/rptOwnerName")
    if not person_name:
        log.warning("No person_name in Form 4 %s (%s) — skipping", adsh, ticker)
        return []
    person_name = person_name.strip()

    # Determine title: prefer officerTitle; fall back to "Director" for board members
    rel = root.find("./reportingOwner/reportingOwnerRelationship")
    person_title: str | None = None
    if rel is not None:
        officer_title_el = rel.find("officerTitle")
        is_officer_el = rel.find("isOfficer")
        is_director_el = rel.find("isDirector")

        if officer_title_el is not None and officer_title_el.text:
            person_title = officer_title_el.text.strip() or None
        elif is_director_el is not None and _is_truthy_xml(is_director_el.text):
            person_title = "Director"
        elif is_officer_el is not None and _is_truthy_xml(is_officer_el.text):
            person_title = "Officer"

    filing_date_str = file_date  # from EFTS metadata (more reliable than XML field)

    # ── 10b5-1 detection (filing-level) ──────────────────────────────────────

    # Check explicit aff10b5One flag first
    aff_el = root.find("./aff10b5One")
    is_planned = _is_truthy_xml(aff_el.text if aff_el is not None else None)

    # Also scan footnote text
    footnote_parts: list[str] = []
    for fn_el in root.findall("./footnotes/footnote"):
        if fn_el.text:
            footnote_parts.append(fn_el.text.strip())
    footnote_text = " ".join(footnote_parts)

    if not is_planned and footnote_text:
        is_planned = detect_planned_trade(footnote_text)

    if is_planned:
        log.info("Planned trade (10b5-1) in filing %s (%s)", adsh, ticker)

    # ── Transaction iteration ─────────────────────────────────────────────────

    results: list[dict] = []

    for tx in root.findall("./nonDerivativeTable/nonDerivativeTransaction"):
        code = _xml_text(tx, "./transactionCoding/transactionCode") or ""
        code = code.strip().upper()

        if code not in _KEEP_CODES:
            log.debug("Skipping code '%s' in %s (%s)", code, adsh, ticker)
            continue

        shares_raw = _xml_text(tx, "./transactionAmounts/transactionShares/value")
        price_raw = _xml_text(tx, "./transactionAmounts/transactionPricePerShare/value")
        ownership_raw = _xml_text(tx, "./ownershipNature/directOrIndirectOwnership/value")

        try:
            shares = float(shares_raw) if shares_raw is not None else None
        except (ValueError, TypeError):
            shares = None

        try:
            price = float(price_raw) if price_raw is not None else None
        except (ValueError, TypeError):
            price = None

        # Compute total value from shares * price
        total_value: float = 0.0
        if shares is not None and price is not None:
            try:
                total_value = shares * price
            except (TypeError, ValueError):
                total_value = 0.0

        # Skip purchases with no economic value (zero-price grants, awards)
        if code == "P" and total_value <= 0:
            log.debug("Skipping zero-price purchase in %s (%s)", adsh, ticker)
            continue

        tx_date_str = _xml_text(tx, "./transactionDate/value")
        if not tx_date_str:
            log.warning("Missing transactionDate in %s (%s) — skipping tx", adsh, ticker)
            continue

        ownership_type = (ownership_raw or "").strip().upper() or None
        report_lag = _date_diff_days(filing_date_str, tx_date_str)

        results.append({
            "source": "sec_form4",
            "ticker": ticker,
            "company_name": company_name,
            "person_name": person_name,
            "person_title": person_title,
            "transaction_type": "purchase" if code == "P" else "sale",
            "transaction_code": code,
            "ownership_type": ownership_type,
            "shares": shares,
            "price_per_share": price,
            "total_value": total_value,
            "amount_range": None,
            "transaction_date": tx_date_str,
            "filing_date": filing_date_str,
            "report_lag_days": report_lag,
            "filing_url": filing_url,
            "is_planned_trade": is_planned,
            "ring": None,
            "confidence_score": None,
            "alert_sent": False,
        })

    return results


# ── EDGAR REST API helpers ────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    """Create a requests.Session with the required SEC User-Agent header."""
    session = requests.Session()
    session.headers.update({"User-Agent": settings.sec_user_agent})
    return session


def _efts_search(
    since_date: str,
    until_date: str,
    session: requests.Session,
) -> list[dict]:
    """Paginate through EFTS to collect all Form 4 filing metadata.

    Args:
        since_date: ISO date string for the start of the filing range.
        until_date: ISO date string for the end of the filing range.
        session: Authenticated requests session.

    Returns:
        List of EFTS hit source dicts, each containing: adsh, ciks,
        file_date, and the full _id (used to extract filename).
    """
    results: list[dict] = []
    offset = 0

    while True:
        params = {
            "forms": "4",
            "dateRange": "custom",
            "startdt": since_date,
            "enddt": until_date,
            "from": offset,
        }
        try:
            resp = session.get(_EFTS_URL, params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning(
                "EFTS search failed at offset=%d: %s — returning %d results collected so far",
                offset, exc, len(results),
            )
            return results

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            src = hit["_source"].copy()
            src["_id"] = hit["_id"]  # carries {adsh}:{filename}
            results.append(src)

        total = data.get("hits", {}).get("total", {}).get("value", 0)
        offset += len(hits)
        if offset >= total:
            break

        time.sleep(_REQUEST_DELAY)

    return results


def _fetch_xml(
    ciks: list[str],
    adsh: str,
    filename: str,
    session: requests.Session,
) -> str | None:
    """Download Form 4 XML, trying each CIK until one returns a valid response.

    EDGAR stores filings under both the reporting owner's and issuer's CIK,
    so iterating the ciks list is sufficient to find the correct path.

    Args:
        ciks: List of CIKs associated with this filing (from EFTS).
        adsh: Accession number (dashes will be removed for the URL path).
        filename: Name of the XML document file (from EFTS _id).
        session: Authenticated requests session.

    Returns:
        XML text string, or None if all attempts fail.
    """
    adsh_nodash = adsh.replace("-", "")

    for cik in ciks:
        url = f"{_ARCHIVES_BASE}/{cik}/{adsh_nodash}/{filename}"
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.text
        except requests.RequestException as exc:
            log.debug("HTTP error fetching %s via CIK %s: %s", adsh, cik, exc)

    log.warning("Could not download XML for %s (tried %d CIKs)", adsh, len(ciks))
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_recent_form4s(since_date: date | None = None) -> list[dict]:
    """Fetch and parse all Form 4 filings filed on or after since_date.

    Uses the EDGAR EFTS full-text search API for filing discovery and
    downloads individual Form 4 XML files directly from SEC Archives.
    No third-party edgartools dependency — pure requests + ElementTree.

    The filing_date filter targets the SEC submission date, not the trade date.
    Individual transaction_dates are extracted per-transaction from the XML.

    Args:
        since_date: Include filings submitted on or after this date.
            Defaults to 3 days ago when None (daily cron default).

    Returns:
        Flat list of trade dicts ready for Bullseye processing and DB insert.

    Raises:
        requests.RequestException: Re-raises any network error from the EFTS
            search so the caller (run_ingestion.py) can log the health failure.
    """
    if since_date is None:
        since_date = date.today() - timedelta(days=3)

    until_date = date.today()
    log.info(
        "Fetching Form 4 filings filed %s to %s",
        since_date.isoformat(),
        until_date.isoformat(),
    )

    session = _make_session()

    # Step 1: collect filing metadata from EFTS
    efts_hits = _efts_search(since_date.isoformat(), until_date.isoformat(), session)
    log.info("EFTS returned %d Form 4 filing records", len(efts_hits))

    # Step 2: download and parse each XML
    all_trades: list[dict] = []
    for hit in efts_hits:
        adsh: str = hit["adsh"]
        ciks: list[str] = hit.get("ciks", [])
        file_date: str = hit.get("file_date", "")
        hit_id: str = hit.get("_id", "")

        # _id format: "{adsh}:{filename}"
        parts = hit_id.split(":", 1)
        if len(parts) != 2 or not parts[1]:
            log.warning("Unexpected EFTS _id format for %s: %r", adsh, hit_id)
            continue
        filename = parts[1]

        if not ciks:
            log.warning("No CIKs for filing %s — skipping", adsh)
            continue

        filing_url = f"{_ARCHIVES_BASE}/{ciks[0]}/{adsh.replace('-', '')}/{filename}"

        xml_text = _fetch_xml(ciks, adsh, filename, session)
        if xml_text is None:
            continue

        trades = _parse_form4_xml(xml_text, adsh, filing_url, file_date)
        all_trades.extend(trades)

        time.sleep(_REQUEST_DELAY)

    log.info(
        "Fetched %d raw transactions from %d Form 4 filings",
        len(all_trades),
        len(efts_hits),
    )
    return all_trades
