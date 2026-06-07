"""Core execution logic for Alpha Bot with SQLite State Management and EOD Autotuner."""

import os
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import time
import json
import math
from datetime import datetime, timedelta, timezone, time as dt_time

import requests
import numpy as np
import pandas as pd


from dotenv import load_dotenv

# Import our SQLite DB Manager
import database
import math_engine
import reporting
import autotuner

# ==========================================
# 1. CONFIGURATION & CREDENTIALS
# ==========================================


ENV_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_FILE_PATH)

COMPOSER_KEY_ID = os.getenv("COMPOSER_KEY_ID")
COMPOSER_SECRET = os.getenv("COMPOSER_SECRET")
acc_ind = os.getenv("ACCOUNT_INDIVIDUAL", "").strip()
acc_roth = os.getenv("ACCOUNT_ROTH", "").strip()
acc_trad = os.getenv("ACCOUNT_TRAD", "").strip()
ACCOUNT_UUIDS = [uid for uid in [acc_ind, acc_roth, acc_trad] if uid]

ACCOUNT_ENABLED_MAP = {
    acc_ind: os.getenv("ACCOUNT_INDIVIDUAL_ENABLED", "True").lower() in ("true", "1", "yes") if acc_ind else False,
    acc_roth: os.getenv("ACCOUNT_ROTH_ENABLED", "True").lower() in ("true", "1", "yes") if acc_roth else False,
    acc_trad: os.getenv("ACCOUNT_TRAD_ENABLED", "True").lower() in ("true", "1", "yes") if acc_trad else False,
}

ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# --- EXECUTION MODE ---
LIVE_EXECUTION = os.getenv("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")
EXECUTION_START_TIME = os.getenv("EXECUTION_START_TIME", "09:30")

# --- STRATEGY PARAMETERS ---
TRIGGER_THRESHOLD_PCT = float(os.getenv("TRIGGER_THRESHOLD_PCT", "15.0"))
TAKE_PROFIT_MC_PCT = float(os.getenv("TAKE_PROFIT_MC_PCT", "5.0"))


VWAP_CROSS_HWM_PCT = float(os.getenv("VWAP_CROSS_HWM_PCT", "1.0"))

# --- VOLATILITY REGIME PARAMETERS ---
# (Legacy VIX Macro-Awareness Removed)

# --- PARABOLIC PARAMETERS ---
PARABOLIC_VELOCITY_THRESHOLD = float(os.getenv("PARABOLIC_VELOCITY_THRESHOLD", "2.0"))
MAX_PARABOLIC_SQUEEZE = float(os.getenv("MAX_PARABOLIC_SQUEEZE", "0.50"))

SIMULATION_PATHS = int(os.getenv("SIMULATION_PATHS", "5000"))
NEIGHBOR_K = int(os.getenv("NEIGHBOR_K", "150"))

HISTORY_CACHE_FILE = "history_cache.json"

COMPOSER_BASE_URL = "https://api.composer.trade/api/v0.1"
ALPACA_BASE_URL = "https://data.alpaca.markets/v2"
YAHOO_FINANCE_BASE_URL = "https://query2.finance.yahoo.com/v8/finance/chart"

# ==========================================
# 4. API CONNECTORS
# ==========================================
def get_composer_headers(key=None, secret=None):
    return {
        "x-api-key-id": key or COMPOSER_KEY_ID,
        "authorization": f"Bearer {secret or COMPOSER_SECRET}",
        "Content-Type": "application/json",
    }

def get_alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

def fetch_symphony_stats(account_id):
    url = f"{COMPOSER_BASE_URL}/portfolio/accounts/{account_id}/symphony-stats-meta"
    try:
        response = requests.get(url, headers=get_composer_headers(), timeout=15)
        time.sleep(1.5)
        if response.status_code == 200:
            try:
                data = response.json()
                symphonies = data.get("symphonies", [])
                return symphonies
            except ValueError:
                print(f"Warning: Failed to decode JSON from Composer API (HTTP 200) for account {account_id}. Returning [].", flush=True)
                return []
        print(f"Error fetching account {account_id}: Composer API Error (HTTP {response.status_code})", flush=True)
    except requests.RequestException as e:
        print(f"Exception fetching account {account_id}: {e}", flush=True)
    return []

def fetch_account_total_stats(account_id):
    url = f"{COMPOSER_BASE_URL}/portfolio/accounts/{account_id}/total-stats"
    try:
        response = requests.get(url, headers=get_composer_headers(), timeout=10)
        time.sleep(1.0)
        if response.status_code == 200:
            return response.json()
    except requests.RequestException as e:
        print(f"Error fetching total stats for {account_id}: {e}", flush=True)
    return {}

def execute_sell_to_cash(actual_symphony_id, account_id, bot_state=None, sym_id=None):
    url = f"{COMPOSER_BASE_URL}/deploy/accounts/{account_id}/symphonies/{actual_symphony_id}/go-to-cash"
    backoff_intervals = [1, 2, 4, 10]
    
    for attempt in range(len(backoff_intervals) + 1):
        try:
            response = requests.post(url, headers=get_composer_headers(), json={}, timeout=10)
            print(f"     -> [API Status]: HTTP {response.status_code}", flush=True)

            if response.status_code in [200, 201, 202]:
                time.sleep(0.5)
                return True
                
            if response.status_code in [401, 404]:
                print(f"     !!! [COMPOSER HTTP {response.status_code}]: Symphony not found/unauthorized. Circuit breaker tripped.", flush=True)
                if bot_state is not None and sym_id is not None:
                    if isinstance(bot_state.get(sym_id), dict):
                        bot_state[sym_id]["removed_by_user"] = True
                        database.save_state(bot_state)
                time.sleep(1.5)
                return False

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                print(f"     !!! [RATE LIMIT HIT 429] Sleeping for {retry_after}s...", flush=True)
                time.sleep(retry_after)
                continue
                
            if response.status_code >= 500 and attempt < len(backoff_intervals):
                delay = backoff_intervals[attempt]
                print(f"     !!! [COMPOSER ERROR HTTP {response.status_code}]", flush=True)
                print(f"     -> Retrying in {delay}s...", flush=True)
                time.sleep(delay)
                continue

            print(f"     !!! [COMPOSER REJECTED]", flush=True)
            time.sleep(1.5)
            return False
        except requests.RequestException as e:
            print(f"     !!! [API CRASH]: {str(e)}", flush=True)
            if attempt < len(backoff_intervals):
                delay = backoff_intervals[attempt]
                print(f"     -> Retrying in {delay}s due to transient network spike...", flush=True)
                time.sleep(delay)
                continue
            return False
    return False


def fetch_alpaca_history(tickers, current_date_str):
    print(f"  -> [TELEMETRY] Requesting Alpaca historical data for {len(tickers)} symbols...", flush=True)
    if "SPY" not in tickers:
        tickers.append("SPY")
    tickers_list = sorted(list(set(tickers)))

    if os.path.exists(HISTORY_CACHE_FILE):
        try:
            with open(HISTORY_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("date") == current_date_str and cache.get("tickers") == tickers_list:
                print("  -> Loading static 3-year history from local cache.", flush=True)
                return cache.get("data", {})
        except json.JSONDecodeError:
            pass

    print(f"Fetching 3-year history from Alpaca for Monte Carlo ({len(tickers)} tickers)...", flush=True)
    start_date = (datetime.now() - timedelta(days=365 * 3 + 30)).strftime("%Y-%m-%dT00:00:00Z")
    headers = get_alpaca_headers()
    historical_data = {}
    batch_size = 30

    for i in range(0, len(tickers_list), batch_size):
        batch = tickers_list[i : i + batch_size]
        symbol_string = ",".join(batch)
        print(f"  -> Downloading batch {i // batch_size + 1}: {len(batch)} tickers...", flush=True)

        page_token = None
        while True:
            url = f"{ALPACA_BASE_URL}/stocks/bars?symbols={symbol_string}&timeframe=1Day&start={start_date}&limit=10000&adjustment=split&feed=iex"
            if page_token:
                url += f"&page_token={page_token}"

            max_retries = 3
            success = False
            for attempt in range(max_retries):
                try:
                    response = requests.get(url, headers=headers, timeout=30)
                    if response.status_code == 200:
                        success = True
                        break
                    print(f"Alpaca API Error on batch (attempt {attempt+1}/{max_retries}): HTTP {response.status_code}", flush=True)
                except requests.RequestException as e:
                    print(f"Alpaca API Request Exception (attempt {attempt+1}/{max_retries}): {e}", flush=True)
                time.sleep(2 * (attempt + 1))

            if not success:
                print("Failed to download batch after multiple retries.", flush=True)
                break

            try:
                data = response.json()
            except ValueError:
                print("Warning: Failed to decode JSON from Alpaca API for batch. Skipping batch.", flush=True)
                break

            if "bars" in data:
                for symbol, bars in data["bars"].items():
                    for j in range(1, len(bars)):
                        prev_close = bars[j - 1]["c"]
                        curr_close = bars[j]["c"]
                        if prev_close > 0:
                            daily_ret = (curr_close - prev_close) / prev_close
                            date_str = bars[j]["t"][:10]
                            if date_str not in historical_data:
                                historical_data[date_str] = {}
                            historical_data[date_str][symbol] = {
                                "c": curr_close, 
                                "daily_ret": daily_ret,
                                "high": bars[j]["h"],
                                "low": bars[j]["l"],
                                "close": curr_close,
                                "volume": bars[j].get("v", 0)
                            }

            page_token = data.get("next_page_token")
            if not page_token:
                break

    print("  -> History download complete. Saving to daily cache.", flush=True)
    try:
        with open(HISTORY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": current_date_str, "tickers": tickers_list, "data": historical_data}, f)
    except OSError as e:
        print(f"  -> Failed to write cache: {e}", flush=True)

    return historical_data


def fetch_intraday_vwaps(tickers, headers, current_et):
    """Fetches minute bars for today to calculate true VWAP for all active holdings."""
    if not tickers:
        return {}

    start_et = current_et.replace(hour=9, minute=30, second=0, microsecond=0)
    start_utc_str = start_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    vwap_data = {}
    batch_size = 30
    tickers_list = list(tickers)

    for i in range(0, len(tickers_list), batch_size):
        batch = tickers_list[i : i + batch_size]
        symbol_string = ",".join(batch)
        url = f"{ALPACA_BASE_URL}/stocks/bars?symbols={symbol_string}&timeframe=1Min&start={start_utc_str}&limit=1000&feed=iex"

        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 200:
                data = response.json().get("bars", {})
                for sym, bars in data.items():
                    if not bars:
                        continue
                    df = pd.DataFrame(bars)
                    df['pv'] = df['c'] * df['v']
                    cumulative_pv = df['pv'].sum()
                    cumulative_v = df['v'].sum()
                    if cumulative_v > 0:
                        vwap = cumulative_pv / cumulative_v
                        last_price = float(df['c'].iloc[-1])
                        vwap_data[sym] = {"vwap": vwap, "last_price": last_price}
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"Error fetching VWAP for batch {batch}: {e}", flush=True)

    return vwap_data


def get_current_et():
    utc_now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        return datetime.now(ZoneInfo("America/New_York"))
    except ZoneInfoNotFoundError:
        if 3 <= utc_now.month <= 11:
            return utc_now - timedelta(hours=4)
        return utc_now - timedelta(hours=5)


# ==========================================
# 6. MAIN EXECUTION LOOP
# ==========================================

def fetch_proxy_from_market_data(ticker):
    clean_ticker = str(ticker).strip().upper()
        
    url = f"https://paper-api.alpaca.markets/v2/assets/{clean_ticker}"
    try:
        # Use headers based on Alpaca API key environment variables
        headers = {
            "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ALPACA_KEY),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ALPACA_SECRET)
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            asset_data = response.json()
            if asset_data.get("status") == "active":
                name = asset_data.get("name", "").upper()
                
                # Dynamic Semantic Resolution
                if any(term in name for term in ["NASDAQ", "TECH", "SEMICONDUCTOR", "INNOVATION", "QQQ"]):
                    proxy = "QQQ"
                elif any(term in name for term in ["SMALL-CAP", "SMALL CAP", "RUSSELL", "IWM"]):
                    proxy = "IWM"
                elif any(term in name for term in ["DOW JONES", "DIA"]):
                    proxy = "DIA"
                else:
                    proxy = "SPY"
                    
                print(f"  -> Searched Alpaca for {clean_ticker} ('{name}'). Assigned dynamic proxy: {proxy}.", flush=True)
                return proxy
                
            print(f"  -> Searched Alpaca for {clean_ticker}. Asset not active. Defaulting to SPY.", flush=True)
            return "SPY"
        else:
            print(f"  -> Warning: Could not find asset {clean_ticker} in Alpaca (HTTP {response.status_code}). Defaulting to SPY.", flush=True)
            return "SPY"
    except requests.RequestException as e:
        print(f"  -> API Error querying Alpaca for {clean_ticker}: {e}. Defaulting to SPY.", flush=True)
        return "SPY"

def run_morning_initialization():
    print("Starting morning holdings ingestion and database-driven sector mapping...", flush=True)
    bot_state = database.load_state()
    state_changed = False
    current_date_str = get_current_et().strftime("%Y-%m-%d")
    
    def fetch_with_backoff(account_id):
        url = f"{COMPOSER_BASE_URL}/portfolio/accounts/{account_id}/symphony-stats-meta"
        backoff_intervals = [1, 2, 4, 10]
        
        for attempt in range(len(backoff_intervals) + 1):
            try:
                response = requests.get(url, headers=get_composer_headers(), timeout=15)
                if response.status_code == 200:
                    time.sleep(1.5)
                    return response.json().get("symphonies", [])
                
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    print(f"     !!! [RATE LIMIT HIT 429] Sleeping for {retry_after}s...", flush=True)
                    time.sleep(retry_after)
                    continue
                    
                if response.status_code >= 500 and attempt < len(backoff_intervals):
                    delay = backoff_intervals[attempt]
                    print(f"     !!! [COMPOSER ERROR HTTP {response.status_code}] -> Retrying in {delay}s...", flush=True)
                    time.sleep(delay)
                    continue
                    
                print(f"     !!! [COMPOSER REJECTED HTTP {response.status_code}]", flush=True)
                time.sleep(1.5)
                return []
            except requests.RequestException as e:
                print(f"     !!! [API CRASH]: {str(e)}", flush=True)
                if attempt < len(backoff_intervals):
                    delay = backoff_intervals[attempt]
                    print(f"     -> Retrying in {delay}s due to transient network spike...", flush=True)
                    time.sleep(delay)
                    continue
                return []
        return []

    for account in ACCOUNT_UUIDS:
        symphonies = fetch_with_backoff(account)
        for sym in symphonies:
            s_id = sym["id"]
            holdings = sym.get("holdings", [])
            if not holdings:
                continue
            
            # Find the holding with the maximum weight/allocation
            highest_holding = max(holdings, key=lambda x: x.get("weight", x.get("allocation", 0.0)))
            raw_ticker = highest_holding.get("ticker", "")
            clean_ticker = raw_ticker.split("::")[-1].split("//")[0].replace("/", ".")
            
            # 1. Check DB
            proxy_etf = database.get_proxy_for_ticker(clean_ticker)
            if not proxy_etf:
                # 2. Query Market Data API if missing
                proxy_etf = fetch_proxy_from_market_data(clean_ticker)
                # 3. Save to DB
                database.save_proxy_for_ticker(clean_ticker, proxy_etf)
                
            # 4. Save daily proxy record
            s_name = sym.get("name", s_id)
            database.save_daily_symphony_proxy(s_name, proxy_etf, current_date_str)
            
            if s_id not in bot_state:
                bot_state[s_id] = {}
            bot_state[s_id]["proxy_etf"] = proxy_etf
            print(f"  -> Assigned {clean_ticker} to {proxy_etf} for Symphony {s_name}", flush=True)
            state_changed = True
            
    if state_changed:
        database.save_state(bot_state)
        print("  -> Morning initialization complete. State saved.", flush=True)


def main():
    if not database.acquire_lock(lease_duration=900):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ Overlap Detected. Database lock is currently held by another process. Skipping...", flush=True)
        return

    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Alpha Bot Waking Up...", flush=True)
        mode_text = "LIVE EXECUTION (DANGER)" if LIVE_EXECUTION else "DRY RUN (SAFE)"
        print(f"MODE: {mode_text}", flush=True)

        force_run = "--force" in sys.argv
        current_et = get_current_et()

        is_weekday = current_et.weekday() < 5
        current_time = current_et.time()

        try:
            start_h, start_m = map(int, EXECUTION_START_TIME.split(":"))
        except ValueError:
            start_h, start_m = 9, 30

        market_open = dt_time(start_h, start_m)
        market_close = dt_time(16, 0)
        rebalance_blackout = dt_time(15, 53)
        post_mortem_cutoff = dt_time(16, 5)

        # --- MORNING INITIALIZATION ---
        if is_weekday and current_time.hour == 9 and current_time.minute == 25 and not force_run:
            print("  -> Running Morning Initialization (Sector Mapping)...", flush=True)
            run_morning_initialization()
            return

        if not is_weekday or current_time < market_open or current_time > post_mortem_cutoff:
            if not force_run:
                print(f"  -> Market closed or in Grace Period (ET: {current_et.strftime('%a %H:%M')}). Sleeping...", flush=True)
                return
            print("  -> Market closed, but --force flag detected! Bypassing gatekeeper...", flush=True)

        if rebalance_blackout <= current_time < market_close:
            bot_state = database.load_state()

            all_snapshotted_tickers = set()
            for s_id, s_data in bot_state.items():
                if isinstance(s_data, dict) and s_data.get("triggered"):
                    for h in s_data.get("triggered_basket_snapshot", []):
                        if h.get("ticker"):
                            all_snapshotted_tickers.add(h.get("ticker"))

            live_vwaps = fetch_intraday_vwaps(list(all_snapshotted_tickers), get_alpaca_headers(), current_et)

            reporting.generate_eod_snapshot(bot_state, current_et.strftime("%Y-%m-%d"), is_post_rebalance=False, discord_webhook_url=DISCORD_WEBHOOK_URL, live_prices=live_vwaps)

            if not force_run:
                print(f"  -> 🛑 COMPOSER REBALANCE BLACKOUT (ET: {current_et.strftime('%H:%M')}). Pausing...", flush=True)
                return
            print("  -> Rebalance blackout active, but --force flag detected! Bypassing...", flush=True)

        if not COMPOSER_KEY_ID or not ALPACA_KEY:
            print("CRITICAL: Missing API Keys. Please check your .env file.", flush=True)
            return

        bot_state = database.load_state()
        chart_history = database.load_chart_history()

        current_date_str = current_et.strftime("%Y-%m-%d")
        current_time_str = current_et.strftime("%H:%M")

        # Check for execution mode toggle
        prev_live_execution = bot_state.get("last_execution_mode")
        state_changed = False
        if prev_live_execution is not None and prev_live_execution != LIVE_EXECUTION:
            print(f"  -> Execution mode toggle detected ({prev_live_execution} -> {LIVE_EXECUTION}). Wiping transient state.", flush=True)
            database.wipe_transient_state(bot_state)
            state_changed = True
        
        if bot_state.get("last_execution_mode") != LIVE_EXECUTION:
            bot_state["last_execution_mode"] = LIVE_EXECUTION
            state_changed = True

        if bot_state.get("date") != current_date_str:
            print(f"  -> New trading day detected ({current_date_str} ET). Wiping transient state keys and chart memory.", flush=True)
            bot_state["date"] = current_date_str
            database.wipe_transient_state(bot_state)
            state_changed = True

        accounts_to_purge = []
        for s_id, s_data in bot_state.items():
            if isinstance(s_data, dict) and "account" in s_data:
                if s_data["account"] not in ACCOUNT_UUIDS:
                    accounts_to_purge.append(s_id)
        
        for s_id in accounts_to_purge:
            print(f"  -> [ACCOUNT REMOVED] Purging ghost symphony from state: {s_id}", flush=True)
            del bot_state[s_id]
            state_changed = True

        if state_changed:
            database.save_state(bot_state)

        if chart_history.get("date") != current_date_str:
            chart_history = {"date": current_date_str, "symphonies": {}}
            database.save_chart_history(chart_history)

        m_open_dt = current_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        m_close_dt = current_et.replace(hour=16, minute=0, second=0, microsecond=0)

        all_tickers = set()
        symphony_data_cache = {}
        
        if "account_totals" not in bot_state:
            bot_state["account_totals"] = {}

        for account in ACCOUNT_UUIDS:
            symphonies = fetch_symphony_stats(account)
            symphony_data_cache[account] = symphonies
            
            # Fetch automated account totals
            t_stats = fetch_account_total_stats(account)
            if t_stats and "portfolio_value" in t_stats:
                bot_state["account_totals"][account] = t_stats["portfolio_value"]

            for sym in symphonies:
                s_id = sym["id"]
                if s_id not in bot_state:
                    bot_state[s_id] = {}
                bot_state[s_id]["account"] = account
                bot_state[s_id]["name"] = sym.get("name", s_id)
                
                for holding in sym.get("holdings", []):
                    raw_ticker = holding.get("ticker", "")
                    clean_ticker = raw_ticker.split("::")[-1].split("//")[0]
                    alpaca_ticker = clean_ticker.replace("/", ".")
                    if alpaca_ticker:
                        all_tickers.add(alpaca_ticker)
                        holding["working_ticker"] = alpaca_ticker

        # Add proxy ETFs and frozen tickers to all_tickers for true Shadow Return tracking
        for s_id, s_data in bot_state.items():
            if isinstance(s_data, dict):
                proxy_etf = s_data.get("proxy_etf")
                if proxy_etf:
                    all_tickers.add(proxy_etf)
                    
                if s_data.get("triggered"):
                    for h in s_data.get("current_holdings", []):
                        t = h.get("ticker")
                        if t and "cash" not in t.lower():
                            all_tickers.add(t)

        # --- NAME-BASED AUTOMATED PORTFOLIO REALIGNMENT ---
        for account in ACCOUNT_UUIDS:
            live_symphonies = symphony_data_cache.get(account, [])
            
            # 1. Map out live positions by their unique normalized names
            live_names = {database.normalize_name(sym.get("name", "")) for sym in live_symphonies if sym.get("name")}
            
            # 2. Map out active, untriggered positions currently held in local bot_state
            tracked_names = set()
            for s_id, s_data in bot_state.items():
                if isinstance(s_data, dict) and s_data.get("account") == account:
                    if not s_data.get("removed_by_user", False):
                        if s_data.get("name"):
                            tracked_names.add(database.normalize_name(s_data["name"]))
                            
            # 3. Assess if a structural strategy mismatch exists (Strategy added or permanently deleted)
            has_strategy_mismatch = (len(live_names ^ tracked_names) > 0)
            
            if has_strategy_mismatch and live_names:
                print(f"  🔄 [AUTOMATIC SYNC] Structural portfolio deviation detected for account {account[:8]}. Executing system flush...", flush=True)
                database.log_symphony_event("system", "Automated portfolio realignment triggered due to strategy name mismatch/rebalance.", "sync", current_date_str)
                
                # Execute the modular flush to clean transient state and re-ingest active targets
                database.execute_system_flush(account)
                
                # Reload state memory registers immediately mid-run to capture fresh keys
                bot_state = database.load_state()
                chart_history = database.load_chart_history()
                
                # ENFORCE DATA PERSISTENCE: Write the freshly synchronized state back to disk securely
                database.save_state(bot_state)
                break  # Break out to allow the next loop tick to parse the updated environment cleanly
        # --------------------------------------------------

        if (market_close <= current_time <= post_mortem_cutoff) or (force_run and current_et.weekday() >= 5):
            if bot_state.get("post_mortem_run") == current_date_str:
                if not force_run:
                    print("  -> EOD Post-Mortem already run for today. Sleeping...", flush=True)
                    return
                print("  -> EOD Post-Mortem already run, but --force flag detected! Running again...", flush=True)

            for account, symphonies in symphony_data_cache.items():
                for sym in symphonies:
                    s_id = sym["id"]
                    if bot_state.get(s_id, {}).get("removed_by_user"):
                        continue
                    if s_id in bot_state:
                        if not bot_state[s_id].get("triggered"):
                            bot_state[s_id]["current_holdings"] = [
                                {"ticker": h.get("working_ticker", h.get("ticker")), "allocation": h.get("weight", h.get("allocation", 0.0))}
                                for h in sym.get("holdings", [])
                            ]
                            bot_state[s_id]["current_return"] = sym.get("last_percent_change", 0.0) * 100
            
            # Save post_mortem flag immediately to prevent race conditions if execution is slow
            bot_state["post_mortem_run"] = current_date_str
            database.save_state(bot_state)

            # NEW: Execute Phase 2 Reporting
            reporting.generate_eod_snapshot(bot_state, current_date_str, is_post_rebalance=True, discord_webhook_url=DISCORD_WEBHOOK_URL)

            # Daily Chart Archiving (Decoupled from Autotuner)
            chart_history = database.load_chart_history()
            if chart_history and chart_history.get("date") == current_date_str:
                print(f"  -> Archiving chart history for {current_date_str} to database...", flush=True)
                for sym_id, data in chart_history.get("symphonies", {}).items():
                    database.save_chart_archive(current_date_str, sym_id, data)

            # NEW: Execute Autotuner (Weekly on Fridays, or Manual Weekends/Force)
            autotuner_changes = None
            
            # NEW: Filter down to only UI-Enabled Accounts for Autotuning
            enabled_account_uuids = [uid for uid in ACCOUNT_UUIDS if ACCOUNT_ENABLED_MAP.get(uid)]
            
            if current_et.weekday() >= 4 or force_run: # 4=Fri, 5=Sat, 6=Sun
                print(f"  -> {'Weekend/Force' if current_et.weekday() >= 5 else 'Friday'} Detected. Starting autotune...", flush=True)
                autotuner_changes = autotuner.run_autotuner(bot_state, current_date_str, enabled_account_uuids, is_forced=force_run)
                
                if autotuner_changes:
                    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL)
                    reporting.send_eod_discord_post(current_date_str, f"post_mortem_{current_date_str}.json", autotuner_changes, discord_webhook)
                else:
                    reporting.send_eod_discord_post(current_date_str, f"post_mortem_{current_date_str}.json", None, DISCORD_WEBHOOK_URL)
            else:
                print(f"  -> Day is {current_et.strftime('%A')}. Skipping weekly autotune.", flush=True)
                reporting.send_eod_discord_post(current_date_str, f"post_mortem_{current_date_str}.json", None, DISCORD_WEBHOOK_URL)

            print("  -> EOD Post-Mortem complete. Ending execution for the day.", flush=True)
            return

        historical_data = fetch_alpaca_history(list(all_tickers), current_date_str)
        if not historical_data:
            return

        live_vwaps = fetch_intraday_vwaps(list(all_tickers), get_alpaca_headers(), current_et)

        spy_today = 0.0
        if "SPY" in historical_data.get(current_date_str, {}):
            spy_today = historical_data[current_date_str]["SPY"].get("daily_ret", 0.0) * 100.0
        print(f"  -> Macro Environment: SPY {spy_today:.2f}%", flush=True)
        print("\nEvaluating Symphonies...", flush=True)

        execution_queue = []

        for account, symphonies in symphony_data_cache.items():
            for sym in symphonies:
                symphony_id = sym["id"]
                if bot_state.get(symphony_id, {}).get("removed_by_user"):
                    continue

                actual_symphony_id = symphony_id

                symphony_name = sym.get("name", "Unknown Symphony")
                normalized_name = database.normalize_name(symphony_name)
                symphony_strat = database.get_symphony_strategy(normalized_name)
                acc_params = symphony_strat.get("params", {})

                acc_TRIGGER_THRESHOLD_PCT = acc_params.get("TRIGGER_THRESHOLD_PCT", TRIGGER_THRESHOLD_PCT)
                acc_TAKE_PROFIT_MC_PCT = acc_params.get("TAKE_PROFIT_MC_PCT", TAKE_PROFIT_MC_PCT)

                acc_VWAP_CROSS_HWM_PCT = acc_params.get("VWAP_CROSS_HWM_PCT", VWAP_CROSS_HWM_PCT)




                holdings = sym.get("holdings", [])
                current_return = sym.get("last_percent_change", 0.0) * 100

                # --- TRUE SHADOW RETURN OVERRIDE ---
                if symphony_id in bot_state and bot_state[symphony_id].get("triggered"):
                    holdings = bot_state[symphony_id].get("current_holdings", [])
                    f_ret = bot_state[symphony_id].get("triggered_at_return", 0.0)
                    trigger_prices = bot_state[symphony_id].get("trigger_prices", {})
                    post_trigger_move = 0.0
                    for h in holdings:
                        t = h.get("ticker")
                        alloc = h.get("weight", h.get("allocation", 0.0))
                        if t in trigger_prices and t in live_vwaps:
                            p_start = trigger_prices[t]
                            p_now = live_vwaps[t]["last_price"]
                            if p_start > 0:
                                post_trigger_move += alloc * ((p_now - p_start) / p_start)
                    current_return = f_ret + (post_trigger_move * 100.0)
                # -----------------------------------

                # Pre-calculate True VWAP difference unconditionally so it can be logged in the chart history
                weighted_vwap_diff = 0.0
                valid_vwap_weight = 0.0
                for h in holdings:
                    h["ticker"] = h.get("working_ticker", h.get("ticker"))
                    t = h["ticker"]
                    alloc = h.get("weight", h.get("allocation", 0.0))
                    if t in live_vwaps:
                        p = live_vwaps[t]["last_price"]
                        v = live_vwaps[t]["vwap"]
                        if v > 0:
                            weighted_vwap_diff += alloc * ((p - v) / v)
                            valid_vwap_weight += alloc
                symphony_holdings = [h.get("ticker") for h in holdings]

                if symphony_id not in bot_state:
                    bot_state[symphony_id] = {
                        "high_water_mark": current_return,
                        "shadow_hwm": current_return,
                        "prev_return": current_return,
                        "armed": False,
                        "tp_armed": False,
                        "para_armed": False,
                        "triggered": False,
                        "mc_history": [],
                        "lowest_mc_seen": 100.0,
                        "lock_engaged_ticks": 0,
                        "below_lock_count": 0,
                        "below_stop_count": 0,
                        "above_tp_count": 0,
                        "vwap_ticks": 0,
                        "breakeven_locked": False,
                        "hwm_hold_ticks": 0,
                    }
                
                # Unconditionally inject identity mapping for the autotuner and cross-referencing
                bot_state[symphony_id]["account"] = account
                bot_state[symphony_id]["name"] = symphony_name

                prev_armed = bot_state[symphony_id].get("armed", False)
                prev_tp_armed = bot_state[symphony_id].get("tp_armed", False)
                prev_triggered = bot_state[symphony_id].get("triggered", False)
                prev_para_armed = bot_state[symphony_id].get("para_armed", False)

                for key in ["triggered", "tp_armed", "breakeven_locked", "para_armed"]:
                    if key not in bot_state[symphony_id]:
                        bot_state[symphony_id][key] = False
                if "lowest_mc_seen" not in bot_state[symphony_id]:
                    bot_state[symphony_id]["lowest_mc_seen"] = 100.0
                for key in ["lock_engaged_ticks", "below_lock_count", "below_stop_count", "above_tp_count", "vwap_ticks", "hwm_hold_ticks"]:
                    if key not in bot_state[symphony_id]:
                        bot_state[symphony_id][key] = 0
                if "mc_history" not in bot_state[symphony_id]:
                    bot_state[symphony_id]["mc_history"] = []

                if current_return > bot_state[symphony_id]["high_water_mark"] and not bot_state[symphony_id]["triggered"]:
                    bot_state[symphony_id]["high_water_mark"] = current_return

                if "shadow_hwm" not in bot_state[symphony_id]:
                    bot_state[symphony_id]["shadow_hwm"] = current_return
                if current_return > bot_state[symphony_id]["shadow_hwm"]:
                    bot_state[symphony_id]["shadow_hwm"] = current_return

                high_water_mark = bot_state[symphony_id]["high_water_mark"]
                safe_hwm = high_water_mark if high_water_mark != -999.0 else current_return

                symphony_vol = math_engine.calculate_20d_vol(holdings, historical_data)
                vol_mult = acc_params.get("VOLATILITY_MAGNITUDE_MULTIPLIER", 0.5)
                
                proxy_etf = bot_state[symphony_id].get("proxy_etf")
                if not proxy_etf:
                    proxy_etf = "SPY"
                proxy_today = historical_data.get(current_date_str, {}).get(proxy_etf, {}).get("daily_ret", 0.0) * 100.0
                prob_beating, prob_loss_dynamic, dynamic_floor = math_engine.run_monte_carlo(
                    current_return, holdings, historical_data, spy_today, proxy_today, 
                    symphony_vol, proxy_etf=proxy_etf, simulation_paths=SIMULATION_PATHS, 
                    neighbor_k=NEIGHBOR_K, volatility_multiplier=vol_mult
                )

                if not bot_state[symphony_id]["triggered"]:
                    bot_state[symphony_id]["lowest_mc_seen"] = min(bot_state[symphony_id].get("lowest_mc_seen", 100.0), prob_beating)

                should_arm = False
                arm_reason = ""

                if acc_TAKE_PROFIT_MC_PCT <= prob_beating < acc_TRIGGER_THRESHOLD_PCT and prob_loss_dynamic >= 25.0:
                    should_arm = True
                    arm_reason = f"MC Prob {prob_beating:.1f}% | Loss Prob {prob_loss_dynamic:.1f}%"

                if should_arm and not bot_state[symphony_id]["armed"] and not bot_state[symphony_id]["triggered"]:
                    bot_state[symphony_id]["armed"] = True
                    print(f"  *** {symphony_name} ARMED ({arm_reason}) ***", flush=True)
                    database.log_symphony_event(symphony_id, f"{symphony_name} ARMED ({arm_reason})", "armed", current_date_str)

                elif bot_state[symphony_id]["armed"] and not bot_state[symphony_id]["triggered"]:
                    if prob_beating > (acc_TRIGGER_THRESHOLD_PCT * 2) and current_return > 0.0:
                        bot_state[symphony_id]["armed"] = False
                        bot_state[symphony_id]["below_stop_count"] = 0
                        print(f"  *** {symphony_name} DISARMED (Conditions Recovered) ***", flush=True)

                bot_state[symphony_id]["mc_history"].append(prob_beating)
                if len(bot_state[symphony_id]["mc_history"]) > 5:
                    bot_state[symphony_id]["mc_history"].pop(0)

                # --- PARABOLIC SQUEEZE LOGIC ---
                prev_return = bot_state[symphony_id].get("prev_return", current_return)
                
                if prev_return is None:
                    prev_return = current_return
                    velocity = 0.0
                    is_para = False
                else:
                    velocity = current_return - prev_return
                    para_threshold = acc_params.get("PARABOLIC_VELOCITY_THRESHOLD", PARABOLIC_VELOCITY_THRESHOLD)
                    is_para = math_engine.check_parabolic_velocity(current_return, prev_return, para_threshold)
                    
                bot_state[symphony_id]["prev_return"] = current_return

                if is_para:
                    if not bot_state[symphony_id]["para_armed"]:
                        bot_state[symphony_id]["para_armed"] = True
                        print(f"  🚀 {symphony_name} PARA-ARMED (Velocity: {velocity:.2f}%) 🚀", flush=True)
                        database.log_symphony_event(symphony_id, f"{symphony_name} PARA-ARMED (Velocity: {velocity:.2f}%)", "para-armed", current_date_str)

                # --- TIME SQUEEZE DECAY LOGIC ---
                m_open_dt = current_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
                m_close_dt = current_et.replace(hour=16, minute=0, second=0, microsecond=0)
                time_ratio = max(0.0, min(1.0, (current_et - m_open_dt).total_seconds() / (m_close_dt - m_open_dt).total_seconds()))
                
                # Fetch multipliers from strategy params or fall back to internal defaults
                m_open = acc_params.get("VOLATILITY_MAGNITUDE_MULTIPLIER", 1.5)
                m_close = acc_params.get("VOLATILITY_CLOSE_MULTIPLIER", 0.5)
                
                dynamic_multiplier, dynamic_min_stop = math_engine.calculate_time_decay_multipliers(
                    time_ratio, 
                    mult_open=m_open, 
                    mult_close=m_close,
                    min_stop_open=0.3, 
                    min_stop_close=0.15
                )

                # Calculate active stop distance based strictly on VW-ATR volatility
                
                vwatr_vol = math_engine.calculate_14d_vwatr_pct(holdings, historical_data)
                safe_vol = vwatr_vol if vwatr_vol > 0 else (symphony_vol if symphony_vol > 0 else 1.0)
                is_squeezed = bot_state[symphony_id].get("para_armed") or bot_state[symphony_id].get("breakeven_locked")
                active_trailing_stop = math_engine.calculate_active_stop_distance(safe_vol, dynamic_multiplier, dynamic_min_stop, is_squeezed, acc_params.get("MAX_PARABOLIC_SQUEEZE", MAX_PARABOLIC_SQUEEZE))

                base_stop_level = safe_hwm - active_trailing_stop

                strat_params = bot_state[symphony_id].get("strategy_params", acc_params)
                breakeven_vol_min = strat_params.get("BREAKEVEN_VOL_MIN", 0.4)
                breakeven_vol_max = strat_params.get("BREAKEVEN_VOL_MAX", 3.0)
                if math_engine.check_breakeven_activation(current_return, symphony_vol, breakeven_vol_min, breakeven_vol_max):
                    bot_state[symphony_id]["hwm_hold_ticks"] += 1
                else:
                    bot_state[symphony_id]["hwm_hold_ticks"] = 0
                
                if bot_state[symphony_id]["hwm_hold_ticks"] >= 5:
                    if not bot_state[symphony_id].get("breakeven_locked"):
                        bot_state[symphony_id]["breakeven_locked"] = True
                        bot_state[symphony_id]["lock_engaged_at"] = datetime.now(timezone.utc).isoformat()

                if bot_state[symphony_id]["breakeven_locked"]:
                    stop_trigger_level = max(base_stop_level, 0.0)
                else:
                    stop_trigger_level = base_stop_level

                highest_stop = bot_state[symphony_id].get("highest_stop_level", -999.0)
                stop_trigger_level = max(stop_trigger_level, highest_stop)
                bot_state[symphony_id]["highest_stop_level"] = stop_trigger_level

                if bot_state[symphony_id]["triggered"]:
                    stop_trigger_level = -999.0

                # Check 1: Trailing Stop & Breakeven Lock Realignment
                is_trailing_stop_hit = False
                is_breakeven_hit = False

                if not bot_state[symphony_id]["triggered"]:
                    is_magnitude_breached = current_return <= (stop_trigger_level - 0.10)
                    
                    # PATH A: Standard Trailing Stop (Only runs when ARMED)
                    if bot_state[symphony_id]["armed"]:
                        if is_magnitude_breached and prob_beating < 60.0:
                            bot_state[symphony_id]["below_stop_count"] += 1
                            if bot_state[symphony_id]["below_stop_count"] == 1:
                                print(f"  ⚠️ {symphony_name[:35]} dipped below trailing stop. Awaiting confirmation...", flush=True)
                            elif bot_state[symphony_id]["below_stop_count"] >= 3:
                                is_trailing_stop_hit = True
                                bot_state[symphony_id]["stop_hit_type"] = "Trailing Stop"
                        else:
                            if bot_state[symphony_id]["below_stop_count"] > 0:
                                print(f"  ✅ {symphony_name[:35]} recovered or trailing sanity check passed. Resetting confirmation.", flush=True)
                            bot_state[symphony_id]["below_stop_count"] = 0
                            if not bot_state[symphony_id]["breakeven_locked"]:
                                bot_state[symphony_id]["stop_hit_type"] = None

                    # PATH B: Independent Breakeven Lock System (Runs independently)
                    if bot_state[symphony_id]["breakeven_locked"] and not is_trailing_stop_hit:
                        if is_magnitude_breached:
                            # Derive persistent lock delta across application recycles
                            lock_duration_mins = 0.0
                            lock_at_str = bot_state[symphony_id].get("lock_engaged_at")
                            if lock_at_str:
                                try:
                                    lock_at_dt = datetime.fromisoformat(lock_at_str)
                                    lock_duration_mins = (datetime.now(timezone.utc) - lock_at_dt).total_seconds() / 60.0
                                except Exception:
                                    pass
                            
                            # Risk Guard Breakeven Path A: Live-MC signals the basket is genuinely toxic
                            be_path_a = prob_beating < 60.0
                            
                            # Risk Guard Breakeven Path B: MC-Stuck Override (Jammed model safety valve)
                            be_path_b = (lock_duration_mins >= 60.0 and bot_state[symphony_id].get("lowest_mc_seen", 100.0) >= 60.0)
                            
                            if be_path_a or be_path_b:
                                bot_state[symphony_id]["below_lock_count"] += 1
                                if bot_state[symphony_id]["below_lock_count"] == 1:
                                    reason_flushed = "MC-Stuck Override Pending" if be_path_b else "Standard Breakeven Breach Pending"
                                    print(f"  ⚠️ {symphony_name[:35]} triggered {reason_flushed}. Awaiting verification...", flush=True)
                                elif bot_state[symphony_id]["below_lock_count"] >= 3:
                                    is_breakeven_hit = True
                                    bot_state[symphony_id]["stop_hit_type"] = "Breakeven Path B (MC-Stuck)" if be_path_b else "Breakeven Path A"
                            else:
                                bot_state[symphony_id]["below_lock_count"] = 0
                        else:
                            bot_state[symphony_id]["below_lock_count"] = 0

                # Check 2: Take Profit
                tp_triggered_now = False
                if prob_beating < acc_TAKE_PROFIT_MC_PCT:
                    if not bot_state[symphony_id]["tp_armed"] and not bot_state[symphony_id]["triggered"]:
                        bot_state[symphony_id]["tp_armed"] = True
                        bot_state[symphony_id]["above_tp_count"] = 0
                        print(f"  *** {symphony_name} TP-ARMED (Exceptional Gain: MC Prob {prob_beating:.1f}% < {acc_TAKE_PROFIT_MC_PCT}%) ***", flush=True)
                        database.log_symphony_event(symphony_id, f"{symphony_name} TP-ARMED (Exceptional Gain: MC Prob {prob_beating:.1f}% < {acc_TAKE_PROFIT_MC_PCT}%)", "tp-armed", current_date_str)
                elif bot_state[symphony_id]["tp_armed"] and not bot_state[symphony_id]["triggered"]:
                    if prob_beating >= acc_TAKE_PROFIT_MC_PCT:
                        bot_state[symphony_id]["above_tp_count"] += 1
                        if bot_state[symphony_id]["above_tp_count"] == 1:
                            print(f"  ⚠️ {symphony_name[:35]} TP signal flashed. Awaiting 2nd tick confirmation...", flush=True)
                        elif bot_state[symphony_id]["above_tp_count"] >= 2:
                            tp_triggered_now = True
                    else:
                        if bot_state[symphony_id]["above_tp_count"] > 0:
                            print(f"  📉 {symphony_name[:35]} TP signal vanished. Still cranking.", flush=True)
                        bot_state[symphony_id]["above_tp_count"] = 0

                # Check 3: True VWAP Breakdown
                is_vwap_broken = False
                
                if not bot_state[symphony_id]['triggered']:
                    current_vwap_diff_pct = weighted_vwap_diff * 100.0
                    vwap_buffer_pct = -(symphony_vol * acc_params.get("VWAP_BAND_MULTIPLIER", 0.10))
                    if valid_vwap_weight > 0.5 and current_vwap_diff_pct < vwap_buffer_pct:
                        if safe_hwm >= acc_VWAP_CROSS_HWM_PCT and current_return < safe_hwm:
                            bot_state[symphony_id]['vwap_ticks'] += 1
                            if bot_state[symphony_id]['vwap_ticks'] >= 3:
                                is_vwap_broken = True
                                print(f'  📉 {symphony_name[:35]} Portfolio VWAP broken. Forcing exit to protect gains.', flush=True)
                        else:
                            bot_state[symphony_id]['vwap_ticks'] = 0
                    else:
                        bot_state[symphony_id]['vwap_ticks'] = 0

                safe_name = symphony_name[:35].encode('ascii', 'ignore').decode('ascii')
                print(f"  -> {safe_name}: Ret: {current_return:.2f}% | HWM: {high_water_mark:.2f}% | Stop Dist: {active_trailing_stop:.2f}% | ArmProb: {prob_beating:.1f}%", flush=True)

                bot_state[symphony_id]["name"] = symphony_name
                bot_state[symphony_id]["account"] = account
                bot_state[symphony_id]["current_return"] = current_return
                bot_state[symphony_id]["mc_prob"] = prob_beating
                bot_state[symphony_id]["stop_trigger"] = stop_trigger_level
                bot_state[symphony_id]["active_stop_distance"] = active_trailing_stop
                bot_state[symphony_id]["symphony_vol"] = symphony_vol
                sym_val = sym.get("current_value", sym.get("value", 0.0))
                if sym_val == 0.0:
                    sym_val = sum(h.get("value", 0.0) for h in holdings)
                bot_state[symphony_id]["current_value"] = sym_val
                if not bot_state[symphony_id].get("triggered"):
                    bot_state[symphony_id]["current_holdings"] = [{"ticker": h.get("ticker"), "allocation": h.get("weight", h.get("allocation", 0.0))} for h in holdings]

                chart_event = None
                if is_vwap_broken:
                    chart_event = "VWAP_Break"
                elif is_trailing_stop_hit or is_breakeven_hit or tp_triggered_now:
                    chart_event = "Triggered"
                elif bot_state[symphony_id].get("para_armed") and not prev_para_armed:
                    chart_event = "Para-Armed"
                elif bot_state[symphony_id]["armed"] and not prev_armed:
                    chart_event = "Armed"
                elif bot_state[symphony_id]["tp_armed"] and not prev_tp_armed:
                    chart_event = "TP-Armed"

                tracked_stop = stop_trigger_level if (bot_state[symphony_id]["armed"] or bot_state[symphony_id]["tp_armed"] or bot_state[symphony_id]["triggered"] or prev_triggered) else None
                if prev_triggered:
                    tracked_stop = bot_state[symphony_id].get("triggered_at_stop", -999.0)
                    if tracked_stop == -999.0:
                        tracked_stop = None

                sym_chart_data = chart_history["symphonies"].setdefault(symphony_id, [])
                sym_chart_data.append({
                    "time": current_time_str,
                    "return": current_return,
                    "stop": tracked_stop,
                    "event": chart_event,
                    "mc_prob": prob_beating,
                    "vol": symphony_vol,
                    "vwap_diff": weighted_vwap_diff,
                    "base_atr_pct": 0.0,
                    "dynamic_multiplier": 1.0,
                    "rvol": math_engine.calculate_current_rvol(holdings, historical_data),
                    "vw_atr": vwatr_vol,
                    "recovery_ticks": bot_state[symphony_id].get("vwap_recovery_ticks", 0)
                })

                if is_trailing_stop_hit or is_breakeven_hit or tp_triggered_now or is_vwap_broken:
                    if tp_triggered_now:
                        reason = "Take-Profit"
                        attempted_level = current_return
                    elif is_breakeven_hit:
                        reason = bot_state[symphony_id].get("stop_hit_type", "Breakeven Lock")
                        attempted_level = stop_trigger_level
                    elif is_vwap_broken:
                        reason = "VWAP Breakdown"
                        attempted_level = safe_hwm
                    else:
                        reason = bot_state[symphony_id].get("stop_hit_type", "Trailing Stop")
                        attempted_level = stop_trigger_level

                    print(f"  🚨 {reason.upper()} HIT FOR {symphony_name} 🚨 - Queuing for Execution", flush=True)
                    database.log_symphony_event(symphony_id, f"{reason.upper()} HIT FOR {symphony_name}. Level: {attempted_level:.2f}", "triggered", current_date_str)

                    execution_queue.append({
                        "symphony_id": symphony_id,
                        "actual_symphony_id": actual_symphony_id,
                        "account": account,
                        "symphony_name": symphony_name,
                        "reason": reason,
                        "attempted_level": attempted_level,
                        "current_return": current_return,
                        "safe_hwm": safe_hwm,
                        "stop_trigger_level": stop_trigger_level,
                        "prob_beating": prob_beating,
                        "weighted_vwap_diff": weighted_vwap_diff,
                        "symphony_vol": symphony_vol,
                        "vwap_ticks": bot_state[symphony_id]["vwap_ticks"],
                        "acc_TAKE_PROFIT_MC_PCT": acc_TAKE_PROFIT_MC_PCT,
                        "prob_loss_dynamic": prob_loss_dynamic,
                        "dynamic_floor": dynamic_floor,
                        "acc_VOLATILITY_MAGNITUDE_MULTIPLIER": vol_mult
                    })

        # --- GHOST SYMPHONY SHADOW CHART FIX ---
        # Process triggered symphonies that have been dropped from the Composer API payload
        for s_id, s_data in bot_state.items():
            if isinstance(s_data, dict) and s_data.get("triggered") and s_id not in active_symphony_ids:
                
                holdings = s_data.get("triggered_basket_snapshot", [])
                f_ret = s_data.get("triggered_at_return", 0.0)
                trigger_prices = s_data.get("trigger_prices", {})
                post_trigger_move = 0.0
                
                if holdings and trigger_prices:
                    for h in holdings:
                        t = h.get("ticker")
                        alloc = h.get("weight", h.get("allocation", 0.0))
                        if t in trigger_prices and t in live_vwaps:
                            p_start = trigger_prices[t]
                            p_now = live_vwaps[t]["last_price"]
                            if p_start > 0:
                                post_trigger_move += alloc * ((p_now - p_start) / p_start)
                                
                    shadow_return = f_ret + (post_trigger_move * 100.0)
                    s_data["current_return"] = shadow_return
                    s_data["shadow_hwm"] = max(s_data.get("shadow_hwm", shadow_return), shadow_return)
                    
                    sym_chart_data = chart_history["symphonies"].setdefault(s_id, [])
                    tracked_stop = s_data.get("triggered_at_stop", -999.0)
                    if tracked_stop == -999.0:
                        tracked_stop = None

                    sym_chart_data.append({
                        "time": current_time_str,
                        "return": shadow_return,
                        "stop": tracked_stop,
                        "event": None,
                        "mc_prob": s_data.get("mc_prob", 0.0),
                        "vol": s_data.get("symphony_vol", 0.0),
                        "vwap_diff": 0.0,
                        "base_atr_pct": 0.0,
                        "dynamic_multiplier": 1.0,
                        "rvol": 1.0,
                        "vw_atr": s_data.get("symphony_vol", 0.0)
                    })

        # Process Execution Queue
        if execution_queue:
            # Group by account to process polling per account
            execution_by_account = {}
            for item in execution_queue:
                acc = item["account"]
                if acc not in execution_by_account:
                    execution_by_account[acc] = []
                execution_by_account[acc].append(item)

            print(f"\nProcessing Execution Queue ({len(execution_queue)} items)...", flush=True)
            
            for account, account_queue in execution_by_account.items():
                for i in range(0, len(account_queue), 25):
                    chunk = account_queue[i:i+25]
                    
                    if i > 0:
                        print("  -> ⏳ Rate limit chunking: Sleeping for 60 seconds before next batch...", flush=True)
                        time.sleep(60)

                    pending_liquidations = []
                    pending_items = {}

                    for item in chunk:
                        sym_id = item["symphony_id"]
                        actual_id = item["actual_symphony_id"]
                        reason = item["reason"]

                        is_enabled = ACCOUNT_ENABLED_MAP.get(account, True)
                        if LIVE_EXECUTION and is_enabled:
                            print(f"  -> [LIVE EXECUTION] Sending sell-to-cash command for {item['symphony_name']}...", flush=True)
                            success = execute_sell_to_cash(actual_id, account, bot_state, sym_id)
                        else:
                            mode_str = "[DRY RUN]" if not LIVE_EXECUTION else "[ACCOUNT DISABLED]"
                            print(f"  -> {mode_str} Execution bypassed for {item['symphony_name']}.", flush=True)
                            success = True
                        
                        if success:
                            pending_liquidations.append(sym_id)
                            pending_items[sym_id] = item
                        else:
                            print(f"     !!! EXECUTION FAILED FOR {item['symphony_name']}. Skipping state update !!!", flush=True)
                            
                    if pending_liquidations:
                        print(f"  -> Polling for liquidation settlement for {len(pending_liquidations)} symphonies...", flush=True)
                        for poll_attempt in range(40):
                            if not pending_liquidations:
                                break
                            
                            current_symphonies = fetch_symphony_stats(account)
                            sym_state_lookup = {s.get("id"): s for s in current_symphonies}
                            
                            verified_liquidations = []
                            for pending_id in pending_liquidations:
                                p_item = pending_items[pending_id]
                                actual_sym_id = p_item["actual_symphony_id"]
                                
                                sym = sym_state_lookup.get(pending_id)
                                if not sym:
                                    sym = next((s for s in current_symphonies if s.get("symphony_id", s.get("id")) == actual_sym_id), None)
                                
                                is_liquidated = False
                                if not sym:
                                    # If not in the list, we can assume it was removed/fully liquidated
                                    is_liquidated = True
                                else:
                                    val = sym.get("current_value", sym.get("value", 0.0))
                                    holdings = sym.get("holdings", [])
                                    
                                    if val < 1:
                                        is_liquidated = True
                                    elif not holdings:
                                        is_liquidated = True
                                    else:
                                        all_cash = True
                                        for h in holdings:
                                            ticker = h.get("ticker", "").upper()
                                            if "$USD" not in ticker and "CASH" not in ticker:
                                                all_cash = False
                                                break
                                        if all_cash:
                                            is_liquidated = True
                                            
                                is_enabled = ACCOUNT_ENABLED_MAP.get(account, True)
                                if is_liquidated or not LIVE_EXECUTION or not is_enabled:
                                    verified_liquidations.append(pending_id)
                                    
                            for v_id in verified_liquidations:
                                pending_liquidations.remove(v_id)
                                item = pending_items[v_id]
                                reason = item["reason"]
                                sym_chart_data = chart_history["symphonies"].get(v_id, [])

                                bot_state[v_id]["armed"] = False
                                bot_state[v_id]["tp_armed"] = False
                                bot_state[v_id]["triggered"] = True
                                bot_state[v_id]["triggered_reason"] = reason
                                bot_state[v_id]["triggered_at_return"] = item["current_return"]
                                bot_state[v_id]["triggered_at_hwm"] = item["safe_hwm"]
                                bot_state[v_id]["triggered_at_stop"] = item["attempted_level"]
                                bot_state[v_id]["triggered_at_time"] = current_time_str
                                bot_state[v_id]["high_water_mark"] = -999.0
                                bot_state[v_id]["prob_loss_dynamic"] = item.get("prob_loss_dynamic")
                                bot_state[v_id]["dynamic_floor"] = item.get("dynamic_floor")

                                trigger_prices = {}
                                triggered_basket_snapshot = []
                                for h in bot_state[v_id].get("current_holdings", []):
                                    t = h.get("ticker")
                                    alloc = h.get("weight", h.get("allocation", 0.0))
                                    price = 0.0
                                    if t in live_vwaps:
                                        price = live_vwaps[t]["last_price"]
                                        trigger_prices[t] = price
                                    triggered_basket_snapshot.append({
                                        "ticker": t,
                                        "allocation": alloc,
                                        "price": price
                                    })
                                bot_state[v_id]["trigger_prices"] = trigger_prices
                                bot_state[v_id]["triggered_basket_snapshot"] = triggered_basket_snapshot

                                if sym_chart_data:
                                    sym_chart_data[-1]["stop"] = item["attempted_level"]
                                    sym_chart_data[-1]["event"] = reason

                                reporting.send_discord_alert(
                                    item["symphony_name"], item["current_return"], item["prob_beating"], 
                                    item["stop_trigger_level"], item["safe_hwm"], LIVE_EXECUTION, DISCORD_WEBHOOK_URL, 
                                    exit_reason=reason, vwap_diff=item["weighted_vwap_diff"], 
                                    vwap_breakdown_ticks=item["vwap_ticks"], tp_threshold=item["acc_TAKE_PROFIT_MC_PCT"],
                                    symphony_vol=item.get("symphony_vol"),
                                    prob_loss_dynamic=item.get("prob_loss_dynamic"),
                                    dynamic_floor=item.get("dynamic_floor"),
                                    volatility_multiplier=item.get("acc_VOLATILITY_MAGNITUDE_MULTIPLIER")
                                )
                                database.log_symphony_event(v_id, "Sell-to-Cash Settlement Confirmed", "execution")
                                print(f"  -> [SETTLED] {item['symphony_name']} successfully moved to cash.", flush=True)
                                database.save_state(bot_state)
                            
                            if pending_liquidations:
                                time.sleep(3)
                                
                        if pending_liquidations:
                            print(f"     !!! TIMEOUT waiting for settlement on {len(pending_liquidations)} symphonies. They will retry or sync on next run.", flush=True)

        database.save_state(bot_state)
        database.save_chart_history(chart_history)

    finally:
        database.release_lock()

if __name__ == "__main__":
    main()
