# **🤖 AlphaBot: Multi-Factor Volatility Engine (v3.5)**

AlphaBot is a high-performance quantitative risk-management framework and automated execution engine designed to interface directly with **Composer.trade** portfolios. By synthesizing real-time market sentiment (VIX), intraday time decay, and individual asset velocity, AlphaBot acts as an intelligent circuit breaker—protecting capital during systemic breakdowns while aggressively locking in gains during parabolic runs.

AlphaBot (v3.0) marks a fundamental shift from a reactive script to a **context-aware risk engine**, built for high-reliability execution across large, complex portfolios.

AlphaBot (v3.5) introduces a **High-Concurrency SQLite Backend**, moving away from fragmented JSON files to provide a robust, database-driven "single source of truth."


## **🌟 The 3-Tier Execution Logic**

AlphaBot no longer uses a "one-size-fits-all" trailing stop. It layers three distinct forces to calculate the optimal stop distance every minute:

### **1. The Macro Foundation (VIX Regime Filter)**

The bot determines the "Market Weather" using the **VIX Index** (cached 15-minute updates). This sets the baseline multiplier for your safety net:

* **Low Volatility (<15 VIX):** Clamps stops tight to prevent "slow bleed" losses.  
* **Normal Regime (15-25 VIX):** Balanced sensitivity for standard market conditions.  
* **Crisis Regime (>25 VIX):** Widens stops to allow for the systemic noise inherent in high-fear markets, preventing premature "whipsaw" exits.

### **2. The Intraday Strangler (Time Decay)**

The system recognizes that volatility typically increases toward the end of the session. It utilizes a **Logarithmic Decay Curve** to tighten the stop from 10:30 AM to 3:54 PM ET.

* **Morning:** Wide stops allow for initial price discovery.  
* **Afternoon:** The stop "strangles" the position, collapsing wiggle room to lock in the day's gains before the final closing auction.

### **3. The Micro Override (Asymmetric Parabolic Squeeze)**

This layer acts as an **Emergency Brake** for vertical moves. It monitors the High Water Mark (HWM) relative to the symphony's 20-day volatility ($\sigma$).

* **Trigger:** If HWM > 2.0x Daily $\sigma$, the asset is flagged as "Parabolic."  
* **Action:** A velocity squeeze multiplier (up to 50% reduction) is applied instantly. This yanks the stop upward to hug the price action during spikes, ensuring sudden reversals result in locked top-tier profits.

## ⚡ High-Reliability Performance Architecture (v3.5 Updates)

AlphaBot is engineered for zero-collision execution, ensuring that the Japan-based user can sleep soundly while the bot manages US market hours.

* **SQLite Atomic Persistence**: All state management, chart history, and VIX caching are consolidated into ```alphabot.db```. This eliminates "Atomic Swap" race conditions and file-locking errors.  
* **Zero-IO Overlap**: The Flask Control Center and the Execution Engine utilize SQLite's native transaction handling. The UI remains responsive even during heavy Monte Carlo simulations.  
* **Smart Initialization UI**: A new "Bot State Initializing" screen with a centered loading animation ensures the user knows exactly when the bot is warming up its first database entry.  
* **Async Threaded Scheduler**: The 1-minute heartbeat is detached from the execution logic, ensuring that API latency never causes the clock to fall behind.  
* **Dynamic Multi-Tick Confirmation**: Signals must breach stop levels for multiple consecutive 1-minute runs (via ```tick_threshold```) to filter out flash-crash noise.

## **⚙️ Environment Configuration**

AlphaBot is tuned via the ```.env``` file. The following variables define the Volatility Engine:

### **Macro (VIX) Settings**

* ```VIX_LOW_THRESHOLD``` / ```VIX_HIGH_THRESHOLD```: Defined boundaries for market regimes (Default: 15 / 25).  
* ```VIX_LOW_MULT``` / ```VIX_MID_MULT``` / ```VIX_HIGH_MULT```: Multipliers applied to daily volatility (ATR) for each regime.

### **Monte Carlo & Squeeze Settings**

* ```TRIGGER_THRESHOLD_PCT```: MC Probability required to arm the trailing stop (Def: 15).  
* ```TAKE_PROFIT_MC_PCT```: MC Probability required to arm the aggressive "Smart TP" trap (Def: 5).  
* ```MAX_SQUEEZE_FLOOR```: The absolute tightest a stop can squeeze (Def: 0.20, or 20% of its base width).  
* ```PARABOLIC_VELOCITY_THRESHOLD```: Multiple of daily $\sigma$ required to trigger the parabolic override (Def: 2.0).

### **Standard Guardrails**

* ```LOSS_ARM_PCT```: Vol-scaled flash crash floor.  
* ```BREAKEVEN_ACTIVATION_PCT```: Percentage at which the stop floor locks at 0.0% to protect the principal.

## **Gemini "Quant Analyst" Integration**

The ```post_mortem_YYYY-MM-DD.json``` file is specifically structured to be analyzed by a Large Language Model (like Google Gemini Gems).

**How to set up your AI Analyst:**

1. Create a new custom Persona/Gem in Gemini.  
2. Name it "AlphaBot Quant Analyst".  
3. Paste the following into the system instructions:
```
You are the AlphaBot Quant Analyst. Your job is to analyze daily execution logs and EOD snapshots from the AlphaBot risk-management system, and recommend parameter tuning for the next trading day.
AlphaBot has recently been upgraded to a Multi-Factor Volatility Engine. You must understand how its 3-tier profit-lock system works to accurately diagnose trades:
The 3-Tier Execution Logic
The Macro Foundation (VIX Regime): The bot fetches the VIX every 15 minutes. It uses this to set the base width of the trailing stop for all assets.
If VIX < VIX_LOW_THRESHOLD, it applies VIX_LOW_MULT.
If VIX > VIX_HIGH_THRESHOLD, it applies VIX_HIGH_MULT.
Otherwise, it applies VIX_MID_MULT.
The Intraday Strangler (Time Decay): As the day progresses from 10:30 AM to 4:00 PM, the stop logarithmically tightens from the morning width down to a fraction of its size.
The Micro Override (Parabolic Squeeze): If an individual asset's High Water Mark (HWM) exceeds 2.0x its normal daily volatility, the asset is considered "Parabolic". The bot applies a fractional multiplier (up to 50% reduction) to the stop distance, violently tightening the stop to lock in the outlier gain.
Interpreting the Logs
Macro Environment: Look for the log Macro Environment: SPY X% | VIX Y (Regime). This tells you the baseline sensitivity for the day.
Parabolic Events: Look for logs starting with ⚡ [SymphonyName] PARABOLIC SQUEEZE: 0.XXx. If a trade was stopped out shortly after this, it was a successful profit-lock of a vertical move, not a premature whipsaw.
Arming/Disarming: Look at the MC Probability. If it dropped below TRIGGER_THRESHOLD_PCT, the bot armed. If it dropped below TAKE_PROFIT_MC_PCT, it armed the smart TP.
Your Tuning Mandate
When recommending parameter changes, you no longer tune a static ATR multiplier. Instead, you must tune the Regime Matrix. You are authorized to recommend changes to the following environment variables:
VIX_LOW_THRESHOLD / VIX_HIGH_THRESHOLD
VIX_LOW_MULT / VIX_MID_MULT / VIX_HIGH_MULT
TRIGGER_THRESHOLD_PCT
TAKE_PROFIT_MC_PCT
LOSS_ARM_PCT
MAX_SQUEEZE_FLOOR
TRAILING_STOP_PCT / ENDING_STOP_PCT
When you provide your daily briefing, analyze whether stops were hit because the VIX Regime Multiplier was too tight for normal noise, or if they were hit because the Parabolic Squeeze correctly trapped a vertical run. Adjust the matrix accordingly.
```
**Daily Workflow:** Simply drop the generated JSON file into the chat at 4:05 PM ET and provide a prompt like, "Tell me how I did today". The AI will provide a complete statistical breakdown and parameter tuning advice for tomorrow.


## **⚙️ Environment Variables**

* ```LIVE_EXECUTION``` *(Default: False)* - Master safety switch. When False, AlphaBot operates in a Dry Run mode, logging logic and sending Discord alerts without executing API trades.  
* ```TRIGGER_THRESHOLD_PCT``` *(Default: 15.0)* - The Monte Carlo probability required to "arm" the trailing stop logic.  
* ```TAKE_PROFIT_MC_PCT``` *(Default: 5.0)* - The extreme top-percentile Monte Carlo probability required to arm the Take-Profit trap.  
* ```MAX_SQUEEZE_FLOOR``` *(Default: 0.20)* - The maximum amount the "Strangler" can tighten a stop by 4:00 PM (e.g., tightens to 20% of its original distance).  
* ```LOSS_ARM_PCT``` *(Default: 1.5)* - The hard volatility floor percentage to arm the stop during a sudden breakdown.  
* ```BASE_ATR_MULTIPLIER``` *(Default: 2.0)* - The multiplier applied to the asset's 20-day volatility to calculate the initial morning stop distance.  
* ```TRAILING_STOP_PCT``` *(Default: 1.5)* - The fallback trailing stop percentage.  
* ```BREAKEVEN_ACTIVATION_PCT``` *(Default: 2.0)* - The profit percentage required to lock the trailing stop at ```0.0%```, scaled dynamically against daily volatility.

## **🚀 Setup & Installation**

1. **Clone the Repository:**  
```
   git clone https://github.com/Jope31/AlphaBot.git  
   cd AlphaBot
```

2. **Install Dependencies:**
```
   pip install flask python-dotenv requests numpy schedule pandas alpaca-trade-api
```

3. **Configure Environment:** Create a ```.env``` file in the root directory and populate it with the variables listed in the Environment section above (including your Composer, Alpaca, and Discord API keys).  

4. **Launch the Control Center:**
```
   python app.py
```
   
5. **Access Dashboard**: Open ```http://localhost:5000```. The bot will automatically initialize the SQLite database on its first run.

## 🕒 Scheduling Details

AlphaBot utilizes a background schedule thread running via ```app.py```.

* **10:30 AM ET Grace Period:** AlphaBot ignores the chaotic opening auction, beginning evaluations once the "Morning Noise" settles.  
* **3:54 PM ET Rebalance Blackout:** To prevent API collisions during Composer's rebalancing window, AlphaBot automatically ceases all execution actions 6 minutes before the close.  
* **Guard Alpha Snapshots:** Daily post-mortems calculate "Guard Alpha"—the exact capital saved by AlphaBot compared to a passive "Hold to EOD" strategy.

**Disclaimer: AlphaBot is an automated execution tool. Algorithmic trading carries significant risk. Always test parameters in Dry Run mode before enabling ```LIVE_EXECUTION```.**
