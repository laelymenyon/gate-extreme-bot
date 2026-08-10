# gate-extreme-bot

High-leverage Gate.io USDT-perpetual futures bot. Built for **selectivity and capital
preservation**, not for trade frequency. When no high-quality setup exists, it does nothing.

> **Status: PHASE 9 of 14 complete.** Environment, REST client, market-data feed, indicators,
> regime detection, signal scoring, signal engine, position sizing, circuit breakers,
> liquidation protection, order execution, backtesting.
> The bot can now decide *whether* to trade, *which way*, *how much*, *whether it is allowed
> to at all*, and *whether the stop survives contact with liquidation* — and it can place and
> verify those orders, including the protective stop.
> **By default every order goes to an in-process simulator.** Reaching the real exchange
> still requires all three safety switches to agree; miss any one and execution stays
> simulated.

**This software can lose money. No win rate or profit is claimed, promised, or implied.**
100x leverage means a 1 % adverse move against full margin is a total loss of that margin.
Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §4 and §7 before considering live use.

---

## Quick start

```bash
cd gate-extreme-bot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

cp .env.example .env        # then fill in GATE_API_KEY / GATE_API_SECRET

.venv/bin/python main.py --status
.venv/bin/python -m pytest tests/ -q
```

## Commands

```bash
python main.py --status                      # config + safety gate state
python main.py --positions                   # live account + open positions (needs API keys)
python main.py --stats                       # performance analytics   (Phase 11)
python main.py --mode paper                  # paper trading loop      (Phase 10)
python main.py --mode backtest               # backtest + walk-forward (Phase 10 wiring)
python main.py --mode live --confirm-live    # real orders             (Phase 14)
```

## The safety gate

Live orders require **three independent switches to agree**:

| Switch | Where | Required value |
|---|---|---|
| `DRY_RUN` | `.env` | `false` |
| `--mode` | CLI | `live` |
| `--confirm-live` | CLI | present |

Missing any one → simulation. `DRY_RUN` is treated as `true` unless it is *literally*
`false`/`0`/`no`, so a typo fails safe. Credentials are also required, and refusal exits `2`.
All eight switch combinations are covered by tests in `tests/test_config.py`.

## What Phase 1 established

Everything below was read from the live API or Gate.io's official SDK — none of it is assumed.

- **Only 31 of 899 USDT contracts allow ≥100x.** Only **BTC and ETH** exceed 100x (max 200x), so
  `leverage: 150` restricts you to two pairs. Most other ≥100x pairs are tokenized equities,
  indices, FX, and commodities with session gaps — the real 24/7 crypto set is
  **BTC, ETH, SOL, XRP**.
- **Maker fee is negative** (−0.01 % rebate); taker is 0.075 %. Entries therefore default to
  post-only, which cuts fee drag from 1.20 R to 0.13 R per trade.
- **Maintenance rate is tiered**, not the flat value on the contract endpoint. Using the flat value
  would under-estimate liquidation risk as size grows.
- **A hard contradiction in the original spec was found.** At 100x with a 0.5 % liquidation buffer
  the widest possible stop is 0.125 % — noise width — and above 100x *no* stop fits at all.
  Full derivation in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §4.

## Configured profile: pure 100x, 0.30 % liquidation buffer

Chosen deliberately (2026-08-09). The buffer was lowered 0.50 % → 0.30 % so that every ≥100x
contract becomes tradable rather than only two.

| | |
|---|---|
| Effective leverage | true 100x (no margin top-up), isolated |
| Tradable pairs | **all 31** (was 2 of 31 at a 0.50 % buffer) |
| Stop ceiling — BTC, ETH (mmr 0.30 %) | **0.325 %**, fee 0.20 R, break-even WR **40.0 %** |
| Stop ceiling — other 29 incl. SOL, XRP (mmr 0.50 %) | **0.125 %**, fee 0.52 R, break-even WR **50.7 %** |
| Above 125x | rejected at config load as unreachable, not silently skipped at runtime |
| Entry order type | **post-only enforced** — a taker entry at a 0.125 % stop needs a 73.3 % win rate to break even. Unfilled entries are normal. |
| Risk per trade | 0.25 % of equity, unchanged |

The buffer change also widened BTC/ETH stops 2.6x, cutting their break-even win rate from 50.7 % to
40.0 %. Accepted cost: less room between stop and liquidation, so a sharp mark-price spike is more
likely to reach `liq_price` before the stop fills. The buffer is protection, not immunity.

Kept unchanged: 100x, isolated margin, no margin top-up, mandatory protective SL, all circuit
breakers (1 % daily loss, 3 % drawdown, 3 consecutive losses, 1 open position).

All 31 pairs include 23 synthetics (equities, indices, FX, commodities). Because a stop cannot
execute while a venue is closed, `session_guard` force-flattens synthetics before the close and
fails closed when the calendar is unknown. To unlock the 29 skipped pairs,
`liquidation_buffer` must fall below 0.425 % — a separate decision, not a default.

## What Phase 2 added — the REST client

`exchange/gate_client.py` is the only path to Gate.io, and it enforces two things the rest of the
bot cannot opt out of.

**Writes are blocked before the socket opens.** Every POST/PUT/DELETE raises `WriteBlocked` unless
the safety gate is open — no network call is made, and `client.stats.writes_blocked` counts the
attempt. Reads keep working, so paper trading still sees real market and account data. This is a
second barrier behind the config gate: a bug elsewhere cannot place a live order by itself.

**Writes never retry blindly.** Reads retry on 429/5xx/network errors with jittered backoff, and
each attempt is re-signed. Writes require a caller-supplied `t-` idempotency key so a duplicate is
detectable; a network failure without one raises `NETWORK_UNCERTAIN` instead of retrying, because
the first attempt may already have filled. Terminal errors (`INVALID_KEY`,
`INSUFFICIENT_AVAILABLE`, `LIQUIDATE_IMMEDIATELY`, …) are never retried.

Verified against the live API: public reads return 200 and parse; private reads return
`INVALID_KEY` on a dummy key, which proves the signature itself was accepted; writes were refused
with zero network calls. BTC_USDT returns **19 risk tiers**, with mmr climbing 0.30 % → 0.35 % →
0.45 % as notional grows — so `select_tier()`, not the flat `maintenance_rate` field, is what the
Phase 7 liquidation guard must use. A test pins that behaviour.

Order semantics encoded in the types: `size` is a signed contract count (negative = short), market
orders are `price="0"` + `tif="ioc"`, and stop-losses use `/price_orders` with `price_type=1`
(**mark price**) because liquidation is priced off the mark. `set_leverage(sym, 0)` is rejected —
on Gate.io `0` means cross margin, which this bot forbids.

89 tests, no network: `python -m pytest tests/ -q`.

## What Phase 3 added — the market-data feed

`exchange/websocket.py` subscribes to four channels on `wss://fx-ws.gateio.ws/v4/ws/usdt`:
`futures.tickers` (last + **mark price** + funding), `futures.book_ticker` (best bid/ask),
`futures.orders`, and `futures.positions`. Private channels sign with HMAC-SHA512 over
`channel=…&event=…&time=…` — a different scheme from REST, verified against the official SDK and
the live endpoint.

**The watchdog fails closed.** `is_healthy()` and the accessors share one predicate, so `book()`
and `ticker()` raise `FeedNotHealthy` whenever the feed is disconnected, awaiting resync, past a
15 s pong timeout, inside the 10 s warmup, or the data is stale (book > 5 s, ticker > 10 s). No
signal can be built from data the watchdog rejects. The accessors check *every* stream for a
symbol, not just the one asked for: book and ticker share a socket, so a stale book beside a fresh
ticker means a subscription died.

**WebSocket is never source of truth after a disconnect.** Every drop clears the cached tickers,
books, positions, orders, and dedup cursors. The reconnect path is fixed and test-enforced:
connect → subscribe → **full REST resync** (positions, open orders, price-triggered orders,
contracts, risk tiers) → reconcile → only then healthy. `resync_pending` blocks every accessor for
the whole window, and a failed resync keeps it blocked rather than falling back to the socket.
Backoff is exponential with 25 % jitter, capped at 60 s, and more than 10 attempts in 300 s stops
the loop instead of hammering the venue.

Duplicate and out-of-order frames are dropped via per-symbol cursors (`u` for books, `time_ms` for
tickers, update ids for positions). Malformed, partial, null, and non-JSON frames increment
`stats.malformed` and are dropped — `handle_message()` never raises into the read loop, so one bad
frame cannot kill the connection.

**The feed cannot answer liquidation questions.** `maintenance_rate()` exists only to raise
`LiquidationTierUnavailable`, pointing callers at `await select_tier(symbol, notional)`, which
resolves the real tier from REST. Live check: 100k notional → tier 1 mmr 0.003, 800k → tier 2
0.0035, 2M → tier 4 0.0045. The liquidation engine itself is Phase 7; this is the seam only.

171 tests. No order was sent — the feed is read-only by construction.

## What Phase 4 added — indicators

`strategy/indicators.py`, pure numpy (TA-Lib was rejected in Phase 1 for needing a system C
library). EMA 9/21/50/200, RSI, MACD, true range, ATR, anchored and rolling VWAP, volume MA,
relative volume, swing pivots and nearest support/resistance.

Every function is length-preserving, pure, and returns **NaN for "not enough data" rather than a
half-warmed number** — the same fail-closed rule the feed uses. RSI and ATR use Wilder smoothing,
EMA and MACD are SMA-seeded, so an EMA 200 here matches a 200 EMA on the chart.

Two decisions worth calling out:

**Relative volume excludes the current bar from its own baseline.** An inclusive average dilutes
the spike it measures: a true 4x bar reads 3.48x, and `filters.btc_volume_spike_multiple: 4.0`
would need a 4.75x bar before suspending alt entries. Excluded, a 4x bar reads 4.0.

**Support/resistance will not use a pivot before its confirmation bar.** A swing high at bar *i*
is only knowable at `i + right`. Reading it earlier is the classic backtest lookahead bug, and
since backtest and live share this code path the guard lives in the indicator. A test asserts
that what live sees (`values[:i+1]`) equals what a backtest sees (`as_of=i`) at every bar; the
same audit against 300 live BTC 1m candles found 0 mismatches. No level found returns NaN, which
callers must treat as missing data, not as open space.

Live check on 300 BTC_USDT 1m candles: RSI 31.59, ATR 14 = 0.047 % of price, MACD −56.57 with the
histogram turning up, support/resistance bracketing the close. That ATR is the number Phase 5
inherits — at `atr_multiplier: 1.5` it implies a 0.071 % stop, below the 0.10 % `min_distance`
floor and well inside BTC's 0.325 % ceiling at 100x.

122 indicator tests, 293 total. Reference values are recomputed independently inside the tests,
so a bug in `indicators.py` cannot make its own test pass.

## What Phase 5 added — the decision layer

`strategy/regime.py`, `strategy/scoring.py`, `strategy/signal_engine.py`. Together they answer
*whether* to trade and *which way*, and stop there — none of them import `exchange/`, so there is
no network path and no order path. A test asserts `SignalEngine` exposes no method named
`order`, `execute`, or `place`.

**One rule closes the lookahead problem.** Gate stamps candles with their *open* time (verified
live: 1h stamps are exact multiples of 3600), so the newest bar of every series is still forming
and a bar on interval *T* completes only at `t + T`. `closed_bars()` is the single place that
convention lives, and it fixes two bugs at once: scoring a forming candle, and asking "what is
the 1h trend?" at 10:05 and getting the unfinished 10:00 bar instead of the 09:00 one. Checked
live at 23:47 UTC — each timeframe dropped exactly its one forming bar and the 1h correctly
reported the 22:00 bar. Every module also takes `as_of` and physically truncates its input, so
tests can assert bar-by-bar that live (`head(i+1)`) and backtest (`as_of=i`) agree.

**Regime checks volatility first, and the order matters.** A textbook EMA stack *during* a
volatility spike is exactly the setup that gaps through a 0.125 % stop, so it must not be
reachable as TRENDING. ATR is ranked against the symbol's own recent history — a fixed threshold
would call every synthetic quiet and every alt violent — using a midrank that counts ties as
half. The obvious `(history <= now).mean()` scores a perfectly steady market at 1.0 and would
veto it as HIGH_VOLATILITY exactly when conditions are best; a test pins that.

**Scoring is direction-aware and fails closed.** Six weighted categories, each earning a 0.0-1.0
fraction of its weight so the 0-100 bound holds by construction. A NaN earns zero, never a
midpoint. The RSI band has a ceiling — buying RSI 85 is chasing, and at a 0.125 % stop there is
no room to survive the snap-back. Support/resistance needed the subtlest fix: "no pivot overhead"
means clear space in a rally but nothing of the sort in a decline, since neither has pivot highs,
so it falls back to the lookback-window extreme excluding the current bar.

**The engine is all vetoes**, each recording a stage and reason: data → spread → liquidity → BTC
filter → regime → volatility → direction → MTF veto → abnormal candle → score. Higher timeframes
decide direction and a split among them is no trade; the BTC correlation filter refuses an alt
outright when BTC data is absent.

On live BTC/ETH/SOL every symbol was rejected at the regime stage (5m and 15m both in a
volatility extreme) — the filter working as specified, not a fault. Whether the trades that do
survive win above the ~40 % break-even rate is a Phase 9 question.

97 new tests (27 regime + 42 scoring + 28 engine), **423 total**, no network.

## What Phase 6 added — sizing and the circuit breakers

`risk/position_sizer.py` and `risk/risk_manager.py`. Neither imports `exchange/` — contracts and
risk tiers arrive as structural protocols — so there is still no network path and no order path.
`risk/liquidation_guard.py` was still a Phase 7 stub at this point; it landed in Phase 7 below.

**Size comes from risk, never from leverage.** `size = (equity × risk.per_trade) / stop_distance`,
floored to whole contracts. At 20x and 100x the same setup gives the same size, the same stop and
the same 0.25 % of equity at risk — only the locked margin differs, by exactly 5x.

**Every rounding shrinks the position.** Contracts floor; `order_size_max` and the top tier's
`risk_limit` cap; the stop price snaps *toward* entry so rounding can never move it closer to
liquidation; and size is derived from the rounded stop, not the ideal one. The single case that
could have gone the other way is `order_size_min` — when the smallest tradable order would risk
more than the budget, the trade is **refused, not rounded up**, because rounding there is a silent
breach of the one number every other guarantee rests on.

**The stop ceiling comes from the tiered maintenance rate.** An empty tier list is a refusal, not
a fallback to the contract's flat `maintenance_rate`, which is only tier 1 and understates
liquidation risk exactly as size grows. Tier and size are mutually dependent — the tier is chosen
by notional, notional is `budget / stop_distance`, the stop is capped by the tier's rate — so a
short monotone fixed point resolves them, always rounding the maintenance rate up rather than down.
The implementation reproduces the Phase 1 table exactly: 0.325 % stop and 0.20 R of fees on
BTC/ETH, 0.125 % and 0.52 R on the other 29.

**Four breakers, three different clearing rules.** Daily loss (1 %) and a 3-loss streak clear at
the next UTC day; drawdown (3 %) needs a **manual reset**; one open position is a condition, not a
latch. Equity is observed on every call rather than inferred from closed trades, because at 100x a
drawdown arrives through the mark price and a breaker counting only settled PnL notices far too
late. The day's baseline is the day's opening equity, so yesterday's loss does not eat today's
allowance — but the high-water mark never resets on a calendar change.

**A reset re-baselines what it cleared.** Clearing the drawdown latch while the peak still sits
3 % above equity would re-trip on the next observation, halting the account forever and making the
reset theatre. Acknowledging it moves the high-water mark to current equity — a deliberate loss of
history, which is why nothing in the bot calls `reset()`.

**The latches are persisted in SQLite (WAL), not held in memory.** The restart case is the point:
the most tempting thing to do after a bad run is restart the bot. Tests cover restart,
reset-then-restart, and a losing streak surviving a restart. Anything unreadable — non-finite
equity, a backwards clock, a corrupt state row, an unopenable database — refuses with
`unknown_state` rather than defaulting.

**No martingale, no averaging down, no revenge trading.** `risk_fraction()` takes no arguments, so
there is nothing to scale by recent losses; adding to a held symbol is refused outright; and a
cooldown follows every trade, 300 s after a loss against 60 s after a win.

131 new tests (56 sizing + 68 risk + 7 config), **554 total**, no network.

## What Phase 7 added — the liquidation guard

`risk/liquidation_guard.py`, the most safety-critical module in the repo. Everything else being
wrong costs a trade; this being wrong costs the margin. One rule: **liquidation is never a
stop-loss.** A position may exist only when its stop clears the liquidation price by
`protection.liquidation_buffer`, measured on the **mark** series, because that is what liquidation
is priced off.

**A bare maintenance rate is not accepted.** `assess()` takes a `TierSnapshot` — the tier ladder
plus the time it was read — and resolves the tier from actual notional. There is no default
maintenance rate anywhere in the module. The consequence shows up in one test: the same 0.325 %
stop **passes at tier 1 (100 k notional, mmr 0.30 %) and is refused at tier 5** (2 M, mmr 0.50 %,
so the widest stop is 0.125 %). Reading Gate's flat contract field would have missed exactly that.

**Fail-closed everywhere.** No snapshot, an empty ladder, one older than an hour, one timestamped
in the future, a non-finite field, a **non-monotonic ladder**, cross margin, leverage above the
ceiling, a tier whose own `leverage_max` is below the configured leverage, notional past the top
tier, or a stop on the wrong side of entry — each refuses naming the stage that refused. Refusing
costs an opportunity; guessing costs the margin.

**Rounding is conservative in one direction only.** The predicted liquidation price snaps onto the
mark-price grid *toward entry*, so it is never optimistic — rounding away would widen the apparent
buffer by up to a tick, which is not negligible when the whole buffer is 0.30 % of price. The
buffer is checked twice, once as a fraction and once in price terms against that pessimistic
figure, and a coarse grid can turn a fractional pass into a refusal.

**The exchange has the last word after the fill.** An API response is not proof: the fill may have
slipped, the leverage may not have applied, the position may have landed in a stricter tier — each
moves liquidation without moving the stop. `verify_fill()` re-reads the exchange's own `liq_price`
and returns `action="flatten"` when it is missing, wrong-sided, inside the buffer, beyond the stop,
reported under cross margin (`leverage=0`), or drifting past `liq_price_tolerance` from the
prediction. That is a recommendation — this module closes nothing.

**The top-up solver still runs.** At the shipped 0.30 % buffer a 0.50 % stop on BTC needs 85.1x
effective leverage (the 72.7x in ARCHITECTURE §4 is the same solve against the original 0.50 %
buffer). With `allow_margin_topup: false` that figure only informs skip-vs-trade, but a refusal
reports how far short it was rather than merely that it was short.

`assess_plan()` consumes the Phase 6 `PositionPlan` and re-derives the buffer from its final
numbers, so a sizing bug surfaces as a refusal rather than a position. Phases 1–6 are untouched,
and the guard imports no `exchange` module.

94 new tests, **648 total**, no network.

## What Phase 8 added — order execution

`execution/order_manager.py` and `execution/protection.py`. Everything the previous phases
protected now gets acted on, under one invariant:

> **A position may not exist without a verified stop-loss.**

```
entry filled → place SL → re-read the SL from the exchange → only then the TP ladder
```

**The re-read is the point.** A 200 on the stop POST means Gate.io accepted the request, not
that a live trigger exists — and between "position open" and "stop confirmed" is the only
moment this bot ever holds unprotected leveraged size. The stop is read back from
`/price_orders` and matched by client id before anything else happens; if it cannot be
confirmed within `sl_retry_attempts`, the position is **market-closed**. Closing at a loss is
correct there: an unprotected 100x position is not a trade, it is an open-ended bet on the
next candle. A test asserts the ordering against the gateway call log rather than trusting
the code to read that way.

The stop triggers on **mark price** (liquidation is priced off the mark, so a last-price stop
races the wrong series) and is a **market order** (a stop that does not fill is not a stop).
A backwards trigger rule would produce an order that can never fire while listing as
protection in every audit, so the side-to-rule mapping is a named function with its own test.

**An API response is not proof.** Fill, size and average price are re-read from
`GET /orders/{id}`. `UNKNOWN` is a first-class state, deliberately distinct from `REJECTED`:
a rejected order certainly does not exist, an unknown one might, and anything unknown counts
as exposure. When a post-only entry times out the manager cancels and then re-reads anyway,
because the cancel may have raced a fill.

**Unfilled is a normal outcome.** Post-only entries frequently do not fill; that reports as
`EXPIRED`, not as an error. At a 0.125 % stop a taker entry needs a 73.3 % win rate to break
even, so not filling beats filling expensively.

**Take-profits are R multiples of the *actual* stop**, so a stop capped by the liquidation
ceiling shrinks the ladder with it. Legs floor and the runner takes the remainder, so they sum
to exactly the position — checked for every size from 1 to 59. Break-even is padded past entry
by the fee buffer, because moving a stop to literal entry is a small guaranteed loss rather
than a free trade. A ratchet makes a stop that loosens *unrepresentable*: giving a losing
trade room is how 0.25 % of risk becomes 3 %. Moving a stop places the replacement **before**
cancelling the original, since cancel-first opens an unprotected gap exactly where the trade
is already moving fast.

**Simulation is the default, not a mode.** `OrderManager.for_config()` returns the in-process
simulator unless the safety gate is open *and* a client was supplied — "live_enabled but
nobody passed a client" simulates, so a wiring mistake fails toward doing nothing. Behind
that, the Phase 2 write-guard still raises `WriteBlocked` before a socket opens, with
`stats.requests == 0`. The simulator fills honestly: a resting post-only order fills only when
the market trades through it, and its liquidation price uses the same formula Phase 7 checks.

`countdown_cancel_all` is armed first in the sequence, so a bot that dies mid-trade leaves
nothing resting unwatched, and `audit()` answers "is what is open right now actually
protected?" after a restart or reconnect.

64 new tests, **712 total**, no network — every one against the simulator.

## What Phase 9 added — the backtester

`backtest/engine.py`. Its job is to produce a number that is **allowed to say no**: a
backtester that flatters a strategy is worse than none, because it converts an unprofitable
idea into a funded one. Every modelling choice resolves against the strategy.

**Intrabar order is adverse.** A bar is four numbers, not a path. When the stop and a target
both lie inside one bar, the stop is taken; when liquidation is in there too, it goes first.
The optimistic reading of the same bar is what turns a losing system into a winning
backtest, and it is invisible in the output.

**Post-only entries do not always fill.** The entry rests at the signal bar's close and
fills only if a later bar trades through it. Assuming fills would hand the strategy the
maker rebate for free — the biggest single lever on whether any of this is profitable.

**Costs are charged, not estimated away**: maker rebate in, taker out, funding at every
8-hour boundary, slippage on every taker exit — but not on a liquidation, where the venue
takes the position at its own price. Liquidation is simulated, and a test asserts the
liquidated exit loses strictly more than the stopped one.

**The verdict refuses below 1000 trades.** Thirty trades cannot distinguish a 40 % win rate
from a 55 % one, so the engine reports INCONCLUSIVE however good the numbers look. Profit
factor comes back as `inf` rather than a big number when nothing was lost, because "too few
losses to judge" is the honest reading. `walk_forward()` splits chronologically 50/25/25 and
flags **OVERFIT** when training is positive and out-of-sample is not.

It drives the real stack — Phase 5 decides, Phase 6 sizes and halts, Phase 7 vetoes — using
`candles.head(i+1)`, the same call the live path makes. `execution/` and `exchange/` are not
imported.

**It found a defect at the Phase 6/7 seam, and it is fixed.** The sizer capped a wide stop
onto the liquidation ceiling *exactly*; the guard then rounds liquidation toward entry and,
on some prices, that consumed the last fraction of buffer and vetoed the plan. Both layers
rounded the safe way and both were individually right — composed, acceptance of a
maximally-capped stop depended on where the last decimals landed (~2 in 26 on the original
fixture). The sizer now caps one tick *inside* the ceiling, so the producer reserves the room
the consumer's rounding needs and no Phase 7 check was loosened. 520 capped plans across four
volatility regimes, zero vetoes. Details in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §16.

47 new tests, **759 total**, no network.

## Core invariants

1. **No position exists without a verified stop-loss.** If the SL cannot be confirmed after bounded
   retries, the position is market-closed.
2. **Liquidation is never used as a stop.** Entry is rejected unless the exchange-reported
   `liq_price` clears the stop by the configured buffer.
3. **Size comes from risk, never from leverage.** `size = (equity × risk) / stop_distance`.
4. **No martingale, no averaging down, no revenge trading.** Not configurable — validation rejects
   `true`.
5. **Kill-switches persist to SQLite.** A restart cannot clear a tripped daily-loss or drawdown
   limit.
6. **An API response is not proof of success.** Fills, size, entry price, SL, and TP are each
   re-read from the exchange.

## Layout

```
config.py  config.yaml  .env         main.py
exchange/   gate_client.py  websocket.py
strategy/   indicators.py  signal_engine.py  regime.py  scoring.py
risk/       risk_manager.py  position_sizer.py  liquidation_guard.py
execution/  order_manager.py  protection.py
backtest/   engine.py
database/   models.py
monitoring/ logger.py  dashboard.py
tests/      test_config.py  test_gate_client.py  test_websocket.py  test_indicators.py
            test_regime.py  test_scoring.py  test_signal_engine.py
            test_position_sizer.py  test_risk_manager.py  test_liquidation_guard.py
            test_execution.py  test_backtest.py
docs/       ARCHITECTURE.md
```

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Environment + architecture | **done** |
| 2 | Gate.io REST client (signing, backoff, idempotency) | **done** |
| 3 | Market data + WebSocket (feed, watchdog, REST resync) | **done** |
| 4 | Indicators | **done** |
| 5 | Regime + signal scoring + signal engine | **done** |
| 6 | Risk manager (sizing + circuit breakers) | **done** |
| 7 | Liquidation protection (tiered mmr + buffer guard) | **done** |
| 8 | Order execution + protection (`execution/`) | **done** |
| 9 | Backtesting + walk-forward | **done** |
| 10 | Paper-trading loop (wiring the layers end to end) | next |
| 11 | Dashboard + database | pending |
| 12 | Testing | pending |
| 13 | Paper trading validation | pending |
| 14 | Live readiness | pending |

Each phase must implement, pass tests, and be reviewed before the next begins. Live trading is not
enabled until paper trading has run correctly and backtesting shows the edge is **not** merely an
artifact of leverage.
