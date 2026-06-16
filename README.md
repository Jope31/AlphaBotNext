## Deprecation Summary & Rationale

To streamline the optimization search space and protect the engine from overfitting past noise, the variable architecture has been condensed from 18 bloated items down to **7 core tactical levers**. The following mechanisms have been completely deprecated or locked as static constants:

* **VWAP Bleed Cut (System B):** Deprecated `VWAP_BLEED_MULTIPLIER`, `VWAP_BLEED_TICKS`, and `VWAP_BLEED_DECAY_RATE`. Slow-drift intraday asset bleeding is now captured more organically by the newly integrated **Breakeven Path B (MC-Stuck Override)**, rendering an independent secondary volume-bleed structure redundant.


* **Morning Gap Defense:** Deprecated `GAP_DEFENSE_THRESHOLD_PCT` and `GAP_DEFENSE_MULTIPLIER`. An overnight gap-up is mathematically a high-velocity vertical move occurring at market open. The standard **Parabolic Squeeze Ratchet** now natively intercepts these moves without needing a separate standalone system.


* **Catastrophic Drop Protocol:** Deprecated `CATASTROPHIC_DROP_PCT`. Rapid structural drawdowns are handled by standard Monte Carlo arming bounds combined with the new independent timestamped Breakeven Lock protections.


* **Monte Carlo KNN Distance Weights:** `MC_W1`, `MC_W2`, and `MC_W3` have been removed from the optimization space and locked as absolute static constraints ($1.0$). This prevents the autotuner from curve-fitting the basic physics of the simulation to past noise.


* **Breakeven Volatility Bounds:** `BREAKEVEN_VOL_MIN` and `BREAKEVEN_VOL_MAX` have been deprecated from tuning parameters and hardcoded directly to fixed structural boundaries ($0.4\%$ and $3.0\%$).



---

# AlphaBot v4.0

## Summary

The primary intent of **AlphaBot** is to function as an institutional-grade, algorithmic risk engine that sits on top of Composer.trade portfolios (referred to as "symphonies"). Rather than relying on passive "buy-and-hold" strategies that leave capital exposed to intraday market crashes, AlphaBot actively monitors live market data minute-by-minute. Its goal is to dynamically calculate intelligent trailing stops and automatically execute "sell-to-cash" orders via API when mathematical risk thresholds are breached. Ultimately, it seeks to generate "Guard Alpha"—mathematically proving that its automated early exits saved the user money compared to holding the asset until the market close.

---

## Features Overview

AlphaBot achieves its goals through a sophisticated combination of data ingestion, multi-layered mathematical defense protocols, concurrency management, and machine learning optimization.

### **Live Data Ingestion & Regime Detection**

* **Alpaca API Integration:** Fetches real-time, 1-minute historical and live pricing data for all active holdings across user portfolios. It utilizes parallel processing and local caching to rapidly generate synthetic intraday history.


* **Dynamic Sector-Conditioned Macro Environment:** Filters the historical Monte Carlo dataset to only use days that closely match today's benchmark performance. The system features a semantic name resolution engine that dynamically assigns the most accurate proxy ETF (SPY, QQQ, IWM, DIA) to a symphony based on its top holdings, preserving highly accurate cross-asset correlations. Features a fully vectorized dual-mode unconditional bootstrap fallback to maintain resilience during unprecedented black swan events or data provider failures.



### **The Multi-Layered Risk Engine**

* **Volatility-Adjusted Risk Sizing (VW-ATR):** Calculates an active trailing stop distance based strictly on the portfolio's 14-day Volume-Weighted Average True Range (VW-ATR), falling back to 20-day standard deviation if granular intraday metrics are unavailable.


* **Strict Monotonicity Ratchet:** Mathematically enforces that a trailing stop can never drop or move backwards tick-to-tick, safely ratcheting the absolute stop level upward even during sudden intraday volatility spikes.


* **Logarithmic Time Squeeze:** Shrinks the trailing stop distance smoothly and predictably based on the time of day using an accelerating logarithmic decay curve. The dynamic multiplier decays from your open parameter (`VOLATILITY_MAGNITUDE_MULTIPLIER`) to your close parameter (`VOLATILITY_CLOSE_MULTIPLIER`) near the bell.


* **Parabolic Squeeze Ratchet:** Measures tick-by-tick *intraday* return velocity. If the velocity exceeds the `PARABOLIC_VELOCITY_THRESHOLD`, the engine permanently ratchets the trailing stop tighter using the `MAX_PARABOLIC_SQUEEZE` multiplier to protect the peak.


* **Hybrid Dual-Path Breakeven Lock:** To lock the absolute downside floor to breakeven ($0.0\%$), live return must hold above a dynamically scaled activation threshold (clamped between $0.4\%$ and $3.0\%$) for 5 ticks. Once locked, it deploys **Path A** (Live-MC sanity filtering) alongside **Path B** (Persistent UTC timestamped `lock_engaged_at` override) to catch structural "MC-Stuck" slow-drift down days.


* **Monte Carlo State Engine:** Runs thousands of vectorized, deterministic Monte Carlo simulations to calculate the probability of the symphony beating its current return, arming defensive trailing stops when the probability falls below the `TRIGGER_THRESHOLD_PCT`.


* **Un-Gated Take-Profit MC Reversal:** Tracks extreme top-of-distribution thresholds via `TAKE_PROFIT_MC_PCT`. Reversion exits execute immediately upon cross-confirmation regardless of absolute return sign, capturing high-statistical relative peaks on macro down-days.


* **Institutional VWAP Breakdown:** Forces tactical exits if the portfolio price drops below its volume-weighted average price pool for 3 consecutive ticks after hitting an established high-water mark threshold defined by `VWAP_CROSS_HWM_PCT`.


* **Strict Exit Confirmation:** Standard trailing stops require 3 consecutive ticks below the stop line with a $0.10\%$ magnitude floor and an active Monte Carlo sanity gate check to eliminate premature exits on minor noise.



### **Symphony-Level Database Architecture**

* **SQLite State Management:** Uses a highly concurrent SQLite database to store states, isolated risk parameters, execution locks, and continuous chart histories.


* **Symphony-Level Strategies:** Maintains independent parameter tuning and variable locks based on unique, normalized symphony names.


* **Ecosystem Flush & Synchronization:** An intelligent "Sync & Flush Portfolio" clean-slate protocol allowing users to seamlessly prune ghost tracking counters and dropped symphonies over the weekend while preserving finely-tuned parameters.


* **Automated Portfolio Sync (Garbage Collection):** Automatically detects and prunes orphaned strategies removed from Composer during rebalances to keep the execution loop and autotuner highly optimized.


* **Persistent Daily Logging:** Captures specific event logs for each symphony into persistent local daily files (`symphony_logs_YYYY-MM-DD.json`), ensuring all historical intraday actions are permanently auditable.



### **Automated Execution & Alerting**

* **Gatekeeper & Scheduler:** A fully internal Flask-based daemon process using the `schedule` library runs the bot every minute during market hours, removing reliance on external cron jobs.


* **Smart Liquidation Verification:** Queues pending liquidations and utilizes a rate-limit-optimized batched polling loop to verify actual settlement into cash via the Composer API before updating internal state.


* **Per-Symphony Circuit Breaker:** Intelligently tracks a "missing streak" to detect manual user interventions. If a user manually liquidates a symphony on Composer mid-day, the bot safely traps the resulting errors, flags the basket, and suspends tracking for that session.


* **Discord Webhooks (Multi-Embed):** Instantly sends structured embed payloads detailing execution metrics, dynamic parameters, and custom color-coded alerts (Green for profit, Orange for trailing breaches, Blue for Breakeven Lock triggers).



### **EOD Autotuning & Post-Mortem Analytics**

* **Two-Stage EOD Pipeline:** Generates a daily post-mortem snapshot by locking shadow returns via live Alpaca pricing at 15:53 ET (Stage 1) before injecting tomorrow's target holdings at 16:00 ET (Stage 2) to protect data integrity.


* **Lean 7-Variable Walk-Forward Engine:** Performs a 125-trading-day Walk-Forward Analysis (80% Train / 20% Out-of-Sample) running 500 parallel trials via Optuna per unique symphony name. The objective function penalizes missed upside, tracks historical execution slippage over 45 days, and forces **Optimization-Driven Disables** (`TRIGGER_THRESHOLD_PCT = 0.0`) if the MC arm is generating net-negative training alpha.



---

## Variables Explanation

### API Keys and Identifiers

* **`COMPOSER_KEY_ID`** & **`COMPOSER_SECRET`**: Authentication credentials for the Composer API.


* **`ACCOUNT_INDIVIDUAL`**, **`ACCOUNT_ROTH`**, **`ACCOUNT_TRAD`**: Your Composer Account UUIDs separated by account type.


* **`ACCOUNT_INDIVIDUAL_ENABLED`**, **`ACCOUNT_ROTH_ENABLED`**, **`ACCOUNT_TRAD_ENABLED`**: Toggles (`True`/`False`) to enable or disable active live execution on a per-account basis.


* **`ALPACA_KEY`** & **`ALPACA_SECRET`**: Alpaca API credentials used to fetch market data.


* **`DISCORD_WEBHOOK_URL`**: Webhook URL where the bot sends alerts and reports.



### Master Control

* **`LIVE_EXECUTION`**: Toggle (`True`/`False`). Set to `False` to run in paper/simulation mode (Safe); set to `True` to allow live "sell-to-cash" execution via API.


* **`EXECUTION_START_TIME`**: The time (e.g., `09:30`) when the bot begins monitoring live stops.



### The 7 Core Optimized Algorithm Parameters

The optimization engine restricts its dynamic walk-forward search space to these 7 core tactical variables, allowing Optuna to cover maximum ground and discover stable parameters without overfitting to historical noise. (Note: While `TRIGGER_THRESHOLD_PCT` exists in the codebase as an 8th variable, it is hardlocked by default as a global structural control, leaving these 7 to be actively tuned.)

* **`TAKE_PROFIT_MC_PCT`:** The target Monte Carlo probability threshold used to activate un-gated take-profit trailing stop traps on exceptional intraday gains.


* **`VWAP_CROSS_HWM_PCT`:** The exact return percentage a symphony must cross to activate institutional volume-pool breakdown tracking.


* **`VWAP_BAND_MULTIPLIER`:** Scales your asset's volatility to establish a localized volume buffer zone, ensuring the bot ignores minor wiggles above or below the institutional VWAP pool.


* **`VOLATILITY_MAGNITUDE_MULTIPLIER`:** The morning multiplier applied to the symphony's trailing stop width, keeping stops wide and forgiving during opening auction noise.


* **`VOLATILITY_CLOSE_MULTIPLIER`:** The target multiplier applied to the end-of-day stop width, defining how tightly the accelerating logarithmic curve chokes the trailing stop near market close.


* **`PARABOLIC_VELOCITY_THRESHOLD`**: The specific minute-by-minute return velocity required to arm a parabolic vertical surge protection mode.


* **`MAX_PARABOLIC_SQUEEZE`:** The aggressive compression multiplier applied continuously to shrink your trailing stop distance whenever a parabolic squeeze is triggered or a breakeven lock is achieved.



---

## Installation Guide

1. **Clone the repository and enter the directory**
```bash
git clone https://github.com/Jope31/AlphaBot.git
cd AlphaBot

```

2.  **Create and activate a virtual environment**
* **Mac/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

* **Windows:**
```bash
    python -m venv venv
    venv\Scripts\activate
```

3.  **Configure Environment Variables**
    Create a `.env` file matching your account parameters:
```env
    COMPOSER_KEY_ID=your_composer_key
    COMPOSER_SECRET=your_composer_secret
    ACCOUNT_INDIVIDUAL=your_uuid_1
    ACCOUNT_INDIVIDUAL_ENABLED=True
    ACCOUNT_ROTH=your_uuid_2
    ACCOUNT_ROTH_ENABLED=True
    ACCOUNT_TRAD=your_uuid_3
    ACCOUNT_TRAD_ENABLED=False
    ALPACA_KEY=your_alpaca_key
    ALPACA_SECRET=your_alpaca_secret
    DISCORD_WEBHOOK_URL=your_discord_webhook
    LIVE_EXECUTION=False
    EXECUTION_START_TIME=09:31
```

4. **Initialize & Run**
Launch the configuration server and background schedule loop by executing:
```bash
python app.py

```


Open your browser to `[http://127.0.0.1:5000](http://127.0.0.1:5000)` to access the live dashboard panel. Use **"Force Run Now"** to instantly map portfolio positions followed by **"Create EOD Analysis"** to build your initial optimized 7-variable baseline sets.
