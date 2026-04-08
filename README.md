# **Alpha Bot Control Center**

Alpha Bot is an automated trading and risk management system built for Composer.trade. It utilizes Monte Carlo simulations and Volatility-Adjusted Trailing Stops (via Normalized Average True Range) to protect profits and limit drawdowns on algorithmic trading strategies (Symphonies).

The system features a live web dashboard that allows you to monitor the state of your portfolio across multiple accounts and dynamically adjust algorithmic risk parameters on the fly.

## **🌟 Key Features**

* **Live Web Dashboard:** Monitor all symphonies across multiple Composer accounts in real-time. View Current Returns, High Water Marks, and Monte Carlo Probabilities.  
* **Monte Carlo "Arming":** Simulates 5,000 potential future paths based on historical Alpaca data. If the probability of beating the current return drops below a threshold, the bot "arms" a trailing stop.  
* **Volatility-Adjusted Stops:** Calculates the Normalized Average True Range (NATR) of your specific holdings to create a dynamic trailing stop that breathes with the market.  
* **The "Profit Parachute":** Automatically tightens the trailing stop multiplier when a symphony goes parabolic (Current Return \> Volatility).  
* **Discord Alerts:** Rich webhook notifications sent immediately upon execution.  
* **Strategy Control Panel:** Edit your .env risk variables directly from the web interface to change the bot's behavior without restarting or editing code.

## **📂 Project Structure**

Alpha\_Bot\_Project/  
├── .env                    \# API Keys and Algorithm Parameters  
├── alpha\_bot\_final.py      \# Core Bot Engine (Math, API Calls, Execution)  
├── app.py                  \# Flask Web Server & Background Scheduler  
├── bot\_state.json          \# Local memory (High Water Marks, Armed status)  
└── templates/  
    └── index.html          \# Web Dashboard UI

## **🚀 Installation & Setup**

1. **Install Python Dependencies:**  
   Ensure you have Python installed, then run:  
   pip install flask schedule python-dotenv requests numpy jinja2

2. **Configure Environment Variables:**  
   Update your .env file with your API credentials (Composer, Alpaca, Discord) and your Account UUIDs (comma-separated).  
3. **Run the Control Center:**  
   Instead of running the bot directly or using Windows Task Scheduler, start the master app:  
   python app.py

   *Note: This starts a background thread that runs alpha\_bot\_final.py every 5 minutes, while simultaneously hosting the web dashboard.*  
4. **Access the Dashboard:**  
   Open your browser and navigate to http://localhost:5000.

## **🎛️ Strategy Control Panel Variables**

You can adjust these values directly from the web dashboard by clicking **Edit Variables**. Changes are saved to your .env file and applied on the very next bot execution.

| **Variable** | **Default** | **Description & Tuning** |

| TRIGGER\_THRESHOLD\_PCT | 15.0 | **The "Arming" switch.** The % of Monte Carlo paths needed to beat the current return. *Lower (5.0)* \= Aggressive/Patient. *Higher (25.0)* \= Conservative/Nervous. |

| ATR\_LOOKBACK\_DAYS | 14 | **Volatility memory bank.** Days of history to calculate normal swings. *Lower (7)* \= Hyper-sensitive to recent chop. *Higher (30)* \= Smoother, consistent stops. |

| BASE\_ATR\_MULTIPLIER | 2.0 | **Primary leash.** Multiplies normal volatility to set the stop distance. *Lower (1.25)* \= Tight leash, locks in fast. *Higher (3.0)* \= Diamond hands, ignores noise. |

| RED\_DAY\_ATR\_MULTIPLIER | 0.75 | **Defensive leash.** Used ONLY if SPY opens lower than yesterday's close. *Lower (0.25)* \= Panic button on red days. *Higher (1.5)* \= Gives room for a morning recovery. |

| MIN\_MULTIPLIER\_FLOOR | 0.5 | **Profit Parachute limit.** The tightest the stop is allowed to get when a stock goes parabolic. *Lower (0.1)* \= Strangles outliers instantly. *Higher (2.0)* \= Disables parachute. |

## **🛠️ How It Works (The Execution Loop)**

1. **Scheduler:** app.py triggers alpha\_bot\_final.py every 5 minutes.  
2. **Data Fetching:** The bot retrieves current holdings/returns from Composer and historical price data from Alpaca.  
3. **Evaluation:** Updates the bot\_state.json with the highest observed return (High Water Mark).  
4. **Monte Carlo:** Calculates the probability of beating the current return by end-of-day. If below TRIGGER\_THRESHOLD\_PCT, the symphony is marked "armed": true.  
5. **Execution:** If Armed, the bot calculates the trailing stop based on Volatility (NATR) \* Multiplier. If the drawdown from the peak exceeds this stop, it executes a sell-all command via Composer API and sends a Discord alert.  
6. **Dashboard:** The web UI reads bot\_state.json every 5 seconds to provide a live-updating view of the entire system.
