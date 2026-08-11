# gate-extreme-bot

High-leverage Gate.io USDT-perpetual futures bot. Built for **selectivity and capital
preservation**, not for trade frequency. When no high-quality setup exists, it does nothing.

> **Status: PHASE 15 of 15 complete.** Environment, REST client, market-data feed, indicators,
> regime detection, signal scoring, signal engine, position sizing, circuit breakers,
> liquidation protection, order execution, backtesting, paper trading, persistence,
> logging, analytics, a cross-layer test suite, paper-trading validation, the
> live-readiness audit, **and a live trading runner** (`live/loop.py`, wired to
> `--mode live`).
> The bot can now decide *whether* to trade, *which way*, *how much*, *whether it is allowed
> to at all*, and *whether the stop survives contact with liquidation*; it can place and
> verify those orders, including the protective stop; it keeps an auditable record of
> what it did and reports the result; it can grade a paper run against explicit
> acceptance criteria that are allowed to say no; it can **audit its own readiness for
> live trading and refuse it**; and it can now run the whole stack against Gate.io —
> reading account, positions, contract metadata and candles, entering post-only, protecting
> every fill, re-synchronising against the exchange on every step, arming the dead-man
> switch while exposed, and force-flattening synthetics before their venues close.
> **By default every order goes to an in-process simulator.** Reaching the real exchange
> still requires all three safety switches to agree; miss any one and execution stays
> simulated. **1183 tests pass, and no live order has ever been sent.**
>
> **Preflight currently returns NO-GO, and that is the correct answer.** No paper trading
> or backtesting history has been accumulated, so the roadmap's precondition for live
> trading is unmet. The live runner exists but refuses to start on a NO-GO — the gate is
> closed and the evidence requirement is unmet, in that order.

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
python main.py --validate                    # grade paper history     (Phase 13)
python main.py --preflight                   # live-readiness GO/NO-GO (Phase 14)
python main.py --connectivity                # read-only creds/auth/account/market check
python main.py --mode paper                  # paper trading loop      (Phase 10)
python main.py --mode backtest               # backtest + walk-forward (Phase 10 wiring)
python main.py --mode live --confirm-live    # live runner (Phase 15) — refuses unless preflight is GO
python main.py --verify-live-order --symbol BTC_USDT
                                             # FINAL barrier: one real minimum-size order,
                                             # protected and closed, after typing
                                             # "BTC_USDT SEND". Never run automatically.
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

## What Phase 10 added — the paper-trading loop

`paper/loop.py`. The first place every layer runs as a system, in the order a live run uses:
market data → risk breakers → signal → size → liquidation guard → entry → protect.

Its value is not more statistics — Phase 9 measures the strategy. It is that this exercises
what a backtest never touches: the order state machine, the SL-first sequence, protective
orders resting on an exchange, and what happens when an entry misses or a stop cannot be
verified. A backtest computes what a trade *would* have earned; this finds out whether the
bot can carry one.

**It cannot trade for real.** `PaperTrader` **refuses to construct** when the safety gate is
open — not a branch inside the loop, and no flag overrides it — and refuses again if the
resolved gateway is not the simulator. Market data is a protocol: replay recorded candles, or
pull live ones through the Phase 2 client whose reads stay available while the write-guard is
shut.

**It found two real defects that only an end-to-end run reaches.** The simulator ignored
`reduce_only`, so after TP1 trimmed a position the full-size stop **reversed** it instead of
closing it. And a post-only entry submitted at the mark filled instantly, gifting every paper
run the maker rebate and erasing the unfilled-entry outcome that is supposed to be frequent —
which would have made every paper result look better than the live venue ever will. Both are
fixed and pinned by tests.

Rejections are counted by the stage that refused (`no_signal`, `cooldown`,
`size:order_size_min`, `liq:buffer`, `entry:expired`), because the loop's normal state is
doing nothing and that should be visible rather than hidden.

38 new tests, **797 total**, no network.

## What Phase 11 added — persistence, logging, analytics

`database/models.py`, `monitoring/logger.py`, `monitoring/dashboard.py`. The run now outlives
the process: what was traded, why it was traded, and what it cost.

**The trade record stores the reasoning, not just the PnL.** Signal score, market regime and
exit reason are columns rather than log lines, because the question worth asking after a
losing week is not "how much" but "which setups, in which regime, at what score" — a schema
that only records PnL cannot answer it. The store is append-only: there is no `update_trade`
and no `delete`. It shares the one SQLite file the Phase 6 kill-switches already use, so a
restart recovers the tripped breakers *and* the history that justifies them from one place;
two files could disagree.

**The equity curve is stored separately from the trades.** At 100x a drawdown can arrive
through an open position's mark price without any trade closing, so a curve reconstructed
from closed trades understates the worst moment. Snapshots are appended independently.

**Redaction is a filter, not a convention.** `GATE_API_KEY`, `GATE_API_SECRET` and the `SIGN`
header are stripped from every record — message, args and extras alike — by a filter attached
to the logger rather than by discipline at the call sites, so a handler added later inherits
it. It also redacts anything *shaped* like a key (a long hex run) that was never registered,
because the failure mode being prevented is an operator pasting a log into an issue tracker.
`logging.redact_secrets=false` is parsed and then ignored with a warning: a switch that turns
off redaction is a switch that eventually gets left off. JSON to file, human-readable to
console, neither derived from the other by parsing.

**Skips are logged, not swallowed.** The bot is designed to reject almost everything, so
"3400 bars, 3390 skipped at the regime stage" *is* the finding — and without the stage that
refused, it is unactionable.

**Empty input yields NaN, never zero.** A win rate of 0.0 means every trade lost; "no trades"
means nothing was learned, and collapsing the second into the first is how an untested
strategy reads as a catastrophic one — or worse, the reverse. Profit factor with no losses is
`inf`, not a large number. `--stats` shows losses with the same prominence as wins, surfaces
tripped kill-switches, reports liquidation distance (the number that decides survival at
100x, and not derivable from PnL), and — reusing Phase 9's threshold — refuses a verdict
below 1000 trades instead of dressing a small sample as an edge. Nothing is annualised or
extrapolated.

Storage and reporting only: no module here imports `exchange`, `execution` or `aiohttp`, and
a test asserts it.

61 new tests, **858 total**, no network.

## What Phase 12 added — testing the seams, not the layers again

Phases 2-11 each tested their own module and passed. That is not the same as the layers
agreeing with each other, and this repo had already shipped one defect of exactly that
kind (`bd7977c`): the sizer capped a stop onto the liquidation ceiling that the guard then
rounded past, so two individually-correct layers composed into an intermittent veto.

So this phase tests what no single phase owns:

- **The suite is now offline by construction.** Every phase claimed "no network"; nothing
  enforced it, and this machine can reach the internet. `tests/conftest.py` installs an
  autouse guard that fails any test opening a socket or resolving a hostname. Verified by
  probe: a real `aiohttp` call to the live Gate.io endpoint is blocked before it leaves.
- **Cross-layer integration** (`test_integration.py`) — the composed stack with the real
  objects on both sides of each seam, including the Phase 10 → 11 handoff that nothing
  tested: a paper run's own trades stored and rendered through the real dashboard, which is
  the path `--stats` uses.
- **A repo-wide safety audit** (`test_safety_audit.py`) — all eight switch combinations
  driven through the real config, the real write-guard and the real order manager; every
  state-changing REST method enumerated *from the source* so a method added later cannot
  quietly skip the guard; and the no-order-path property asserted structurally for every
  non-trading module.
- **The six core invariants, swept** (`test_invariants.py`) — over a deliberately hostile
  grid (prices across five orders of magnitude, dead to violent volatility, both
  directions, four account sizes) rather than the one fixture that made each convenient.
- **Regressions for defects actually shipped** (`test_regressions.py`) — each pinned so it
  fails when the *fix* is reverted, which is not what the original tests asserted. The
  post-only entry is the clearest case: the Phase 10 test allows `limit <= mark`, which the
  defect satisfies.
- **The CLI** (`test_cli.py`) — `main.py` had no tests at all. Exit codes, the gate
  display's agreement with the resolved config, and the phase table's agreement with this
  README.

**It found one real defect.** The sizer→guard sweep caught a plan the guard rejected as
`stop_side`: where the entry price is off the order grid and the tick is wide relative to
the stop, rounding "toward entry" overshot to the *far side* of it — a long holding a stop
**above** its own entry, reported as healthy because the distance calculation takes an
absolute value. `resolve_stop` now checks the side on the price before deriving a distance
from it, and refuses with `price_grid`. No guard was loosened to accommodate it.

Each new assertion was mutation-checked: the defect was reintroduced and the test confirmed
to fail. A test that cannot fail is a comment.

170 new tests, **1028 total**, no network and no real order.

## What Phase 13 added — grading the paper run, and a defect that made it impossible

The roadmap gates live trading on one sentence: paper trading must have **run correctly**,
and the edge must **not merely be an artifact of leverage**. Those are two claims that fail
in different ways, so this phase keeps them apart.

- **"Ran correctly" is about the machine, not the money.** Answerable from a handful of
  trades: was every filled entry protected, did anything get liquidated, did one loss
  exceed what the sizer budgeted, does the ledger reconcile with the account, did the run
  end flat. A run that *lost money* while obeying every invariant passes. A run that *made
  money* while leaving one position unprotected does not. **None of these has a
  configurable threshold** — a safety property you can tune off is not one.
- **"Not an artifact of leverage" is about R.** Size is `risk / stop_distance`, so a
  trade's R-multiple is what it earned per unit of *risk*, which leverage cannot move.
  Expectancy in R is the only expectancy worth gating on.

**Which is when the second claim turned out to be unanswerable.** `TradeRecord.from_paper`
reads each field with a `getattr` default, and `PaperTrade` never defined `r_multiple` — so
every paper trade ever stored had `r_multiple = 0.0`. `expectancy_r` is the mean of that
column, and a positive verdict requires it above zero, so **a profitable paper run that
reached the 1000-trade threshold could only ever have been graded NEGATIVE.** `margin` and
`liquidation_price` vanished the same way. The adapter had even computed the right
denominator and discarded it, in a dead local that was also wrong — it omits the quanto
multiplier, a factor of 10,000 on BTC. R now comes from the producer, which knows the
contract; the adapter still refuses to invent one, and an unmeasurable loss budget counts
as a *failed* check rather than a satisfied one.

Phase 12 missed it because the adapter's test passes a hand-written stand-in that *does*
define `r_multiple`. The stand-in was more complete than the real producer.

**The equity curve had no writer either.** `record_equity` shipped in Phase 11 and nothing
outside the tests ever called it, so `--stats` reported "max drawdown 0.00%" for every run.
Sessions now sample it **mark-to-market** — at 100x the drawdown that decides survival
arrives through an open position's mark price, before any fill, and a curve built from
realised equity cannot show it.

**Withheld is not passed.** Below the 1000-trade threshold the edge is withheld, not
graded — the same refusal Phase 9 and Phase 11 make, reading the same config key. A run
that filled nothing passes every conduct check vacuously, so `exercised` stops silence
reading as success. And reading stored history is deliberately weaker than watching a run:
the database records outcomes, three conduct properties are events, and those are withheld
rather than assumed — so `--validate` over history alone can never reach VALIDATED.

**A pass is not permission.** The report writes no config and sets no flag; a test asserts
the module's source contains no `DRY_RUN`, no `live_enabled =` and no `os.environ`.
Enabling live trading is Phase 14's decision and a human's.

48 new tests, **1076 total**, no network and no real order.

## What Phase 14 added — the readiness audit, and why it says no

Every phase before this one was built to *refuse*. This is the first whose success
condition is *permitting* something, which makes it the one place a mistake is expensive in
the direction that matters. So it ships as an audit, not a switch.

`execution/preflight.py` turns the roadmap's own sentence — *live trading is not enabled
until paper trading has run correctly and backtesting shows the edge is not merely an
artifact of leverage* — into something executable, because a precondition that lives only in
prose is one that gets skipped at 2am by someone sure they remember it. It reads state,
compares it against conditions the repo already committed to, and returns GO or NO-GO.

**Run it today and it returns NO-GO**, which is the correct answer:

```
evidence  [ FAIL ] paper_validated   no paper trades have ever been recorded
          [ FAIL ] backtest_edge     no backtest history is stored
gate      [ FAIL ] three_switches    shut — the correct resting state
          [ pass ] martingale, averaging_down, post_only_entry
account   [UNKNOWN] reachable        not read; preflight will not assume a balance
risk      [ pass ] breakers_clear
```

Four design points:

- **Unknown blocks.** In Phase 13 `INSUFFICIENT` withheld a verdict and left conduct clean.
  Here it refuses. "We could not establish this" and "this is fine" must not share an
  outcome when the next step is real money at 100x.
- **Stored history alone can never reach GO.** Three of Phase 13's conduct checks are
  *events* — was a position ever carried unprotected, does the ledger reconcile against an
  independent account figure, did the run end flat — and a trade table cannot testify to
  them. Clearing them needs an observed validation report from a *supervised* paper run.
  That is the intended path to live, and it requires a human to have watched it run.
- **Starting flat is not a preference.** Size, stop and liquidation distance all describe a
  position this bot opened; inheriting someone else's leaves every one of those numbers
  describing something that does not exist.
- **A GO authorises nothing.** The verdict says so in words. Live trading still requires
  `DRY_RUN=false` **and** `--mode live` **and** `--confirm-live`, typed by a human who read
  the report. Preflight cannot open the gate: a test parses the module's AST and asserts it
  contains no environment write and no assignment to any gate attribute.

**No live trading loop was built, deliberately.** The phase is *readiness*, and the repo's
own precondition is unmet — so a runner would be code the rules say must not execute.
`--mode live` runs the audit and reports; there is no code path in this repository that
sends a real order, and `config.py`, `exchange/gate_client.py`, `execution/order_manager.py`
and `execution/protection.py` are unchanged by this phase.

30 new tests, **1106 total**, no network and no real order.

## What Phase 15 added — the live trading runner, and the last reasons it stays locked

Phase 14 built the audit; this phase builds the thing the audit guards. `live/loop.py` is the
paper stack pointed at the real exchange: the same veto chain (market data → risk breakers →
signal → size → liquidation guard → entry → protect), but construction **requires** an open
safety gate and a live client — the exact mirror of `PaperTrader`, which refuses both. There
is no flag that runs it against a closed gate and no path that falls back to the simulator.

**Preflight is the runner's own first act.** `run_live` re-reads account, positions and
resting orders through the Phase 2 client, runs the Phase 14 audit on what it actually found,
and returns exit code 3 on a NO-GO before any trader exists. Exit codes are the contract a
supervisor reads: 0 ran, 1 runtime failure, 2 refused configuration, 3 preflight NO-GO.

**The exchange is the only source of truth, read every step.** Equity is taken from the
account, position size is re-read after every fill and on every step, the protective orders
are re-read until verified, and the post-fill liquidation price is the exchange's own. Fills
are recorded with `mode="live"`, so a live run can never masquerade as paper evidence to the
Phase 13/14 audit. The dead-man switch is re-armed while any exposure is held, and the
session calendar (`risk/session_guard.py`) — new — force-flattens non-24/7 synthetics before
their venues close and blocks entries inside the close/open windows, so a weekend gap can no
longer jump past a 0.125% stop.

**The loop cannot lose track of a position, and cannot double-enter one it never saw.** A
fill whose stop cannot be verified is tracked (not abandoned): the dead-man stays armed, the
position is settled and re-protected every step until it is protected or gone, and no second
entry is ever attempted on top of it. A position on the exchange that the loop does not
track — opened manually, or orphaned by a previous run — blocks entries until it is gone
(preflight refuses it at start; the loop refuses to enter on top of it mid-run). If the
flatten misses its window, the loop keeps attempting it while the venue is closed instead of
giving up and letting the position ride the gap. A transient read failure defers settlement
rather than killing the loop, and an unexpected step error stops the loop the same way a
Ctrl-C does: the dead-man countdown is disarmed on *every* stop path, so a monitored
position never becomes a naked one 60 seconds after the bot stops.

**A supervised run's testimony is now stored, not lost with the process.** Phase 13's conduct
checks — was a position ever carried unprotected, does the ledger reconcile, did the run end
flat — are events a trade table cannot answer, which blocked preflight forever. A watched
paper run now persists its observed evidence to the same database (table `session_evidence`)
and `--preflight` re-grades it on read; corrupt or version-mismatched evidence refuses rather
than half-reads.

**Two deliberate commands finish the path.** `--connectivity` proves the real credentials
sign and the account is readable — balance, positions, open orders, contract, mark price,
risk tiers — placing nothing. `--verify-live-order --symbol BTC_USDT` is the FINAL barrier:
it places **one** real market order of the minimum contract size (market, deliberately, so
the fill is guaranteed), immediately protects it with a verified stop-loss, closes it, and
records the round trip as `mode="live"` — the complete submit → acknowledge → fill →
protect → monitor → close → record lifecycle witnessed on the real account, only after the
operator types `BTC_USDT SEND` at the prompt. It refuses a shut gate, empty credentials, a
missing symbol, the simulator, a wrong confirmation, or a non-flat/unfunded/unreachable
account, an untradable contract, an unreadable market (no order is confirmed against a
price nobody could read), or a leverage that cannot be set — and nothing, not even the
leverage write, happens before the typed confirmation. The post-confirmation sequence is
exception-guarded: any failure after an order may exist routes through a recovery path
that market-closes what is open, leaves any resting stop in force, and disarms the dead-man
countdown so nothing is cancelled behind the operator's back — then reports exactly what it
knows instead of claiming success. Stopping the live loop with an open position releases
the dead-man countdown (instead of letting it cancel the resting stop-loss), so a deliberate
stop never turns a protected position into a naked one.

**The bot is LIVE EXECUTION READY BUT LOCKED.** Every layer through order placement exists and
is tested; no code path can send a real order unless `DRY_RUN=false` **and** `--mode live`
**and** `--confirm-live` are all deliberately set *and* preflight reports GO over real
account reads. Today preflight is NO-GO (no paper/backtest evidence has been accumulated),
credentials are empty, and the gate is shut.

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
risk/       risk_manager.py  position_sizer.py  liquidation_guard.py  session_guard.py
execution/  order_manager.py  protection.py  preflight.py
paper/      loop.py  validation.py
backtest/   engine.py
database/   models.py
live/       loop.py
monitoring/ logger.py  dashboard.py
tests/      test_config.py  test_gate_client.py  test_websocket.py  test_indicators.py
            test_regime.py  test_scoring.py  test_signal_engine.py
            test_position_sizer.py  test_risk_manager.py  test_liquidation_guard.py
            test_execution.py  test_backtest.py  test_paper.py  test_monitoring.py
            conftest.py  test_integration.py  test_safety_audit.py
            test_invariants.py  test_regressions.py  test_cli.py  test_validation.py
            test_preflight.py  test_live.py  test_session_guard.py
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
| 10 | Paper-trading loop (wiring the layers end to end) | **done** |
| 11 | Persistence + logging + analytics (`database/`, `monitoring/`) | **done** |
| 12 | Testing | **done** |
| 13 | Paper trading validation | **done** |
| 14 | Live readiness (audit only — no live runner) | **done** |
| 15 | Live trading runner (`live/loop.py`, wired to `--mode live`) | **done** |

Each phase must implement, pass tests, and be reviewed before the next begins. Live trading is not
enabled until paper trading has run correctly and backtesting shows the edge is **not** merely an
artifact of leverage.

**That condition is currently unmet, and live trading is disabled.** Phase 15 delivered the
runner that would execute it (`live/loop.py`), still behind the audit that checks it
(`--preflight`) — no paper or backtest history has been accumulated, so preflight returns
NO-GO. All 15 phases are complete; the bot is LIVE EXECUTION READY BUT LOCKED, and no code
path in this repository can send a real order unless all three switches are open *and*
preflight reports GO.
