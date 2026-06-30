import time
import math
import optuna
from datetime import datetime, timedelta
import database
import synthetic_history
import glob
import json
import math_engine
import numpy as np

optuna.logging.set_verbosity(optuna.logging.WARNING)

def calculate_historical_deviation(current_date_str):
    """
    Scans local directory for post_mortem_*.json from the last 45 calendar days.
    Calculates average deviation (exit_return - attempted_trigger_level) grouped by exit_reason.
    """
    deviation_dict = {
        "Take-Profit": 0.0,
        "Trailing Stop": -0.20,
        "VWAP Breakdown": -0.40,
        "VWAP Bleed Cut": -0.25
    }
    
    deviation_sums = {k: 0.0 for k in deviation_dict.keys()}
    deviation_counts = {k: 0 for k in deviation_dict.keys()}

    try:
        current_dt = datetime.strptime(current_date_str, "%Y-%m-%d")
        lookback_dt = current_dt - timedelta(days=45)

        files = glob.glob("post_mortem_*.json")
        for f_path in files:
            try:
                # Extract date from filename: post_mortem_YYYY-MM-DD.json
                date_part = f_path.replace("post_mortem_", "").replace(".json", "")
                file_dt = datetime.strptime(date_part, "%Y-%m-%d")
                if file_dt < lookback_dt or file_dt >= current_dt:
                    continue

                with open(f_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    triggers = data.get("triggers", [])
                    for t in triggers:
                        reason = t.get("exit_reason")
                        exit_ret = t.get("exit_return")
                        attempted = t.get("attempted_trigger_level")

                        if reason in deviation_sums and exit_ret is not None and attempted is not None:
                            deviation_sums[reason] += (exit_ret - attempted)
                            deviation_counts[reason] += 1
            except:
                continue

        for k in deviation_dict.keys():
            if deviation_counts[k] > 0:
                avg_dev = deviation_sums[k] / deviation_counts[k]
                if avg_dev > 0.0:
                    print(f"  -> WARNING: Positive slippage detected for {k} ({avg_dev:.3f}). Capping at 0.0 for conservative backtesting.", flush=True)
                    avg_dev = 0.0
                deviation_dict[k] = round(avg_dev, 3)
    except Exception as e:
        print(f"      -> Warning: Deviation calculation failed ({e}). Using defaults.", flush=True)

    print(f"  -> Historical Execution Deviation Penalties: {deviation_dict}", flush=True)
    return deviation_dict

def run_autotuner(bot_state, current_date_str, account_uuids, is_forced=False):
    """
    Runs a 6-month walk-forward optimization to find the best variables using Bayesian Optimization per account.
    Implements True Walk-Forward Analysis (80% train, 20% OOS test).
    """
    print(f"  -> Starting EOD Autotune (125-day WFA: 80% Train / 20% OOS per Symphony)...", flush=True)

    # 0. Calculate Historical Execution Deviation
    deviation_dict = calculate_historical_deviation(current_date_str)

    # 1. Archive today's charts to the permanent DB
    # chart_history = database.load_chart_history()
    # if chart_history and chart_history.get("date") == current_date_str:
    #     for sym_id, data in chart_history.get("symphonies", {}).items():
    #         database.save_chart_archive(current_date_str, sym_id, data)

    # 2. Fetch the rolling 125-trading-day synthetic forward-looking data
    history_125d = synthetic_history.generate_synthetic_history(bot_state, current_date_str)
    if not history_125d:
        print("  -> Autotuner aborted: Failed to generate synthetic history.", flush=True)
        return

    # Extract global dates and partition 80/20
    all_dates = set()
    for sym_data in history_125d.values():
        all_dates.update(sym_data.keys())
    sorted_dates = sorted(list(all_dates))
    
    total_days = len(sorted_dates)
    if total_days < 2:
        print("  -> Autotuner aborted: Need at least 2 days of history for WFA.", flush=True)
        return

    # CPCV Setup: 5 blocks, 3 train -> 10 paths
    blocks = synthetic_history.generate_cpcv_blocks(sorted_dates, num_blocks=5, purge_buffer_days=1)
    cpcv_paths = synthetic_history.generate_cpcv_paths(blocks, n_train=3)

    # Extract unique normalized symphony names from the current bot_state for enabled accounts only
    symphony_names = set()
    for sym_id, data in bot_state.items():
        if isinstance(data, dict) and "name" in data and "account" in data:
            if data["account"] in account_uuids:
                symphony_names.add(database.normalize_name(data["name"]))

    optimization_results = {}

    for normalized_name in symphony_names:
        print(f"     Optimizing Symphony: {normalized_name}", flush=True)
        strat_data = database.get_symphony_strategy(normalized_name)
        locked_vars = strat_data.get("locked_vars", [])
        current_params = strat_data.get("params", {})
        original_params = current_params.copy()

        # Helper to run the simulation
        def run_simulation(p, history_data, acc_sym_ids, current_date_str, deviation_dict):
            total_guard_alpha = 0.0
            decay_rate = 0.015
            current_dt = datetime.strptime(current_date_str, "%Y-%m-%d")

            for sym_id in acc_sym_ids:
                dates_data = history_data.get(sym_id, {})
                for date, ticks in dates_data.items():
                    if not ticks: continue

                    hwm = -999.0
                    highest_stop_level = -999.0
                    armed = False
                    tp_armed = False
                    vwap_ticks = 0
                    para_armed = False
                    breakeven_locked = False
                    prev_return = None
                    hwm_hold_ticks = 0
                    below_stop_count = 0
                    above_tp_count = 0
                    mc_history = []
                    lowest_mc_seen = 100.0
                    lock_engaged_ticks = 0

                    triggered_return = None
                    eod_return = ticks[-1]["return"]
                    day_max_return = max(t.get("return", 0.0) for t in ticks)

                    for tick_idx, tick in enumerate(ticks):
                        ret = tick.get("return", 0.0)
                        mc = tick.get("mc_prob", 50.0)
                        lowest_mc_seen = min(lowest_mc_seen, mc)
                        prob_loss_dynamic = tick.get("prob_loss_dynamic", 0.0)
                        vol = tick.get("vol", 1.0)
                        vwap_diff = tick.get("vwap_diff", 0.0)
                        base_atr_pct = tick.get("base_atr_pct", vol)

                        if ret > hwm: hwm = ret
                        safe_hwm = max(hwm, ret)
                        
                        # --- PARABOLIC SQUEEZE LOGIC ---
                        if prev_return is None:
                            prev_return = ret
                            velocity = 0.0
                            is_para = False
                        else:
                            velocity = ret - prev_return
                            is_para = math_engine.check_parabolic_velocity(ret, prev_return, p.get("PARABOLIC_VELOCITY_THRESHOLD", 2.0))
                            prev_return = ret
                        if is_para:
                            para_armed = True
                        # ------------------------------

                        if not armed:
                            # Must meet MC threshold AND downside magnitude risk
                            if p.get("TAKE_PROFIT_MC_PCT", 5.0) <= mc < p.get("TRIGGER_THRESHOLD_PCT", 15.0) and prob_loss_dynamic >= 25.0: 
                                armed = True
                        else:
                            if mc > (p.get("TRIGGER_THRESHOLD_PCT", 15.0) * 2) and ret > 0.0:
                                armed = False
                                below_stop_count = 0

                        mc_history.append(mc)
                        if len(mc_history) > 5: mc_history.pop(0)

                        # --- TIME SQUEEZE DECAY LOGIC ---
                        # Assuming ticks are minute bars (9:30-16:00 = 390 mins)
                        time_ratio = min(1.0, max(0.0, tick_idx / 390.0))
                        
                        m_open = p.get("VOLATILITY_MAGNITUDE_MULTIPLIER", 1.5)
                        m_close = p.get("VOLATILITY_CLOSE_MULTIPLIER", 0.5)
                        
                        dynamic_multiplier, dynamic_min_stop = math_engine.calculate_time_decay_multipliers(
                            time_ratio,
                            mult_open=m_open,
                            mult_close=m_close,
                            min_stop_open=0.3,
                            min_stop_close=0.15
                        )

                        effective_regime = tick.get("effective_regime", "unknown")
                        regime_correlation = tick.get("regime_correlation", "Low")
                        
                        safe_vol = base_atr_pct if base_atr_pct > 0 else (vol if vol > 0 else 1.0)
                        is_squeezed = para_armed or breakeven_locked

                        active_stop_dist = math_engine.calculate_active_stop_distance(
                            safe_vol, dynamic_multiplier, dynamic_min_stop, is_squeezed, 
                            p.get("MAX_PARABOLIC_SQUEEZE", 0.50), effective_regime, regime_correlation
                        )

                        base_stop = safe_hwm - active_stop_dist
                        
                        # --- RISK GUARD LOGIC ---
                        if math_engine.check_breakeven_activation(ret, vol):
                            hwm_hold_ticks += 1
                        else:
                            hwm_hold_ticks = 0
                        
                        if hwm_hold_ticks >= 5:
                            breakeven_locked = True
                        
                        if breakeven_locked:
                            lock_engaged_ticks += 1
                        else:
                            lock_engaged_ticks = 0
                        
                        stop_level = max(base_stop, 0.0) if breakeven_locked else base_stop
                        highest_stop_level = max(stop_level, highest_stop_level)
                        stop_level = highest_stop_level
                        # ------------------------

                        is_trailing_hit = False
                        is_breakeven_hit = False
                        exit_reason_trailing = "Trailing Stop"
                        
                        is_magnitude_breached = ret <= (stop_level - 0.10)

                        if armed:
                            if is_magnitude_breached and mc < 60.0:
                                below_stop_count += 1
                                if below_stop_count >= 3:
                                    is_trailing_hit = True
                                    exit_reason_trailing = "Trailing Stop"
                            else:
                                below_stop_count = 0
                                
                        if breakeven_locked and not is_trailing_hit:
                            if is_magnitude_breached:
                                be_path_a = mc < 60.0
                                # 1 tick = 1 minute in the simulation
                                be_path_b = (lock_engaged_ticks >= 60 and lowest_mc_seen >= 60.0) 
                                
                                if be_path_a or be_path_b:
                                    below_lock_count = bot_state.get("dummy", 0) + 1 # Use local counter
                                    if 'below_lock_sim' not in locals(): below_lock_sim = 0
                                    below_lock_sim += 1
                                    
                                    if below_lock_sim >= 3:
                                        is_breakeven_hit = True
                                        exit_reason_trailing = "Breakeven Path B (MC-Stuck)" if be_path_b else "Breakeven Path A"
                                else:
                                    below_lock_sim = 0
                            else:
                                below_lock_sim = 0

                        is_tp_hit = False
                        if mc < p.get("TAKE_PROFIT_MC_PCT", 5.0):
                            if not tp_armed:
                                tp_armed = True
                                above_tp_count = 0
                        elif tp_armed:
                            if mc >= p.get("TAKE_PROFIT_MC_PCT", 5.0):
                                above_tp_count += 1
                                if above_tp_count >= 2:
                                    is_tp_hit = True
                            else: above_tp_count = 0

                        current_vwap_diff_pct = vwap_diff * 100.0
                        vwap_buffer_pct = -vol * p.get("VWAP_BAND_MULTIPLIER", 0.10)

                        effective_regime = tick.get("effective_regime", "unknown")
                        is_vwap_broken, vwap_ticks = math_engine.evaluate_vwap_breakdown(
                            current_vwap_diff_pct, vwap_buffer_pct, safe_hwm, ret, 
                            p.get("VWAP_CROSS_HWM_PCT", 1.0), vwap_ticks, effective_regime,
                            strategy_params=p
                        )

                        if is_trailing_hit or is_breakeven_hit or is_tp_hit or is_vwap_broken:
                            reason_str = exit_reason_trailing
                            if is_tp_hit: 
                                reason_str = "Take-Profit"
                            elif is_vwap_broken:
                                vol_base = abs(vwap_buffer_pct) if vwap_buffer_pct != 0 else 0.15
                                is_bleed = current_vwap_diff_pct < -(vol_base * p.get("VWAP_BLEED_MULTIPLIER", 1.5))
                                reason_str = "VWAP Bleed Cut" if is_bleed else "VWAP Breakdown"
                            
                            penalty = deviation_dict.get(reason_str, -0.20)
                            triggered_return = ret + penalty
                            break

                    if triggered_return is not None:
                        guard_alpha = triggered_return - eod_return
                        missed_upside = day_max_return - triggered_return
                        drawdown_from_peak = safe_hwm - triggered_return
                        
                        # Exponential Time-Decay Weighting
                        days_ago = (current_dt - datetime.strptime(date, "%Y-%m-%d")).days
                        weight = math.exp(-decay_rate * days_ago)

                        # 1. Penalize Missed Upside (Exiting too early before a run)
                        if missed_upside > 1.0: # Only penalize if we missed out on more than 1%
                            total_guard_alpha -= (missed_upside * 1.5 * weight)

                        # 2. NEW: Penalize Peak-to-Exit Drawdown (Giving back too much profit)
                        # If we reached at least a 1% gain, penalize giving back more than 1.5% of it
                        if safe_hwm > 1.0 and drawdown_from_peak > 1.5:
                            total_guard_alpha -= (drawdown_from_peak * 0.75 * weight)

                        # 3. Apply standard EOD-based guard alpha
                        if guard_alpha < 0:
                            total_guard_alpha += (guard_alpha * 2.0 * weight)
                        else:
                            total_guard_alpha += (guard_alpha * weight)

                        # 4. Profit/Drawdown Prioritization
                        safe_drawdown = max(drawdown_from_peak, 0.01)
                        pd_ratio = triggered_return / safe_drawdown
                            
                        # Add scaled profit/drawdown ratio to objective
                        total_guard_alpha += (pd_ratio * 0.5 * weight)

            return -total_guard_alpha

        def objective(trial):
            p = current_params.copy()
            if "TRIGGER_THRESHOLD_PCT" not in locked_vars:
                p["TRIGGER_THRESHOLD_PCT"] = trial.suggest_float("TRIGGER_THRESHOLD_PCT", 5.0, 25.0)
            if "TAKE_PROFIT_MC_PCT" not in locked_vars:
                p["TAKE_PROFIT_MC_PCT"] = trial.suggest_float("TAKE_PROFIT_MC_PCT", 2.0, 10.0)
            if "VWAP_CROSS_HWM_PCT" not in locked_vars:
                p["VWAP_CROSS_HWM_PCT"] = trial.suggest_float("VWAP_CROSS_HWM_PCT", 0.5, 2.5)
            if "VWAP_BAND_MULTIPLIER" not in locked_vars:
                p["VWAP_BAND_MULTIPLIER"] = trial.suggest_float("VWAP_BAND_MULTIPLIER", 0.02, 0.40)
            if "VOLATILITY_MAGNITUDE_MULTIPLIER" not in locked_vars:
                p["VOLATILITY_MAGNITUDE_MULTIPLIER"] = trial.suggest_float("VOLATILITY_MAGNITUDE_MULTIPLIER", 0.5, 2.5, step=0.1)
            if "VOLATILITY_CLOSE_MULTIPLIER" not in locked_vars:
                p["VOLATILITY_CLOSE_MULTIPLIER"] = trial.suggest_float("VOLATILITY_CLOSE_MULTIPLIER", 0.1, 1.0, step=0.1)
            if "PARABOLIC_VELOCITY_THRESHOLD" not in locked_vars:
                p["PARABOLIC_VELOCITY_THRESHOLD"] = trial.suggest_float("PARABOLIC_VELOCITY_THRESHOLD", 1.5, 5.0)
            if "MAX_PARABOLIC_SQUEEZE" not in locked_vars:
                p["MAX_PARABOLIC_SQUEEZE"] = trial.suggest_float("MAX_PARABOLIC_SQUEEZE", 0.25, 0.80)
            if "VWAP_BLEED_MULTIPLIER" not in locked_vars:
                p["VWAP_BLEED_MULTIPLIER"] = trial.suggest_float("VWAP_BLEED_MULTIPLIER", 0.5, 3.0, step=0.1)
            if "VWAP_BLEED_TICKS" not in locked_vars:
                p["VWAP_BLEED_TICKS"] = trial.suggest_int("VWAP_BLEED_TICKS", 3, 30)

            acc_sym_ids = [k for k, v in bot_state.items() if isinstance(v, dict) and database.normalize_name(v.get("name", "")) == normalized_name]
            if not acc_sym_ids: return 0.0
            target_sym_id = acc_sym_ids[0]
            
            path_is_returns = []
            path_oos_returns = []
            
            for train_dates_list, test_dates_list in cpcv_paths:
                train_dates_set = set(train_dates_list)
                test_dates_set = set(test_dates_list)
                
                history_train_path = {sym: {d: ticks for d, ticks in data.items() if d in train_dates_set} for sym, data in history_125d.items()}
                history_test_path = {sym: {d: ticks for d, ticks in data.items() if d in test_dates_set} for sym, data in history_125d.items()}
                
                alpha_is = -run_simulation(p, history_train_path, [target_sym_id], current_date_str, deviation_dict)
                alpha_oos = -run_simulation(p, history_test_path, [target_sym_id], current_date_str, deviation_dict)
                
                path_is_returns.append(alpha_is)
                path_oos_returns.append(alpha_oos)
                
            trial.set_user_attr("is_path_returns", path_is_returns)
            trial.set_user_attr("oos_path_returns", path_oos_returns)
            
            # Calculate CRRA Expected Utility across all OOS paths
            # Using 25th percentile of the utilities for conservatism
            path_crra_oos = [math_engine.calculate_crra_utility([ret]) for ret in path_oos_returns]
            return float(np.percentile(path_crra_oos, 25))

        start_time = time.time()
        
        # --- MULTI-ACCOUNT OPTUNA ISOLATION LAYER ---
        account_suffix = account_uuids[0][:8] if len(account_uuids) == 1 else "shared"
        db_url = f"sqlite:///optuna_studies_{account_suffix}.db"
        storage = optuna.storages.RDBStorage(
            url=db_url,
            engine_kwargs={"connect_args": {"timeout": 60}}
        )

        study = optuna.create_study(
            study_name=f"{normalized_name}_{current_date_str}_cpcv", 
            storage=storage, 
            load_if_exists=True, 
            direction="maximize"
        )
        
        # Execute trials with n_jobs=1 to isolate the Tuning Engine computationally from CPCV expansion
        study.optimize(objective, n_trials=200, n_jobs=1)
        
        best_params = study.best_params
        best_trial = study.best_trial
        
        is_matrix = []
        oos_matrix = []
        for trial in study.trials:
            if trial.state == optuna.trial.TrialState.COMPLETE:
                is_matrix.append(trial.user_attrs.get("is_path_returns", []))
                oos_matrix.append(trial.user_attrs.get("oos_path_returns", []))
                
        pbo_score = math_engine.calculate_pbo(is_matrix, oos_matrix)
        
        best_oos_returns = best_trial.user_attrs.get("oos_path_returns", [])
        raw_sortino = math_engine.calculate_sortino_ratio(best_oos_returns)
        haircut_sortino = math_engine.calculate_harvey_liu_haircut(raw_sortino, len(is_matrix))
        
        # Evaluate baseline parameters across all CPCV OOS paths
        acc_sym_ids = [k for k, v in bot_state.items() if isinstance(v, dict) and database.normalize_name(v.get("name", "")) == normalized_name]
        target_sym_id = acc_sym_ids[0] if acc_sym_ids else None
        
        baseline_oos_returns = []
        for _, test_dates_list in cpcv_paths:
            test_dates_set = set(test_dates_list)
            history_test_path = {sym: {d: ticks for d, ticks in data.items() if d in test_dates_set} for sym, data in history_125d.items()}
            base_alpha_oos = -run_simulation(database.DEFAULT_STRATEGY, history_test_path, [target_sym_id] if target_sym_id else [], current_date_str, deviation_dict)
            baseline_oos_returns.append(base_alpha_oos)
            
        baseline_sortino = math_engine.calculate_sortino_ratio(baseline_oos_returns)
        
        optimization_results[normalized_name] = {}
        baseline_decision = ""
        
        # The Acceptance Gate
        if haircut_sortino > baseline_sortino and pbo_score < 15.0:
            print(f"       OOS validation passed (CPCV)! Haircut Sortino: {haircut_sortino:.2f} > Baseline: {baseline_sortino:.2f} (PBO: {pbo_score:.1f}%)", flush=True)
            for name, val in best_params.items():
                current_params[name] = round(val, 2)
            baseline_decision = "Adopted AI (CPCV)"
        else:
            rejection_reason = f"Haircut Sortino ({haircut_sortino:.2f}) <= Baseline ({baseline_sortino:.2f})" if haircut_sortino <= baseline_sortino else f"PBO ({pbo_score:.1f}%) >= 15.0%"
            print(f"       OOS validation failed. {rejection_reason}. Reverting to Default.", flush=True)
            for k, v in database.DEFAULT_STRATEGY.items():
                current_params[k] = v
            baseline_decision = f"Reverted to Default: {rejection_reason}"
            
        database.log_autotune_result(
            current_date_str, normalized_name, len(is_matrix), pbo_score, haircut_sortino, best_params, baseline_decision
        )

        # Build Discord logs ensuring all newly tuned variables are captured
        optimization_results[normalized_name]["_baseline_chosen"] = baseline_decision
        for k, new_val in current_params.items():
            old_val = original_params.get(k, new_val)
            optimization_results[normalized_name][k] = {"old": old_val, "new": new_val}

        elapsed = time.time() - start_time
        print(f"       Optimization completed in {elapsed:.2f}s. OOS Sortino: {haircut_sortino:.2f} (PBO: {pbo_score:.1f}%)", flush=True)

        database.save_symphony_strategy(normalized_name, current_params, locked_vars)

    print("  -> Autotuner finished all symphonies.", flush=True)
    
    # Clean up large synthetic history JSON files to save disk space
    try:
        import glob
        import os
        for f in glob.glob("cache/synthetic_history_*.json"):
            os.remove(f)
            print(f"  -> Deleted synthetic history cache: {f}", flush=True)
    except Exception as e:
        print(f"  -> Failed to delete synthetic history cache: {e}", flush=True)

    return optimization_results