# PHASE 1 — Environment, Verified API Facts, Architecture

Status: **PHASE 1 only.** No trading logic implemented yet.
All numbers below were pulled live from Gate.io on 2026-08-09, not from memory.

---

## 1. Runtime environment

| Item | Value |
|---|---|
| Python | 3.14.4 |
| pip | 25.1.1 (installed via `apt-get install python3-pip python3-venv`; not present initially) |
| venv | `/root/gate-extreme-bot/.venv` |
| Cores | 8 |
| Disk free | 185 G |
| SQLite | 3.46.1 (stdlib `sqlite3`) |
| Network | api.gateio.ws reachable, pypi.org reachable |

Dependencies installed **and import-verified** on 3.14: aiohttp 3.14.3, websockets 17.0.1,
numpy 2.5.2, pandas 3.0.5, PyYAML 6.0.3, python-dotenv 1.2.2, rich 15.0.0, pytest 9.1.1.

Rejected libraries, with reason:
- **TA-Lib** — needs a system C library; indicators will be plain numpy/pandas.
- **ccxt** — hides the contract-size / isolated-margin / risk-tier semantics this bot must control exactly.
- **gate_api** (official SDK) — synchronous urllib3, would block the asyncio loop. We reuse its
  **signing algorithm** (read from source) but issue requests via aiohttp.

---

## 2. Verified Gate.io API facts

Source: live `api.gateio.ws` responses + official `gateio/gateapi-python` generated docs.
Base `https://api.gateio.ws/api/v4`, settle currency `usdt`.

**Auth** (confirmed against `gate_api/api_client.py`, then smoke-tested live):
```
sign_string = METHOD \n /api/v4<path> \n <query_string> \n SHA512(body_hex) \n <unix_ts>
SIGN        = HMAC_SHA512(secret, sign_string)
headers     = KEY, SIGN, Timestamp
```
The signed path **includes** the `/api/v4` prefix. Smoke test with a dummy key returned
`401 INVALID_KEY` — not a signature error — which confirms the request shape parses correctly.

**Endpoints that this bot will use** (all confirmed to exist):
```
GET  /futures/{settle}/contracts[/{contract}]      contract spec + leverage_max
GET  /futures/{settle}/risk_limit_tiers            tiered maintenance_rate  <-- critical
GET  /futures/{settle}/candlesticks                interval, limit (max 2000 pts)
GET  /futures/{settle}/order_book                  spread / depth
GET  /futures/{settle}/tickers
GET  /futures/{settle}/accounts                    balance, available, margin_mode
GET  /futures/{settle}/positions[/{contract}]      entry_price, liq_price, leverage, margin
POST /futures/{settle}/positions/{contract}/leverage
POST /futures/{settle}/positions/{contract}/margin   <-- key to the design, see §4
POST /futures/{settle}/positions/{contract}/risk_limit
POST /futures/{settle}/orders                      entry
GET  /futures/{settle}/orders/{order_id}           fill verification
POST /futures/{settle}/price_orders                SL / TP (price-triggered)
DELETE /futures/{settle}/price_orders[/{order_id}]
POST /futures/{settle}/countdown_cancel_all        dead-man switch
```

**Order semantics** (from `FuturesOrder` schema):
- `size` is an **integer count of contracts**; **positive = long, negative = short**. Not a coin amount.
- Coin amount = `size × quanto_multiplier`. BTC = 0.0001 BTC/contract, ETH = 0.01, SOL = 1, XRP = 10.
- Market order = `price="0"` with `tif="ioc"`.
- `tif="poc"` = post-only, guaranteed maker fee.
- `reduce_only=true` for all exits; `close=true` with `size=0` closes the whole position.
- `text` must be prefixed `t-` — will be used for **idempotency keys** to prevent duplicate orders on retry.
- Stop-loss goes through `/price_orders` with `trigger.price_type`: `0`=last, `1`=mark, `2`=index.
  **We will use `1` (mark price)** — liquidation is computed off mark price, so the SL must reference
  the same series it is racing against.

**Fees (identical across all 899 contracts, live-verified):**
- taker `0.00075` (0.075 %)
- maker `-0.0001` (**negative — a 0.01 % rebate**)

That maker rebate is the single most valuable fact found in this phase. See §5.

---

## 3. Leverage reality — this changes the pair universe

Of **899 tradable USDT contracts**:

| leverage_max | pairs |
|---|---|
| 200x | **2** |
| 100x | 29 |
| 75x | 71 |
| 50x | 196 |
| 25x | 162 |
| 20x | 352 |
| other | 87 |

- `>= 100x` → **31 pairs**
- `>= 125x` → **2 pairs** (BTC_USDT, ETH_USDT only)
- `>= 150x` → **2 pairs** (same two)

**`LEVERAGE=150` is only possible on BTC and ETH. Nothing on Gate.io offers exactly 125x or 150x
as a max — it is 200x or 100x.** The config value is still usable (150 ≤ 200), it just restricts
the universe to two pairs.

Breaking down the 31 pairs that allow ≥100x:

| Class | Pairs |
|---|---|
| **Crypto, 24/7** | BTC, ETH (200x) · SOL, XRP (100x) |
| Gold tokens | PAXG, XAUT |
| Tokenized equities | AAPLX, AMZNX, GOOGLX, METAX, MSFT, NVDAX, SPCX, TSLAX, TSM |
| Indices | HK50, JPN225, NAS100, SPX500, UK100, US30 |
| FX | EURUSD, GBPUSD |
| Commodities | BZ, CL, NG, XAG, XAU, XCU, XPD, XPT |

**The genuine 24/7 high-liquidity crypto universe at ≥100x is 4 pairs: BTC, ETH, SOL, XRP.**
Everything else is a synthetic with exchange session hours, gap risk over weekends/closes, and
thinner books — actively hostile to 100x. The scanner will default to those 4 and treat the rest
as opt-in.

**Maintenance rate is tiered, not fixed.** BTC_USDT tier 1 (notional ≤ 500 k USDT) is
`maintenance_rate=0.003`, and it *rises* with position size (tier 5 = 0.005, tier 8 = 0.01).
`leverage_max` also *falls* per tier (tier 1: 200x → tier 8: 50x). The contract-level
`maintenance_rate` field is only the tier-1 value. The liquidation guard must read
`/risk_limit_tiers` and select the tier matching actual notional — using the flat field would
under-estimate liquidation risk on larger size.

---

## 4. The core conflict in the spec, and how it is resolved

Liquidation distance for an isolated position, as a fraction of entry price:

```
liq_distance ≈ 1/leverage − maintenance_rate − taker_fee
```

Applied to the requested config (`LEVERAGE=100`, `MIN_LIQUIDATION_BUFFER=0.5%`):

| Pair | lev | mmr | fee-aware liq distance | max SL allowed by 0.5 % buffer |
|---|---|---|---|---|
| BTC/ETH | 100x | 0.30 % | 0.625 % | **0.125 %** |
| SOL/XRP | 100x | 0.50 % | 0.425 % | **-0.075 % → impossible** |
| BTC | 125x | 0.30 % | 0.425 % | **-0.075 % → impossible** |
| BTC | 150x | 0.30 % | 0.292 % | **-0.208 % → impossible** |
| BTC | 200x | 0.30 % | 0.125 % | **-0.375 % → impossible** |

Three requirements in the spec cannot all hold at once:

1. §2 — leverage ≥ 100x
2. §4 — stop-loss at least 0.5 % clear of liquidation
3. §9 — "jangan membuat SL terlalu dekat sehingga terkena noise"

At 100x on BTC the only SL that satisfies (1)+(2) is **0.125 %** — which is roughly one 1-minute
candle of BTC noise, so it violates (3). On SOL and XRP, and at any leverage above 100x,
**no stop-loss satisfies the buffer at all** and a literal implementation would reject 100 % of
trades. And the fee damage at that stop distance is brutal: 0.15 % round-trip taker against a
0.125 % stop = **1.2 R lost to fees per trade**, requiring a **73 % win rate just to break even**.

**Resolution — DECISION (user, 2026-08-09): pure 100x, no margin top-up, buffer lowered to 0.30 %.**

> **Amendment (same day).** `liquidation_buffer` was lowered from 0.50 % to 0.30 % specifically to
> admit the `maintenance_rate` 0.50 % contracts. Live-verified effect at 100x:
>
> | tier | contracts | liq distance | max stop | fee drag | break-even WR |
> |---|---|---|---|---|---|
> | mmr 0.30 % | BTC_USDT, ETH_USDT | 0.625 % | **0.325 %** | 0.20 R | **40.0 %** |
> | mmr 0.50 % | the other 29, incl. SOL, XRP | 0.425 % | **0.125 %** | 0.52 R | **50.7 %** |
>
> **All 31 pairs now pass the guard** (previously 2 of 31). BTC/ETH also gained 2.6x more stop room
> than under the 0.50 % buffer, which cut their break-even win rate from 50.7 % to 40.0 % — the
> buffer change improved the two pairs that already worked, not just the 29 that didn't.
>
> Accepted cost: **less room between stop and liquidation.** A sharp mark-price spike is now more
> likely to reach `liq_price` before the resting stop fills, and the mmr-0.50 % pairs sit only
> 0.125 % from their ceiling. The buffer is protection, not immunity. Everything else was kept:
> 100x, isolated margin, no margin top-up, mandatory protective SL, circuit breakers active.
>
> Side effect: 125x became mathematically reachable (0.125 % of stop room on BTC/ETH). **We stay at
> 100x by decision**; `test_125x_became_reachable_but_is_not_configured` pins this so a future bump
> is deliberate. 150x and 200x remain unreachable and are still rejected at config load.

Original resolution follows.

The margin-top-up route (set 100x, then post extra isolated margin via
`POST /positions/{contract}/margin` to push liquidation away) was offered and **declined**. The bot
therefore runs at a true 100x effective leverage, and the following consequences are accepted
deliberately rather than discovered later:

1. **The stop-loss ceiling is per-contract, resolved at runtime** from the contract's tiered
   `maintenance_rate`: 0.325 % on BTC/ETH, 0.125 % on the other 29.
   `stop_loss.on_sl_exceeds_max: cap` clamps any ATR- or structure-derived stop to that ceiling.
   At 0.125 % the stop is noise-width; frequent stop-outs there are expected, not a malfunction.
2. **All 31 pairs are tradable** at a 0.30 % buffer, including SOL and XRP. The mmr-0.50 % pairs are
   materially harder than BTC/ETH — half the stop room and 2.6x the fee drag — so
   `signal.min_score` should arguably be *higher* for them. Deferred to Phase 5.
3. **Leverage above 125x is impossible** at a 0.30 % buffer (150x → liq 0.292 %, 200x → 0.125 %,
   both ≤ buffer). `config.py` rejects it at load time with an explicit "unreachable configuration"
   error rather than silently skipping every trade at runtime. 125x is reachable but not
   configured.
4. **Post-only entry is mandatory, not optional.** Fee drag scales inversely with stop width:
   at a 0.125 % stop, taker entry costs 1.20 R and needs a **73.3 %** win rate to break even, while
   post-only costs 0.52 R and needs **50.7 %**. Validation rejects `entry_tif: ioc` whenever
   `leverage.default >= 100`. The trade-off is that post-only entries often will not fill — an
   unfilled entry is a normal, frequent outcome.

The guard still solves and reports the required margin for each candidate; with top-up disabled it
uses that figure only to decide *skip vs trade*. Re-enabling `allow_margin_topup` later requires
`max_effective_leverage < leverage.default`, which validation enforces.

Buffer sensitivity on the mmr-0.50 % contracts (liq distance 0.425 %):

| buffer | max stop | status |
|---|---|---|
| 0.50 % | −0.075 % | untradable (the original setting) |
| 0.40 % | 0.025 % | technically fits, economically absurd |
| **0.30 % (current)** | **0.125 %** | **tradable** |
| 0.20 % | 0.225 % | more room, thinner liquidation margin |

### Session-gap risk (new, from the 31-pair universe)

23 of the 31 pairs are synthetics — tokenized equities, indices, FX, commodities — with real
trading sessions. **A stop-loss cannot execute while the venue is closed.** A weekend or overnight
gap jumps straight past a 0.125 % stop, so the loss is bounded by *liquidation*, not by the stop,
and risk-per-trade does not hold across a gap. `session_guard` therefore force-flattens synthetics
30 min before close, blocks entries 60 min before close and 15 min after open, and
**fails closed when the session calendar is unavailable** (validation enforces this).

---

### For reference: the rejected alternative

Had margin top-up been enabled, the numbers would have been:
```
notional          = 50 %   of equity
margin @100x      = 0.500 % of equity   -> liq 0.625 % away  -> FAILS (needs 1.0 %)
required eff lev  = 72.7x
margin to post    = 0.688 % of equity   -> liq 1.054 % away  -> PASSES
extra margin      = 0.188 % of equity   (collateral, not risk)
SL                = 0.50 % (ATR-derived, survives noise)
max loss on stop  = 0.25 % of equity    (identical either way)
break-even WR     = 37.7 %  (vs 50.7 % at pure 100x)
```
Risk per trade is 0.25 % in **both** designs — leverage never changed the risk, only the locked
margin, the stop width, and therefore the fee drag.

Max exchange leverage that still satisfies a 0.5 % buffer, by stop distance:

| SL distance | BTC/ETH (mmr 0.30 %) | SOL/XRP (mmr 0.50 %) |
|---|---|---|
| 0.125 % | 100.0x | 83.3x |
| 0.25 % | 88.9x | 75.5x |
| 0.50 % | 72.7x | 63.5x |
| 1.00 % | 53.3x | 48.2x |

---

## 5. Fee math drives the entry design

Round-trip cost as a multiple of R, and the resulting breakeven win rate at RR=2:

| Entry / exit | SL | fee cost | net win | net loss | breakeven WR |
|---|---|---|---|---|---|
| taker / taker | 0.125 % | 1.20 R | +0.80 R | −2.20 R | **73.3 %** |
| maker / taker | 0.125 % | 0.52 R | +1.48 R | −1.52 R | 50.7 % |
| taker / taker | 0.50 % | 0.30 R | +1.70 R | −1.30 R | 43.3 % |
| **maker / taker** | **0.50 %** | **0.13 R** | **+1.87 R** | **−1.13 R** | **37.7 %** |

Two design rules fall directly out of this table:

- **Entries default to post-only (`tif="poc"`).** The maker rebate is negative — the exchange pays
  0.01 %. Combined with a workable stop this cuts fee drag from 1.2 R to 0.13 R. The cost is that
  post-only entries can fail to fill; the engine must treat an unfilled entry as a normal,
  frequent, non-error outcome and cancel cleanly.
- **Exits are taker.** A stop-loss must fill, so it eats 0.075 %. This is non-negotiable and is
  budgeted into breakeven and into the break-even-stop buffer (§12 needs ≥ 0.085 % of movement
  before BE is even meaningful).

---

## 6. Architecture

```
main.py  (CLI: --mode paper|backtest|live|--status|--positions|--stats, --confirm-live)
   |
config.py ---- config.yaml + .env  (pydantic-free dataclass validation, fail-closed)
   |
   +-- exchange/gate_client.py   aiohttp REST, HMAC-SHA512 signing, token-bucket rate limit,
   |                             exponential backoff, `t-` idempotency keys, contract-spec cache
   +-- exchange/websocket.py     fx-ws.gateio.ws/v4/ws/usdt — tickers, book, orders, positions;
   |                             heartbeat, auto-reconnect, forced REST resync on reconnect
   |
   +-- strategy/indicators.py    EMA 9/21/50/200, RSI, MACD, ATR, ADX, VWAP, volume, S/R  (numpy)
   +-- strategy/regime.py        TRENDING/RANGING/HIGH_VOL/LOW_VOL/BREAKOUT/BREAKDOWN, else SKIP
   +-- strategy/scoring.py       trend 25 / momentum 20 / volume 15 / PA 20 / vol 10 / S-R 10
   +-- strategy/signal_engine.py MTF 1m+5m+15m+1h, BTC filter, threshold >= 80
   |
   +-- risk/position_sizer.py    size = (equity x risk) / stop_distance, then floor to contract
   |                             granularity, check min/max order + available margin
   +-- risk/liquidation_guard.py tiered mmr from /risk_limit_tiers, required-margin solver (§4),
   |                             post-fill verification against exchange liq_price
   +-- risk/risk_manager.py      daily loss 1 %, drawdown 3 %, 3 consecutive losses,
   |                             max 1 open position, kill-switch latch (persisted)
   |
   +-- execution/order_manager.py  state machine, fill verification, no-assume-success
   +-- execution/protection.py     SL-first invariant, retry, emergency flatten, trailing, BE, TP1/2/3
   |
   +-- backtest/engine.py        bar-replay w/ fees, slippage, intrabar liquidation sim, walk-forward
   +-- database/models.py        SQLite, WAL, full trade record per §21
   +-- monitoring/logger.py      structured JSON + rich console, secrets redacted
   +-- monitoring/dashboard.py   equity, DD, PF, expectancy, liq distance, not just PnL
```

**Central safety invariant, enforced in `execution/protection.py`:**

> A position may not exist without a verified stop-loss.

Sequence: place entry (post-only) → poll until filled/cancelled → **immediately** place SL →
**re-read** the SL from the exchange to confirm it is live → only then place TP ladder.
If the SL cannot be confirmed after bounded retries, **market-close the position** (`close=true`,
`reduce_only=true`) rather than leave leveraged size unprotected. `countdown_cancel_all` is armed
as a dead-man switch so that a bot crash does not leave stale orders.

**Kill-switch is persisted to SQLite**, not held in memory — a process restart must not reset a
tripped daily-loss or drawdown limit.

**DRY_RUN is fail-closed**: live order submission requires `DRY_RUN=false` in `.env` **and**
`--mode live` **and** `--confirm-live`. Any one missing → simulation. The client will refuse to
issue a POST unless all three align.

---

## 7. Honest assessment before we build further

- Leverage does not create edge. At 100x with a 0.5 % stop, the account risks the same 0.25 % it
  would at 20x — leverage only reduces locked margin. The **only** thing that makes this
  profitable is whether the score-80 filter actually selects trades winning above ~38 % at RR 2.
  Phase 9 backtesting exists to answer that, and it may well answer "no."
- A score ≥ 80 across 6 categories on 4 timeframes will be **rare**. Expect long idle periods.
  That is the design working as specified (§30), not a bug.
- Funding is charged every 8 h. At 50 % notional it is small but not zero, and it will be modelled
  in the backtest rather than ignored.
- No stop-loss survives a genuine gap. Mark-price SL at 0.5 % on BTC is safe in ordinary
  conditions and is *not* safe through an exchange-wide liquidation cascade. The 0.5 % buffer plus
  effective-leverage reduction is what buys survival room; it is mitigation, not immunity.
- I will not claim any win rate. The metrics in §22 are reported as measured, including losses.

---

## 8. Phase 1 exit criteria — met

- [x] Environment inspected; pip/venv bootstrapped; all deps installed and imports verified on 3.14
- [x] Endpoints, auth, order semantics verified against official sources — nothing invented
- [x] Real leverage limits enumerated (31 pairs ≥100x; 150x = BTC/ETH only)
- [x] Real fees confirmed (maker rebate −0.01 %) and folded into the design
- [x] Tiered maintenance-rate discovered; flat-field trap identified
- [x] Spec's leverage/buffer/stop contradiction found, quantified, and resolved
- [x] Skeleton, `config.yaml`, `.env.example`, `.gitignore`, pinned `requirements.txt` created

---

## 9. PHASE 2 — REST client (`exchange/gate_client.py`)

### Safety properties

**The write-guard is the point of this module.** Every POST/PUT/DELETE/PATCH is refused
unless `Config.live_enabled` is True, and the refusal happens *before* any socket is opened —
`WriteBlocked` is raised, `stats.writes_blocked` increments, and the network is never touched.
There is exactly one call site that reaches the network (`_request`), so no method can bypass it.
This is an independent second barrier behind the config gate: a bug elsewhere in the bot cannot
produce a live order on its own.

Reads remain available while the gate is closed — paper trading must still observe the market and
the account.

**Retry policy is asymmetric by design.** Reads retry on 429/5xx/network errors with exponential
backoff plus up to 25 % jitter (jitter breaks retry convoys after an exchange hiccup). Terminal
error labels — `INVALID_KEY`, `INSUFFICIENT_AVAILABLE`, `LIQUIDATE_IMMEDIATELY`, `ORDER_NOT_FOUND`
and others — are never retried, because they cannot succeed on a second attempt.

Writes carry a caller-supplied `t-` idempotency key, so a duplicate submission is *detectable*
after a timeout. A network failure on a write that lacks that protection raises
`NETWORK_UNCERTAIN` rather than retrying: the first attempt may already have filled, and at 100x a
double-fill is not a recoverable mistake. The caller must reconcile against the exchange.

Each retry attempt is re-signed with a fresh timestamp; reusing the original would be rejected as
`REQUEST_EXPIRED`.

### Verified against the live API

| Check | Result |
|---|---|
| Public reads (contract, tiers, book, candles, mark candles) | 200, parsed correctly |
| Private reads (accounts, positions, orders, price_orders) | 401 **INVALID_KEY** — signature accepted, only the dummy key rejected |
| Terminal errors retried? | No — 0 retries on 401 |
| Write while gate closed | `WriteBlocked`, 0 network calls |
| Risk tiers for BTC_USDT | **19 tiers** returned; mmr rises 0.003 → 0.0035 → 0.0045 as notional grows |
| Live spread on BTC_USDT | 0.02 bps — comfortably inside the 4 bps filter |

The 19-tier result matters: the contract-level `maintenance_rate` is only tier 1. `select_tier()`
resolves the rate from actual notional, and `test_flat_maintenance_rate_understates_risk_at_size`
pins that the flat field is the optimistic one. The liquidation guard in Phase 7 must go through
`select_tier()`, never through `Contract.maintenance_rate`.

### Order semantics encoded

- `size` is a **signed contract count** — positive long, negative short — not a coin amount.
  `Contract.contracts_for_coin_amount()` converts via `quanto_multiplier`, always rounding **down**.
- Market order = `price="0"` + `tif="ioc"`; the client rejects any other combination.
- `size=0` is only valid with `close=True`.
- Stop-losses go through `/price_orders` with `price_type=1` (**mark price**), because liquidation
  is computed off the mark price — the stop must race the same series it is trying to beat. The
  order price is `"0"` (market): a stop that does not fill is not a stop.
- `set_leverage(symbol, 0)` is rejected — on Gate.io `0` selects cross margin, which this bot forbids.

### Tests

89 tests total (43 config + 46 client), no network. A fake session records requests and replays
scripted responses. Coverage includes: all six write methods blocked when the gate is closed,
signature byte-equality against an independent reference implementation, proof that omitting the
`/api/v4` prefix or the query string produces a *different* signature, retry/no-retry paths,
idempotency-key validation, mark-price stop defaults, and tier selection against payloads captured
live in Phase 1.

---

## 10. PHASE 3 — market data + WebSocket (`exchange/websocket.py`)

Endpoint `wss://fx-ws.gateio.ws/v4/ws/usdt`. Phase 1 and 2 decisions are untouched: 100x,
isolated margin, 0.30% liquidation buffer, no top-up, mandatory SL, circuit breakers armed,
write-guard closed.

### Channels

| Channel | Payload | Carries |
|---|---|---|
| `futures.tickers` | `[symbol]` | last, **mark price**, index, funding rate, 24h volume |
| `futures.book_ticker` | `[symbol]` | best bid/ask + sizes, `t` (ms), `u` (update id) |
| `futures.orders` | `[user_id, symbol]` | fills, `finish_as`, `left`, client `text` |
| `futures.positions` | `[user_id, symbol]` | size, entry, `liq_price`, margin, leverage |

Public channels subscribe unsigned. Private channels require
`auth={"method":"api_key","KEY":…,"SIGN":…}` where SIGN is HMAC-SHA512 over
`channel=<ch>&event=<ev>&time=<ts>` — **not** the REST signing envelope. `build_request()`
raises rather than emitting an unsigned private subscribe. Without credentials the feed runs
public-only and `position()`/`order()` fail closed instead of returning empty state.

Verified live: subscribe error codes are `1` = malformed payload, `2` = unknown channel,
`4` = `INVALID_KEY` on a signature mismatch. All three raise `SubscriptionError`.

### Staleness watchdog — fail closed

`is_healthy()` and the guarded accessors share **one** predicate, `_unhealthy_reason()`. The
feed is unusable when: not connected · REST resync pending · no pong within 15s of a 5s ping
cadence · inside the 10s post-connect warmup · book older than 5s · ticker older than 10s.
`book()` and `ticker()`
raise `FeedNotHealthy` in every one of those states, so a signal can never be built from data
the watchdog rejects.

The accessors check *every* stream for a symbol, not just the one requested. Book and ticker
share a socket, so a stale book beside a fresh ticker means a subscription died — the fresh
stream is not evidence the feed is alive. A live run caught the earlier, looser version:
during warmup `is_healthy()` was `False` while `book()` still served data. A parametrised
regression test now asserts accessor and health check agree in all five unhealthy states.

### Reconnect — REST is the source of truth

Backoff is exponential with jitter (25% of the delay), `1s → 60s` cap, and a **reconnect-loop
breaker**: more than 10 attempts inside a 300s window raises `FeedNotHealthy` and stops the loop
rather than hammering the venue. On every disconnect the WebSocket state is discarded — tickers, books, positions, orders, and dedup
cursors are all cleared, because a post-disconnect cache is a guess about the exchange, not a
reading of it.

Order after reconnect is fixed and enforced by test: **connect → subscribe → full REST resync
(positions, open orders, price-triggered orders, contract + risk tiers) → reconcile → only then
mark healthy.** `_resync_pending` stays `True` for the whole window, so accessors refuse
throughout. A failing resync leaves the flag set and retries; it never degrades to trusting the
socket.

### Malformed, duplicate, out-of-order

Per-symbol cursors reject replays: `u` (update id) for books, `time_ms` for tickers, and
`(id, status, left)` for orders. Any regression in those counters increments `stats.duplicates`
or `stats.out_of_order` and drops the frame. Non-JSON, wrong-type, missing-field, null, empty,
and partial frames increment `stats.malformed` and are dropped — `handle_message()` never raises
into the read loop, so one bad frame cannot kill the connection.

### Liquidation tier — interface only

Phase 2 found BTC_USDT has **19 tiers** with maintenance rate rising by notional, so the flat
`contract.maintenance_rate` is not a usable liquidation input. The feed exposes
`await select_tier(symbol, notional)`, which resolves the real tier from cached
`/futures/usdt/contracts/{symbol}/risk_limit_tiers` data. Live: notional 100k → tier 1 mmr
0.003, 800k → tier 2 mmr 0.0035, 2M → tier 4 mmr 0.0045.

`maintenance_rate(symbol)` exists only to **refuse**: it raises `LiquidationTierUnavailable`
pointing at `select_tier()`, so no caller can smuggle a flat rate into a liquidation
calculation. Tests assert the market-data layer cannot bypass tier information, that tiers are
re-fetched on resync, and that an unavailable tier fails closed. The liquidation engine itself
is Phase 7 — this is the integration seam only.

### Tests

171 total (43 config + 46 client + 82 WebSocket), no network. Coverage: connect, subscribe frame
shape, private signing, heartbeat and pong timeout, ticker/book/order/position parsing from
frames captured live on 2026-08-09, stale feed, disconnect, reconnect ordering, state
invalidation, REST resync and reconciliation, duplicate and out-of-order events, malformed and
partial payloads, subscription failure for all three live error codes, reconnect-loop breaker,
and the tier-bypass guards.

**No order was sent in this phase.** The write-guard blocked nothing because nothing was
attempted; the feed is read-only by construction and has no order-placing path.

## 11. PHASE 4 — indicators (`strategy/indicators.py`)

Pure numpy, no TA-Lib (rejected in Phase 1 for needing a system C library). Nothing in this
module decides anything: no signals, no scoring, no thresholds. Those are Phase 5.

### Conventions, chosen so backtest and live share one code path

- **Length-preserving.** Output index *i* always corresponds to bar *i*; nothing is re-aligned
  downstream.
- **NaN means "not enough data", never a number.** Insufficient history yields an all-NaN array
  rather than a partially-warmed value — the same fail-closed rule the market-data feed uses.
- **Pure functions.** No I/O, no config, no state.
- **Wilder smoothing** for RSI and ATR, **SMA-seeded EMA** for EMA/MACD, so an EMA 200 here
  matches a 200 EMA on the chart instead of drifting for hundreds of bars.

`Candles.from_gate()` parses the `/candlesticks` payload as-is — string prices, contract volume
in `v`, settle turnover in `sum`. It does **not** drop the in-progress final bar: whether to use a
forming bar is a strategy decision, not an indicator one.

### Relative volume excludes the current bar from its own baseline

`relative_volume` divides a bar's volume by the average of the `period` bars **before** it, not
by an average that contains it. The inclusive form dilutes exactly the spike it is meant to
measure:

| definition | a true 4x bar reads | bar needed to read 4.0 | ceiling at period=20 |
|---|---|---|---|
| inclusive (bar in its own average) | 3.48x | 4.75x | 20.0 |
| **exclusive (current)** | **4.00x** | **4.0x** | unbounded |

`filters.btc_volume_spike_multiple: 4.0` is written in the intuitive unit — "this bar is 4x
normal". Under the inclusive form that filter needs a 4.75x bar before it suspends alt entries,
so a protective filter fires late or not at all. First valid index is `period`, not `period - 1`,
because the baseline needs `period` prior bars.

### Support / resistance — the lookahead guard

`swing_highs`/`swing_lows` mark strict local extremes: strictly greater (or less) than all `left`
bars before and all `right` bars after. Equality is ambiguous, so a plateau of equal highs yields
**no** pivot rather than turning one flat top into two levels a tick apart. NaN bars compare
False and are never pivots.

A pivot at bar *i* is only **knowable** at bar `i + right`. `nearest_support`/`nearest_resistance`
enforce that delay: a pivot is ineligible until its confirmation bar, and only pivots within
`lookback` bars count (`stop_loss.structure_lookback: 50`). This is the classic backtest
lookahead bug — levels that were not yet discoverable make a strategy look prescient in replay
and fall apart live — and because backtest and live share this code path the guard has to live
in the indicator, not in the caller.

`test_as_of_equals_replaying_on_a_truncated_series` pins it from both directions: what live sees
(`values[:i+1]`) must equal what a backtest sees (`as_of=i` on the full array) at every bar. The
same audit was run against 300 live BTC_USDT 1m candles — **0 mismatches**.

Both functions return **NaN when no level is found**, which a caller sizing a structure stop must
treat as missing data and fall back, never as open space.

### Verified against live BTC_USDT 1m candles (300 bars, 2026-08-09)

| Indicator | Value | Sanity |
|---|---|---|
| EMA 9 / EMA 200 | 65002.12 / 65151.04 | fast below slow — consistent with the down leg |
| RSI 14 | 31.59 | in range, near oversold |
| ATR 14 | 30.58 = **0.047 % of price** | see below |
| MACD / histogram | −56.57 / +1.62 | negative line, histogram turning up |
| VWAP (anchored) | 65213.96 | above last close 64994.8 |
| nearest support / resistance | 64974.9 / 65013.7 | brackets the close |

**The ATR reading matters for Phase 5.** At `stop_loss.atr_multiplier: 1.5`, an ATR of 0.047 %
gives an ATR stop of **0.071 %** — *tighter* than the 0.325 % ceiling BTC/ETH have at 100x, so in
this volatility regime `on_sl_exceeds_max: cap` will not bind on BTC. But it is also below
`stop_loss.min_distance: 0.001` (0.10 %), which would floor it back up to 0.10 %. And
`min_sl_atr_ratio: 0.20` compares the final stop to ATR: 0.10 % / 0.047 % = 2.1x, which passes.
The interaction of the ATR stop, the min-distance floor, and the per-contract ceiling is a Phase 5
question; this phase only establishes that the numbers feeding it are real.

### Tests

122 indicator tests, 293 total, no network. Reference values are recomputed independently inside
the tests — hand-rolled Wilder and EMA recurrences, closed-form VWAP cases — so a bug in
`indicators.py` cannot make its own test pass. Coverage: length preservation and all-NaN warm-up
for every function, first-valid-index for each, EMA SMA-seeding, RSI boundary conventions
(100/0/flat-is-50) and off-by-one alignment, MACD signal seeded only from valid MACD values,
true-range gap terms in both directions, ATR Wilder recurrence, VWAP invariance to the contract
multiplier, rolling-vs-anchored VWAP divergence, relative-volume exclusivity and its unbounded
ceiling, pivot plateau/NaN/edge handling, and the confirmation-delay and backtest-parity guards.

**No order was sent in this phase.** The module has no network path and no order-placing path.

**Next: PHASE 5 — regime, scoring, signal engine.** Complete; see §12.

---

## 12. PHASE 5 — regime, scoring, signal engine (`strategy/`)

Three modules that decide *whether* to trade and *which way*. They stop there: nothing in this
phase sizes, places, or manages an order, and none of them import `exchange/`, so there is no
network path and no order path by construction. A test asserts `SignalEngine` exposes no method
whose name contains `order`, `execute`, or `place`.

```
regime.py       classify(candles, as_of) -> RegimeResult   what kind of market is this?
scoring.py      score(candles, direction, as_of) -> ScoreResult   how good is this setup?
signal_engine.py evaluate(symbol, candles, now, btc) -> Signal    should we act, and which way?
```

### The lookahead problem, and the one rule that closes it

Gate stamps every candle with its **open** time — verified live: 1h stamps are exact multiples
of 3600 and the newest one opened minutes ago. So the newest bar of every series is still
forming, and a bar on interval *T* is complete only at `t + T`.

That single fact causes two distinct bugs, and `closed_bars()` is the only place either is
resolved:

1. **The forming bar.** Scoring it means trading a candle whose close is not yet known; in
   replay it is reading the future outright.
2. **Higher-timeframe leakage.** The 1h bar covering *now* has not closed either. Asking "what
   is the 1h trend?" at 10:05 must answer from the 09:00 bar, not the 10:00 bar that will not
   finish until 11:00.

Live confirmation at 23:47 UTC — each timeframe drops exactly the one forming bar, and the 1h
correctly reports the 22:00 bar rather than the unfinished 23:00 one:

| timeframe | fetched | closed | newest closed bar opened |
|---|---|---|---|
| 1m | 300 | 299 | 23:46:00 |
| 5m | 300 | 299 | 23:40:00 |
| 15m | 300 | 299 | 23:30:00 |
| 1h | 300 | 299 | 22:00:00 |

Every module also takes `as_of` and truncates via `Candles.head()` rather than promising not to
peek, so lookahead is impossible by construction rather than by review. Tests assert bar-by-bar
that what live sees (`head(i+1)`) equals what a backtest sees (`as_of=i`) — for regime, for
scoring, and for the engine end-to-end.

### Regime — volatility is checked first, and that ordering is load-bearing

Six states plus `None` (= SKIP, a first-class answer). Order of tests is not interchangeable:
volatility gates → breakout/breakdown → trend → range → ambiguous.

A textbook EMA stack *during a volatility spike* is precisely the setup that gaps through a
0.125 % stop, so it must not be reachable as TRENDING. The ATR percentile is measured against the
symbol's **own** recent history, because a fixed ATR% threshold would call every synthetic quiet
and every alt violent.

**The percentile uses a midrank, counting ties as half.** Under the obvious
`(history <= now).mean()`, a market with perfectly steady volatility scores 1.0 — every sample
equals the current one — so a calm, regular tape reports as HIGH_VOLATILITY and gets vetoed
exactly when conditions are best. Midrank sends a constant series to 0.5. This was a real bug,
caught by a test asserting a steady market is not a volatility extreme.

Neither volatility regime appears in any `*_allowed` list, so both are effectively a skip; they
are still reported by name because "ATR in its 95th percentile" and "the tape is dead" are
different post-mortems. `HIGH/LOW_VOLATILITY` on **any** timeframe disqualifies, not just the
entry one — a 5m blowout is the same hazard to a 1m entry whether or not the 1m has noticed yet.

ADX was added to `indicators.py` for this: it distinguishes a real trend from an EMA stack that
merely happens to be in order during chop, which a moving-average comparison cannot. Verified
100.0 on clean trends in both directions, 0.0 flat, 4.6 in chop, first valid index `2*period - 1`.

### Scoring — six weighted categories, and what a NaN is worth

Weights come from `strategy.scoring_weights` and each category earns a 0.0-1.0 fraction of its
weight, so the 0-100 bound holds by construction rather than by clamping. Scores are
direction-aware: RSI 68 is strength for a long and exhaustion for a short, so there is no such
thing as a directionless score and `direction=0` is rejected outright.

**A NaN input earns zero, never a midpoint** — an unwarmed indicator is missing information, and
missing information must not accumulate into a passing score.

Two findings from live data changed the design here:

- **The RSI band has a ceiling.** Buying RSI 85 is chasing something already extended; at 100x
  with a 0.125 % stop there is no room to survive the snap-back. Full momentum credit requires
  RSI in 50-72 for a long, not merely "high".
- **Support/resistance had a real bug.** `nearest_resistance` returning NaN was scored as zero
  ("unknown, not clear"), which zeroed the category for *every trade into new highs* — the whole
  breakout playbook — because a monotonic rise contains no pivot highs at all. But the mirror
  case is also true: a monotonic *decline* has no pivot highs either, while overhead supply is
  everywhere. Pivot-shaped structure is therefore the wrong sole measure. When no confirmed pivot
  blocks the way, the scorer falls back to the plainest available fact — the highest high in the
  lookback window, **excluding the current bar**, since a bar's own high is by definition ≥ its
  close and including it means no new high could ever read as clear. A "nothing in the way" claim
  also requires the full lookback horizon behind it; a short window scores zero rather than
  handing thinly-backed symbols 10 free points.

### Signal engine — everything is a veto

Gates run in order, each ending evaluation with a recorded `stage` and `reason`: data → spread →
liquidity → BTC filter → regime → volatility → direction → MTF veto → abnormal candle → score.
The default answer is no. Rejections are auditable — "skipped 400 bars at the spread stage" is
actionable, "no signal" is not.

Higher timeframes decide direction; the entry timeframe only gets a say when they are silent, and
a split among the veto timeframes is no trade. RANGING does not veto (a 1h range is context a 5m
trend can work inside); an opposing *directional* regime does. The BTC correlation filter
**fails closed**: an alt evaluated without BTC candles is refused, not waved through.

The engine holds no cooldown or position state on purpose — those are Phase 6 concerns, and
duplicating them here would create two sources of truth about whether trading is permitted.

### Config

New keys under `strategy.regime`, `strategy.scoring`, `strategy.timeframe_weights`, and
`strategy.veto_timeframes`, all validated fail-closed at load: timeframe weights must cover every
configured timeframe and sum to 1.0 (a weightless timeframe would be dropped from the blend in
silence), a veto timeframe must actually be evaluated (otherwise it could never veto anything),
regime names must be real, ATR percentile bounds must be ordered, and the ADX bands must not
overlap.

### What this phase does *not* establish

It does not show the strategy is profitable. A score ≥ 80 across six categories on four
timeframes is rare by design (§7), and on live BTC/ETH/SOL at 23:47 UTC every symbol was rejected
at the regime stage — 5m and 15m were both in a volatility extreme. That is the filter working as
specified, but whether the surviving trades win above the ~40 % break-even rate is a Phase 9
backtesting question and may still answer "no".

### Tests

97 new tests (27 regime + 42 scoring + 28 engine), **423 total**, no network. Expectations are
derived from hand-built paths whose character is obvious by construction — a monotonic ramp *is*
a trend — so a bug cannot make its own test pass. Coverage: all six regimes reachable, the
volatility-first ordering, midrank tie handling, score bounds and direction-awareness, NaN-earns-
zero, the RSI ceiling, both SR fallback directions, the closed-bar boundary (inclusive at
`t + T`, exclusive a tick before), MTF conflict, BTC fail-closed, and bar-by-bar backtest/live
parity for all three modules.

**No order was sent in this phase.** No module here imports `exchange/`; there is no network path
and no order-placing path.

**Next: PHASE 6 — risk manager.** Not started; awaiting go-ahead.
