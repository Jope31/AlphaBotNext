# **Alpha Bot Control Center**

Alpha Bot is an automated trading and risk management system built for Composer.trade. It utilizes Monte Carlo simulations and Volatility-Adjusted Trailing Stops (via Normalized Average True Range) to protect profits and limit drawdowns on algorithmic trading strategies (Symphonies).

The system features a live web dashboard that allows you to monitor the state of your portfolio across multiple accounts and dynamically adjust your API credentials and algorithmic risk parameters on the fly.

## **🌟 Key Features**

* **Live Web Dashboard:** Monitor all symphonies across your Composer accounts (Individual, Roth IRA, Trad. IRA) in real-time. View Current Returns, High Water Marks, and Monte Carlo Probabilities.  
* **In-Browser Control Panel:** Safely update your Composer keys, Alpaca keys, Account UUIDs, and all algorithmic risk variables directly from the web interface—no coding required.  
* **Monte Carlo "Arming":** Simulates 5,000 potential future paths based on historical Alpaca data. If the probability of beating the current return drops below a threshold, the bot "arms" a trailing stop.  
* **Volatility-Adjusted Stops:** Calculates the Normalized Average True Range (NATR) of your specific holdings to create a dynamic trailing stop that breathes with the market.  
* **The "Profit Parachute":** Automatically tightens the trailing stop multiplier when a symphony goes parabolic (Current Return \> Volatility).  
* **Discord Alerts:** Rich webhook notifications sent immediately upon execution.

## **📂 Project Structure**

Alpha\_Bot\_Project/  
├── .env&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp; \# API Keys and Algorithm Parameters  
├── alpha\_bot\_final.py&emsp;\# Core Bot Engine (Math, API Calls, Execution)  
├── app.py&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;\# Flask Web Server & Background Scheduler  
├── bot\_state.json&emsp;&emsp;&emsp;\# Local memory (High Water Marks, Armed status)  
└── templates/  
&emsp;&emsp;└── index.html&emsp;&emsp;&emsp;\# Web Dashboard UI

## **🚀 Installation & Setup**

1. **Install Python Dependencies:**  
   Ensure you have Python installed, then open your terminal and run:  
   ```pip install flask schedule python-dotenv requests numpy```

2. **Initial Configuration:**  
   Create an empty .env file in your root folder (or use your existing one). You can input all your keys directly through the web dashboard once it's running.
   
4. **Run the Control Center:**  
   Start the master application:  
   ```python app.py```

   *Note: This starts a background thread that runs alpha\_bot\_final.py every 5 minutes, while simultaneously hosting the web dashboard.*  
5. **Access the Dashboard:**  
   Open your browser and navigate to http://localhost:5000. Click **Edit Variables** to configure your accounts and risk settings.

## **🎛️ The Control Panel**

You can adjust these settings directly from the web dashboard by clicking **Edit Variables**. Changes are saved to your .env file and applied instantly to the very next scheduled bot execution.

### **API Credentials & Accounts**

* **Composer Keys:** Required to read your portfolio and trigger "Sell to Cash" executions.  
* **Alpaca Keys:** Required to fetch the historical daily data used in the Monte Carlo simulations. (A free data-only or paper trading key works perfectly).  
* **Account UUIDs:** Enter up to 3 Composer Account UUIDs. The dashboard will automatically label these as **Individual**, **Roth IRA**, and **Trad. IRA**.

### **Strategy Variables**

| Variable | Default | Description & Tuning |
| :---- | :---- | :---- |
| TRIGGER\_THRESHOLD\_PCT | 15.0 | **The "Arming" switch.** The % of Monte Carlo paths needed to beat the current return. *Lower (5.0)* \= Aggressive/Patient. *Higher (25.0)* \= Conservative/Nervous. |
| ATR\_LOOKBACK\_DAYS | 14 | **Volatility memory bank.** Days of history to calculate normal swings. *Lower (7)* \= Hyper-sensitive to recent chop. *Higher (30)* \= Smoother, consistent stops. |
| BASE\_ATR\_MULTIPLIER | 2.0 | **Primary leash.** Multiplies normal volatility to set the stop distance. *Lower (1.25)* \= Tight leash, locks in fast. *Higher (3.0)* \= Diamond hands, ignores noise. |
| RED\_DAY\_ATR\_MULTIPLIER | 0.75 | **Defensive leash.** Used ONLY if SPY opens lower than yesterday's close. *Lower (0.25)* \= Panic button on red days. *Higher (1.5)* \= Gives room for a morning recovery. |
| MIN\_MULTIPLIER\_FLOOR | 0.5 | **Profit Parachute limit.** The tightest the stop is allowed to get when a stock goes parabolic. *Lower (0.1)* \= Strangles outliers instantly. *Higher (2.0)* \= Disables parachute. |

## **🛠️ How It Works (The Execution Loop)**

1. **Scheduler:** app.py triggers alpha\_bot\_final.py every 5 minutes.  
2. **Data Fetching:** The bot retrieves current holdings/returns from Composer and historical price data from Alpaca.  
3. **Evaluation:** Updates the local bot\_state.json with the highest observed return (High Water Mark).  
4. **Monte Carlo:** Calculates the probability of beating the current return by end-of-day. If below TRIGGER\_THRESHOLD\_PCT, the symphony is marked "armed": true.  
5. **Execution:** If Armed, the bot calculates the trailing stop based on Volatility (NATR) \* Multiplier. If the drawdown from the peak exceeds this stop, it executes a sell-all command via the Composer API and sends a Discord alert.  
6. **Dashboard Monitoring:** The web UI reads bot\_state.json every 5 seconds to provide a live-updating view of the entire system's state.
