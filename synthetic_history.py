import os
import json
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from joblib import Parallel, delayed

import math_engine
import database
import regime_classifier

from dotenv import load_dotenv
load_dotenv()
ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://data.alpaca.markets/v2")

def get_alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET
    }

def fetch_bars(tickers_list, start_str, end_str, timeframe="1Day"):
    headers = get_alpaca_headers()
    batch_size = 30
    all_data = {}
    
    for i in range(0, len(tickers_list), batch_size):
        batch = tickers_list[i : i + batch_size]
        symbol_string = ",".join(batch)
        print(f"      -> Fetching {timeframe} bars batch {i // batch_size + 1}/{len(tickers_list)//batch_size + 1}...", flush=True)

        page_token = None
        while True:
            url = f"{ALPACA_BASE_URL}/stocks/bars?symbols={symbol_string}&timeframe={timeframe}&start={start_str}&end={end_str}&limit=10000&adjustment=split&feed=iex"
            if page_token:
                url += f"&page_token={page_token}"

            success = False
            for attempt in range(10): # Allow up to 10 retries for rate limits
                try:
                    response = requests.get(url, headers=headers, timeout=30)
                    if response.status_code == 200:
                        success = True
                        break
                    elif response.status_code == 429:
                        print(f"      -> Rate limit hit (429). Sleeping for 15s... (Attempt {attempt+1}/10)", flush=True)
                        time.sleep(15)
                    else:
                        print(f"      -> API Error {response.status_code}: {response.text}", flush=True)
                        time.sleep(5)
                except Exception as e:
                    print(f"      -> Request Exception: {e}", flush=True)
                    time.sleep(5)

            if not success:
                print(f"      -> Failed to fetch batch {i // batch_size + 1} after 10 attempts. Aborting.", flush=True)
                break

            data = response.json()
            if "bars" in data:
                for symbol, bars in data["bars"].items():
                    if symbol not in all_data:
                        all_data[symbol] = []
                    all_data[symbol].extend(bars)

            page_token = data.get("next_page_token")
            if not page_token:
                break
            time.sleep(0.35)
                
    return all_data

def generate_synthetic_history(bot_state, current_date_str):
    print("  -> [TELEMETRY] Starting Synthetic History Generation...", flush=True)
    print("  -> Generating Synthetic Forward-Looking Intraday History...", flush=True)
    
    # 1. Extract tickers
    all_tickers = set()
    symphony_holdings = {}
    for sym_id, state in bot_state.items():
        if isinstance(state, dict) and "current_holdings" in state:
            holdings = state["current_holdings"]
            symphony_holdings[sym_id] = holdings
            for h in holdings:
                all_tickers.add(h["ticker"])
                
    if not all_tickers:
        return {}

    # Check cache based on date and exact holdings
    import hashlib
    holdings_str = json.dumps(symphony_holdings, sort_keys=True)
    holdings_hash = hashlib.md5(holdings_str.encode('utf-8')).hexdigest()
    
    cache_dir = "cache"
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"synthetic_history_{current_date_str}_{holdings_hash}.json")
    
    if os.path.exists(cache_file):
        print(f"  -> Loading cached synthetic history from {cache_file}...", flush=True)
        try:
            with open(cache_file, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"  -> Cache load failed: {e}. Regenerating...", flush=True)

    tickers_list = list(all_tickers)
    for sym_id, state in bot_state.items():
        if isinstance(state, dict):
            proxy = state.get("proxy_etf", "SPY")
            if proxy not in tickers_list:
                tickers_list.append(proxy)
    if "SPY" not in tickers_list:
        tickers_list.append("SPY")
    
    # 2. Compute date ranges
    end_date = datetime.strptime(current_date_str, "%Y-%m-%d")
    
    # Use UTC to prevent local timezone (e.g. Japan) from messing up the 'today' comparison
    from datetime import timezone
    now_utc = datetime.now(timezone.utc)
    # If the requested end_date is today (or in the future) relative to US market hours, cap it to yesterday
    # US Eastern is UTC-4 (EDT) or UTC-5 (EST). We can approximate by shifting UTC by -4 hours.
    now_us = now_utc + timedelta(hours=-4)
    today_us = now_us.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    
    if end_date >= today_us:
        end_date = today_us - timedelta(days=1)
    
    # 120 trading days is ~180 calendar days
    start_daily = end_date - timedelta(days=180)
    start_daily_str = start_daily.strftime("%Y-%m-%dT00:00:00Z")
    
    # 6 months lookback (125 trading days is ~180 calendar days)
    start_1m = end_date - timedelta(days=180)
    start_1m_str = start_1m.strftime("%Y-%m-%dT00:00:00Z")
    end_date_str_utc = end_date.strftime("%Y-%m-%dT23:59:59Z")
    
    # 3. Fetch Data
    daily_bars_raw = fetch_bars(tickers_list, start_daily_str, end_date_str_utc, "1Day")
    intraday_bars_raw = fetch_bars(tickers_list, start_1m_str, end_date_str_utc, "1Min")
    
    # 4. Process Daily Bars into historical_data format
    historical_daily = {}
    for sym, bars in daily_bars_raw.items():
        for i in range(1, len(bars)):
            prev_close = bars[i - 1]["c"]
            curr_close = bars[i]["c"]
            if prev_close > 0:
                date_str = bars[i]["t"][:10]
                if date_str not in historical_daily:
                    historical_daily[date_str] = {}
                historical_daily[date_str][sym] = {
                    "c": curr_close,
                    "daily_ret": (curr_close - prev_close) / prev_close,
                    "high": bars[i]["h"],
                    "low": bars[i]["l"],
                    "close": curr_close,
                    "volume": bars[i].get("v", 0)
                }
                
    daily_dates = sorted(list(historical_daily.keys()))
    
    # 5. Process Intraday Bars
    intraday_by_date = {}
    for sym, bars in intraday_bars_raw.items():
        df = pd.DataFrame(bars)
        if df.empty: continue
        df['date'] = df['t'].str[:10]
        for date_str, group in df.groupby('date'):
            if date_str not in intraday_by_date:
                intraday_by_date[date_str] = {}
            
            group = group.copy()
            group['pv'] = group['c'] * group['v']
            group['cum_pv'] = group['pv'].cumsum()
            group['cum_v'] = group['v'].cumsum()
            group['vwap'] = np.where(group['cum_v'] > 0, group['cum_pv'] / group['cum_v'], group['c'])
            
            intraday_by_date[date_str][sym] = group[['t', 'c', 'vwap']].to_dict(orient='records')

    intraday_dates = sorted(list(intraday_by_date.keys()))[-125:] # Get last 125 trading days (~6 months)
    
    # --- PRE-COMPUTE REGIMES ---
    regime_map = {}
    correlation_map = {}
    for date_str in intraday_dates:
        regime_map[date_str] = {}
        correlation_map[date_str] = {}
        
        date_idx = daily_dates.index(date_str) if date_str in daily_dates else -1
        if date_idx < 20: 
            continue
            
        window_dates = daily_dates[date_idx-20:date_idx]
        spy_window = [historical_daily[d].get("SPY", {}).get("daily_ret", 0.0) for d in window_dates]
        
        for sym_id, holdings in symphony_holdings.items():
            sym_window = []
            for d in window_dates:
                d_ret = 0.0
                valid_alloc = 0.0
                for h in holdings:
                    ticker = h.get("ticker")
                    alloc = h.get("weight", h.get("allocation", 0.0))
                    if ticker in historical_daily[d]:
                        d_ret += alloc * historical_daily[d][ticker].get("daily_ret", 0.0)
                        valid_alloc += alloc
                if valid_alloc > 0:
                    d_ret /= valid_alloc
                sym_window.append(d_ret)
                
            regime = regime_classifier.classify_regime(sym_window)
            correlation = math_engine.calculate_correlation(sym_window, spy_window)
            
            regime_map[date_str][sym_id] = regime or "unknown"
            correlation_map[date_str][sym_id] = correlation
    # --------------------------
    
    history_125d = {sym_id: {} for sym_id in symphony_holdings.keys()}
    
    def process_day(date_str):
        day_history = {}
        # Get historical_data UP TO the day before
        prev_dates = [d for d in daily_dates if d < date_str]
        if not prev_dates: return date_str, {}
        
        hist_data_up_to_yesterday = {d: historical_daily[d] for d in prev_dates}
        
        yesterday_date = prev_dates[-1]
        yesterday_closes = {sym: historical_daily[yesterday_date][sym]["c"] 
                          for sym in historical_daily[yesterday_date]}
                          
        spy_today = 0.0
        if "SPY" in historical_daily.get(date_str, {}):
            spy_today = historical_daily[date_str]["SPY"]["daily_ret"] * 100.0
            
        for sym_id, holdings in symphony_holdings.items():
            ticks = []
            
            proxy_etf = bot_state.get(sym_id, {}).get("proxy_etf", "SPY")
            ref_sym = holdings[0]["ticker"] if holdings else None
            
            anchor_sym = None
            if ref_sym and ref_sym in intraday_by_date.get(date_str, {}) and intraday_by_date[date_str][ref_sym]:
                anchor_sym = ref_sym
            elif proxy_etf in intraday_by_date.get(date_str, {}) and intraday_by_date[date_str][proxy_etf]:
                anchor_sym = proxy_etf
            elif "SPY" in intraday_by_date.get(date_str, {}) and intraday_by_date[date_str]["SPY"]:
                anchor_sym = "SPY"
            else:
                valid_tickers = [k for k, v in intraday_by_date.get(date_str, {}).items() if v]
                if valid_tickers:
                    anchor_sym = valid_tickers[0]
            
            if not anchor_sym:
                continue
                
            anchor_bars = intraday_by_date[date_str][anchor_sym]
            timestamps = [row['t'] for row in anchor_bars]
            
            # Synthesize or resolve intraday bars for each holding
            day_intraday = intraday_by_date.get(date_str, {})
            symphony_intraday = {}
            for h in holdings:
                ticker = h["ticker"]
                if ticker in day_intraday and day_intraday[ticker]:
                    symphony_intraday[ticker] = day_intraday[ticker]
                else:
                    # Mathematically construct 1-minute bars using proxy anchor trajectory
                    target_daily_ret = 0.0
                    if date_str in historical_daily and ticker in historical_daily[date_str]:
                        target_daily_ret = historical_daily[date_str][ticker].get("daily_ret", 0.0)
                    
                    y_close_target = yesterday_closes.get(ticker)
                    if y_close_target is None or y_close_target <= 0:
                        if date_str in historical_daily and ticker in historical_daily[date_str]:
                            c_today = historical_daily[date_str][ticker].get("c", 1.0)
                            y_close_target = c_today / (1.0 + target_daily_ret) if (1.0 + target_daily_ret) != 0 else c_today
                        else:
                            y_close_target = 1.0
                            
                    y_close_anchor = yesterday_closes.get(anchor_sym)
                    if y_close_anchor is None or y_close_anchor <= 0:
                        y_close_anchor = anchor_bars[0]['c'] if anchor_bars else 1.0
                        
                    anchor_daily_ret = (anchor_bars[-1]['c'] - y_close_anchor) / y_close_anchor if y_close_anchor > 0 else 0.0
                    
                    synth_bars = []
                    for idx, bar_a in enumerate(anchor_bars):
                        c_anchor_idx = bar_a['c']
                        if y_close_anchor > 0:
                            anchor_ret_idx = (c_anchor_idx - y_close_anchor) / y_close_anchor
                        else:
                            anchor_ret_idx = 0.0
                            
                        if abs(anchor_daily_ret) > 1e-7:
                            path_ratio = anchor_ret_idx / anchor_daily_ret
                        else:
                            path_ratio = idx / (len(anchor_bars) - 1) if len(anchor_bars) > 1 else 0.0
                            
                        synth_ret = path_ratio * target_daily_ret
                        synth_close = y_close_target * (1.0 + synth_ret)
                        
                        synth_bars.append({
                            't': bar_a['t'],
                            'c': synth_close,
                            'vwap': synth_close
                        })
                    symphony_intraday[ticker] = synth_bars
            
            vol = math_engine.calculate_20d_vol(holdings, hist_data_up_to_yesterday)
            base_atr = math_engine.calculate_14d_vwatr_pct(holdings, hist_data_up_to_yesterday)
            
            # Extract multiplier from strategy params
            ref_name = bot_state.get(sym_id, {}).get("name", "")
            strat_data = database.get_symphony_strategy(database.normalize_name(ref_name))
            vol_mult = strat_data.get("params", {}).get("VOLATILITY_MAGNITUDE_MULTIPLIER", 0.5)

            for i, ts in enumerate(timestamps):
                agg_ret = 0.0
                weighted_vwap_diff = 0.0
                valid_alloc = 0.0
                
                for h in holdings:
                    ticker = h.get("ticker")
                    alloc = h.get("weight", h.get("allocation", 0.0))
                    
                    if ticker in symphony_intraday and i < len(symphony_intraday[ticker]):
                        bar = symphony_intraday[ticker][i]
                        c = bar['c']
                        v = bar['vwap']
                        
                        y_close = yesterday_closes.get(ticker, c)
                        if y_close > 0:
                            ret = (c - y_close) / y_close
                            agg_ret += alloc * ret
                            
                        if v > 0:
                            weighted_vwap_diff += alloc * ((c - v) / v)
                        valid_alloc += alloc
                        
                # ENFORCE LIVE GATE: Only track VWAP difference if we have VWAP data for >50% of the portfolio
                if valid_alloc <= 0.5:
                    weighted_vwap_diff = 0.0

                # Reduce neighbor_k and paths for speed, 300 paths is fine for tuning approximation
                proxy_etf = bot_state.get(sym_id, {}).get("proxy_etf", "SPY")
                proxy_today = 0.0
                if proxy_etf in historical_daily.get(date_str, {}):
                    proxy_today = historical_daily[date_str][proxy_etf].get("daily_ret", 0.0) * 100.0
                    
                mc_prob, prob_loss_dynamic, dynamic_floor = math_engine.run_monte_carlo(
                    agg_ret * 100.0, holdings, hist_data_up_to_yesterday, spy_today, proxy_today, 
                    vol, proxy_etf=proxy_etf, simulation_paths=300, neighbor_k=25, 
                    volatility_multiplier=vol_mult
                )
                
                ticks.append({
                    "time": ts[11:16], 
                    "return": agg_ret * 100.0,
                    "mc_prob": mc_prob,
                    "prob_loss_dynamic": prob_loss_dynamic,
                    "dynamic_floor": dynamic_floor,
                    "vol": vol,
                    "vwap_diff": weighted_vwap_diff,
                    "base_atr_pct": base_atr,
                    "effective_regime": regime_map.get(date_str, {}).get(sym_id, "unknown"),
                    "regime_correlation": correlation_map.get(date_str, {}).get(sym_id, "Low")
                })
                
            day_history[sym_id] = ticks
            
        return date_str, day_history

    print(f"  -> [TELEMETRY] Processing synthetic ticks for {len(intraday_dates)} days...", flush=True)
    print(f"  -> Simulating {len(intraday_dates)} days of Intraday Tick Data using Parallel Processing...", flush=True)
    results = Parallel(n_jobs=-1)(delayed(process_day)(d) for d in intraday_dates)
    
    for date_str, day_history in results:
        for sym_id, ticks in day_history.items():
            if ticks:
                history_125d[sym_id][date_str] = ticks
                
    try:
        with open(cache_file, "w") as f:
            json.dump(history_125d, f)
    except Exception as e:
        print(f"  -> Failed to write cache file: {e}", flush=True)
                
    return history_125d

def generate_cpcv_blocks(sorted_dates, num_blocks=5, purge_buffer_days=1):
    """
    Divides chronological dates into blocks and applies a purge buffer at the boundaries.
    """
    n = len(sorted_dates)
    block_size = n // num_blocks
    blocks = []
    
    for i in range(num_blocks):
        start_idx = i * block_size
        end_idx = (i + 1) * block_size if i < num_blocks - 1 else n
        block_dates = sorted_dates[start_idx:end_idx]
        
        if len(block_dates) > purge_buffer_days * 2:
            if i > 0:
                block_dates = block_dates[purge_buffer_days:]
            if i < num_blocks - 1:
                block_dates = block_dates[:-purge_buffer_days]
                
        blocks.append(block_dates)
        
    return blocks

def generate_cpcv_paths(blocks, n_train=3):
    """
    Generates combinatorial paths of training and testing sets.
    """
    import itertools
    num_blocks = len(blocks)
    block_indices = list(range(num_blocks))
    
    paths = []
    n_test = num_blocks - n_train
    for test_idx in itertools.combinations(block_indices, n_test):
        train_idx = [i for i in block_indices if i not in test_idx]
        
        train_dates = []
        for idx in train_idx:
            train_dates.extend(blocks[idx])
            
        test_dates = []
        for idx in test_idx:
            test_dates.extend(blocks[idx])
            
        train_dates.sort()
        test_dates.sort()
        paths.append((train_dates, test_dates))
        
    return paths
