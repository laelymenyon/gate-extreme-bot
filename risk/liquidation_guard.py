"""The most safety-critical module.

PHASE 7. Not implemented yet.

Contract:
- Selects the risk_limit tier matching actual notional (tiered maintenance_rate).\n- liq_distance ~= 1/leverage - maintenance_rate - taker_fee.\n- Solves the isolated margin top-up needed for liq_distance >= SL + buffer.\n- After fill, re-reads Position.liq_price from the exchange and re-verifies.\n- Rejects the trade if the buffer cannot be met. Liquidation is never a stop-loss.
"""

raise NotImplementedError("Phase 7 not implemented")
