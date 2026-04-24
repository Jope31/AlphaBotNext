# **AlphaBot Quant Features: Integration Guide**

Because your original alpha\_bot\_execution.py script acts as a bridge between Composer (Symphonies) and Alpaca, we need to map the stock-specific volatility data to your Symphony-level execution logic.

Follow these 4 steps to patch your original file.

### **Step 1: Add the Intraday Analysis Functions**

*Paste these functions near the top of your file, right after your Alpaca API initialization.*

import glob  
from alpaca\_trade\_api.rest import TimeFrame

def analyze\_intraday\_data(api\_client, symbols, target\_date\_et, lookback\_days=5):  
    """Fetches multi-day 1-minute bars to calculate rolling Noise Floor and EOD Volatility."""  
    intraday\_stats \= {}  
    try:  
        start\_date \= target\_date\_et \- timedelta(days=lookback\_days \+ 2\)  
        start\_time \= start\_date.replace(hour=9, minute=30, second=0, microsecond=0)  
        end\_time \= target\_date\_et.replace(hour=16, minute=0, second=0, microsecond=0)  
          
        start\_str \= start\_time.isoformat()  
        end\_str \= end\_time.isoformat()

        for symbol in symbols:  
            bars \= api\_client.get\_bars(symbol, TimeFrame.Minute, start=start\_str, end=end\_str).df  
            if bars.empty: continue  
                  
            if bars.index.tz is None:  
                bars.index \= bars.index.tz\_localize('UTC').tz\_convert('US/Eastern')  
            else:  
                bars.index \= bars.index.tz\_convert('US/Eastern')

            bars\['return'\] \= bars.groupby(bars.index.date)\['close'\].pct\_change()  
            noise\_floor \= bars\['return'\].std()

            mid\_day\_bars \= bars.between\_time('09:30', '15:30')  
            eod\_bars \= bars.between\_time('15:30', '16:00')

            mid\_day\_vol \= mid\_day\_bars\['return'\].std() if len(mid\_day\_bars) \> 2 else 0.0001  
            eod\_vol \= eod\_bars\['return'\].std() if len(eod\_bars) \> 2 else 0.0001  
              
            if mid\_day\_vol \== 0 or pd.isna(mid\_day\_vol): mid\_day\_vol \= 0.0001  
            if eod\_vol \== 0 or pd.isna(eod\_vol): eod\_vol \= 0.0001

            eod\_vol\_ratio \= eod\_vol / mid\_day\_vol

            tick\_threshold \= 2  
            if noise\_floor \> 0.0015: tick\_threshold \= 3  
            elif noise\_floor \< 0.0003: tick\_threshold \= 1

            intraday\_stats\[symbol\] \= {  
                "noise\_floor\_pct": round(float(noise\_floor) \* 100, 4),  
                "eod\_vol\_ratio": round(float(eod\_vol\_ratio), 2),  
                "recommended\_tick\_threshold": tick\_threshold  
            }  
    except Exception as e:  
        print(f"Error during intraday analysis: {e}")  
    return intraday\_stats

def get\_latest\_post\_mortem\_profiles():  
    """Loads yesterday's dynamic intraday profiles."""  
    try:  
        files \= glob.glob("post\_mortem\_\*.json")  
        if not files: return {}  
        latest\_file \= sorted(files, reverse=True)\[0\]  
        with open(latest\_file, 'r') as f:  
            return json.load(f).get("intraday\_analysis", {})  
    except:  
        return {}

### **Step 2: Inject Dynamic States upon Symphony Initialization**

*Find the section where you initialize bot\_state\[symphony\_id\] for the day. Because Symphonies hold multiple tickers, we will aggregate the volatility of the underlying assets (taking the highest volatility metric to be safe).*

\# Insert this right before assigning default values to bot\_state\[symphony\_id\]  
profiles \= get\_latest\_post\_mortem\_profiles()

\# Extract the list of ticker symbols currently held in this Symphony from Composer  
\# (Assuming you have a variable \`symphony\_holdings\` like \["TQQQ", "SQQQ"\])  
symphony\_tick\_threshold \= 2  
symphony\_eod\_ratio \= 1.0

if profiles and symphony\_holdings:  
    ticks \= \[profiles.get(sym, {}).get("recommended\_tick\_threshold", 2\) for sym in symphony\_holdings\]  
    eods \= \[profiles.get(sym, {}).get("eod\_vol\_ratio", 1.0) for sym in symphony\_holdings\]  
      
    \# Base the Symphony's threshold on its most volatile holding  
    if ticks: symphony\_tick\_threshold \= max(ticks)   
    if eods: symphony\_eod\_ratio \= max(eods)

\# Now, add these to your bot\_state assignment:  
bot\_state\[symphony\_id\]\["tick\_threshold"\] \= symphony\_tick\_threshold  
bot\_state\[symphony\_id\]\["eod\_vol\_ratio"\] \= symphony\_eod\_ratio

### **Step 3: Apply the EOD Relief Valve to the Strangler**

*Find your Strangler logic (where you calculate the decay\_curve based on time\_ratio). Insert this right after the initial decay\_curve calculation.*

\# Your existing code likely looks something like this:  
\# decay\_curve \= math.log10(1 \+ 9 \* time\_ratio)

\# \--- INJECT EOD RELIEF VALVE HERE \---  
current\_time\_et \= datetime.now(EST)  
if current\_time\_et.hour \== 15 and current\_time\_et.minute \>= 30:  
    eod\_ratio \= bot\_state\[symphony\_id\].get('eod\_vol\_ratio', 1.0)  
    if eod\_ratio \> 1.2:   
        relief\_factor \= min(0.15, (eod\_ratio \- 1.0) \* 0.1)   
        decay\_curve \= decay\_curve \* (1.0 \- relief\_factor)  
        print(f"  \-\> \[{symphony\_name}\] Applied EOD Relief Valve. Relaxing squeeze by {relief\_factor\*100:.1f}%")  
\# \------------------------------------

\# Then your code continues:  
\# dynamic\_trailing\_stop \= morning\_stop \- ((morning\_stop \- afternoon\_stop) \* decay\_curve)

### **Step 4: Implement Dynamic Tick Confirmation**

*Find the exact line where you check if the stop has been breached twice (around line 538 in your script based on the trace). Replace the hardcoded 2 with the dynamic threshold.*

**Change this:**

elif bot\_state\[symphony\_id\]\["below\_stop\_count"\] \>= 2:

**To this:**

tick\_threshold \= bot\_state\[symphony\_id\].get("tick\_threshold", 2\)  
elif bot\_state\[symphony\_id\]\["below\_stop\_count"\] \>= tick\_threshold:  
