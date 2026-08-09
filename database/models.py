"""SQLite schema and persistence (WAL mode).

PHASE 11. Not implemented yet.

Contract:
- trades: timestamp, symbol, side, leverage, entry, exit, size, margin,\n  stop_loss, take_profit, fees, pnl, pnl_percent, signal_score,\n  market_regime, reason, duration, liquidation_price.\n- kill_switch state so limits survive restarts.\n- equity_curve snapshots for drawdown.
"""

raise NotImplementedError("Phase 11 not implemented")
