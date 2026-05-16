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
BREAKEVEN_VOL_MIN = 0.4
BREAKEVEN_VOL_MAX = 3.0
BREAKEVEN_ACTIVATION_BUFFER = 0.2

# VWAP Bleed Bounds
VWAP_BLEED_FLOOR = -3.0
VWAP_BLEED_CEILING = -0.5

def run_monte_carlo(current_symphony_return, holdings, historical_data, spy_today_return, symphony_vol, simulation_paths=5000, neighbor_k=150, volatility_multiplier=0.5):
    """
    Vectorized Monte Carlo simulation using Nearest Neighbors matching.
    Includes an unconditional bootstrap fallback.
    """
    np.random.seed(42)
    valid_dates = sorted(list(historical_data.keys()))
    if len(valid_dates) < VOLATILITY_WINDOW_DAYS:
        return 100.0, 0.0, 0.0

    tickers = [h["ticker"] for h in holdings]
    weights = np.array([h.get("allocation", 0.0) for h in holdings])

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

    # 1. Calculate distances based on SPY return and rolling 20-day volatility
    spy_returns = np.array([historical_data[date].get("SPY", {}).get("daily_ret", 0.0) for date in valid_dates])
    
    spy_vols = np.zeros_like(spy_returns)
    for i in range(len(spy_returns)):
        start_idx = max(0, i - VOLATILITY_INDEX_OFFSET)
        if i > 0:
            spy_vols[i] = np.std(spy_returns[start_idx:i+1])
        else:
            spy_vols[i] = 0.0
            
    spy_today_ret_dec = spy_today_return / PERCENT_CONVERSION
    if len(spy_returns) >= VOLATILITY_INDEX_OFFSET:
        today_vol = np.std(np.append(spy_returns[-VOLATILITY_INDEX_OFFSET:], spy_today_ret_dec))
    else:
        today_vol = np.std(np.append(spy_returns, spy_today_ret_dec))

    # Euclidean distance across 2 dimensions
    distances = np.sqrt((spy_returns - spy_today_ret_dec)**2 + (spy_vols - today_vol)**2)
    
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
    weights = np.array([h.get("allocation", 0.0) for h in holdings])
    
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
    weights = np.array([h.get("allocation", 0.0) for h in holdings])
    
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

def check_parabolic_velocity(current_return, prev_return, threshold):
    return (current_return - prev_return) >= threshold

def calculate_time_decay_multipliers(time_ratio, mult_open=1.5, mult_close=0.5, min_stop_open=0.3, min_stop_close=0.15):
    decay = math.log10(DECAY_LOG_BASE + DECAY_LOG_MULTIPLIER * time_ratio)
    dynamic_multiplier = mult_open - (mult_open - mult_close) * decay
    dynamic_min_stop = min_stop_open - (min_stop_open - min_stop_close) * decay
    return dynamic_multiplier, dynamic_min_stop

def calculate_active_stop_distance(safe_vol, dynamic_multiplier, dynamic_min_stop, is_squeezed, max_para_squeeze):
    distance = max((safe_vol * dynamic_multiplier), dynamic_min_stop)
    if is_squeezed:
        distance *= max_para_squeeze
    return float(distance)

def check_breakeven_activation(current_return, symphony_vol):
    dynamic_activation = max(BREAKEVEN_VOL_MIN, min(BREAKEVEN_VOL_MAX, symphony_vol))
    return current_return >= (dynamic_activation - BREAKEVEN_ACTIVATION_BUFFER)

def calculate_vwap_bleed_threshold(symphony_vol, bleed_multiplier):
    return max(VWAP_BLEED_FLOOR, min(VWAP_BLEED_CEILING, -(symphony_vol * bleed_multiplier)))
