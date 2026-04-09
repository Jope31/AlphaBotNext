import os
import sys
import time
import json
import requests
import numpy as np
from datetime import datetime, timedelta, timezone, time as dt_time
from dotenv import load_dotenv

# ==========================================
# 1. CONFIGURATION & CREDENTIALS
# ==========================================
# Load environment variables from .env file
load_dotenv()

COMPOSER_KEY_ID = os.getenv("COMPOSER_KEY_ID")
COMPOSER_SECRET = os.getenv("COMPOSER_SECRET")
ACCOUNT_UUIDS = [uid.strip() for uid in os.getenv("ACCOUNT_UUIDS", "").split(",") if uid.strip()]

ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# --- EXECUTION MODE ---
LIVE_EXECUTION = os.getenv("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")

# Algorithm Parameters 
TRIGGER_THRESHOLD_PCT = float(os.getenv("TRIGGER_THRESHOLD_PCT", "15.0"))
SIMULATION_PATHS = 5000
NEIGHBOR_K = 150 

# Trailing Stop & Volatility Settings
ATR_LOOKBACK_DAYS = int(os.getenv("ATR_LOOKBACK_DAYS", "14"))
BASE_ATR_MULTIPLIER = float(os.getenv("BASE_ATR_MULTIPLIER", "2.0"))
RED_DAY_ATR_MULTIPLIER = float(os.getenv("RED_DAY_ATR_MULTIPLIER", "0.75"))
MIN_MULTIPLIER_FLOOR = float(os.getenv("MIN_MULTIPLIER_FLOOR", "0.5"))

# ==========================================
# 2. STATE MANAGEMENT & LOGGING
# ==========================================
STATE_FILE = "bot_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

# ==========================================
# 3. API CONNECTORS & RATE LIMIT HANDLING
# ==========================================

def get_composer_headers():
    return {
        "x-api-key-id": COMPOSER_KEY_ID,
        "authorization": f"Bearer {COMPOSER_SECRET}",
        "Content-Type": "application/json"
    }

def fetch_symphony_stats(account_id):
    url = f"https://api.composer.trade/api/v0.1/portfolio/accounts/{account_id}/symphony-stats-meta"
    response = requests.get(url, headers=get_composer_headers())
    time.sleep(1.5)  
    if response.status_code == 200:
        return response.json().get("symphonies", [])
    print(f"Error fetching account {account_id}: {response.text}")
    return []

def execute_sell_to_cash(symphony_id, account_id):
    url = f"https://api.composer.trade/api/v0.1/deploy/symphonies/{symphony_id}/sell-all"
    response = requests.post(url, headers=get_composer_headers())
    time.sleep(1.5)
    return response.status_code in [200, 201, 202]

def send_discord_alert(symphony_name, current_return, prob_beating, drawdown, stop_distance, is_live):
    if not DISCORD_WEBHOOK_URL:
        return
        
    title = "🚨 Profit Locked: Trailing Stop Triggered" if is_live else "⚠️ [DRY RUN] Profit Locked"
    color = 15158332 if is_live else 16766720
    action_text = "Executed 'Sell to Cash' via API." if is_live else "Bypassed (Dry Run Mode)"
        
    payload = {
        "embeds": [{
            "title": title,
            "color": color, 
            "fields": [
                {"name": "Symphony", "value": symphony_name, "inline": True},
                {"name": "Exit Return", "value": f"{current_return:.2f}%", "inline": True},
                {"name": "MC Probability", "value": f"{prob_beating:.1f}%", "inline": True},
                {"name": "Drawdown from Peak", "value": f"{drawdown:.2f}%", "inline": True},
                {"name": "Dynamic Stop Level", "value": f"{stop_distance:.2f}%", "inline": True},
                {"name": "Action Taken", "value": action_text, "inline": False}
            ],
            "footer": {"text": "Alpha Bot • Volatility-Adjusted Trailing Stop"}
        }]
    }
    requests.post(DISCORD_WEBHOOK_URL, json=payload)

def fetch_alpaca_history(tickers):
    print(f"Fetching 3-year history from Alpaca for {len(tickers)} tickers in batches...")
    if "SPY" not in tickers:
        tickers.append("SPY")
        
    start_date = (datetime.now() - timedelta(days=365*3 + 30)).strftime('%Y-%m-%dT00:00:00Z')
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    historical_data = {}
    batch_size = 30 
    
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        symbol_string = ",".join(list(set(batch)))
        print(f"  -> Downloading batch {i//batch_size + 1}: {len(batch)} tickers...")
        
        page_token = None
        while True:
            url = f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbol_string}&timeframe=1Day&start={start_date}&limit=10000&adjustment=split"
            if page_token:
                url += f"&page_token={page_token}"
                
            response = requests.get(url, headers=headers)
            if response.status_code != 200:
                print(f"Alpaca API Error on batch: {response.text}")
                break
                
            data = response.json()
            if "bars" in data:
                for symbol, bars in data["bars"].items():
                    for j in range(1, len(bars)):
                        prev_close = bars[j-1]['c']
                        curr_close = bars[j]['c']
                        if prev_close > 0:
                            daily_ret = (curr_close - prev_close) / prev_close
                            date_str = bars[j]['t'][:10]
                            if date_str not in historical_data:
                                historical_data[date_str] = {}
                            historical_data[date_str][symbol] = {
                                'o': bars[j]['o'], 'h': bars[j]['h'], 'l': bars[j]['l'],
                                'c': curr_close, 'prev_c': prev_close, 'daily_ret': daily_ret
                            }
            
            page_token = data.get("next_page_token")
            if not page_token:
                break 
                
    print("  -> History download complete.")
    return historical_data

# ==========================================
# 4. MATH ENGINE: VOLATILITY & MONTE CARLO
# ==========================================

def calculate_portfolio_natr(holdings, historical_data, lookback_days=14):
    valid_dates = sorted(list(historical_data.keys()))[-lookback_days:]
    weighted_natr = 0.0
    for h in holdings:
        ticker = h.get('working_ticker', h.get('ticker'))
        weight = h.get('allocation', 0.0)
        true_ranges = []
        closes = []
        for date in valid_dates:
            data = historical_data[date].get(ticker)
            if data:
                high, low, prev_c = data['h'], data['l'], data['prev_c']
                tr = max(high - low, abs(high - prev_c), abs(low - prev_c))
                true_ranges.append(tr)
                closes.append(data['c'])
        if true_ranges and closes and closes[-1] > 0:
            avg_tr = sum(true_ranges) / len(true_ranges)
            natr_pct = (avg_tr / closes[-1]) * 100 
            weighted_natr += (natr_pct * weight)
    return weighted_natr if weighted_natr > 0 else 1.5 

def run_monte_carlo(holdings, historical_data, spy_today_return):
    current_symphony_return = sum(
        (h.get('last_percent_change', 0.0) * 100.0) * h.get('allocation', 0.0) 
        for h in holdings if h.get('last_percent_change') is not None
    )
    valid_dates = sorted(list(historical_data.keys())) 
    if len(valid_dates) < 20: return 100.0 

    distances = []
    for date in valid_dates:
        spy_hist = historical_data[date].get("SPY", {}).get("daily_ret", 0.0)
        distances.append((abs(spy_hist - (spy_today_return / 100.0)), date))
    distances.sort(key=lambda x: x[0])
    nearest_days = [d[1] for d in distances[:NEIGHBOR_K]]

    weights = {h['ticker']: h.get('allocation', 0.0) for h in holdings}
    latest_valid_day = valid_dates[-1]
    missing_tickers = [t for t in weights.keys() if t not in historical_data.get(latest_valid_day, {})]

    sim_results = np.zeros(SIMULATION_PATHS)
    for i in range(SIMULATION_PATHS):
        random_day = np.random.choice(nearest_days)
        path_return = 0.0
        for ticker, weight in weights.items():
            if ticker in missing_tickers:
                daily_ret = historical_data[random_day].get("SPY", {}).get("daily_ret", 0.0)
            else:
                daily_ret = historical_data[random_day].get(ticker, {}).get("daily_ret", 0.0)
            path_return += (daily_ret * 100.0) * weight
        sim_results[i] = path_return

    sim_results.sort()
    below_count = np.searchsorted(sim_results, current_symphony_return)
    return ((SIMULATION_PATHS - below_count) / SIMULATION_PATHS) * 100.0

# ==========================================
# 5. MAIN EXECUTION LOOP
# ==========================================

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Alpha Bot Waking Up...")
    print(f"MODE: {'LIVE EXECUTION (DANGER)' if LIVE_EXECUTION else 'DRY RUN (SAFE)'}")
    
    # --- MARKET HOURS GATEKEEPER ---
    force_run = "--force" in sys.argv
    utc_now = datetime.now(timezone.utc)
    
    try:
        from zoneinfo import ZoneInfo
        current_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Robust fallback for systems without tzdata installed
        if 3 <= utc_now.month <= 11:
            current_et = utc_now - timedelta(hours=4) # EDT approx
        else:
            current_et = utc_now - timedelta(hours=5) # EST approx
            
    is_weekday = current_et.weekday() < 5
    current_time = current_et.time()
    market_open = dt_time(9, 30)
    market_close = dt_time(16, 0)
    
    if not is_weekday or not (market_open <= current_time <= market_close):
        if not force_run:
            print(f"  -> Market closed (ET: {current_et.strftime('%a %H:%M')}). Sleeping to conserve API limits...")
            return
        else:
            print(f"  -> Market closed, but --force flag detected! Bypassing gatekeeper...")
    
    # --- END GATEKEEPER ---

    if not COMPOSER_KEY_ID or not ALPACA_KEY:
        print("CRITICAL: Missing API Keys. Please check your .env file.")
        return

    bot_state = load_state()
    current_date_str = current_et.strftime('%Y-%m-%d')
    
    if bot_state.get("date") != current_date_str:
        print(f"  -> New trading day detected ({current_date_str} ET). Wiping old state memory.")
        bot_state = {"date": current_date_str}
        save_state(bot_state)

    all_tickers = set()
    symphony_data_cache = {} 
    
    for account in ACCOUNT_UUIDS:
        symphonies = fetch_symphony_stats(account)
        symphony_data_cache[account] = symphonies
        for sym in symphonies:
            for holding in sym.get('holdings', []):
                raw_ticker = holding.get('ticker', '')
                clean_ticker = raw_ticker.split('::')[-1].split('//')[0]
                alpaca_ticker = clean_ticker.replace('/', '.')
                if alpaca_ticker:
                    all_tickers.add(alpaca_ticker)
                    holding['working_ticker'] = alpaca_ticker
                    
    historical_data = fetch_alpaca_history(list(all_tickers))
    if not historical_data: return
    
    latest_date = sorted(historical_data.keys())[-1]
    latest_spy_data = historical_data[latest_date].get("SPY", {})
    spy_today = latest_spy_data.get("daily_ret", 0.0) * 100 
    spy_open = latest_spy_data.get('o', 1)
    spy_prev_c = latest_spy_data.get('prev_c', 1)
    spy_gap_ret = ((spy_open - spy_prev_c) / spy_prev_c) * 100 if spy_prev_c > 0 else 0.0
    market_tone_red = spy_gap_ret < 0
    
    print(f"Market Open Tone: {'RED' if market_tone_red else 'GREEN'} (Gap: {spy_gap_ret:.2f}%)\n")

    for account, symphonies in symphony_data_cache.items():
        print(f"Evaluating Account: {account}")
        for sym in symphonies:
            symphony_id = sym['id']
            symphony_name = sym.get('name', 'Unknown Symphony')
            holdings = sym.get('holdings', [])
            current_return = sym.get('last_percent_change', 0.0) * 100
            
            for h in holdings:
                h['ticker'] = h.get('working_ticker', h.get('ticker'))
            
            if symphony_id not in bot_state:
                bot_state[symphony_id] = {"high_water_mark": current_return, "armed": False}

            if current_return > bot_state[symphony_id]["high_water_mark"]:
                bot_state[symphony_id]["high_water_mark"] = current_return

            high_water_mark = bot_state[symphony_id]["high_water_mark"]
            
            # --- MATH CALCS ---
            prob_beating = run_monte_carlo(holdings, historical_data, spy_today)
            portfolio_natr = calculate_portfolio_natr(holdings, historical_data, ATR_LOOKBACK_DAYS)
            
            # Calculate dynamic stop parameters for UI
            active_multiplier = RED_DAY_ATR_MULTIPLIER if market_tone_red else BASE_ATR_MULTIPLIER
            if high_water_mark > portfolio_natr and portfolio_natr > 0:
                outlier_ratio = high_water_mark / portfolio_natr
                active_multiplier = max(MIN_MULTIPLIER_FLOOR, active_multiplier / outlier_ratio)
            
            trailing_stop_distance = portfolio_natr * active_multiplier
            safe_hwm = high_water_mark if high_water_mark != -999.0 else current_return
            stop_trigger_level = safe_hwm - trailing_stop_distance
            
            print(f"  -> {symphony_name}: Live Return = {current_return:.2f}% | High Water Mark = {high_water_mark:.2f}% | Prob Beating = {prob_beating:.1f}% | NATR = {portfolio_natr:.2f}%")

            # --- SAVE FULL STATE FOR UI ---
            bot_state[symphony_id]["name"] = symphony_name
            bot_state[symphony_id]["account"] = account
            bot_state[symphony_id]["current_return"] = current_return
            bot_state[symphony_id]["mc_prob"] = prob_beating
            bot_state[symphony_id]["natr"] = portfolio_natr
            bot_state[symphony_id]["stop_distance"] = trailing_stop_distance
            bot_state[symphony_id]["stop_trigger"] = stop_trigger_level
            save_state(bot_state)
            
            if prob_beating < TRIGGER_THRESHOLD_PCT and not bot_state[symphony_id]["armed"]:
                bot_state[symphony_id]["armed"] = True
                save_state(bot_state)
                print(f"  *** WARNING: {symphony_name} ARMED. ***")
                
            if bot_state[symphony_id]["armed"]:
                drawdown_from_peak = high_water_mark - current_return
                
                if drawdown_from_peak >= trailing_stop_distance:
                    print(f"  *** TRAILING STOP HIT FOR {symphony_name} ***")
                    
                    if LIVE_EXECUTION:
                        print("  -> [LIVE EXECUTION] Sending sell-to-cash command to Composer API...")
                        success = execute_sell_to_cash(symphony_id, account)
                        if not success:
                            print("     !!! ERROR: Composer API execution failed !!!")
                    else:
                        print("  -> [DRY RUN] Execution bypassed.")
                    
                    send_discord_alert(symphony_name, current_return, prob_beating, drawdown_from_peak, trailing_stop_distance, LIVE_EXECUTION)
                    
                    bot_state[symphony_id]["armed"] = False
                    bot_state[symphony_id]["high_water_mark"] = -999.0
                    save_state(bot_state)

if __name__ == "__main__":
    main()
