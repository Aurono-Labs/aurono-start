Below is the **full, canonical draft** of **`aurono_v1_behavior.md`**.
This document is written as a **behavioral specification**, not as code commentary.
It describes **what Aurono v1 does**, including edge cases and known imperfections.

You should treat this as **frozen historical truth**.

---

# Aurono v1 — Behavioral Specification (Frozen)

**Status:** Frozen
**Purpose:** Historical reference for Aurono v2 redesign and migration
**Scope:** Decision logic, trade lifecycle, capital semantics, execution semantics
**Non-goal:** This document does *not* justify or optimize v1 behavior

---

## 1. High-Level Operating Model

Aurono v1 operates as a **stateful, cycle-based trading engine**.

* Strategies are evaluated periodically based on timeframe schedules
* State is stored directly in a mutable SQLite database
* Trades, capital, and strategy configuration are tightly coupled
* There is no immutable event log
* History can be overwritten or deleted

Aurono v1 prioritizes:

* deterministic triggers
* capital safety via allocation limits
* pragmatic exchange execution

---

## 2. Strategy Evaluation Lifecycle

### 2.1 Strategy Selection

At each cycle:

* All strategies with `enabled = 1` are loaded
* If a timeframe is provided, only strategies matching that timeframe are evaluated
* Strategies are evaluated sequentially in database row order

If no strategies are found, the cycle exits without action.

---

### 2.2 Exchange Resolution

For each strategy:

* The strategy’s `exchange` field is used
* If missing or empty:

  * A fallback exchange from config (`exchange`) is used
  * A warning is logged
* Exchange objects are cached per runtime

If the exchange cannot be initialized (e.g. missing credentials):

* The strategy is skipped
* No retries occur during the same cycle

---

## 3. Market Data Semantics

### 3.1 Ticker

* A live ticker price is fetched before OHLC evaluation
* If ticker fetch fails or returns ≤ 0:

  * Strategy is skipped for this cycle

Ticker price is used for:

* logging
* volume calculation
* not for trigger evaluation

---

### 3.2 OHLC Fetching

* OHLC data is fetched per strategy timeframe
* At least **3 candles** must be returned
* Data is expected oldest → newest

If insufficient OHLC data is returned:

* Strategy is skipped

---

### 3.3 Closed Candle Selection (Important)

Aurono v1 infers the “most recent closed candle” as follows:

* Compare timestamps of the last two candles
* Infer candle interval
* If the last candle started **less than half an interval ago**:

  * Use the **previous candle**
* Otherwise:

  * Use the last candle

This heuristic is used to avoid trading on partially formed candles.

---

## 4. Price Change Calculation

For the selected candle:

* `open_price` = candle open
* `close_price` = candle close
* Percentage change is calculated as:

```
pct_change = (close - open) / open * 100
```

Triggers are evaluated **only on this percentage**.

---

## 5. Trade Preconditions

Before any trade logic:

* Average Cost Basis (ACB) is computed from historical trades
* Balance is derived from historical trades
* No persistent inventory table exists

Balance and ACB are recomputed **every cycle**.

---

## 6. BUY Logic

### 6.1 Trigger Condition

A BUY trigger occurs if:

```
pct_change <= drop_trigger
```

BUY logic is evaluated **before SELL logic**.

If BUY logic triggers:

* SELL logic is skipped entirely for this cycle

---

### 6.2 Capital Check

A BUY is rejected if:

```
allocated_eur < buy_amount_eur
```

Notes:

* `allocated_eur` is per-strategy
* No exchange balance check is performed
* Capital safety relies entirely on `allocated_eur`

---

### 6.3 Volume Calculation

* Fee rate is taken from exchange object (defaulted)
* Volume is calculated as:

```
volume = buy_eur / (ticker_price * (1 + fee))
```

* Volume is quantized conservatively
* Final normalization happens inside the exchange adapter

If computed volume ≤ 0:

* BUY is rejected

---

### 6.4 Trade Recording & Execution (BUY)

BUY execution order:

1. Trade row is inserted into `trades`
2. Limit order is submitted to exchange
3. If order is **not accepted**:

   * Trade row is deleted (“phantom trade”)
4. If order is accepted:

   * `allocated_eur` is immediately reduced by `limit_price * volume`
5. Execution reconciliation may later adjust allocation

Important:

* Trades are recorded **before** exchange placement
* Failed placements require cleanup logic

---

## 7. SELL Logic

SELL logic is evaluated **only if BUY did not trigger**.

---

### 7.1 Trigger Condition

A SELL trigger occurs if:

```
pct_change >= rise_trigger
```

---

### 7.2 SELL Preconditions

A SELL is rejected if:

* ACB is `None`
* `close_price <= ACB`
* Notional value < €6.00

```
notional = balance * close_price
```

---

### 7.3 Volume Calculation

* Target notional is `sell_amount_eur`
* Volume is capped to available balance
* Fee-adjusted volume is calculated
* Final normalization occurs in exchange adapter

If volume ≤ 0:

* SELL is rejected

---

### 7.4 Trade Recording & Execution (SELL)

SELL execution mirrors BUY with inverted capital flow:

1. Trade row inserted
2. Order submitted
3. On rejection:

   * Trade row deleted
4. On acceptance:

   * `allocated_eur` is increased by `limit_price * volume`
5. Execution reconciliation may later adjust allocation

---

## 8. Execution Reconciliation

After order submission:

* Engine waits ~12 seconds
* Fetches order details
* If filled or partially filled:

  * Trade row is **updated in-place** with actual price & amount
  * Execution delta vs reserved amount is computed
  * `allocated_eur` is adjusted accordingly

Partial fills:

* Overwrite original trade
* No partial inventory accounting exists

---

## 9. Trade History Semantics

* Trades are mutable
* Trades can be deleted
* Trades can be overwritten after execution
* There is no order lifecycle table
* A trade represents both:

  * execution intent
  * execution outcome

Trade history is **not append-only**.

---

## 10. Balance & ACB Semantics

### 10.1 Balance

Balance is derived as:

```
sum(buys.amount) - sum(sells.amount)
```

There is no persistent inventory table.

---

### 10.2 Average Cost Basis (ACB)

ACB is calculated by:

* Accumulating total cost and quantity
* On SELL:

  * Cost is reduced proportionally
* If quantity drops to zero or below:

  * ACB resets to `None`

ACB is recalculated from scratch each cycle.

---

## 11. Capital Semantics

* Capital safety is enforced via `allocated_eur`
* Exchange balances are ignored
* Capital is mutated:

  * on submit acceptance
  * again after execution reconciliation
* Capital corrections are asymmetric for BUY vs SELL

There is no capital ledger.

---

## 12. Scheduling Semantics

In LIVE mode:

* 1h strategies: at `xx:01` UTC
* 4h strategies: at `00/04/08/12/16/20 :03` UTC
* 1d strategies: at `00:05` UTC
* 1w strategies: Mondays at `00:08` UTC

In DEV mode:

* All strategies run in a loop
* Sleep duration = `dev_sleep_hours`

---

## 13. Failure Handling

* Most failures cause the strategy to be skipped
* No retries occur within the same cycle
* Errors are logged, not escalated
* One crashing strategy does not stop others

---

## 14. Known Intentional Limitations

Aurono v1 does **not**:

* track order lifecycle events
* support stop-losses
* support trailing logic
* distinguish intent vs execution
* support immutable history
* enforce exchange balance safety
* support replay or simulation

These are accepted constraints of v1.

---

## 15. Migration Implications

Aurono v1 history:

* Is **factual**, not causal
* May contain deleted or overwritten trades
* Cannot be perfectly replayed
* Must be imported as *historical state*, not events

Aurono v2 is expected to **intentionally diverge**.

---

## 16. Final Statement

This document defines **Aurono v1 as it existed in production**.

It is frozen to:

* protect migration correctness
* enable principled redesign
* avoid historical disputes

Aurono v2 is not required to reproduce these behaviors —
only to **understand and respect them**.

---

**End of document**
