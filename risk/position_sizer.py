"""Risk-derived position sizing.

PHASE 6. Not implemented yet.

Contract:
- size = (equity * risk.per_trade) / stop_distance, converted to integer contracts\n  via the contract's quanto_multiplier, then floored.\n- Validates order_size_min/max, available margin, and risk-limit tier.\n- Leverage never increases risk; it only reduces locked margin.
"""

raise NotImplementedError("Phase 6 not implemented")
