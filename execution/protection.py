"""Enforces: no position without a verified stop-loss.

PHASE 10. Not implemented yet.

Contract:
- Order: entry filled -> place SL -> verify SL live -> place TP ladder.\n- SL placement failure -> bounded retry -> emergency market close.\n- TP1 40% + move SL to breakeven (fee-padded), TP2 35%, runner on ATR trail.\n- Arms countdown_cancel_all as a dead-man switch.
"""

raise NotImplementedError("Phase 10 not implemented")
