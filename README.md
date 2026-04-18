# **🤖 AlphaBot: Intelligent Profit and Loss Guardian (v2.0 "Risk Guard")**

AlphaBot is an advanced, automated risk-management and trailing-stop execution engine designed to interface directly with Composer.trade portfolios. By combining real-time intraday data from Alpaca with K-Nearest Neighbor Monte Carlo simulations, AlphaBot acts as an intelligent circuit breaker—defending your portfolio against sudden intraday breakdowns and locking in parabolic gains.

Recently upgraded with empirical data from live-market "Risk Guard" post-mortems, AlphaBot (v2.0) has evolved from a static script into a highly sophisticated, noise-resistant execution engine.

## **🌟 Key Upgrades (v2.0 "Risk Guard" Mechanics)**

Theoretical safety nets often trigger on market microstructure noise. AlphaBot v2.0 introduces four core features designed specifically to eliminate false positives and keep you in the trade:

1. **Volatility-Scaled Loss Arming (The "Flash Crash" Filter):** Assets no longer arm on a flat percentage drop. A ```-1.5%``` drop is an emergency for a Treasury Bond, but normal noise for a 3x Leveraged ETF. AlphaBot now arms the trailing stop based on the asset's specific baseline: ```max(LOSS_ARM_PCT, Daily_Volatility)```.  
2. **Multi-Tick Noise Confirmation:** To prevent execution on momentary bid/ask spread noise or single-tick flashes, a symphony must breach its stop level (or take-profit threshold) for **2 consecutive 1-minute ticks** before an API sell order is dispatched.  
3. **Extended Morning Grace Period:** The opening hour is a slaughterhouse of volatility and false trend breakdowns. AlphaBot now completely ignores the market from 9:30 AM to **10:30 AM ET**, ensuring the market establishes a true directional trend before trailing stops engage.  
4. **Vol-Scaled Sticky Breakeven Lock:** Once a symphony's High Water Mark (HWM) reaches ```max(BREAKEVEN_ACTIVATION_PCT, Daily_Volatility)```, the stop loss is permanently locked at ```0.0%``` for the remainder of the day, guaranteeing a risk-free trade regardless of afternoon volatility.

## **🧠 Core Strategy Mechanics**

AlphaBot operates independently of your browser, evaluating your live Composer symphonies every 60 seconds.

### **1. The Strangler (Dynamic Trailing Stop)**

AlphaBot runs a 5,000-path Monte Carlo bootstrap simulation on your symphony's holdings against current SPY conditions.

* **The Choke Mechanism:** If the current trajectory falls below the ```TRIGGER_THRESHOLD_PCT``` (default: bottom 15% of historical paths), the system is **```ARMED```**. The trailing stop distance immediately dynamically tightens (squeezes) based on how far the probability has fallen.  
* **ATR & Time Decay:** Stop distances automatically widen for high-volatility symphonies and tighten for stable ones based on a rolling 20-day standard deviation. Stops also start wide in the morning and logarithmically squeeze tighter as the day approaches the 4:00 PM ET close.

### **2. Smart Take-Profit (Mean Reversion)**

If a symphony goes parabolic and the Monte Carlo probability drops below the ```TAKE_PROFIT_MC_PCT``` (default: 5.0% - meaning the return is exceptionally high), the system arms a Take-Profit trap. It does *not* sell immediately. It waits for the momentum to break and the probability to rise back *above* the threshold for 2 consecutive ticks before executing a sell-to-cash to lock in the run.

## **🎛️ Interactive Web Dashboard**

* **Live Monitoring:** Track all your accounts, real-time High-Water Marks, armed statuses, active stop distances, and live MC probabilities from a unified interface.  
* **Post-Market Sandbox Simulator:** A built-in visualization table that lets you replay the day's price action using interactive sliders. Backtest how different ATR multipliers, Loss Arm floors, and Strangler parameters behave under stress.  
* **Settings UI:** Update your core strategy variables directly from the web interface. Changes instantly write back to your ```.env``` file and apply to the next execution loop.  
* **Account Liquidation:** Built-in modal to execute manual "Sell to Cash" overrides for entire accounts simultaneously.

## **⚙️ Environment Variables (.env)**

Configure the following variables in your ```.env``` file (or edit them directly via the Web Dashboard's **Edit Variables** panel):

### **API Credentials**

* ```LIVE_EXECUTION``` - Set to True to allow actual sell orders via the Composer API. False enables Dry Run mode.  
* ```COMPOSER_KEY_ID``` / ```COMPOSER_SECRET``` - Your Composer API credentials.  
* ```ACCOUNT_UUIDS``` - Comma-separated list of Composer Account UUIDs to track.  
* ```ALPACA_KEY``` / ```ALPACA_SECRET``` - Your Alpaca Data API credentials (Free tier is sufficient).  
* ```DISCORD_WEBHOOK_URL``` - (Optional) Webhook URL for rich-embed execution alerts.

### **Strategy Parameters**

* ```TRIGGER_THRESHOLD_PCT``` *(Default: 15.0)* - The Monte Carlo probability threshold that "arms" the defensive trailing stop.  
* ```TAKE_PROFIT_MC_PCT``` *(Default: 5.0)* - The exceptionally low MC probability threshold that arms the Smart Take-Profit trap.  
* ```LOSS_ARM_PCT``` *(Default: 1.5)* - The minimum raw percentage drop required to arm the system, scaled dynamically against daily volatility.  
* ```MAX_SQUEEZE_FLOOR``` *(Default: 0.20)* - The Strangler limit. When armed, the stop will shrink up to this percentage (e.g., 20%) of its original distance.  
* ```BASE_ATR_MULTIPLIER``` *(Default: 2.0)* - Multiplier for calculating the base stop distance from the asset's daily volatility.  
* ```MIN_MULTIPLIER_FLOOR``` *(Default: 0.5)* - The tightest the normal ATR stop is allowed to get before the Strangler kicks in.  
* ```TRAILING_STOP_PCT``` *(Default: 1.5)* - Fallback start stop percentage if historical volatility data cannot be fetched.  
* ```ENDING_STOP_PCT``` *(Default: 0.5)* - Fallback ending stop percentage.  
* ```BREAKEVEN_ACTIVATION_PCT``` *(Default: 2.0)* - The profit percentage required to lock the trailing stop at ```0.0%```, scaled dynamically against daily volatility.

## **🚀 Setup & Installation**

1. **Clone the Repository:**  
```
   git clone https://github.com/Jope31/AlphaBot.git
   cd AlphaBot
```

3. **Install Dependencies:**  
```
   pip install flask python-dotenv requests numpy schedule
```

5. **Configure Environment:**  
   Create a ```.env``` file in the root directory and populate it with the variables listed in the Environment section above.
   
7. **Launch the Control Center:**  
```
   python app.py
```

   *The Flask server will start on ```http://localhost:5000``` and automatically spawn the background execution scheduler.*

## **🕒 Scheduling Details**

AlphaBot utilizes a background schedule thread running via ```app.py```.

* **1-Minute Ticks:** The bot evaluates your portfolio precisely at the top of every minute (```:00```) to support the 2-tick confirmation logic.  
* **10:30 AM Grace Period:** The bot explicitly ignores the highly volatile market open and begins its daily execution loop at **10:30 AM ET**.  
* **Rebalance Blackout:** Executions are automatically paused just before 3:55 PM ET to prevent API collisions with Composer's daily rebalancing routines.

*Disclaimer: AlphaBot is an automated execution tool. Algorithmic trading carries significant risk. Always test parameters in Dry Run mode before enabling LIVE\_EXECUTION.*
