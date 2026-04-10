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
load_dotenv()

COMPOSER_KEY_ID = os.getenv("COMPOSER_KEY_ID")
COMPOSER_SECRET = os.getenv("COMPOSER_SECRET")
ACCOUNT_UUIDS = [uid.strip() for uid in os.getenv("ACCOUNT_UUIDS", "").split(",") if uid.strip()]

ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# --- EXECUTION MODE ---
LIVE_EXECUTION = os.getenv("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")

# --- STRATEGY PARAMETERS (APPROACH A) ---
TRIGGER_THRESHOLD_PCT = float(os.getenv("TRIGGER_THRESHOLD_PCT", "15.0"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "1.5"))
BREAKEVEN_ACTIVATION_PCT = float(os.getenv("BREAKEVEN_ACTIVATION_PCT", "2.0"))

SIMULATION_PATHS = 5000
NEIGHBOR_K = 150 

# ==========================================
# 2. STATE MANAGEMENT & LOGGING
# ==========================================
STATE_FILE = "bot_state.json"
HISTORY_CACHE_FILE = "history_cache.json"

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

def execute_sell_to_cash(actual_symphony_id, account_id):
    url = f"https://api.composer.trade/api/v0.1/deploy/accounts/{account_id}/symphonies/{actual_symphony_id}/go-to-cash"
    try:
        response = requests.post(url, headers=get_composer_headers(), json={})
        print(f"     -> [API Status]: HTTP {response.status_code}")
        
        if response.status_code in [200, 201, 202]:
            try:
                print(f"     -> [Composer Receipt]: {response.json()}")
            except:
                pass
            time.sleep(1.5)
            return True
        else:
            print(f"     !!! [COMPOSER REJECTED]: {response.text}")
            time.sleep(1.5)
            return False
    except Exception as e:
        print(f"     !!! [API CRASH]: {str(e)}")
        return False

def send_discord_alert(symphony_name, current_return, prob_beating, stop_trigger_level, is_live):
    if not DISCORD_WEBHOOK_URL:
        return
        
    title = "🚨 Profit Locked: Trailing Stop Triggered" if is_live else "⚠️ [DRY RUN] Profit Locked"
    color = 15158332 if is_live else 16766720
    action_text = "Executed 'Sell to Cash' via API. Trade queued for Composer execution window." if is_live else "Bypassed (Dry Run Mode)"
        
    payload = {
        "embeds": [{
            "title": title,
            "color": color, 
            "fields": [
                {"name": "Symphony", "value": symphony_name, "inline": True},
                {"name": "Exit Return", "value": f"{current_return:.2f}%", "inline": True},
                {"name": "Stop Level", "value": f"{stop_trigger_level:.2f}%", "inline": True},
                {"name": "MC Probability", "value": f"{prob_beating:.1f}%", "inline": True},
                {"name": "Action Taken", "value": action_text, "inline": False}
            ],
            "footer": {"text": "Alpha Bot • Hybrid Trailing Stop"}
        }]
    }
    requests.post(DISCORD_WEBHOOK_URL, json=payload)

def fetch_alpaca_history(tickers, current_date_str):
    if "SPY" not in tickers:
        tickers.append("SPY")
    
    tickers_list = sorted(list(set(tickers)))
    
    # 1. Check if we already downloaded the static 3-year history today
    if os.path.exists(HISTORY_CACHE_FILE):
        try:
            with open(HISTORY_CACHE_FILE, "r") as f:
                cache = json.load(f)
            # If the cache is from today AND has all the tickers we need, load it!
            if cache.get("date") == current_date_str and cache.get("tickers") == tickers_list:
                print("  -> Loading static 3-year history from local cache.")
                return cache.get("data", {})
        except Exception:
            pass

    # 2. If no valid cache, do the heavy API download
    print(f"Fetching 3-year history from Alpaca for Monte Carlo ({len(tickers)} tickers)...")
    start_date = (datetime.now() - timedelta(days=365*3 + 30)).strftime('%Y-%m-%dT00:00:00Z')
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    historical_data = {}
    batch_size = 30 
    
    for i in range(0, len(tickers_list), batch_size):
        batch = tickers_list[i:i + batch_size]
        symbol_string = ",".join(batch)
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
                                'c': curr_close, 'daily_ret': daily_ret
                            }
            
            page_token = data.get("next_page_token")
            if not page_token:
                break 
                
    # 3. Save to cache so we don't do this again today
    print("  -> History download complete. Saving to daily cache.")
    try:
        with open(HISTORY_CACHE_FILE, "w") as f:
            json.dump({
                "date": current_date_str,
                "tickers": tickers_list,
                "data": historical_data
            }, f)
    except Exception as e:
        print(f"  -> Failed to write cache: {e}")
        
    return historical_data

def get_live_spy_data():
    """Fetches just SPY's live intraday price to feed the Monte Carlo engine"""
    start_date = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%dT00:00:00Z')
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    url = f"https://data.alpaca.markets/v2/stocks/bars?symbols=SPY&timeframe=1Day&start={start_date}&limit=10"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            bars = response.json().get("bars", {}).get("SPY", [])
            if len(bars) >= 2:
                prev_c = bars[-2]['c']
                curr_c = bars[-1]['c']
                daily_ret = (curr_c - prev_c) / prev_c
                return daily_ret * 100.0
    except Exception as e:
        print(f"Error fetching live SPY data: {e}")
    return 0.0

# ==========================================
# 4. MATH ENGINE: MONTE CARLO ONLY
# ==========================================
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
                    
    # Load 3-Year History (Cached once per day)
    historical_data = fetch_alpaca_history(list(all_tickers), current_date_str)
    if not historical_data: return
    
    # Grab live SPY performance right now to feed the MC engine
    spy_today = get_live_spy_data()
    print(f"  -> Live SPY Intraday Return: {spy_today:.2f}%")
    
    print("\nEvaluating Symphonies...")

    for account, symphonies in symphony_data_cache.items():
        for sym in symphonies:
            # Use 'id' for the state dictionary mapping, but extract the true symphony_id for the API execution
            symphony_id = sym['id']
            actual_symphony_id = sym.get('symphony_id', symphony_id) 
            
            symphony_name = sym.get('name', 'Unknown Symphony')
            holdings = sym.get('holdings', [])
            current_return = sym.get('last_percent_change', 0.0) * 100
            
            for h in holdings:
                h['ticker'] = h.get('working_ticker', h.get('ticker'))
            
            if symphony_id not in bot_state:
                bot_state[symphony_id] = {"high_water_mark": current_return, "armed": False, "triggered": False}

            if "triggered" not in bot_state[symphony_id]:
                bot_state[symphony_id]["triggered"] = False

            if current_return > bot_state[symphony_id]["high_water_mark"] and not bot_state[symphony_id]["triggered"]:
                bot_state[symphony_id]["high_water_mark"] = current_return

            high_water_mark = bot_state[symphony_id]["high_water_mark"]
            
            # --- 1. MC Probability Engine ---
            prob_beating = run_monte_carlo(holdings, historical_data, spy_today)
            
            # --- 2. Fixed Trailing Stop & Breakeven Lock Math ---
            safe_hwm = high_water_mark if high_water_mark != -999.0 else current_return
            base_stop_level = safe_hwm - TRAILING_STOP_PCT
            
            if safe_hwm >= BREAKEVEN_ACTIVATION_PCT:
                stop_trigger_level = max(base_stop_level, 0.0)
            else:
                stop_trigger_level = base_stop_level

            if bot_state[symphony_id]["triggered"]:
                stop_trigger_level = -999.0

            print(f"  -> {symphony_name[:35]}: Ret: {current_return:.2f}% | HWM: {high_water_mark:.2f}% | Stop: {stop_trigger_level:.2f}% | ArmProb: {prob_beating:.1f}%")

            bot_state[symphony_id]["name"] = symphony_name
            bot_state[symphony_id]["account"] = account
            bot_state[symphony_id]["current_return"] = current_return
            bot_state[symphony_id]["mc_prob"] = prob_beating
            bot_state[symphony_id]["stop_trigger"] = stop_trigger_level
            save_state(bot_state)
            
            # --- 3. Dual-Arming Mechanism ---
            should_arm = False
            arm_reason = ""
            if prob_beating < TRIGGER_THRESHOLD_PCT:
                should_arm = True
                arm_reason = f"MC Prob {prob_beating:.1f}%"
            elif current_return < 0.0:
                should_arm = True
                arm_reason = "Negative Return"

            if should_arm and not bot_state[symphony_id]["armed"] and not bot_state[symphony_id]["triggered"]:
                bot_state[symphony_id]["armed"] = True
                save_state(bot_state)
                print(f"  *** {symphony_name} ARMED ({arm_reason}) ***")
                
            # --- 4. Execution Check ---
            if bot_state[symphony_id]["armed"]:
                if current_return <= stop_trigger_level:
                    print(f"  🚨 TRAILING STOP HIT FOR {symphony_name} 🚨")
                    
                    if LIVE_EXECUTION:
                        print("  -> [LIVE EXECUTION] Sending sell-to-cash command to Composer API...")
                        # Pass the extracted true symphony_id here
                        success = execute_sell_to_cash(actual_symphony_id, account)
                        if not success:
                            print("     !!! ERROR: Composer API execution failed !!!")
                    else:
                        print("  -> [DRY RUN] Execution bypassed.")
                    
                    send_discord_alert(symphony_name, current_return, prob_beating, stop_trigger_level, LIVE_EXECUTION)
                    
                    bot_state[symphony_id]["armed"] = False
                    bot_state[symphony_id]["triggered"] = True
                    bot_state[symphony_id]["high_water_mark"] = -999.0
                    save_state(bot_state)

if __name__ == "__main__":
    main()
