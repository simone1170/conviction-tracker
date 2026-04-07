"""Phase 2 tests: SEC scraper parsing logic and 10b5-1 detection.

All tests are fully mocked — no live EDGAR network calls are made.
The new scraper uses direct XML parsing; tests supply raw XML strings.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.scrapers.edgar_scraper import (
    _date_diff_days,
    _normalize_date,
    _parse_form4_xml,
    detect_planned_trade,
)


# ── XML fixture helpers ───────────────────────────────────────────────────────

def _make_tx_xml(
    code: str = "P",
    shares: float = 1000.0,
    price: float = 500.0,
    ownership: str = "D",
    tx_date: str = "2026-04-01",
) -> str:
    """Return a <nonDerivativeTransaction> XML block."""
    return f"""
    <nonDerivativeTransaction>
        <transactionDate><value>{tx_date}</value></transactionDate>
        <transactionCoding>
            <transactionCode>{code}</transactionCode>
        </transactionCoding>
        <transactionAmounts>
            <transactionShares><value>{shares}</value></transactionShares>
            <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        </transactionAmounts>
        <ownershipNature>
            <directOrIndirectOwnership><value>{ownership}</value></directOrIndirectOwnership>
        </ownershipNature>
    </nonDerivativeTransaction>"""


def _make_form4_xml(
    transactions_xml: str = "",
    ticker: str = "NVDA",
    company: str = "NVIDIA Corp",
    person_name: str = "Jensen Huang",
    officer_title: str = "CEO",
    footnotes_xml: str = "",
    aff10b5: str = "false",
    filing_date: str = "2026-04-03",
) -> str:
    """Return a complete ownershipDocument XML string."""
    return f"""<?xml version="1.0"?>
<ownershipDocument>
    <issuer>
        <issuerName>{company}</issuerName>
        <issuerTradingSymbol>{ticker}</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerName>{person_name}</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isOfficer>1</isOfficer>
            <officerTitle>{officer_title}</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>{aff10b5}</aff10b5One>
    <nonDerivativeTable>
        {transactions_xml}
    </nonDerivativeTable>
    <footnotes>
        {footnotes_xml}
    </footnotes>
</ownershipDocument>"""


_ADSH = "0001234567-26-000001"
_URL = "https://www.sec.gov/Archives/edgar/data/0001234567/000123456726000001/form4.xml"
_FILE_DATE = "2026-04-03"


# ── detect_planned_trade ──────────────────────────────────────────────────────

def test_detect_planned_trade_10b5_1():
    assert detect_planned_trade("pursuant to a Rule 10b5-1 trading plan") is True


def test_detect_planned_trade_rule_10b():
    assert detect_planned_trade("adopted pursuant to Rule 10b5-1") is True


def test_detect_planned_trade_trading_plan():
    assert detect_planned_trade("in accordance with a pre-established trading plan") is True


def test_detect_planned_trade_pre_arranged():
    assert detect_planned_trade("This is a pre-arranged plan dated January 1") is True


def test_detect_planned_trade_case_insensitive():
    assert detect_planned_trade("RULE 10B5-1 PLAN") is True


def test_detect_planned_trade_negative():
    assert detect_planned_trade("open market purchase in accordance with company policy") is False


def test_detect_planned_trade_empty():
    assert detect_planned_trade("") is False


# ── _normalize_date ───────────────────────────────────────────────────────────

def test_normalize_date_string():
    assert _normalize_date("2026-04-01") == "2026-04-01"


def test_normalize_date_date_object():
    assert _normalize_date(date(2026, 4, 1)) == "2026-04-01"


def test_normalize_date_none():
    assert _normalize_date(None) is None


# ── _date_diff_days ───────────────────────────────────────────────────────────

def test_date_diff_days_basic():
    assert _date_diff_days("2026-04-03", "2026-04-01") == 2


def test_date_diff_days_same_day():
    assert _date_diff_days("2026-04-01", "2026-04-01") == 0


def test_date_diff_days_missing():
    assert _date_diff_days(None, "2026-04-01") is None
    assert _date_diff_days("2026-04-01", None) is None


# ── _parse_form4_xml ──────────────────────────────────────────────────────────

def test_parse_keeps_purchase():
    xml = _make_form4_xml(_make_tx_xml(code="P"))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)

    assert len(results) == 1
    assert results[0]["transaction_type"] == "purchase"
    assert results[0]["transaction_code"] == "P"


def test_parse_keeps_sale():
    xml = _make_form4_xml(_make_tx_xml(code="S", price=0.0))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert any(r["transaction_code"] == "S" for r in results)


def test_parse_discards_A():
    xml = _make_form4_xml(_make_tx_xml(code="A"))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results == []


def test_parse_discards_M():
    xml = _make_form4_xml(_make_tx_xml(code="M"))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results == []


def test_parse_discards_G():
    xml = _make_form4_xml(_make_tx_xml(code="G"))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results == []


def test_total_value_computed_from_shares_price():
    """total_value = shares * price_per_share."""
    xml = _make_form4_xml(_make_tx_xml(code="P", shares=100.0, price=50.0))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert len(results) == 1
    assert results[0]["total_value"] == pytest.approx(5000.0)


def test_report_lag_computed():
    """report_lag_days = filing_date - transaction_date."""
    xml = _make_form4_xml(_make_tx_xml(code="P", tx_date="2026-04-01"), filing_date="2026-04-03")
    results = _parse_form4_xml(xml, _ADSH, _URL, "2026-04-03")
    assert results[0]["report_lag_days"] == 2


def test_skip_on_missing_ticker():
    xml = _make_form4_xml(_make_tx_xml(code="P"), ticker="")
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results == []


def test_skip_on_missing_person_name():
    xml = _make_form4_xml(_make_tx_xml(code="P"), person_name="")
    # Empty name element → stripped to empty → treated as missing
    # Build XML manually with blank person name
    xml = """<?xml version="1.0"?>
<ownershipDocument>
    <issuer>
        <issuerName>NVIDIA Corp</issuerName>
        <issuerTradingSymbol>NVDA</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerName></rptOwnerName>
        </reportingOwnerId>
    </reportingOwner>
    <nonDerivativeTable>""" + _make_tx_xml(code="P") + """</nonDerivativeTable>
</ownershipDocument>"""
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results == []


def test_skip_zero_price_purchase():
    """Purchase with zero total_value is skipped (likely a grant/award)."""
    xml = _make_form4_xml(_make_tx_xml(code="P", shares=1000.0, price=0.0))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results == []


def test_planned_trade_flagged_via_footnote():
    """is_planned_trade is True when footnotes match 10b5-1 patterns."""
    xml = _make_form4_xml(
        _make_tx_xml(code="P"),
        footnotes_xml='<footnote id="F1">Pursuant to a Rule 10b5-1 trading plan</footnote>',
    )
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert len(results) == 1
    assert results[0]["is_planned_trade"] is True


def test_planned_trade_flagged_via_aff10b5():
    """is_planned_trade is True when aff10b5One element is '1' or 'true'."""
    xml = _make_form4_xml(_make_tx_xml(code="P"), aff10b5="1")
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert len(results) == 1
    assert results[0]["is_planned_trade"] is True


def test_planned_trade_not_flagged_on_normal_footnote():
    xml = _make_form4_xml(
        _make_tx_xml(code="P"),
        footnotes_xml='<footnote id="F1">Open market purchase</footnote>',
    )
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results[0]["is_planned_trade"] is False


def test_source_is_always_sec_form4():
    xml = _make_form4_xml(_make_tx_xml(code="P"))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results[0]["source"] == "sec_form4"


def test_invalid_xml_returns_empty():
    results = _parse_form4_xml("<not valid xml<<", _ADSH, _URL, _FILE_DATE)
    assert results == []


def test_amount_range_is_none_for_sec():
    """SEC Form 4 has exact values, not ranges — amount_range must be None."""
    xml = _make_form4_xml(_make_tx_xml(code="P"))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results[0]["amount_range"] is None


def test_ownership_type_direct():
    xml = _make_form4_xml(_make_tx_xml(code="P", ownership="D"))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results[0]["ownership_type"] == "D"


def test_ownership_type_indirect():
    xml = _make_form4_xml(_make_tx_xml(code="P", ownership="I"))
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results[0]["ownership_type"] == "I"


def test_ticker_uppercased():
    xml = _make_form4_xml(_make_tx_xml(code="P"), ticker="nvda")
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results[0]["ticker"] == "NVDA"


def test_multiple_transactions():
    two_txs = _make_tx_xml(code="P", shares=100.0) + _make_tx_xml(code="S", price=0.0)
    xml = _make_form4_xml(two_txs)
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    codes = [r["transaction_code"] for r in results]
    assert "P" in codes
    assert "S" in codes


def test_person_title_extracted():
    xml = _make_form4_xml(_make_tx_xml(code="P"), officer_title="Chief Financial Officer")
    results = _parse_form4_xml(xml, _ADSH, _URL, _FILE_DATE)
    assert results[0]["person_title"] == "Chief Financial Officer"
