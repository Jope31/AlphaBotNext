# **🤖 AlphaBot: Intelligent Profit and Loss Guardian**

Alpha Bot is an advanced, automated risk-management and trailing-stop execution engine designed to interface directly with Composer.trade portfolios. By combining real-time intraday data from Alpaca with K-Nearest Neighbor Monte Carlo simulations, Alpha Bot dynamically protects your capital and locks in profits based on statistical probabilities rather than standard static price drops.

## **🌟 Core Features**

### **🧠 The Monte Carlo "Strangler" (New!)**

Alpha Bot now employs a **5-Period Moving Average (25-minute memory)** of Monte Carlo probabilities to evaluate the actual health of an active run.

* **The Choke Mechanism:** When a symphony's statistical outlook drops below your target threshold, the bot "arms" and dynamically tightens (strangles) the trailing stop distance relative to how far the probability has fallen.  
* **Intraday Noise Reduction:** By using a smoothed moving average, the bot ignores 5-minute flash crashes and standard lunch-hour volume lulls, preventing premature stop-outs and keeping you in the trade for the afternoon continuation.  
* **Dynamic Squeeze Floor:** You control how tight the noose gets via the ```MAX_SQUEEZE_FLOOR``` variable. A floor of ```0.20``` means the stop shrinks to exactly 20% of its normal volatility distance when a trend fully breaks down, locking in the "meat of the move".

### **📉 Volatility-Adjusted & Time-Decayed Stops**

* **ATR Volatility Scaling:** Stop distances automatically widen for high-volatility symphonies and tighten for stable ones based on a rolling 20-day standard deviation calculation.  
* **Logarithmic Time Decay:** Stops start wide in the morning to survive the opening chop and logarithmically squeeze tighter as the day approaches the 4:00 PM ET close.

### **🛡️ Breakeven Defense**

* The bot automatically shifts the trailing stop to a hard 0.0% profit floor once a symphony crosses your defined ```BREAKEVEN_ACTIVATION_PCT``` (e.g., 2.0% profit), guaranteeing a green trade.

### **🎛️ Interactive Web Dashboard**

* **Live Monitoring:** Track all your accounts, real-time High-Water Marks, armed statuses, active stop distances, and live MC probabilities from a unified interface.  
* **Post-Market Sandbox Simulator:** A built-in visualization table that lets you replay the day's price action using interactive sliders. Backtest how different ATR multipliers, MC thresholds, and Strangler Squeeze Floors would have impacted your actual exits.  
* **Settings UI:** Update your core strategy variables directly from the web interface. Changes instantly write back to your .env file and apply to the next execution loop.  
* **Account Liquidation:** Built-in modal to execute manual "Sell to Cash" overrides for entire accounts simultaneously.

## **⚙️ Environment Variables (.env)**

Configure the following variables in your ```.env``` file (or edit them directly via the Web Dashboard's **Edit Variables** panel):

### **API Credentials**

* ```LIVE_EXECUTION``` - Set to True to allow actual sell orders. False enables Dry Run mode.  
* ```COMPOSER_KEY_ID``` - Your Composer API Key ID.  
* ```COMPOSER_SECRET``` - Your Composer API Secret.  
* ```ACCOUNT_UUIDS``` - Comma-separated list of Composer Account UUIDs to track.  
* ```ALPACA_KEY``` - Alpaca Data API Key.  
* ```ALPACA_SECRET``` - Alpaca Data API Secret.  
* ```DISCORD_WEBHOOK_URL``` - (Optional) Webhook URL for execution and alerting logs.

### **Strategy Parameters**

* ```TRIGGER_THRESHOLD_PCT``` - (Default: ```15.0```) The Monte Carlo probability threshold that "arms" the bot and triggers the Strangler.  
* ```BASE_ATR_MULTIPLIER``` - (Default: ```2.0```) Controls the width of the volatility-adjusted trailing stop.  
* ```MIN_MULTIPLIER_FLOOR``` - (Default: ```0.5```) The tightest the normal ATR stop is allowed to get before the Strangler kicks in.  
* ```MAX_SQUEEZE_FLOOR``` - (Default: ```0.20```) The Strangler limit. When armed, the stop will shrink up to this percentage (e.g., 20%) of its original distance.  
* ```TRAILING_STOP_PCT``` - (Default: ```1.5```) Fallback starting stop percentage if historical volatility data is missing.  
* ```ENDING_STOP_PCT``` - (Default: ```0.5```) Fallback ending stop percentage.  
* ```BREAKEVEN_ACTIVATION_PCT``` - (Default: ```2.0```) The profit percentage required to lock the trailing stop at 0.0%.

## **🚀 Setup & Installation**

1. **Clone the Repository:**  
Alphabot is available here: ```https://github.com/Jope31/AlphaBot```
```
   git clone https://github.com/Jope31/AlphaBot.git  
   cd AlphaBot
```

3. **Install Dependencies:**  
```
   pip install flask python-dotenv requests numpy schedule
```

4. **Configure Environment:**  
   Create a ```.env``` file in the root directory and populate it with the variables listed in the Environment section above.  
5. **Launch the Control Center:**  
```
   python app.py
```

   *The Flask server will start on ```http://127.0.0.1:5000``` and automatically spawn the background execution scheduler.*

## **🕒 Scheduling Details**

AlphaBot utilizes a background schedule thread running via ```app.py```.

* **Grace Period:** The bot skips the highly volatile market open and schedules its first run for **9:50 AM ET**.  
* **Intraday Execution:** The bot runs precisely every 5 minutes (e.g., 10:00, 10:05, 10:10) to align with standard 5-minute candle closes.  
* **Rebalance Blackout:** Executions are automatically paused just before 3:55 PM ET to prevent API collisions with Composer's daily rebalancing routines.

*Disclaimer: Alpha Bot is an automated execution tool. Algorithmic trading carries significant risk. Always test parameters in Dry Run mode before enabling LIVE\_EXECUTION.*
