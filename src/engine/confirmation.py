"""Congressional confirmation matching engine (Phase 5 — optional module).

After each congressional ingestion cycle, checks whether a newly ingested
congressional purchase aligns with an existing Inner or Middle Ring SEC signal
(fired within the last CONFIRMATION_LOOKBACK_DAYS days).

If a match is found:
  - Sets ring = 'confirmation' on the congressional trade
  - Sets confirmation_of = <matched_trade_id>
  - Boosts the matched SEC signal's confidence_score by +10 (capped at 100)
  - Sends a confirmation Telegram alert

If no match is found, checks Outer Ring anomaly rules (SPEC §2.3).
Congressional trades that match neither confirmation nor anomaly rules are
logged silently for pattern research.

This module is entirely skipped when config.congress_enabled is False.
"""

# TODO Phase 5: implement run_confirmation_matching(conn) per SPEC §5.6
