import numpy as np
import math

# --- SYSTEM CONSTANTS ---
PERCENT_CONVERSION = 100.0

# Windows & Lookbacks
VOLATILITY_WINDOW_DAYS = 20
VOLATILITY_INDEX_OFFSET = 19
ATR_WINDOW_DAYS = 15

# Monte Carlo Safeguards
MIN_SAFE_VOL_FLOOR = 0.5

# Time Decay Curve ( math.log10(BASE + MULTIPLIER * time_ratio) )
DECAY_LOG_BASE = 1.0
DECAY_LOG_MULTIPLIER = 9.0

# Breakeven Bounds
BREAKEVEN_ACTIVATION_BUFFER = 0.2

# VWAP Bleed Bounds
VWAP_BLEED_FLOOR = -3.0
VWAP_BLEED_CEILING = -0.5

def run_monte_carlo(current_symphony_return, holdings, historical_data, spy_today_return, proxy_today_return, symphony_vol, proxy_etf="SPY", simulation_paths=5000, neighbor_k=150, volatility_multiplier=0.5):
    """
    Vectorized Monte Carlo simulation using Sector-Conditioned Nearest Neighbors matching (3D).
    Includes an unconditional bootstrap fallback.
    """
    if not proxy_etf:
        proxy_etf = "SPY"
    np.random.seed(42)
    valid_dates = sorted(list(historical_data.keys()))
    if len(valid_dates) < VOLATILITY_WINDOW_DAYS:
        return 100.0, 0.0, 0.0

    tickers = [h["ticker"] for h in holdings]
    weights = np.array([h.get("weight", h.get("allocation", 0.0)) for h in holdings])

    # Step 1: Pre-compute portfolio returns for ALL valid dates
    all_returns_matrix = np.zeros((len(valid_dates), len(tickers)))
    for i, date in enumerate(valid_dates):
        day_data = historical_data[date]
        spy_ret = day_data.get("SPY", {}).get("daily_ret", 0.0)
        for j, ticker in enumerate(tickers):
            if ticker in day_data:
                all_returns_matrix[i, j] = day_data[ticker].get("daily_ret", 0.0)
            else:
                all_returns_matrix[i, j] = spy_ret
                
    all_day_returns = all_returns_matrix.dot(weights) * PERCENT_CONVERSION

    # Step 2: Calculate unconditional distribution
    unconditional_returns = np.random.choice(all_day_returns, size=simulation_paths, replace=True)
    unconditional_returns.sort()
    
    below_count_unc = np.searchsorted(unconditional_returns, current_symphony_return)
    unconditional_prob_beating = ((simulation_paths - below_count_unc) / simulation_paths) * PERCENT_CONVERSION
    
    # Dynamic Floor Edge Case: Enforce a minimum safe volatility so floor doesn't collapse to 0
    safe_vol_for_floor = max(symphony_vol, MIN_SAFE_VOL_FLOOR)
    dynamic_floor = current_symphony_return - (safe_vol_for_floor * volatility_multiplier)
    below_floor_count_unc = np.searchsorted(unconditional_returns, dynamic_floor)
    unconditional_prob_loss_dynamic = (below_floor_count_unc / simulation_paths) * PERCENT_CONVERSION

    # Step 3: Safety check for SPY data
    if spy_today_return is None or not isinstance(spy_today_return, (int, float)) or np.isnan(spy_today_return):
        return unconditional_prob_beating, unconditional_prob_loss_dynamic, dynamic_floor

    # 1. Calculate distances based on SPY return, SectorProxy return, and rolling 20-day volatility
    spy_returns = np.array([historical_data[date].get("SPY", {}).get("daily_ret", 0.0) for date in valid_dates])
    proxy_returns = np.array([historical_data[date].get(proxy_etf, {}).get("daily_ret", 0.0) for date in valid_dates])
    
    spy_vols = np.zeros_like(spy_returns)
    for i in range(len(spy_returns)):
        start_idx = max(0, i - VOLATILITY_INDEX_OFFSET)
        if i > 0:
            spy_vols[i] = np.std(spy_returns[start_idx:i+1])
        else:
            spy_vols[i] = 0.0
            
    spy_today_ret_dec = spy_today_return / PERCENT_CONVERSION
    if proxy_today_return is None or not isinstance(proxy_today_return, (int, float)) or np.isnan(proxy_today_return):
        proxy_today_ret_dec = 0.0
    else:
        proxy_today_ret_dec = proxy_today_return / PERCENT_CONVERSION

    if len(spy_returns) >= VOLATILITY_INDEX_OFFSET:
        today_vol = np.std(np.append(spy_returns[-VOLATILITY_INDEX_OFFSET:], spy_today_ret_dec))
    else:
        today_vol = np.std(np.append(spy_returns, spy_today_ret_dec))

    # Euclidean distance across 3 dimensions
    distances = np.sqrt(
        1.0 * ((spy_returns - spy_today_ret_dec)**2) + 
        1.0 * ((proxy_returns - proxy_today_ret_dec)**2) + 
        1.0 * ((spy_vols - today_vol)**2)
    )
    
    # 2. Get top K indices
    if len(distances) <= neighbor_k:
        nearest_indices = np.arange(len(distances))
    else:
        # argpartition is faster than full sort
        nearest_indices = np.argpartition(distances, neighbor_k)[:neighbor_k]
    
    if len(nearest_indices) < VOLATILITY_WINDOW_DAYS:
        return unconditional_prob_beating, unconditional_prob_loss_dynamic, dynamic_floor
    
    # 4. Calculate path returns using the pre-computed array directly
    nearest_day_returns = all_day_returns[nearest_indices]
    
    # 5. Random selection & Cumulative Distribution
    sim_results = np.random.choice(nearest_day_returns, size=simulation_paths)
    
    sim_results.sort()
    below_count = np.searchsorted(sim_results, current_symphony_return)
    prob_beating = ((simulation_paths - below_count) / simulation_paths) * PERCENT_CONVERSION
    
    below_floor_count = np.searchsorted(sim_results, dynamic_floor)
    prob_loss_dynamic = (below_floor_count / simulation_paths) * PERCENT_CONVERSION
    
    return prob_beating, prob_loss_dynamic, dynamic_floor

def calculate_20d_vol(holdings, historical_data):
    """
    Calculates the 20-day historical volatility of the given holdings based on historical_data.
    Vectorized for performance.
    """
    valid_dates = sorted(list(historical_data.keys()))[-VOLATILITY_WINDOW_DAYS:]
    if len(valid_dates) < VOLATILITY_WINDOW_DAYS:
        return 0.0

    tickers = [h.get("ticker") for h in holdings]
    weights = np.array([h.get("weight", h.get("allocation", 0.0)) for h in holdings])
    
    returns_matrix = np.zeros((len(valid_dates), len(tickers)))
    
    for i, date in enumerate(valid_dates):
        day_data = historical_data[date]
        spy_ret = day_data.get("SPY", {}).get("daily_ret", 0.0)
        for j, ticker in enumerate(tickers):
            if ticker in day_data:
                returns_matrix[i, j] = day_data[ticker].get("daily_ret", 0.0)
            else:
                returns_matrix[i, j] = spy_ret

    daily_returns = returns_matrix.dot(weights) * PERCENT_CONVERSION

    if len(daily_returns) == 0:
        return 0.0

    return float(np.std(daily_returns))

def calculate_14d_atr_pct(holdings, historical_data):
    """
    Calculates the 14-day Volatility-Adjusted (ATR) percentage for the holdings.
    Falls back to calculate_20d_vol if high/low data is missing.
    """
    valid_dates = sorted(list(historical_data.keys()))[-ATR_WINDOW_DAYS:]
    if len(valid_dates) < ATR_WINDOW_DAYS:
        return calculate_20d_vol(holdings, historical_data)

    tickers = [h.get("ticker") for h in holdings]
    weights = np.array([h.get("weight", h.get("allocation", 0.0)) for h in holdings])
    
    atr_pct_array = np.zeros(len(tickers))
    
    for j, ticker in enumerate(tickers):
        tr_list = []
        last_close = None
        has_missing_data = False
        
        for date in valid_dates:
            day_data = historical_data[date].get(ticker)
            if not day_data or "high" not in day_data or "low" not in day_data or "close" not in day_data:
                has_missing_data = True
                break
                
            high = day_data["high"]
            low = day_data["low"]
            close = day_data["close"]
            
            if last_close is not None:
                tr = max(high - low, abs(high - last_close), abs(low - last_close))
                tr_list.append(tr)
            last_close = close
            
        if has_missing_data or len(tr_list) == 0:
            return calculate_20d_vol(holdings, historical_data)
            
        avg_tr = np.mean(tr_list)
        recent_close = last_close
        if recent_close and recent_close > 0:
            atr_pct_array[j] = (avg_tr / recent_close) * PERCENT_CONVERSION
        else:
            return calculate_20d_vol(holdings, historical_data)
            
    portfolio_atr_pct = atr_pct_array.dot(weights)
    return float(portfolio_atr_pct)

def calculate_14d_vwatr_pct(holdings, historical_data):
    """
    Calculates the 14-day Volume-Weighted ATR (VW-ATR) percentage for the holdings.
    Falls back to calculate_20d_vol if data is missing.
    """
    all_dates = sorted(list(historical_data.keys()))
    if len(all_dates) < ATR_WINDOW_DAYS + 20:
        return calculate_20d_vol(holdings, historical_data)

    valid_dates = all_dates[-ATR_WINDOW_DAYS:]

    tickers = [h.get("ticker") for h in holdings]
    weights = np.array([h.get("weight", h.get("allocation", 0.0)) for h in holdings])
    
    vwatr_pct_array = np.zeros(len(tickers))
    
    for j, ticker in enumerate(tickers):
        vwtr_list = []
        last_close = None
        has_missing_data = False
        
        for date in valid_dates:
            date_idx = all_dates.index(date)
            # Need 20 previous days INCLUDING the current date (as per typical SMA for RVol)
            sma_dates = all_dates[date_idx - 19 : date_idx + 1]
            if len(sma_dates) < 20:
                has_missing_data = True
                break
                
            vol_sum = 0
            for d in sma_dates:
                day_data = historical_data[d].get(ticker)
                if not day_data or "volume" not in day_data:
                    has_missing_data = True
                    break
                vol_sum += day_data["volume"]
                
            if has_missing_data:
                break
                
            sma_vol = vol_sum / 20.0
            
            day_data = historical_data[date].get(ticker)
            if not day_data or "high" not in day_data or "low" not in day_data or "close" not in day_data or "volume" not in day_data:
                has_missing_data = True
                break
                
            today_vol = day_data["volume"]
            rvol = (today_vol / sma_vol) if sma_vol > 0 else 1.0
            
            high = day_data["high"]
            low = day_data["low"]
            close = day_data["close"]
            
            if last_close is not None:
                tr = max(high - low, abs(high - last_close), abs(low - last_close))
                vwtr = tr * rvol
                vwtr_list.append(vwtr)
            last_close = close
            
        if has_missing_data or len(vwtr_list) == 0:
            return calculate_20d_vol(holdings, historical_data)
            
        avg_vwtr = np.mean(vwtr_list)
        recent_close = last_close
        if recent_close and recent_close > 0:
            vwatr_pct_array[j] = (avg_vwtr / recent_close) * PERCENT_CONVERSION
        else:
            return calculate_20d_vol(holdings, historical_data)
            
    portfolio_vwatr_pct = vwatr_pct_array.dot(weights)
    return float(portfolio_vwatr_pct)

def check_parabolic_velocity(current_return, prev_return, threshold):
    return (current_return - prev_return) >= threshold

def calculate_time_decay_multipliers(time_ratio, mult_open=1.5, mult_close=0.5, min_stop_open=0.3, min_stop_close=0.15):
    # Enforce logarithmic acceleration curve: width tightens slowly early, then accelerates near close
    decay = math.log10(1.0 + 9.0 * time_ratio)
    dynamic_multiplier = mult_open - (mult_open - mult_close) * decay
    dynamic_min_stop = min_stop_open - (min_stop_open - min_stop_close) * decay
    return float(dynamic_multiplier), float(dynamic_min_stop)

def calculate_active_stop_distance(safe_vol, dynamic_multiplier, dynamic_min_stop, is_squeezed, max_para_squeeze):
    distance = max((safe_vol * dynamic_multiplier), dynamic_min_stop)
    if is_squeezed:
        distance *= max_para_squeeze
    return float(distance)

def check_breakeven_activation(current_return, symphony_vol):
    dynamic_activation = max(0.4, min(3.0, symphony_vol))
    return current_return >= (dynamic_activation - BREAKEVEN_ACTIVATION_BUFFER)



def calculate_current_rvol(holdings, historical_data):
    all_dates = sorted(list(historical_data.keys()))
    if len(all_dates) < 20:
        return 1.0
        
    tickers = [h.get("ticker") for h in holdings]
    weights = np.array([h.get("weight", h.get("allocation", 0.0)) for h in holdings])
    
    rvol_array = np.zeros(len(tickers))
    
    for j, ticker in enumerate(tickers):
        date = all_dates[-1]
        date_idx = all_dates.index(date)
        sma_dates = all_dates[date_idx - 19 : date_idx + 1]
        if len(sma_dates) < 20:
            rvol_array[j] = 1.0
            continue
            
        vol_sum = 0
        for d in sma_dates:
            day_data = historical_data[d].get(ticker)
            if not day_data or "volume" not in day_data:
                vol_sum = -1
                break
            vol_sum += day_data["volume"]
            
        if vol_sum < 0:
            rvol_array[j] = 1.0
            continue
            
        sma_vol = vol_sum / 20.0
        day_data = historical_data[date].get(ticker)
        today_vol = day_data["volume"]
        rvol_array[j] = (today_vol / sma_vol) if sma_vol > 0 else 1.0
        
    portfolio_rvol = rvol_array.dot(weights)
    return float(portfolio_rvol)

