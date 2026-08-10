# gate-extreme-bot

High-leverage Gate.io USDT-perpetual futures bot. Built for **selectivity and capital
preservation**, not for trade frequency. When no high-quality setup exists, it does nothing.

> **Status: PHASE 5 of 14 complete.** Environment, REST client, market-data feed, indicators,
> regime detection, signal scoring, signal engine.
> The bot can now decide *whether* and *which way* to trade. It cannot act on that decision:
> sizing and risk begin in Phase 6, execution in Phase 10.
> **Nothing in this repo can place an order.**

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
python main.py --mode paper                  # paper trading           (Phase 8)
python main.py --mode backtest               # backtest + walk-forward (Phase 9)
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
| 6 | Risk manager | next |
| 7 | Liquidation protection | pending |
| 8 | Paper trading | pending |
| 9 | Backtesting + walk-forward | pending |
| 10 | Order execution | pending |
| 11 | Dashboard + database | pending |
| 12 | Testing | pending |
| 13 | Paper trading validation | pending |
| 14 | Live readiness | pending |

Each phase must implement, pass tests, and be reviewed before the next begins. Live trading is not
enabled until paper trading has run correctly and backtesting shows the edge is **not** merely an
artifact of leverage.
