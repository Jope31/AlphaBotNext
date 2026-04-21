"""Core execution logic for Alpha Bot with VIX Regimes, Lockfile, and Optimistic Saving."""

import os
import sys
import time
import json
import math
import glob
from datetime import datetime, timedelta, timezone, time as dt_time
import requests
import numpy as np
import pandas as pd
from alpaca_trade_api.rest import TimeFrame
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

# --- STRATEGY PARAMETERS ---
TRIGGER_THRESHOLD_PCT = float(os.getenv("TRIGGER_THRESHOLD_PCT", "15.0"))
MAX_SQUEEZE_FLOOR = float(os.getenv("MAX_SQUEEZE_FLOOR", "0.20"))
TAKE_PROFIT_MC_PCT = float(os.getenv("TAKE_PROFIT_MC_PCT", "5.0"))
LOSS_ARM_PCT = float(os.getenv("LOSS_ARM_PCT", "1.5"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "1.5"))
ENDING_STOP_PCT = float(os.getenv("ENDING_STOP_PCT", "0.5"))
BREAKEVEN_ACTIVATION_PCT = float(os.getenv("BREAKEVEN_ACTIVATION_PCT", "2.0"))
MIN_MULTIPLIER_FLOOR = float(os.getenv("MIN_MULTIPLIER_FLOOR", "0.5"))

# --- VOLATILITY REGIME PARAMETERS ---
VIX_LOW_THRESHOLD = float(os.getenv("VIX_LOW_THRESHOLD", "15.0"))
VIX_HIGH_THRESHOLD = float(os.getenv("VIX_HIGH_THRESHOLD", "25.0"))
VIX_LOW_MULT = float(os.getenv("VIX_LOW_MULT", "1.5"))
VIX_MID_MULT = float(os.getenv("VIX_MID_MULT", "2.0"))
VIX_HIGH_MULT = float(os.getenv("VIX_HIGH_MULT", "2.5"))

# --- PARABOLIC PARAMETERS ---
PARABOLIC_VELOCITY_THRESHOLD = 2.0
MAX_PARABOLIC_SQUEEZE = 0.50

SIMULATION_PATHS = 5000
NEIGHBOR_K = 150

# ==========================================
# 2. STATE MANAGEMENT & LOGGING
# ==========================================
STATE_FILE = "bot_state.json"
HISTORY_CACHE_FILE = "history_cache.json"
CHART_FILE = "chart_history.json"
VIX_CACHE_FILE = "vix_cache.json"
LOCK_FILE = "alpha_bot.lock"


def acquire_lock():
    """Acquires a file lock to prevent overlapping minute executions."""
    if os.path.exists(LOCK_FILE):
        try:
            # If a lock is older than 3 minutes, assume a crash and break it
            if time.time() - os.path.getmtime(LOCK_FILE) > 180:
                os.remove(LOCK_FILE)
            else:
                return False
        except OSError:
            return False
    try:
        with open(LOCK_FILE, 'w') as f:
            f.write(str(time.time()))
        return True
    except IOError:
        return False


def release_lock():
    """Releases the execution lock."""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError:
        pass


def load_state():
    """Loads the bot state from a JSON file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    """Saves the bot state to a JSON file."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)


def load_chart_history():
    """Loads the intraday chart history."""
    if os.path.exists(CHART_FILE):
        try:
            with open(CHART_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_chart_history(history):
    """Saves the intraday chart history."""
    with open(CHART_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, separators=(",", ":"))


def analyze_intraday_data(api_client, symbols, target_date_et, lookback_days=5):
    """Fetches multi-day 1-minute bars to calculate rolling Noise Floor and EOD Volatility."""
    intraday_stats = {}
    try:
        start_date = target_date_et - timedelta(days=lookback_days + 2)
        start_time = start_date.replace(hour=9, minute=30, second=0, microsecond=0)
        end_time = target_date_et.replace(hour=16, minute=0, second=0, microsecond=0)

        start_str = start_time.isoformat()
        end_str = end_time.isoformat()

        for symbol in symbols:
            bars = api_client.get_bars(symbol, TimeFrame.Minute, start=start_str, end=end_str).df
            if bars.empty:
                continue

            if bars.index.tz is None:
                bars.index = bars.index.tz_localize("UTC").tz_convert("US/Eastern")
            else:
                bars.index = bars.index.tz_convert("US/Eastern")

            bars["return"] = bars.groupby(bars.index.date)["close"].pct_change()
            noise_floor = bars["return"].std()

            mid_day_bars = bars.between_time("09:30", "15:30")
            eod_bars = bars.between_time("15:30", "16:00")

            mid_day_vol = mid_day_bars["return"].std() if len(mid_day_bars) > 2 else 0.0001
            eod_vol = eod_bars["return"].std() if len(eod_bars) > 2 else 0.0001

            if mid_day_vol == 0 or pd.isna(mid_day_vol):
                mid_day_vol = 0.0001
            if eod_vol == 0 or pd.isna(eod_vol):
                eod_vol = 0.0001

            eod_vol_ratio = eod_vol / mid_day_vol

            tick_threshold = 2
            if noise_floor > 0.0015:
                tick_threshold = 3
            elif noise_floor < 0.0003:
                tick_threshold = 1

            intraday_stats[symbol] = {
                "noise_floor_pct": round(float(noise_floor) * 100, 4),
                "eod_vol_ratio": round(float(eod_vol_ratio), 2),
                "recommended_tick_threshold": tick_threshold,
            }
    except Exception as e:
        print(f"Error during intraday analysis: {e}")
    return intraday_stats


def get_latest_post_mortem_profiles():
    """Loads yesterday's dynamic intraday profiles."""
    try:
        files = glob.glob("post_mortem_*.json")
        if not files:
            return {}
        latest_file = sorted(files, reverse=True)[0]
        with open(latest_file, "r") as f:
            return json.load(f).get("intraday_analysis", {})
    except:
        return {}


def generate_eod_snapshot(bot_state, current_date_str, is_post_rebalance=False):
    """Generates a two-stage daily post-mortem JSON snapshot."""
    report_file = f"post_mortem_{current_date_str}.json"

    if not is_post_rebalance:
        # ========================================================
        # STAGE 1 (15:54 ET): Freeze Math & Shadow Returns
        # ========================================================
        if os.path.exists(report_file):
            return  # Already generated Stage 1 today

        print(f"  -> Generating Stage 1 Post-Mortem (Locking Math): {report_file}")

        report = {
            "date": current_date_str,
            "summary": {
                "total_monitored": 0,
                "total_triggered": 0,
                "positive_guard_alpha_count": 0,
            },
            "tomorrow_target_holdings": {"STATUS": "Pending Composer Rebalance"},
            "triggers": [],
        }

        for sym_id, sym in bot_state.items():
            if sym_id == "date":
                continue

            report["summary"]["total_monitored"] += 1

            if sym.get("triggered"):
                report["summary"]["total_triggered"] += 1

                f_ret = sym.get("triggered_at_return", 0.0)
                live_ret = sym.get("current_return", 0.0)
                saved_pct = f_ret - live_ret  # Guard Alpha Math locked before API wiped

                if saved_pct > 0:
                    report["summary"]["positive_guard_alpha_count"] += 1

                exit_reason = (
                    "Take-Profit" if f_ret == sym.get("triggered_at_stop") else "Trailing Stop"
                )

                report["triggers"].append(
                    {
                        "symphony_name": sym.get("name", "Unknown"),
                        "exit_reason": exit_reason,
                        "exit_return": round(f_ret, 2),
                        "shadow_return": round(live_ret, 2),
                        "saved_pct_guard_alpha": round(saved_pct, 2),
                        "hwm_at_trigger": round(sym.get("triggered_at_hwm", 0.0), 2),
                        "time_triggered": sym.get("triggered_at_time", ""),
                        "symphony_vol": round(sym.get("symphony_vol", 0.0), 2),
                        "next_day_holdings": ["Pending..."],
                    }
                )

        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=4)

    else:
        # ========================================================
        # STAGE 2 (16:00 ET): Inject Tomorrow's Holdings
        # ========================================================
        if not os.path.exists(report_file):
            print("  -> Warning: Stage 1 snapshot missing. Cannot inject new holdings.")
            return

        with open(report_file, "r", encoding="utf-8") as f:
            report = json.load(f)

        if "STATUS" not in report.get("tomorrow_target_holdings", {}):
            return  # Already completed Stage 2 today

        print(f"  -> Generating Stage 2 Post-Mortem (Injecting Holdings): {report_file}")

        # Aggregate tomorrow's holdings from the updated bot_state
        portfolio_holdings_summary = {}

        for sym_id, sym in bot_state.items():
            if sym_id == "date":
                continue

            # 1. Update individual trigger next_day_holdings
            sym_holdings = [h.get("ticker") for h in sym.get("current_holdings", [])]
            for trigger in report.get("triggers", []):
                if trigger.get("symphony_name") == sym.get("name"):
                    trigger["next_day_holdings"] = sym_holdings

            # 2. Update portfolio-wide aggregate map
            for holding in sym.get("current_holdings", []):
                ticker = holding.get("ticker", "UNKNOWN")
                weight = holding.get("allocation", 0.0)
                if ticker not in portfolio_holdings_summary:
                    portfolio_holdings_summary[ticker] = 0.0
                portfolio_holdings_summary[ticker] += weight

        # Sort portfolio holdings by weight
        sorted_holdings = dict(
            sorted(portfolio_holdings_summary.items(), key=lambda item: item[1], reverse=True)
        )
        report["tomorrow_target_holdings"] = sorted_holdings

        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=4)


# ==========================================
# 3. API CONNECTORS & RATE LIMIT HANDLING
# ==========================================
def get_composer_headers(key=None, secret=None):
    """Returns headers required for the Composer API."""
    return {
        "x-api-key-id": key or COMPOSER_KEY_ID,
        "authorization": f"Bearer {secret or COMPOSER_SECRET}",
        "Content-Type": "application/json",
    }


def get_alpaca_headers():
    """Returns headers required for the Alpaca API."""
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


def fetch_symphony_stats(account_id):
    """Fetches symphony stats for a given account ID."""
    url = (
        f"https://api.composer.trade/api/v0.1/portfolio/accounts/"
        f"{account_id}/symphony-stats-meta"
    )
    try:
        response = requests.get(url, headers=get_composer_headers(), timeout=15)
        time.sleep(1.5)
        if response.status_code == 200:
            return response.json().get("symphonies", [])
        print(f"Error fetching account {account_id}: {response.text}")
    except requests.RequestException as e:
        print(f"Exception fetching account {account_id}: {e}")
    return []


def execute_sell_to_cash(actual_symphony_id, account_id):
    """Executes a sell-to-cash command for a symphony."""
    url = (
        f"https://api.composer.trade/api/v0.1/deploy/accounts/{account_id}"
        f"/symphonies/{actual_symphony_id}/go-to-cash"
    )
    try:
        response = requests.post(url, headers=get_composer_headers(), json={}, timeout=10)
        print(f"     -> [API Status]: HTTP {response.status_code}")

        if response.status_code in [200, 201, 202]:
            try:
                print(f"     -> [Composer Receipt]: {response.json()}")
            except ValueError:
                pass
            time.sleep(1.5)
            return True

        print(f"     !!! [COMPOSER REJECTED]: {response.text}")
        time.sleep(1.5)
        return False
    except requests.RequestException as e:
        print(f"     !!! [API CRASH]: {str(e)}")
        return False


def send_discord_alert(
    symphony_name,
    current_return,
    prob_beating,
    stop_trigger_level,
    high_water_mark,
    is_live,
    exit_reason="Trailing Stop",
):
    """Sends a discord alert about trade execution."""
    if not DISCORD_WEBHOOK_URL:
        return

    # --- Context-Aware Discord Formatting ---
    if exit_reason == "Take-Profit":
        base_title = "🎯 Smart Take-Profit Locked"
        live_color = 5763719  # Discord Green
    elif current_return > 0:
        base_title = "✅ Profit Locked"
        live_color = 5763719  # Discord Green
    elif current_return < 0:
        base_title = "🛑 Bleed Stopped"
        live_color = 15548997  # Discord Red
    else:
        base_title = "🛡️ Breakeven Locked"
        live_color = 3447003  # Discord Blue

    title = f"{base_title}: {exit_reason} Triggered" if is_live else f"⚠️ [DRY RUN] {base_title}"
    color = live_color if is_live else 16766720  # Yellow for dry runs
    action_text = (
        "Executed 'Sell to Cash' via API. " "Trade queued for Composer execution window."
        if is_live
        else "Bypassed (Dry Run Mode)"
    )

    payload = {
        "embeds": [
            {
                "title": title,
                "color": color,
                "fields": [
                    {"name": "Symphony", "value": symphony_name, "inline": True},
                    {
                        "name": "Exit Return",
                        "value": f"{current_return:.2f}%",
                        "inline": True,
                    },
                    {
                        "name": "High Water Mark",
                        "value": f"{high_water_mark:.2f}%",
                        "inline": True,
                    },
                    {
                        "name": "Stop Level",
                        "value": f"{stop_trigger_level:.2f}%",
                        "inline": True,
                    },
                    {
                        "name": "MC Probability",
                        "value": f"{prob_beating:.1f}%",
                        "inline": True,
                    },
                    {"name": "Action Taken", "value": action_text, "inline": False},
                ],
                "footer": {"text": "Alpha Bot • Hybrid Trailing Stop"},
            }
        ]
    }
    requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)


def fetch_alpaca_history(tickers, current_date_str):
    """Fetches historical data from Alpaca."""
    if "SPY" not in tickers:
        tickers.append("SPY")

    tickers_list = sorted(list(set(tickers)))

    if os.path.exists(HISTORY_CACHE_FILE):
        try:
            with open(HISTORY_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("date") == current_date_str and cache.get("tickers") == tickers_list:
                print("  -> Loading static 3-year history from local cache.")
                return cache.get("data", {})
        except json.JSONDecodeError:
            pass

    print(f"Fetching 3-year history from Alpaca for Monte Carlo " f"({len(tickers)} tickers)...")
    start_date = (datetime.now() - timedelta(days=365 * 3 + 30)).strftime("%Y-%m-%dT00:00:00Z")
    headers = get_alpaca_headers()
    historical_data = {}
    batch_size = 30

    for i in range(0, len(tickers_list), batch_size):
        batch = tickers_list[i : i + batch_size]
        symbol_string = ",".join(batch)
        print(f"  -> Downloading batch {i // batch_size + 1}: " f"{len(batch)} tickers...")

        page_token = None
        while True:
            url = (
                f"https://data.alpaca.markets/v2/stocks/bars?"
                f"symbols={symbol_string}&timeframe=1Day&"
                f"start={start_date}&limit=10000&adjustment=split"
            )
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
                    else:
                        print(
                            f"Alpaca API Error on batch (attempt {attempt+1}/{max_retries}): HTTP {response.status_code} - {response.text}"
                        )
                except requests.RequestException as e:
                    print(f"Alpaca API Request Exception (attempt {attempt+1}/{max_retries}): {e}")
                time.sleep(2 * (attempt + 1))

            if not success:
                print("Failed to download batch after multiple retries.")
                break

            data = response.json()
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
                            }

            page_token = data.get("next_page_token")
            if not page_token:
                break

    print("  -> History download complete. Saving to daily cache.")
    try:
        with open(HISTORY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": current_date_str,
                    "tickers": tickers_list,
                    "data": historical_data,
                },
                f,
            )
    except OSError as e:
        print(f"  -> Failed to write cache: {e}")

    return historical_data


def get_live_spy_data():
    """Fetches the latest SPY return data."""
    start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
    headers = get_alpaca_headers()
    url = (
        f"https://data.alpaca.markets/v2/stocks/bars?"
        f"symbols=SPY&timeframe=1Day&start={start_date}&limit=10"
    )

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            bars = response.json().get("bars", {}).get("SPY", [])
            if len(bars) >= 2:
                prev_c = bars[-2]["c"]
                curr_c = bars[-1]["c"]
                daily_ret = (curr_c - prev_c) / prev_c
                return daily_ret * 100.0
    except requests.RequestException as e:
        print(f"Error fetching live SPY data: {e}")
    return 0.0


def get_live_vix():
    """Fetches live VIX index via Yahoo Finance, using a 15-minute local cache to prevent rate limits."""
    current_time = time.time()
    
    # 1. Check local cache first
    if os.path.exists(VIX_CACHE_FILE):
        try:
            with open(VIX_CACHE_FILE, "r") as f:
                cache = json.load(f)
                # If cache is less than 15 minutes (900 seconds) old, use it
                if current_time - cache.get("timestamp", 0) < 900:
                    return float(cache.get("vix_value", 20.0))
        except:
            pass

    # 2. If cache is missing or expired, fetch fresh data
    url = "https://query2.finance.yahoo.com/v8/finance/chart/^VIX?interval=1d&range=1d"
    headers = {'User-Agent': 'Mozilla/5.0'}
    vix_value = 20.0 # Default fallback
    
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            vix_value = float(data['chart']['result'][0]['meta']['regularMarketPrice'])
            
            # Save the new value to cache
            with open(VIX_CACHE_FILE, "w") as f:
                json.dump({"timestamp": current_time, "vix_value": vix_value}, f)
                
    except Exception as e:
        print(f"  ⚠️ Error fetching VIX: {e}. Defaulting to Normal Regime or previous cache.")
        
    return vix_value


# ==========================================
# 4. MATH ENGINE: MONTE CARLO & VOLATILITY
# ==========================================
def run_monte_carlo(holdings, historical_data, spy_today_return):
    """Runs monte carlo simulation based on historical data."""
    current_symphony_return = sum(
        (h.get("last_percent_change", 0.0) * 100.0) * h.get("allocation", 0.0)
        for h in holdings
        if h.get("last_percent_change") is not None
    )
    valid_dates = sorted(list(historical_data.keys()))
    if len(valid_dates) < 20:
        return 100.0

    distances = []
    for date in valid_dates:
        spy_hist = historical_data[date].get("SPY", {}).get("daily_ret", 0.0)
        distances.append((abs(spy_hist - (spy_today_return / 100.0)), date))
    distances.sort(key=lambda x: x[0])
    nearest_days = [d[1] for d in distances[:NEIGHBOR_K]]

    weights = {h["ticker"]: h.get("allocation", 0.0) for h in holdings}
    latest_valid_day = valid_dates[-1]
    missing_tickers = {
        t for t in weights.keys() if t not in historical_data.get(latest_valid_day, {})
    }

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


def calculate_20d_vol(holdings, historical_data):
    """Calculates the weighted 20-day standard deviation of a symphony's holdings."""
    valid_dates = sorted(list(historical_data.keys()))[-20:]
    if len(valid_dates) < 20:
        return 0.0

    daily_returns = []
    for date in valid_dates:
        day_return = 0.0
        for h in holdings:
            ticker = h.get("ticker")
            weight = h.get("allocation", 0.0)
            if ticker in historical_data[date]:
                day_return += (historical_data[date][ticker].get("daily_ret", 0.0) * 100.0) * weight
            else:
                # Fallback to SPY if specific ticker is missing on that historical day
                spy_ret = historical_data[date].get("SPY", {}).get("daily_ret", 0.0)
                day_return += (spy_ret * 100.0) * weight
        daily_returns.append(day_return)

    if not daily_returns:
        return 0.0

    return float(np.std(daily_returns))


# ==========================================
# 5. MAIN EXECUTION LOOP
# ==========================================
def get_current_et():
    """Gets the current time in ET, safely handling missing Windows tzdata."""
    utc_now = datetime.now(timezone.utc)
    try:
        # pylint: disable=import-outside-toplevel
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback if tzdata is missing
        if 3 <= utc_now.month <= 11:
            return utc_now - timedelta(hours=4)  # EDT approx
        return utc_now - timedelta(hours=5)  # EST approx


def main():
    """Main execution entry point."""
    
    # 1. Ensure absolute process exclusivity to prevent State Corruption
    if not acquire_lock():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ Overlap Detected: Previous minute is still executing (likely processing liquidations). Skipping...")
        return

    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Alpha Bot Waking Up...")
        mode_text = "LIVE EXECUTION (DANGER)" if LIVE_EXECUTION else "DRY RUN (SAFE)"
        print(f"MODE: {mode_text}")

        # --- MARKET HOURS GATEKEEPER ---
        force_run = "--force" in sys.argv
        current_et = get_current_et()

        is_weekday = current_et.weekday() < 5
        current_time = current_et.time()

        market_open = dt_time(10, 30)
        market_close = dt_time(16, 0)
        rebalance_blackout = dt_time(15, 54) 
        post_mortem_cutoff = dt_time(16, 5)  

        if not is_weekday or current_time < market_open or current_time > post_mortem_cutoff:
            if not force_run:
                print(f"  -> Market closed or in Grace Period (ET: {current_et.strftime('%a %H:%M')}). Sleeping to conserve API limits...")
                return
            print("  -> Market closed, but --force flag detected! Bypassing gatekeeper...")

        # --- COMPOSER REBALANCE BLACKOUT (STAGE 1 SNAPSHOT) ---
        if rebalance_blackout <= current_time < market_close:
            bot_state = load_state()
            generate_eod_snapshot(bot_state, current_et.strftime("%Y-%m-%d"), is_post_rebalance=False)

            if not force_run:
                print(f"  -> 🛑 COMPOSER REBALANCE BLACKOUT (ET: {current_et.strftime('%H:%M')}). Pausing to prevent false triggers during Composer's end-of-day rebalance...")
                return
            print("  -> Rebalance blackout active, but --force flag detected! Bypassing...")

        if not COMPOSER_KEY_ID or not ALPACA_KEY:
            print("CRITICAL: Missing API Keys. Please check your .env file.")
            return

        bot_state = load_state()
        chart_history = load_chart_history()

        current_date_str = current_et.strftime("%Y-%m-%d")
        current_time_str = current_et.strftime("%H:%M")

        # WIPE STATE ON NEW DAY
        if bot_state.get("date") != current_date_str:
            print(f"  -> New trading day detected ({current_date_str} ET). Wiping old state and chart memory.")
            bot_state = {"date": current_date_str}
            save_state(bot_state)

        if chart_history.get("date") != current_date_str:
            chart_history = {"date": current_date_str, "symphonies": {}}
            save_chart_history(chart_history)

        # --- LOGARITHMIC TIME DECAY RATIO CALCULATION ---
        m_open_dt = current_et.replace(hour=10, minute=30, second=0, microsecond=0)
        m_close_dt = current_et.replace(hour=16, minute=0, second=0, microsecond=0)

        total_trading_minutes = (m_close_dt - m_open_dt).total_seconds() / 60.0
        elapsed_minutes = (current_et - m_open_dt).total_seconds() / 60.0

        time_ratio = max(0.0, min(1.0, elapsed_minutes / total_trading_minutes))
        base_decay_curve = math.log10(1 + 9 * time_ratio)

        all_tickers = set()
        symphony_data_cache = {}

        for account in ACCOUNT_UUIDS:
            symphonies = fetch_symphony_stats(account)
            symphony_data_cache[account] = symphonies
            for sym in symphonies:
                for holding in sym.get("holdings", []):
                    raw_ticker = holding.get("ticker", "")
                    clean_ticker = raw_ticker.split("::")[-1].split("//")[0]
                    alpaca_ticker = clean_ticker.replace("/", ".")
                    if alpaca_ticker:
                        all_tickers.add(alpaca_ticker)
                        holding["working_ticker"] = alpaca_ticker

        # --- EOD POST-MORTEM SNAPSHOT STAGE 2 (16:00 - 16:05 ET) ---
        if market_close <= current_time <= post_mortem_cutoff:
            for account, symphonies in symphony_data_cache.items():
                for sym in symphonies:
                    s_id = sym["id"]
                    if s_id in bot_state:
                        bot_state[s_id]["current_holdings"] = [
                            {
                                "ticker": h.get("working_ticker", h.get("ticker")),
                                "allocation": h.get("allocation", 0.0),
                            }
                            for h in sym.get("holdings", [])
                        ]
            save_state(bot_state)
            generate_eod_snapshot(bot_state, current_date_str, is_post_rebalance=True)
            print("  -> EOD Post-Mortem Snapshot complete. Ending execution for the day.")
            return

        historical_data = fetch_alpaca_history(list(all_tickers), current_date_str)
        if not historical_data:
            return

        # Fetch Macro Environment Data
        spy_today = get_live_spy_data()
        vix_today = get_live_vix()

        # Determine Market Regime based on VIX
        if vix_today < VIX_LOW_THRESHOLD:
            regime_mult = VIX_LOW_MULT
            regime_name = "LOW VOLATILITY"
        elif vix_today > VIX_HIGH_THRESHOLD:
            regime_mult = VIX_HIGH_MULT
            regime_name = "CRISIS/ELEVATED"
        else:
            regime_mult = VIX_MID_MULT
            regime_name = "NORMAL"

        print(f"  -> Macro Environment: SPY {spy_today:.2f}% | VIX {vix_today:.2f} ({regime_name} Regime: {regime_mult}x Vol)")
        print("\nEvaluating Symphonies...")

        for account, symphonies in symphony_data_cache.items():
            for sym in symphonies:
                symphony_id = sym["id"]
                actual_symphony_id = sym.get("symphony_id", symphony_id)

                symphony_name = sym.get("name", "Unknown Symphony")
                holdings = sym.get("holdings", [])
                current_return = sym.get("last_percent_change", 0.0) * 100

                for h in holdings:
                    h["ticker"] = h.get("working_ticker", h.get("ticker"))

                # --- INIT BOT STATE & BACKWARD COMPATIBILITY ---
                profiles = get_latest_post_mortem_profiles()
                symphony_holdings = [h.get("ticker") for h in holdings]
                symphony_tick_threshold = 2
                symphony_eod_ratio = 1.0

                if profiles and symphony_holdings:
                    ticks = [profiles.get(sym, {}).get("recommended_tick_threshold", 2) for sym in symphony_holdings]
                    eods = [profiles.get(sym, {}).get("eod_vol_ratio", 1.0) for sym in symphony_holdings]

                    if ticks:
                        symphony_tick_threshold = max(ticks)
                    if eods:
                        symphony_eod_ratio = max(eods)

                if symphony_id not in bot_state:
                    bot_state[symphony_id] = {
                        "high_water_mark": current_return,
                        "armed": False,
                        "tp_armed": False,
                        "triggered": False,
                        "mc_history": [],
                        "below_stop_count": 0,
                        "above_tp_count": 0,
                        "breakeven_locked": False,
                        "tick_threshold": symphony_tick_threshold,
                        "eod_vol_ratio": symphony_eod_ratio,
                    }
                else:
                    bot_state[symphony_id]["tick_threshold"] = symphony_tick_threshold
                    bot_state[symphony_id]["eod_vol_ratio"] = symphony_eod_ratio

                # Tracking previous state to determine Chart Events
                prev_armed = bot_state[symphony_id].get("armed", False)
                prev_tp_armed = bot_state[symphony_id].get("tp_armed", False)
                prev_triggered = bot_state[symphony_id].get("triggered", False)

                for key in ["triggered", "tp_armed", "breakeven_locked"]:
                    if key not in bot_state[symphony_id]:
                        bot_state[symphony_id][key] = False
                for key in ["below_stop_count", "above_tp_count"]:
                    if key not in bot_state[symphony_id]:
                        bot_state[symphony_id][key] = 0
                if "mc_history" not in bot_state[symphony_id]:
                    bot_state[symphony_id]["mc_history"] = []

                # Update High Water Mark
                if current_return > bot_state[symphony_id]["high_water_mark"] and not bot_state[symphony_id]["triggered"]:
                    bot_state[symphony_id]["high_water_mark"] = current_return

                high_water_mark = bot_state[symphony_id]["high_water_mark"]

                # --- 1. MC Probability & Volatility Engine ---
                prob_beating = run_monte_carlo(holdings, historical_data, spy_today)
                symphony_vol = calculate_20d_vol(holdings, historical_data)

                decay_curve = base_decay_curve

                # --- INJECT EOD RELIEF VALVE HERE ---
                if current_et.hour == 15 and current_et.minute >= 30:
                    eod_ratio = bot_state[symphony_id].get("eod_vol_ratio", 1.0)
                    if eod_ratio > 1.2:
                        relief_factor = min(0.15, (eod_ratio - 1.0) * 0.1)
                        decay_curve = decay_curve * (1.0 - relief_factor)
                        print(f"  -> [{symphony_name}] Applied EOD Relief Valve. Relaxing squeeze by {relief_factor*100:.1f}%")

                # --- ASYMMETRIC PARABOLIC SQUEEZE LOGIC ---
                velocity_squeeze = 1.0
                if symphony_vol > 0.5:
                    velocity = high_water_mark / symphony_vol
                    if velocity > PARABOLIC_VELOCITY_THRESHOLD:
                        excess = min(1.0, (velocity - PARABOLIC_VELOCITY_THRESHOLD) / 2.0)
                        velocity_squeeze = 1.0 - (excess * MAX_PARABOLIC_SQUEEZE)
                        print(f"  ⚡ [{symphony_name[:20]}] PARABOLIC SQUEEZE: {velocity_squeeze:.2f}x (HWM: {high_water_mark:.1f}% | Vol: {symphony_vol:.1f}%)")

                # --- Vol-Adjusted Dynamic Stop with VIX Regime ---
                if symphony_vol > 0:
                    morning_stop = max(symphony_vol * regime_mult, MIN_MULTIPLIER_FLOOR)
                    afternoon_stop = max(symphony_vol * (regime_mult * 0.33), MIN_MULTIPLIER_FLOOR * 0.5)
                else:
                    morning_stop = TRAILING_STOP_PCT
                    afternoon_stop = ENDING_STOP_PCT

                # Apply Time Decay + Velocity Squeeze
                dynamic_trailing_stop = (morning_stop - ((morning_stop - afternoon_stop) * decay_curve)) * velocity_squeeze

                # --- 2. Arming & Disarming Mechanism ---
                should_arm = False
                arm_reason = ""
                effective_loss_threshold = max(LOSS_ARM_PCT, symphony_vol)

                if prob_beating < TRIGGER_THRESHOLD_PCT and prob_beating >= TAKE_PROFIT_MC_PCT:
                    should_arm = True
                    arm_reason = f"MC Prob {prob_beating:.1f}%"
                elif current_return < -effective_loss_threshold:
                    should_arm = True
                    arm_reason = f"Vol-Scaled Loss (<-{effective_loss_threshold:.2f}%)"

                if should_arm and not bot_state[symphony_id]["armed"] and not bot_state[symphony_id]["triggered"]:
                    bot_state[symphony_id]["armed"] = True
                    print(f"  *** {symphony_name} ARMED ({arm_reason}) ***")

                elif bot_state[symphony_id]["armed"] and not bot_state[symphony_id]["triggered"]:
                    if prob_beating > (TRIGGER_THRESHOLD_PCT * 2) and current_return > 0.0:
                        bot_state[symphony_id]["armed"] = False
                        bot_state[symphony_id]["below_stop_count"] = 0
                        print(f"  *** {symphony_name} DISARMED (Conditions Recovered) ***")

                # --- 3. Strangler (SMA + Multiplier) ---
                bot_state[symphony_id]["mc_history"].append(prob_beating)
                if len(bot_state[symphony_id]["mc_history"]) > 5:
                    bot_state[symphony_id]["mc_history"].pop(0)

                smoothed_mc = sum(bot_state[symphony_id]["mc_history"]) / len(bot_state[symphony_id]["mc_history"])

                if bot_state[symphony_id]["armed"]:
                    mc_health_ratio = max(0.0, min(1.0, smoothed_mc / TRIGGER_THRESHOLD_PCT))
                    strangle_multiplier = MAX_SQUEEZE_FLOOR + (mc_health_ratio * (1.0 - MAX_SQUEEZE_FLOOR))
                    active_trailing_stop = dynamic_trailing_stop * strangle_multiplier
                else:
                    active_trailing_stop = dynamic_trailing_stop

                # --- 4. Dynamic Trailing Stop & Vol-Scaled Breakeven Lock Math ---
                safe_hwm = high_water_mark if high_water_mark != -999.0 else current_return
                base_stop_level = safe_hwm - active_trailing_stop

                effective_breakeven_activation = max(BREAKEVEN_ACTIVATION_PCT, symphony_vol)
                if safe_hwm >= effective_breakeven_activation:
                    bot_state[symphony_id]["breakeven_locked"] = True

                if bot_state[symphony_id]["breakeven_locked"]:
                    stop_trigger_level = max(base_stop_level, 0.0)
                else:
                    stop_trigger_level = base_stop_level

                if bot_state[symphony_id]["triggered"]:
                    stop_trigger_level = -999.0

                # --- 4.5 Dynamic Tick Confirmation Trailing Stop Logic ---
                is_trailing_stop_hit = False
                if bot_state[symphony_id]["armed"] and not bot_state[symphony_id]["triggered"]:
                    if current_return <= stop_trigger_level:
                        bot_state[symphony_id]["below_stop_count"] += 1
                        tick_threshold = bot_state[symphony_id].get("tick_threshold", 2)
                        if bot_state[symphony_id]["below_stop_count"] == 1 and tick_threshold > 1:
                            print(f"  ⚠️ {symphony_name[:35]} dipped below stop. Awaiting tick confirmation...")
                        elif bot_state[symphony_id]["below_stop_count"] >= tick_threshold:
                            is_trailing_stop_hit = True
                    else:
                        if bot_state[symphony_id]["below_stop_count"] > 0:
                            print(f"  ✅ {symphony_name[:35]} recovered above stop. Confirmation reset.")
                        bot_state[symphony_id]["below_stop_count"] = 0

                # --- 4.6 Take-Profit Smart Trailing Exit Math (With 2-Tick Confirmation) ---
                tp_triggered_now = False
                if prob_beating < TAKE_PROFIT_MC_PCT:
                    if not bot_state[symphony_id]["tp_armed"] and not bot_state[symphony_id]["triggered"]:
                        bot_state[symphony_id]["tp_armed"] = True
                        bot_state[symphony_id]["above_tp_count"] = 0
                        print(f"  *** {symphony_name} TP-ARMED (Exceptional Gain: MC Prob {prob_beating:.1f}% < {TAKE_PROFIT_MC_PCT}%) ***")
                elif bot_state[symphony_id]["tp_armed"] and not bot_state[symphony_id]["triggered"]:
                    if prob_beating >= TAKE_PROFIT_MC_PCT:
                        bot_state[symphony_id]["above_tp_count"] += 1
                        if bot_state[symphony_id]["above_tp_count"] == 1:
                            print(f"  ⚠️ {symphony_name[:35]} TP signal flashed. Awaiting 2nd tick confirmation...")
                        elif bot_state[symphony_id]["above_tp_count"] >= 2:
                            if current_return > 0:
                                tp_triggered_now = True
                            else:
                                bot_state[symphony_id]["tp_armed"] = False
                                bot_state[symphony_id]["above_tp_count"] = 0
                                print(f"  *** {symphony_name} TP-DISARMED (MC Rose but Return <= 0) ***")
                    else:
                        if bot_state[symphony_id]["above_tp_count"] > 0:
                            print(f"  📉 {symphony_name[:35]} TP signal vanished. Still cranking.")
                        bot_state[symphony_id]["above_tp_count"] = 0

                print(f"  -> {symphony_name[:35]}: Ret: {current_return:.2f}% | HWM: {high_water_mark:.2f}% | Stop Dist: {active_trailing_stop:.2f}% | ArmProb: {prob_beating:.1f}%")

                bot_state[symphony_id]["name"] = symphony_name
                bot_state[symphony_id]["account"] = account
                bot_state[symphony_id]["current_return"] = current_return
                bot_state[symphony_id]["mc_prob"] = prob_beating
                bot_state[symphony_id]["stop_trigger"] = stop_trigger_level
                bot_state[symphony_id]["active_stop_distance"] = active_trailing_stop
                bot_state[symphony_id]["symphony_vol"] = symphony_vol
                bot_state[symphony_id]["velocity_squeeze"] = velocity_squeeze
                
                # Store current holdings so 16:00 ET loop knows what to carry over into tomorrow
                bot_state[symphony_id]["current_holdings"] = [
                    {"ticker": h.get("ticker"), "allocation": h.get("allocation", 0.0)}
                    for h in holdings
                ]

                save_state(bot_state)

                # --- Chart Visualization Logging ---
                chart_event = None
                if is_trailing_stop_hit or tp_triggered_now:
                    chart_event = "Triggered"
                elif bot_state[symphony_id]["armed"] and not prev_armed:
                    chart_event = "Armed"
                elif bot_state[symphony_id]["tp_armed"] and not prev_tp_armed:
                    chart_event = "TP-Armed"

                tracked_stop = (
                    stop_trigger_level
                    if (bot_state[symphony_id]["armed"] or bot_state[symphony_id]["tp_armed"] or bot_state[symphony_id]["triggered"] or prev_triggered)
                    else None
                )
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
                })

                # --- 5. Execution Check & Optimistic Saving ---
                if is_trailing_stop_hit or tp_triggered_now:
                    reason = "Take-Profit" if tp_triggered_now else "Trailing Stop"
                    print(f"  🚨 {reason.upper()} HIT FOR {symphony_name} 🚨")

                    # -------------------------------------------------------------
                    # OPTIMISTIC STATE SAVE: Instantly update file before API sleep
                    # -------------------------------------------------------------
                    bot_state[symphony_id]["armed"] = False
                    bot_state[symphony_id]["tp_armed"] = False
                    bot_state[symphony_id]["triggered"] = True

                    bot_state[symphony_id]["triggered_at_return"] = current_return
                    bot_state[symphony_id]["triggered_at_hwm"] = safe_hwm
                    bot_state[symphony_id]["triggered_at_stop"] = current_return if tp_triggered_now else stop_trigger_level
                    bot_state[symphony_id]["triggered_at_time"] = current_time_str
                    bot_state[symphony_id]["high_water_mark"] = -999.0
                    save_state(bot_state)

                    sym_chart_data[-1]["stop"] = bot_state[symphony_id]["triggered_at_stop"]

                    if LIVE_EXECUTION:
                        print("  -> [LIVE EXECUTION] Sending sell-to-cash command to Composer API...")
                        success = execute_sell_to_cash(actual_symphony_id, account)

                        if success:
                            send_discord_alert(symphony_name, current_return, prob_beating, stop_trigger_level, safe_hwm, LIVE_EXECUTION, exit_reason=reason)
                        else:
                            print("     !!! EXECUTION FAILED. Reverting state to retry next loop !!!")
                            # Rollback state so it tries again next minute
                            bot_state = load_state()
                            bot_state[symphony_id]["triggered"] = False
                            bot_state[symphony_id]["armed"] = not tp_triggered_now
                            bot_state[symphony_id]["tp_armed"] = tp_triggered_now
                            bot_state[symphony_id]["high_water_mark"] = safe_hwm
                            save_state(bot_state)
                            sym_chart_data[-1]["stop"] = stop_trigger_level
                    else:
                        print("  -> [DRY RUN] Execution bypassed.")
                        send_discord_alert(symphony_name, current_return, prob_beating, stop_trigger_level, safe_hwm, LIVE_EXECUTION, exit_reason=reason)

        save_chart_history(chart_history)

    finally:
        # 3. Always release lock so the next minute can process, even if there's an error
        release_lock()


if __name__ == "__main__":
    main()
