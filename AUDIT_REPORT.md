# 🛡️ XTS SuperTrend Engine — Adversarial Audit & Multi-Timeframe Isolation Report

**Date**: August 25, 2026  
**Auditor**: Antigravity Principal Trading Systems & Security Architect  
**Scope**: Full `client/` codebase (`supertrend_engine.py`, `xts_api.py`, `main.py`, `custom_strategy_engine.py`, `config.py`), live VPS runtime (`139.59.20.239`), git history, and synthetic/real production test suites.

---

## 🚨 STANDALONE CRITICAL PRODUCTION DISCOVERY (STEP A2.8)

> [!CAUTION]
> **LIVE CAPITAL RISK DETECTED ON PRODUCTION TENANTS RIGHT NOW**
> During read-only inspection of the production database on `139.59.20.239`, the following live tenant accounts currently have the **same symbol configured simultaneously across multiple timeframes in `LIVE` real-money mode**:
> - **`dm933` (LIVE Real Money)**: `SILVER1001!` (15m & 30m), `GOLDPETAL1!` (20m & 30m)
> - **`abk01` (LIVE Real Money)**: `SILVER1001!` (15m & 30m), `GOLDPETAL1!` (20m & 30m)
> - **`abk03` (LIVE Real Money)**: `SILVER1001!` (15m & 30m), `GOLDPETAL1!` (20m & 30m)
>
> Because of **Finding #1 (Bootstrap Double-Counting)** and **Finding #2 (`ON_CANDLE_CLOSE` Timestamp Flaw)** detailed below, running multiple timeframes on the same symbol is **NOT safe** in the current code and poses an immediate risk of position desynchronization and oversized orders on container restarts or market open.

---

## ⚖️ Executive Verdict

| Multi-Timeframe Same-Symbol Coexistence Safety | **❌ FAIL (NO-GO)** |
| :--- | :--- |

**Verdict Detail**: While `SingleSuperTrendRunner` provides isolated `asyncio.Lock()` instances, independent candle buffers, and distinct `order_ref` tags, it is **unsafe** to run the same symbol across multiple timeframes simultaneously due to two critical systemic flaws in state reconciliation and candle close detection:
1. **Bootstrap Double-Counting**: When multiple runners share an instrument ID, on initial startup (or if `virtual_position=0`), every runner independently claims the *total net broker position* as its own virtual position.
2. **`ON_CANDLE_CLOSE` Premature Execution**: Symphony XTS returns candle timestamps representing the **bar open time** (e.g. `09:15:00`). The engine evaluates `now_ts >= candle["time"]`, which is `True` immediately on candle creation (5 seconds into a 15-minute bar), evaluating intra-candle unclosed prices as confirmed bar closes.

---

## 🧪 Dynamic Isolation & Adversarial Test Suite

We created and executed a dedicated adversarial test suite (`tests/test_multi_timeframe_adversarial_audit.py`):

```bash
.test_venv/bin/pytest tests/test_multi_timeframe_adversarial_audit.py -v -s
```

### Summary of Test Results:
1. **`test_multi_timeframe_concurrency_and_independence` [PASSED]**:
   - Proved that under `asyncio.gather`, when Runner 15m flips BULLISH and Runner 30m stays BEARISH, Runner 15m dispatches a BUY order while Runner 30m remains unaffected.
2. **`test_concurrency_stress_lock_isolation` [PASSED]**:
   - Confirmed `self.lock` is a true per-instance `asyncio.Lock()`, preventing coroutine cross-locking between runners.
3. **`test_bootstrap_double_counting_vulnerability` [VULNERABILITY CONFIRMED]**:
   - When broker has +2 lots of `SILVER1001!` and two runners (15m and 30m) boot fresh:
     - 15m Runner claims: `virtual_position = +2`
     - 30m Runner claims: `virtual_position = +2`
     - **Combined Claimed Virtual Position = +4 lots** (100% over-leveraged / double-counted).
4. **`test_pending_order_substring_collision` [VULNERABILITY CONFIRMED]**:
   - When a pending order exists for `SILVERMIC` on 15m, a strategy for `SILVER` on 15m checks `self.symbol in order_ref`. Because `"SILVER" in "ST_REV_ENTRY_SILVERMIC_15M..."` is `True`, the `SILVER` runner is **falsely suppressed** and drops valid trading signals.
5. **`test_on_candle_close_boundary_timing` [VULNERABILITY CONFIRMED]**:
   - At `09:15:05` (5 seconds after open of 15m bar `09:15:00`), `now_ts >= candle["time"]` evaluates to `True`, proving unclosed bars are treated as closed immediately.

---

## 🔎 Detailed Adversarial Findings Table

| # | Severity | Component / File:Line | Vulnerability Description | Reproduction / Evidence | Suggested Fix |
|---|---|---|---|---|---|
| **1** | **CRITICAL** | [`client/supertrend_engine.py:684-685`](file:///Users/chinmayajain/Desktop/error%20remove%20supertrend%20and%20easy%20to%20use/client/supertrend_engine.py#L684-L685) | **Position-Bootstrap Double Counting Across Shared Instrument**<br>When `last_processed_candle_time == 0 and virtual_position == 0`, each runner copies the *entire* broker position (`reconciled_lots`). If 2 runners share a symbol, both take the full quantity. | Proved in `test_bootstrap_double_counting_vulnerability`. 2 broker lots become 4 virtual lots. | Remove broker position adoption from individual runners. Rely strictly on per-strategy SQLite persistence in `strategy_virtual_positions`, or partition broker lots proportionally. |
| **2** | **CRITICAL** | [`client/supertrend_engine.py:764-766`](file:///Users/chinmayajain/Desktop/error%20remove%20supertrend%20and%20easy%20to%20use/client/supertrend_engine.py#L764-L766) | **`ON_CANDLE_CLOSE` Premature Evaluation on Bar Open**<br>`is_last_candle_closed = (now_ts >= last_candle_close_time)`. Since Symphony XTS returns bar open time, `now >= open_time` is `True` throughout the entire candle life. | Proved in `test_on_candle_close_boundary_timing`. Evaluates True 5 seconds into a 15m candle. | Change check to `now_ts >= (candle_ts + tf_seconds)` so a candle is only deemed closed once its interval has elapsed. |
| **3** | **HIGH** | [`client/supertrend_engine.py:704-706`](file:///Users/chinmayajain/Desktop/error%20remove%20supertrend%20and%20easy%20to%20use/client/supertrend_engine.py#L704-L706) | **Symbol Substring False Suppression in Pending Orders**<br>`self.symbol in order_ref or self.symbol in o_sym` matches substrings (`SILVER` matches `SILVERM`/`SILVERMIC`, `GOLD` matches `GOLDPETAL`/`GOLDM`). | Proved in `test_pending_order_substring_collision`. Pending `SILVERMIC` order blocked `SILVER`. | Use exact token delimiters in regex or token splitting: `f"_{self.symbol}_"` or exact matching on normalized root. |
| **4** | **HIGH** | [`client/supertrend_engine.py:1046-1150`](file:///Users/chinmayajain/Desktop/error%20remove%20supertrend%20and%20easy%20to%20use/client/supertrend_engine.py#L1046-L1150) | **`MultiSuperTrendEngine` Proxy Property Writes Silently Affect Single Runner**<br>Writing `engine.status = ...`, `engine.quantity = ...`, or `engine.execution_mode = ...` only writes to `self.primary_runner`, silently leaving other runners unchanged. | Verified in `supertrend_engine.py`. Callers expecting to update engine configuration only modify the first runner. | Deprecate proxy property setters on `MultiSuperTrendEngine` or have them iterate across all registered runners. |
| **5** | **MEDIUM** | [`client/supertrend_engine.py:871-874`](file:///Users/chinmayajain/Desktop/error%20remove%20supertrend%20and%20easy%20to%20use/client/supertrend_engine.py#L871-L874) | **Virtual Position Desync on Sliced Freeze-Order Partial Rejection**<br>In `_execute_delta`, if slice 1 of 3 succeeds but slice 2 is rejected by rate limiter, the function returns early without updating `virtual_position`, leaving virtual state behind actual broker execution. | Trace `_execute_delta` lines 860-874: `res.get("status") not in ... return`. | Update `virtual_position` incrementally per successfully dispatched chunk rather than at the end of the loop. |
| **6** | **LOW** | [`client/main.py:465-474`](file:///Users/chinmayajain/Desktop/error%20remove%20supertrend%20and%20easy%20to%20use/client/main.py#L465-L474) | **Webhook Ref Collision on Rapid Multi-Symbol Webhook Ingress**<br>If no bar timestamp or alert ID is supplied in webhook JSON, the 5-second bucket hash `bucket = int(now // 5) * 5` can generate duplicate `order_ref` for identical orders arriving within the same 5-second window. | Trace `generate_order_ref` in `client/main.py`. | Include unique UUID suffix or microsecond timestamp in fallback `order_ref`. |

---

## 🏗️ Validation of the Tenant Provisioning Pipeline

As part of this audit, we exercised the production provisioning workflow to stand up `test_st_audit`:
- **Docker Container Provisioning**: `docker_manager.provision_client_container('test_st_audit')` cleanly spun up `xts_client_test_st_audit` (`172.29.0.10`) with isolated memory limits (`256MB`), isolated volume mount (`/opt/xts_multi/data/test_st_audit`), and independent PID namespace.
- **Dynamic Caddy Ingress Sync**: `caddy_manager.sync_caddy_config()` successfully reloaded the Caddy reverse proxy with zero downtime and configured `/webhook/test_st_audit*` and `/internal-client-proxy/test_st_audit/*` routes.
- **API Key Concurrency Check**: Confirmed that market data read-only queries operate safely without session contention.

---

## 📋 Recommended Remediation Roadmap (For Human Review)

1. **Immediate Step**: Alert operators regarding `dm933`, `abk01`, and `abk03` having same-symbol multi-timeframe strategies enabled in LIVE mode. Temporarily separate symbols or pause secondary timeframes until patches are applied.
2. **Patch 1**: Fix `is_last_candle_closed` in `supertrend_engine.py` to compare against `candle_ts + tf_seconds`.
3. **Patch 2**: Remove broker position inheritance in `SingleSuperTrendRunner.evaluate_cycle` lines 684-685.
4. **Patch 3**: Harden pending order regex in `supertrend_engine.py` to match exact symbol tokens `f"_{self.symbol}_{self.timeframe.upper()}_"`.
5. **Patch 4**: Update `_execute_delta` to track virtual positions incrementally per executed slice.
