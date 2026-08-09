"""Account-level circuit breakers.

PHASE 6. Not implemented yet.

Contract:
- Daily loss 1%, drawdown 3%, 3 consecutive losses, max 1 open position.\n- Kill-switch latch persisted to SQLite; a restart must not clear it.\n- Post-loss cooldown. No martingale, no averaging down, no revenge trading.
"""

raise NotImplementedError("Phase 6 not implemented")
