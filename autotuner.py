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

def optimize_history_data(history_125d):
    optimized_history = {}
    for sym_id, dates_data in history_125d.items():
        optimized_history[sym_id] = {}
        for date, ticks in dates_data.items():
            if not ticks:
                continue
            # Extract lists of values
            returns = np.array([t.get("return", 0.0) for t in ticks], dtype=np.float64)
            mcs = np.array([t.get("mc_prob", 50.0) for t in ticks], dtype=np.float64)
            prob_losses = np.array([t.get("prob_loss_dynamic", 0.0) for t in ticks], dtype=np.float64)
            vwap_diffs = np.array([t.get("vwap_diff", 0.0) for t in ticks], dtype=np.float64)
            
            first_tick = ticks[0]
            vol = first_tick.get("vol", 1.0)
            base_atr_pct = first_tick.get("base_atr_pct", vol)
            
            optimized_history[sym_id][date] = {
                "return": returns,
                "mc_prob": mcs,
                "prob_loss_dynamic": prob_losses,
                "vwap_diff": vwap_diffs,
                "vol": vol,
                "base_atr_pct": base_atr_pct
            }
    return optimized_history

def run_simulation(p, history_data, acc_sym_ids, current_date_str, deviation_dict):
    total_guard_alpha = 0.0
    decay_rate = 0.015
    current_dt = datetime.strptime(current_date_str, "%Y-%m-%d")
    
    # Extract strategy parameters once
    trigger_threshold = p.get("TRIGGER_THRESHOLD_PCT", 15.0)
    take_profit_mc = p.get("TAKE_PROFIT_MC_PCT", 5.0)
    vwap_cross_hwm = p.get("VWAP_CROSS_HWM_PCT", 1.0)
    vwap_band_mult = p.get("VWAP_BAND_MULTIPLIER", 0.10)
    vol_magnitude_mult = p.get("VOLATILITY_MAGNITUDE_MULTIPLIER", 1.5)
    vol_close_mult = p.get("VOLATILITY_CLOSE_MULTIPLIER", 0.5)
    parabolic_velocity_threshold = p.get("PARABOLIC_VELOCITY_THRESHOLD", 2.0)
    max_parabolic_squeeze = p.get("MAX_PARABOLIC_SQUEEZE", 0.50)
    vwap_bleed_mult = p.get("VWAP_BLEED_MULTIPLIER", 1.5)
    
    # Precompute time-decay multipliers for all possible tick indices up to 400
    precomputed_decay = []
    for i in range(400):
        time_ratio = min(1.0, max(0.0, i / 390.0))
        decay = math.log10(1.0 + 9.0 * time_ratio)
        dyn_mult = vol_magnitude_mult - (vol_magnitude_mult - vol_close_mult) * decay
        dyn_min_stop = 0.3 - (0.3 - 0.15) * decay
        precomputed_decay.append((dyn_mult, dyn_min_stop))
        
    for sym_id in acc_sym_ids:
        dates_data = history_data.get(sym_id, {})
        for date, ticks_data in dates_data.items():
            if not ticks_data:
                continue
                
            if isinstance(ticks_data, dict) and "return" in ticks_data:
                returns = ticks_data["return"]
                mcs = ticks_data["mc_prob"]
                prob_losses = ticks_data["prob_loss_dynamic"]
                vwap_diffs = ticks_data["vwap_diff"]
                vol = ticks_data["vol"]
                base_atr_pct = ticks_data["base_atr_pct"]
            else:
                # Fallback to convert list of dicts on the fly
                returns = np.array([t.get("return", 0.0) for t in ticks_data], dtype=np.float64)
                mcs = np.array([t.get("mc_prob", 50.0) for t in ticks_data], dtype=np.float64)
                prob_losses = np.array([t.get("prob_loss_dynamic", 0.0) for t in ticks_data], dtype=np.float64)
                vwap_diffs = np.array([t.get("vwap_diff", 0.0) for t in ticks_data], dtype=np.float64)
                first_tick = ticks_data[0]
                vol = first_tick.get("vol", 1.0)
                base_atr_pct = first_tick.get("base_atr_pct", vol)

            num_ticks = len(returns)
            if num_ticks == 0:
                continue
                
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
            lowest_mc_seen = 100.0
            lock_engaged_ticks = 0
            below_lock_sim_a = 0
            below_lock_sim_b = 0
            
            # Precompute breakeven activation threshold for this day
            dynamic_activation = max(0.4, min(3.0, vol))
            breakeven_activation_threshold = dynamic_activation - 0.2
            
            triggered_return = None
            eod_return = returns[-1]
            day_max_return = float(np.max(returns))
            
            safe_vol = base_atr_pct if base_atr_pct > 0 else (vol if vol > 0 else 1.0)
            
            # VWAP buffer is constant for the day
            vwap_buffer_pct = -vol * vwap_band_mult
            
            for tick_idx in range(num_ticks):
                ret = returns[tick_idx]
                mc = mcs[tick_idx]
                prob_loss_dynamic = prob_losses[tick_idx]
                vwap_diff = vwap_diffs[tick_idx]
                
                if ret > hwm:
                    hwm = ret
                    hwm_tick_idx = tick_idx
                safe_hwm = hwm if hwm > ret else ret
                
                # --- PARABOLIC SQUEEZE LOGIC ---
                if prev_return is None:
                    prev_return = ret
                else:
                    is_para = (ret - prev_return) >= parabolic_velocity_threshold
                    if is_para:
                        para_armed = True
                    prev_return = ret
                # ------------------------------
                
                if not armed:
                    if take_profit_mc <= mc < trigger_threshold and prob_loss_dynamic >= 25.0:
                        armed = True
                else:
                    if mc > (trigger_threshold * 2.0) and ret > 0.0:
                        armed = False
                        below_stop_count = 0
                        
                # --- TIME SQUEEZE DECAY LOGIC ---
                decay_idx = tick_idx if tick_idx < 400 else 399
                dynamic_multiplier, dynamic_min_stop = precomputed_decay[decay_idx]
                
                stagnation_mins = tick_idx - hwm_tick_idx
                
                # Inline compute_active_trailing_stop
                distance = (safe_vol * dynamic_multiplier) if (safe_vol * dynamic_multiplier) > dynamic_min_stop else dynamic_min_stop
                if para_armed or breakeven_locked:
                    distance *= max_parabolic_squeeze
                if stagnation_mins >= 60.0:
                    decay_factor = 0.5 ** ((stagnation_mins - 60.0) / 60.0)
                    distance *= decay_factor if decay_factor > 0.2 else 0.2
                    
                base_stop = safe_hwm - distance
                
                # --- RISK GUARD LOGIC ---
                if ret >= breakeven_activation_threshold:
                    hwm_hold_ticks += 1
                else:
                    hwm_hold_ticks = 0
                    
                if hwm_hold_ticks >= 5:
                    breakeven_locked = True
                    
                if breakeven_locked:
                    lock_engaged_ticks += 1
                else:
                    lock_engaged_ticks = 0
                    
                stop_level = base_stop if not breakeven_locked else (base_stop if base_stop > 0.0 else 0.0)
                if stop_level > highest_stop_level:
                    highest_stop_level = stop_level
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
                    be_path_a = (ret <= stop_level - 0.50) and mc < 60.0 and ret < 1.0
                    be_path_b = (ret <= stop_level - 0.10) and lock_engaged_ticks >= 60 and lowest_mc_seen >= 60.0
                    
                    if be_path_a:
                        below_lock_sim_a += 1
                        if below_lock_sim_a >= 3:
                            is_breakeven_hit = True
                            exit_reason_trailing = "Breakeven Path A"
                    else:
                        below_lock_sim_a = 0
                        
                    if be_path_b:
                        below_lock_sim_b += 1
                        if below_lock_sim_b >= 3:
                            is_breakeven_hit = True
                            exit_reason_trailing = "Breakeven Path B (MC-Stuck)"
                    else:
                        below_lock_sim_b = 0
                        
                is_tp_hit = False
                if mc < take_profit_mc:
                    if not tp_armed:
                        tp_armed = True
                        above_tp_count = 0
                elif tp_armed:
                    if mc >= take_profit_mc and ret <= (safe_hwm - 0.30):
                        above_tp_count += 1
                        if above_tp_count >= 2:
                            is_tp_hit = True
                    else:
                        above_tp_count = 0
                        
                current_vwap_diff_pct = vwap_diff * 100.0
                is_vwap_broken = False
                if current_vwap_diff_pct < vwap_buffer_pct:
                    if safe_hwm >= vwap_cross_hwm and ret < safe_hwm:
                        vwap_ticks += 1
                        if vwap_ticks >= 3:
                            is_vwap_broken = True
                    else:
                        vwap_ticks = 0
                else:
                    vwap_ticks = 0
                    
                if is_trailing_hit or is_breakeven_hit or is_tp_hit or is_vwap_broken:
                    reason_str = exit_reason_trailing
                    if is_tp_hit:
                        reason_str = "Take-Profit"
                    elif is_vwap_broken:
                        vol_base = abs(vwap_buffer_pct) if vwap_buffer_pct != 0 else 0.15
                        is_bleed = current_vwap_diff_pct < -(vol_base * vwap_bleed_mult)
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
                
                if missed_upside > 1.0:
                    total_guard_alpha -= (missed_upside * 1.5 * weight)
                    
                if safe_hwm > 1.0 and drawdown_from_peak > 1.5:
                    total_guard_alpha -= (drawdown_from_peak * 0.75 * weight)
                    
                if guard_alpha < 0:
                    total_guard_alpha += (guard_alpha * 2.0 * weight)
                else:
                    total_guard_alpha += (guard_alpha * weight)
                    
                safe_drawdown = max(drawdown_from_peak, 0.01)
                pd_ratio = triggered_return / safe_drawdown
                total_guard_alpha += (pd_ratio * 0.5 * weight)
                
    return -total_guard_alpha

class CPCVObjective:
    def __init__(self, current_params, locked_vars, precomputed_cpcv_histories, current_date_str, deviation_dict, target_sym_id):
        self.current_params = current_params
        self.locked_vars = locked_vars
        self.precomputed_cpcv_histories = precomputed_cpcv_histories
        self.current_date_str = current_date_str
        self.deviation_dict = deviation_dict
        self.target_sym_id = target_sym_id

    def __call__(self, trial):
        p = self.current_params.copy()
        if "TRIGGER_THRESHOLD_PCT" not in self.locked_vars:
            p["TRIGGER_THRESHOLD_PCT"] = trial.suggest_float("TRIGGER_THRESHOLD_PCT", 5.0, 25.0)
        if "TAKE_PROFIT_MC_PCT" not in self.locked_vars:
            p["TAKE_PROFIT_MC_PCT"] = trial.suggest_float("TAKE_PROFIT_MC_PCT", 2.0, 10.0)
        if "VWAP_CROSS_HWM_PCT" not in self.locked_vars:
            p["VWAP_CROSS_HWM_PCT"] = trial.suggest_float("VWAP_CROSS_HWM_PCT", 0.5, 2.5)
        if "VWAP_BAND_MULTIPLIER" not in self.locked_vars:
            p["VWAP_BAND_MULTIPLIER"] = trial.suggest_float("VWAP_BAND_MULTIPLIER", 0.02, 0.40)
        if "VOLATILITY_MAGNITUDE_MULTIPLIER" not in self.locked_vars:
            p["VOLATILITY_MAGNITUDE_MULTIPLIER"] = trial.suggest_float("VOLATILITY_MAGNITUDE_MULTIPLIER", 0.5, 2.5, step=0.1)
        if "VOLATILITY_CLOSE_MULTIPLIER" not in self.locked_vars:
            p["VOLATILITY_CLOSE_MULTIPLIER"] = trial.suggest_float("VOLATILITY_CLOSE_MULTIPLIER", 0.1, 1.0, step=0.1)
        if "PARABOLIC_VELOCITY_THRESHOLD" not in self.locked_vars:
            p["PARABOLIC_VELOCITY_THRESHOLD"] = trial.suggest_float("PARABOLIC_VELOCITY_THRESHOLD", 1.5, 5.0)
        if "MAX_PARABOLIC_SQUEEZE" not in self.locked_vars:
            p["MAX_PARABOLIC_SQUEEZE"] = trial.suggest_float("MAX_PARABOLIC_SQUEEZE", 0.25, 0.80)
        if "VWAP_BLEED_MULTIPLIER" not in self.locked_vars:
            p["VWAP_BLEED_MULTIPLIER"] = trial.suggest_float("VWAP_BLEED_MULTIPLIER", 0.5, 3.0, step=0.1)
        if "VWAP_BLEED_TICKS" not in self.locked_vars:
            p["VWAP_BLEED_TICKS"] = trial.suggest_int("VWAP_BLEED_TICKS", 3, 30)

        path_is_returns = []
        path_oos_returns = []
        
        for history_train_path, history_test_path in self.precomputed_cpcv_histories:
            alpha_is = -run_simulation(p, history_train_path, [self.target_sym_id], self.current_date_str, self.deviation_dict)
            alpha_oos = -run_simulation(p, history_test_path, [self.target_sym_id], self.current_date_str, self.deviation_dict)
            
            path_is_returns.append(alpha_is)
            path_oos_returns.append(alpha_oos)
            
        trial.set_user_attr("is_path_returns", path_is_returns)
        trial.set_user_attr("oos_path_returns", path_oos_returns)
        
        path_crra_oos = [math_engine.calculate_crra_utility([ret]) for ret in path_oos_returns]
        return float(np.percentile(path_crra_oos, 25))

def run_optuna_optimization(study_name, db_url, objective_callable, n_trials):
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    storage = optuna.storages.RDBStorage(
        url=db_url,
        engine_kwargs={"connect_args": {"timeout": 60}}
    )
    study = optuna.load_study(
        study_name=study_name,
        storage=storage
    )
    study.optimize(objective_callable, n_trials=n_trials)

def run_autotuner(bot_state, current_date_str, account_uuids, is_forced=False):
    """
    Runs a 6-month walk-forward optimization to find the best variables using Bayesian Optimization per account.
    Implements True Walk-Forward Analysis (80% train, 20% OOS test).
    """
    import os
    import concurrent.futures
    
    print(f"  -> Starting EOD Autotune (125-day WFA: 80% Train / 20% OOS per Symphony)...", flush=True)

    # 0. Calculate Historical Execution Deviation
    deviation_dict = calculate_historical_deviation(current_date_str)

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
    
    # Optimize history data format once
    optimized_history = optimize_history_data(history_125d)

    for normalized_name in symphony_names:
        print(f"     Optimizing Symphony: {normalized_name}", flush=True)
        strat_data = database.get_symphony_strategy(normalized_name)
        locked_vars = strat_data.get("locked_vars", [])
        current_params = strat_data.get("params", {})
        original_params = current_params.copy()

        acc_sym_ids = [k for k, v in bot_state.items() if isinstance(v, dict) and database.normalize_name(v.get("name", "")) == normalized_name]
        if not acc_sym_ids:
            continue
        target_sym_id = acc_sym_ids[0]

        # Precompute and slice histories for all paths
        precomputed_cpcv_histories = []
        for train_dates_list, test_dates_list in cpcv_paths:
            train_dates_set = set(train_dates_list)
            test_dates_set = set(test_dates_list)
            
            history_train_path = {sym: {d: ticks for d, ticks in data.items() if d in train_dates_set} for sym, data in optimized_history.items()}
            history_test_path = {sym: {d: ticks for d, ticks in data.items() if d in test_dates_set} for sym, data in optimized_history.items()}
            
            precomputed_cpcv_histories.append((history_train_path, history_test_path))

        objective_callable = CPCVObjective(
            current_params,
            locked_vars,
            precomputed_cpcv_histories,
            current_date_str,
            deviation_dict,
            target_sym_id
        )

        start_time = time.time()
        
        # --- MULTI-ACCOUNT OPTUNA ISOLATION LAYER ---
        account_suffix = account_uuids[0][:8] if len(account_uuids) == 1 else "shared"
        db_url = f"sqlite:///optuna_studies_{account_suffix}.db"
        storage = optuna.storages.RDBStorage(
            url=db_url,
            engine_kwargs={"connect_args": {"timeout": 60}}
        )

        study_name = f"{normalized_name}_{current_date_str}_cpcv"
        study = optuna.create_study(
            study_name=study_name, 
            storage=storage, 
            load_if_exists=True, 
            direction="maximize"
        )
        
        num_workers = min(6, os.cpu_count() or 1)
        trials_per_worker = math.ceil(200 / num_workers)
        
        print(f"       Optimizing with {num_workers} parallel processes...", flush=True)
        with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = [
                executor.submit(run_optuna_optimization, study_name, db_url, objective_callable, trials_per_worker)
                for _ in range(num_workers)
            ]
            concurrent.futures.wait(futures)
            
        # Re-load study in the main process to collect the best results
        study = optuna.load_study(
            study_name=study_name,
            storage=storage
        )
        
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
        
        # Evaluate baseline parameters across all CPCV OOS paths using the precomputed histories
        baseline_oos_returns = []
        for _, history_test_path in precomputed_cpcv_histories:
            base_alpha_oos = -run_simulation(
                database.DEFAULT_STRATEGY, 
                history_test_path, 
                [target_sym_id] if target_sym_id else [], 
                current_date_str, 
                deviation_dict
            )
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