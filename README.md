# **🤖 AlphaBot: Intelligent Profit and Loss Guardian**

AlphaBot is an automated, risk-management sidecar application designed to monitor your Composer.trade accounts. It utilizes historical Alpaca market data, a local Monte Carlo simulation engine, and per-symphony volatility metrics to execute dynamic "sell-to-cash" API calls when a symphony begins to break down.

It acts as an intelligent safety net, protecting your profits and cutting your losses before Composer's end-of-day rebalance window.

## **✨ Key Features**

* **Volatility-Adjusted Stops:** Stop-loss distances are uniquely calibrated to each individual symphony based on its weighted 20-day historical standard deviation. High-volatility symphonies get more breathing room; low-volatility symphonies get tighter leashes.  
* **Monte Carlo Probability Engine:** Runs 5,000 simulations against historical data (using K-Nearest Neighbors based on the current day's SPY return) to determine the statistical probability that a symphony will close higher than its current intraday return.  
* **Time-Decay "Squeeze" Logic:** The dynamic trailing stop logarithmically tightens as the trading day progresses, locking in profits tighter as we approach the closing bell.  
* **Hysteresis Disarming:** If a symphony recovers from a morning dip (probability doubles and returns go positive), the bot will stand down and disarm, preventing premature sell-offs on healthy pullbacks.  
* **Morning Grace Period:** Ignores the extreme volatility and price-discovery of the first 20 minutes of the market open (starts evaluating at 9:50 AM ET).  
* **Local Web Dashboard & Sandbox:** Features a beautifully designed local control center (```localhost:5000```) with a real-time ledger, live variable tuning, and a **Post-Market Simulator** to backtest your settings.  
* **Discord Integration:** Sends rich webhooks detailing executed trades, exit returns, probabilities, and triggers.

## **🧠 How the Logic Works (The Pipeline)**

AlphaBot evaluates every symphony in your Composer account on a 5-minute loop between 9:50 AM ET and 4:00 PM ET (excluding the 3:54 PM \- 4:00 PM rebalance blackout window).

1. **High-Water Mark (HWM) Tracking:** It records the highest intraday return a symphony achieves.  
2. **Arming Condition:** The bot "Arms" a symphony (putting it on high alert) if:  
   * The Monte Carlo probability of finishing strong drops below your ```TRIGGER_THRESHOLD_PCT``` (e.g., 15%).  
   * *OR* the symphony experiences a sustained negative return (drops below ```-0.50%```).  
3. **Volatility Calibration:** It calculates a dynamic trailing stop distance based on ```volatility * BASE_ATR_MULTIPLIER```.  
4. **Execution Check:** If an armed symphony's current return drops below the HWM minus the dynamic trailing stop distance, the bot triggers a Composer API command to liquidate that specific symphony to cash immediately.  
5. **Breakeven Lock:** If a symphony achieves a high enough return (e.g., ```> 2.0%```), the dynamic stop is hard-floored at ```0.0%```, ensuring a winning trade cannot turn into a losing trade.

## **⚙️ Environment Variables (```.env```)**

Configure the bot by creating a ```.env``` file in the root directory. You can also edit these live via the Web Dashboard.

### **Core Credentials**

* ```LIVE_EXECUTION```: Set to True to allow actual API calls. False will only simulate and log triggers.  
* ```COMPOSER_KEY_ID```: Your Composer API Key.  
* ```COMPOSER_SECRET```: Your Composer API Secret.  
* ```ACCOUNT_UUIDS```: Comma-separated list of Composer Account UUIDs to monitor.  
* ```ALPACA_KEY```: Alpaca Market Data API Key.  
* ```ALPACA_SECRET```: Alpaca Market Data API Secret.  
* ```DISCORD_WEBHOOK_URL```: (Optional) Discord webhook for alert notifications.

### **Strategy Parameters**

* ```TRIGGER_THRESHOLD_PCT```: (Default ```15.0```) The Monte Carlo probability threshold that "arms" the bot.  
* ```BASE_ATR_MULTIPLIER```: (Default ```2.0```) The multiplier applied to a symphony's 20-day volatility to calculate the morning trailing stop distance.  
* ```MIN_MULTIPLIER_FLOOR```: (Default ```0.5```) The absolute minimum percentage distance allowed for a stop, regardless of how low volatility gets.  
* ```BREAKEVEN_ACTIVATION_PCT```: (Default ```2.0```) Once a symphony hits this intraday return, the trailing stop will never be allowed to drop below 0.0% (guaranteeing a breakeven exit).  
* *(Legacy)* ```TRAILING_STOP_PCT``` & ```ENDING_STOP_PCT```: Fallback flat percentages used only if Alpaca historical data fails to download.

## **🚀 Installation & Usage**

1. **Clone the repository:**  
```
git clone [https://github.com/yourusername/AlphaBot.git\](https://github.com/yourusername/AlphaBot.git)  
cd AlphaBot
```

2. **Install requirements:**  
```
pip install -r requirements.txt
```

   *(Requires: ```flask```, ```requests```, ```numpy```, ```python-dotenv```, ```schedule```)*  
4. **Set up your ```.env``` file:**  
   Use the provided ```.env``` as a template and fill in your API keys.  
5. **Run the Control Center:**  
```
python app.py
```

   This will boot the background scheduler and the Flask web server.  
6. **Access the Dashboard:**  
   Open your web browser and go to ```http://localhost:5000```.

## **🛑 Important Disclaimer**

**Use at your own risk.** AlphaBot interacts directly with your live brokerage/Composer accounts when ```LIVE_EXECUTION=True```.

* Always test your parameters in ```LIVE_EXECUTION=False``` (Dry Run Mode) first.  
* Use the built-in Post-Market Simulator to understand how your ATR and threshold variables affect different symphonies.  
* The author is not responsible for financial losses, missed executions due to API rate limits, or unexpected bot behavior.
