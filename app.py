"""Flask application for AlphaBot Control Center with Account-Level settings."""

import os
import sys
import time
import threading
import subprocess
from datetime import datetime
from datetime import time as dt_time
import json
import alpha_bot_execution
import schedule
import requests
import logging
from flask import Flask, render_template, jsonify, request
from dotenv import dotenv_values, set_key

import database

ENV_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

COMPOSER_BASE_URL = "https://api.composer.trade/api/v0.1"

# --- 1. Bot Execution Logic ---
def trigger_alpha_bot(force=False):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Triggering Alpha Bot...", flush=True)
    try:
        cmd = [sys.executable, "-u", "alpha_bot_execution.py"]
        if force: cmd.append("--force")
        
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            env_latest = dotenv_values(ENV_FILE_PATH)
            for k, v in env_latest.items():
                if v is not None:
                    env[k] = str(v)
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Warning: Failed to reload .env for child process: {e}", flush=True)
        
        process = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8")
        
        # Readline loop prevents internal iterator block-buffering
        for line in iter(process.stdout.readline, ''):
            print(line, end="", flush=True)
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd)
    except subprocess.CalledProcessError as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Execution failed: {e}", flush=True)

def threaded_trigger():
    # Evaluate current time to prevent overlap with the EOD pipeline
    current_et = alpha_bot_execution.get_current_et()
    current_time = current_et.time()
    
    # Bypass standard triggers during the critical EOD generation window (15:52 - 16:05)
    # This prevents regular executions from colliding with Stage 2 and EOD lockups
    if dt_time(15, 53) <= current_time <= dt_time(16, 0):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Standing by during EOD blackout window...", flush=True)
        return
        
    threading.Thread(target=trigger_alpha_bot, daemon=True).start()

def run_scheduler():
    # Regular real-time evaluation runs every minute, except during the blackout and EOD window
    schedule.every().minute.at(":00").do(threaded_trigger)
    
    while True:
        current_et = alpha_bot_execution.get_current_et()
        current_time = current_et.time()
        
        # 1. Always evaluate pending schedule jobs to keep UI timers synchronized!
        schedule.run_pending()
            
        # 2. Automated EOD Post-Mortem Sequence: Single-occurrence execution completely isolated from standard threads
        if current_time.hour == 16 and current_time.minute == 1:
            bot_state = database.load_state()
            if bot_state.get("post_mortem_run") != current_et.strftime("%Y-%m-%d"):
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [SCHEDULER] Commencing single-occurrence Friday/EOD Post-Mortem pipeline...", flush=True)
                trigger_alpha_bot(force=True)
                time.sleep(65) # Advance clock past the active minute to prevent re-triggering
                
        time.sleep(1)

# --- 2. Web Dashboard Routes ---
@app.route("/")
def dashboard():
    return render_template("index.html")

@app.route("/api/state")
def get_state():
    try:
        now = datetime.now()
        # Always use clock math for perfectly synchronized UI countdowns (bot runs at :00)
        next_run_seconds = 60 - now.second

        state_data = database.load_state()
        if not state_data:
            return jsonify({"status": "waiting", "message": "Bot state initializing.", "next_run_seconds": next_run_seconds})

        env_vars = dotenv_values(".env")
        live_mode = env_vars.get("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")
        # Always use clock math for perfectly synchronized UI countdowns (bot runs at :00)
        next_run_seconds = 60 - now.second

        account_labels = {}
        acc_ind = env_vars.get("ACCOUNT_INDIVIDUAL", "").strip()
        acc_roth = env_vars.get("ACCOUNT_ROTH", "").strip()
        acc_trad = env_vars.get("ACCOUNT_TRAD", "").strip()
        
        if acc_ind: account_labels[acc_ind] = "Individual"
        if acc_roth: account_labels[acc_roth] = "Roth IRA"
        if acc_trad: account_labels[acc_trad] = "Trad. IRA"

        active_uuids = [uid for uid in [acc_ind, acc_roth, acc_trad] if uid]

        account_totals = state_data.get("account_totals", {})
        account_balances = {
            "MANUAL_BAL_IND": account_totals.get(acc_ind, 0.0),
            "MANUAL_BAL_ROTH": account_totals.get(acc_roth, 0.0),
            "MANUAL_BAL_TRAD": account_totals.get(acc_trad, 0.0)
        }

        # Render HTML for UI
        symphony_keys = [k for k in state_data.keys() if isinstance(state_data[k], dict)]
        accounts_map = {}
        for k in symphony_keys:
            sym = state_data[k]
            acc_id = sym.get("account", "Unknown Account")
            if acc_id not in active_uuids:
                continue
            if acc_id not in accounts_map:
                accounts_map[acc_id] = []
            sym["id"] = k
            sym["normalized_name"] = database.normalize_name(sym.get("name", ""))
            accounts_map[acc_id].append(sym)

        # Sorting logic
        sort_col = request.args.get("sortCol", "name")
        sort_dir = request.args.get("sortDir", "asc")
        is_desc = (sort_dir == "desc")

        def get_status_rank(s):
            if s.get("triggered"):
                if s.get("triggered_reason") == "VWAP Breakdown": return 5
                return 4
            if s.get("para_armed"): return 3
            if s.get("tp_armed"): return 2
            if s.get("armed"): return 1
            return 0

        def get_exit_ret(s):
            if s.get("triggered"):
                return s.get("triggered_at_return") if s.get("triggered_at_return") is not None else (s.get("current_return") or -999.0)
            return s.get("current_return") if s.get("current_return") is not None else -999.0

        for acc_id in accounts_map:
            if sort_col == "mc_prob":
                accounts_map[acc_id].sort(key=lambda s: s.get("mc_prob") if s.get("mc_prob") is not None else -999.0, reverse=is_desc)
            elif sort_col == "status":
                accounts_map[acc_id].sort(key=get_status_rank, reverse=is_desc)
            elif sort_col == "stop_level":
                accounts_map[acc_id].sort(key=lambda s: s.get("triggered_at_stop") if s.get("triggered") and s.get("triggered_at_stop") is not None else (s.get("stop_trigger") if s.get("stop_trigger") is not None else -999.0), reverse=is_desc)
            elif sort_col == "current_return":
                accounts_map[acc_id].sort(key=get_exit_ret, reverse=is_desc)
            elif sort_col == "high_water_mark":
                accounts_map[acc_id].sort(key=lambda s: s.get("shadow_hwm", -999.0), reverse=is_desc)
            elif sort_col == "shadow":
                accounts_map[acc_id].sort(key=lambda s: s.get("current_return") if s.get("current_return") is not None else -999.0, reverse=is_desc)
            else: # name
                accounts_map[acc_id].sort(key=lambda s: (s.get("name") or s.get("id", "")).lower(), reverse=is_desc)

        rendered_html = render_template("table_partial.html", accounts_map=accounts_map, account_labels=account_labels, sort_col=sort_col, sort_dir=sort_dir)

        return jsonify({
            "status": "active",
            "state": state_data,
            "live_mode": live_mode,
            "execution_start_time": env_vars.get("EXECUTION_START_TIME", "09:30"),
            "next_run_seconds": next_run_seconds,
            "html": rendered_html,
            "account_balances": account_balances
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/history_dates")
def get_history_dates():
    import glob
    try:
        files = glob.glob("symphony_logs_*.json")
        dates = []
        for f in files:
            date_str = f.replace("symphony_logs_", "").replace(".json", "")
            dates.append(date_str)
        dates.sort(reverse=True)
        return jsonify(dates)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/logs/<date_str>/<symphony_id>")
def api_symphony_logs(date_str, symphony_id):
    try:
        if symphony_id.lower() == "all":
            # fetch all logs for the date
            log_file = f"symphony_logs_{date_str}.json"
            import os, json
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8") as f:
                    logs = json.load(f)
                return jsonify(logs)
            return jsonify({})
        else:
            logs = database.get_symphony_logs(symphony_id, date_str=date_str)
            return jsonify(logs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/chart/<symphony_id>")
def get_chart_data(symphony_id):
    try:
        chart_data = database.load_chart_history()
        symphony_data = chart_data.get("symphonies", {}).get(symphony_id, [])
        return jsonify({"status": "success", "data": symphony_data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/trigger", methods=["POST"])
def manual_trigger():
    threading.Thread(target=trigger_alpha_bot, args=(True,)).start()
    return jsonify({"status": "success", "message": "Bot execution forced."})

@app.route("/api/force_eod", methods=["POST"])
def force_eod():
    if not database.acquire_lock(lease_duration=900):
        return jsonify({"status": "error", "message": "Database currently locked by active execution. Please wait."}), 409

    try:
        from datetime import datetime, timedelta
        bot_state = database.load_state()
        chart_history = database.load_chart_history()
        prev_date_str = chart_history.get("date") or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        env_vars = dotenv_values(ENV_FILE_PATH)
        acc_ind = env_vars.get("ACCOUNT_INDIVIDUAL", "").strip()
        acc_roth = env_vars.get("ACCOUNT_ROTH", "").strip()
        acc_trad = env_vars.get("ACCOUNT_TRAD", "").strip()
        discord_webhook = env_vars.get("DISCORD_WEBHOOK_URL", "")

        # Build filtered list based on UI/Environment flags
        enabled_uuids = []
        if acc_ind and env_vars.get("ACCOUNT_INDIVIDUAL_ENABLED", "True").lower() in ("true", "1", "yes"):
            enabled_uuids.append(acc_ind)
        if acc_roth and env_vars.get("ACCOUNT_ROTH_ENABLED", "True").lower() in ("true", "1", "yes"):
            enabled_uuids.append(acc_roth)
        if acc_trad and env_vars.get("ACCOUNT_TRAD_ENABLED", "True").lower() in ("true", "1", "yes"):
            enabled_uuids.append(acc_trad)

        def run_eod_tasks():
            try:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Forcing EOD Analysis for {prev_date_str}...", flush=True)
                import reporting
                import autotuner
                import traceback
                
                reporting.generate_eod_snapshot(bot_state, prev_date_str, is_post_rebalance=False, discord_webhook_url=discord_webhook)
                reporting.generate_eod_snapshot(bot_state, prev_date_str, is_post_rebalance=True, discord_webhook_url=discord_webhook)
                autotuner_changes = autotuner.run_autotuner(bot_state, prev_date_str, enabled_uuids, is_forced=True)
                
                try:
                    reporting.send_eod_discord_post(prev_date_str, f"post_mortem_{prev_date_str}.json", autotuner_changes, discord_webhook)
                except Exception as discord_err:
                    print(f"!!! [ERROR] Failed to send Discord EOD report: {discord_err}", flush=True)
                    traceback.print_exc()
                    
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Forced EOD Analysis complete.", flush=True)
            except Exception as e:
                import traceback
                print(f"!!! [CRITICAL ERROR] Background EOD Task failed abruptly: {e}", flush=True)
                traceback.print_exc()
            finally:
                database.release_lock()

        threading.Thread(target=run_eod_tasks, daemon=True).start()
        return jsonify({"status": "success", "message": "EOD Analysis initiated for " + prev_date_str})
    except Exception as e:
        database.release_lock()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/resend_discord", methods=["POST"])
def resend_discord():
    try:
        from datetime import datetime, timedelta
        chart_history = database.load_chart_history()
        prev_date_str = chart_history.get("date")
        if not prev_date_str:
            prev_date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        env_vars = dotenv_values(ENV_FILE_PATH)
        discord_webhook = env_vars.get("DISCORD_WEBHOOK_URL", "")

        def run_discord_push():
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Resending Discord Report for {prev_date_str}...", flush=True)
            import reporting
            # Pass None for optimization_results to skip tuning and just send the current JSON
            reporting.send_eod_discord_post(prev_date_str, f"post_mortem_{prev_date_str}.json", None, discord_webhook)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Discord resend complete.", flush=True)

        threading.Thread(target=run_discord_push, daemon=True).start()
        return jsonify({"status": "success", "message": "Discord push initiated for " + prev_date_str})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/performance_benchmark")
def get_performance_benchmark():
    try:
        from dotenv import dotenv_values
        import glob
        import json
        import os
        from datetime import datetime, timedelta
        
        env_vars = dotenv_values(".env")
        acc_ind = env_vars.get("ACCOUNT_INDIVIDUAL", "").strip()
        acc_roth = env_vars.get("ACCOUNT_ROTH", "").strip()
        acc_trad = env_vars.get("ACCOUNT_TRAD", "").strip()
        
        composer_key = env_vars.get("COMPOSER_KEY_ID", "")
        composer_secret = env_vars.get("COMPOSER_SECRET", "")
        headers = {"x-api-key-id": composer_key, "authorization": f"Bearer {composer_secret}", "Content-Type": "application/json"}
        
        # Load state to get current values & account mapping
        state_data = database.load_state()
        if not state_data:
            return jsonify({"status": "error", "message": "Bot state not initialized."}), 400
            
        uuid_to_key = {}
        if acc_ind: uuid_to_key[acc_ind] = 'ind'
        if acc_roth: uuid_to_key[acc_roth] = 'roth'
        if acc_trad: uuid_to_key[acc_trad] = 'trad'
        
        # 1. Fetch Local Logs for Shadow Alpha ("If Held")
        target_points = 30
        files = glob.glob("post_mortem_*.json")
        date_files = []
        for f in files:
            date_part = f.replace("post_mortem_", "").replace(".json", "")
            date_files.append((date_part, f))
            
        date_files.sort(key=lambda x: x[0])
        date_files = date_files[-target_points:]
        
        valid_dates_full = [d[0] for d in date_files] # YYYY-MM-DD
        global_dates = [d[5:] for d in valid_dates_full] # MM-DD
        target_points = len(valid_dates_full)
        
        # 2. Fetch True Historical Series from Composer API
        composer_history = {}
        
        for acc_uuid, acc_key in uuid_to_key.items():
            composer_history[acc_key] = {"strat_series": [], "total_val": 0.0, "simple_return": 0.0}
            
            # Fetch true headline performance from bot_state
            if "account_performance" in state_data and acc_uuid in state_data["account_performance"]:
                composer_history[acc_key]["simple_return"] = state_data["account_performance"][acc_uuid].get("simple_return", 0.0)
            
            url = f"{COMPOSER_BASE_URL}/portfolio/accounts/{acc_uuid}/portfolio-history"
            try:
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    series = data.get("series", [])
                    epochs = data.get("epoch_ms", [])
                    
                    if series:
                        composer_history[acc_key]["total_val"] = series[-1]
                        
                        date_to_val = {}
                        for epoch, val in zip(epochs, series):
                            dt_str = datetime.fromtimestamp(epoch/1000).strftime("%Y-%m-%d")
                            date_to_val[dt_str] = val
                            
                        # Extract exactly the dates from post_mortem logs
                        recent_series = []
                        last_val = series[0] if series else 1.0
                        for d_str in valid_dates_full:
                            if d_str in date_to_val:
                                last_val = date_to_val[d_str]
                            recent_series.append(last_val)
                        
                        if recent_series:
                            start_val = recent_series[0] if recent_series[0] > 0 else 1.0
                            strat_series = [round(((v / start_val) - 1.0) * 100.0, 2) for v in recent_series]
                            composer_history[acc_key]["strat_series"] = strat_series
            except Exception as e:
                print(f"Failed to fetch portfolio history for {acc_uuid}: {e}", flush=True)

        # (Logs already fetched above)
        
        daily_returns = {'ind': [], 'roth': [], 'trad': [], 'total': []}
        
        for date_str, f_path in date_files:
            try:
                with open(f_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except:
                continue
                
            daily_pnl = data.get("daily_pnl")
            if daily_pnl is None:
                continue
                
            agg = {
                'ind': {'held': 0.0, 'weight': 0.0},
                'roth': {'held': 0.0, 'weight': 0.0},
                'trad': {'held': 0.0, 'weight': 0.0},
                'total': {'held': 0.0, 'weight': 0.0}
            }
            
            for pnl in daily_pnl:
                symphony_id = pnl.get("symphony_id", "")
                if "mock_held_baseline" in symphony_id:
                    continue
                    
                acc_id = pnl.get("account_id")
                key = uuid_to_key.get(acc_id)
                weight = pnl.get("value", 1.0)
                if weight <= 0: weight = 1.0
                
                held_ret = pnl.get("held_ret", 0.0)
                
                if key:
                    agg[key]['held'] += held_ret * weight
                    agg[key]['weight'] += weight
                    
                agg['total']['held'] += held_ret * weight
                agg['total']['weight'] += weight
                
            for k in ['ind', 'roth', 'trad', 'total']:
                w = agg[k]['weight']
                if w > 0:
                    daily_returns[k].append(agg[k]['held'] / w)
                else:
                    daily_returns[k].append(0.0)

        results = {
            "dates": global_dates,
            "accounts": {}
        }
        
        # 3. Assemble Final Payload
        total_strat_series = [0.0] * target_points
        total_val_sum = 0.0
        total_simple_return_weighted = 0.0
        
        for k in ['ind', 'roth', 'trad']:
            if k not in composer_history:
                continue
                
            c_hist = composer_history[k]
            strat_series = c_hist["strat_series"]
            # Pad strat series if needed
            while len(strat_series) < target_points:
                strat_series.insert(0, 0.0)
                
            held_mult = 1.0
            held_series = []
            for i, h_ret in enumerate(daily_returns[k]):
                if i == 0:
                    held_series.append(0.0)
                else:
                    held_mult *= (1.0 + h_ret / 100.0)
                    held_series.append(round((held_mult - 1.0) * 100.0, 2))
                
            while len(held_series) < target_points:
                held_series.insert(0, 0.0)
                
            final_strat_pct = strat_series[-1] if strat_series else 0.0
            final_held_pct = held_series[-1] if held_series else 0.0
            current_bal = c_hist["total_val"]
            
            if final_strat_pct > -100.0:
                b_start = current_bal / (1.0 + final_strat_pct / 100.0)
            else:
                b_start = 0.0
                
            strat_usd = current_bal - b_start
            held_usd = b_start * (final_held_pct / 100.0)
            
            total_val_sum += current_bal
            total_simple_return_weighted += final_strat_pct * current_bal
            
            for i in range(target_points):
                total_strat_series[i] += strat_series[i] * current_bal
            
            results["accounts"][k] = {
                "strat_series": strat_series,
                "held_series": held_series,
                "total_return_pct": round(final_strat_pct, 2),
                "if_held_pct": round(final_held_pct, 2),
                "total_return_usd": round(strat_usd, 2),
                "if_held_usd": round(held_usd, 2)
            }

        # Calculate Total Portfolio
        total_strat_series = [round((val / total_val_sum), 2) if total_val_sum > 0 else 0.0 for val in total_strat_series]
        
        total_held_mult = 1.0
        total_held_series = []
        for i, h_ret in enumerate(daily_returns['total']):
            if i == 0:
                total_held_series.append(0.0)
            else:
                total_held_mult *= (1.0 + h_ret / 100.0)
                total_held_series.append(round((total_held_mult - 1.0) * 100.0, 2))
        while len(total_held_series) < target_points:
            total_held_series.insert(0, 0.0)
            
        total_final_strat_pct = (total_simple_return_weighted / total_val_sum) if total_val_sum > 0 else 0.0
        total_final_held_pct = total_held_series[-1] if total_held_series else 0.0
        
        if total_final_strat_pct > -100.0:
            b_start_total = total_val_sum / (1.0 + total_final_strat_pct / 100.0)
        else:
            b_start_total = 0.0
            
        results["accounts"]['total'] = {
            "strat_series": total_strat_series,
            "held_series": total_held_series,
            "total_return_pct": round(total_final_strat_pct, 2),
            "if_held_pct": round(total_final_held_pct, 2),
            "total_return_usd": round(total_val_sum - b_start_total, 2),
            "if_held_usd": round(b_start_total * (total_final_held_pct / 100.0), 2)
        }
            
        return jsonify(results)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/history/<int:days>")
def get_history(days):
    import glob, json, os
    from datetime import datetime, timedelta
    from dotenv import dotenv_values
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    files = glob.glob("post_mortem_*.json")
    
    env_vars = dotenv_values(".env")
    acc_ind = env_vars.get("ACCOUNT_INDIVIDUAL", "").strip()
    acc_roth = env_vars.get("ACCOUNT_ROTH", "").strip()
    acc_trad = env_vars.get("ACCOUNT_TRAD", "").strip()
    
    uuid_to_key = {}
    if acc_ind: uuid_to_key[acc_ind] = 'ind'
    if acc_roth: uuid_to_key[acc_roth] = 'roth'
    if acc_trad: uuid_to_key[acc_trad] = 'trad'
    
    accounts_stats = {}
    for k in ['ind', 'roth', 'trad', 'total']:
        accounts_stats[k] = {
            "total_alpha": 0.0,
            "total_saved": 0.0,
            "trigger_count": 0,
            "wins": 0,
            "by_reason": {},
            "daily_alpha": {}
        }
    
    for f_path in files:
        try:
            date_part = f_path.replace("post_mortem_", "").replace(".json", "")
            file_date = datetime.strptime(date_part, "%Y-%m-%d")
            if start_date <= file_date <= end_date:
                with open(f_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for t in data.get("triggers", []):
                        alpha = t.get("saved_pct_guard_alpha", 0.0)
                        dollars = t.get("saved_dollars", 0.0)
                        reason = t.get("exit_reason", "Unknown")
                        acc_id = t.get("account_id", "")
                        acc_key = uuid_to_key.get(acc_id, "unknown")
                        
                        keys_to_update = ['total']
                        if acc_key in ['ind', 'roth', 'trad']:
                            keys_to_update.append(acc_key)
                            
                        for k in keys_to_update:
                            accounts_stats[k]["total_alpha"] += alpha
                            accounts_stats[k]["total_saved"] += dollars
                            accounts_stats[k]["trigger_count"] += 1
                            if alpha > 0: accounts_stats[k]["wins"] += 1
                            
                            if reason not in accounts_stats[k]["by_reason"]:
                                accounts_stats[k]["by_reason"][reason] = {"alpha": 0.0, "count": 0, "wins": 0}
                            accounts_stats[k]["by_reason"][reason]["alpha"] += alpha
                            accounts_stats[k]["by_reason"][reason]["count"] += 1
                            if alpha > 0: accounts_stats[k]["by_reason"][reason]["wins"] += 1
                            
                            accounts_stats[k]["daily_alpha"][date_part] = accounts_stats[k]["daily_alpha"].get(date_part, 0.0) + alpha
        except: continue

    for k in ['ind', 'roth', 'trad', 'total']:
        stats = accounts_stats[k]
        if stats["trigger_count"] > 0:
            stats["avg_guard_alpha"] = stats["total_alpha"] / stats["trigger_count"]
            stats["win_rate"] = (stats["wins"] / stats["trigger_count"]) * 100
        else:
            stats["avg_guard_alpha"] = 0
            stats["win_rate"] = 0
            
        sorted_daily = []
        for d in sorted(stats["daily_alpha"].keys()):
            sorted_daily.append({"date": d, "alpha": stats["daily_alpha"][d]})
        stats["daily_alpha"] = sorted_daily
        
    return jsonify(accounts_stats)


# --- 3. Account Liquidation ---
def perform_account_liquidation(account_id, key, secret, live_mode):
    headers = {"x-api-key-id": key, "authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    url = f"{COMPOSER_BASE_URL}/portfolio/accounts/{account_id}/symphony-stats-meta"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            for sym in resp.json().get("symphonies", []):
                if live_mode:
                    sell_url = f"{COMPOSER_BASE_URL}/deploy/accounts/{account_id}/symphonies/{sym['id']}/go-to-cash"
                    sell_resp = requests.post(sell_url, headers=headers, json={}, timeout=10)
                    print(f"Liquidated {sym.get('name')} (HTTP {sell_resp.status_code})", flush=True)
                    time.sleep(1.5)
    except Exception as e:
        print(f"Liquidation Error: {e}", flush=True)

@app.route("/api/sell_account", methods=["POST"])
def sell_account():
    data = request.json
    account_id = data.get("account_id")
    env_vars = dotenv_values(".env")
    live_mode = env_vars.get("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")

    if account_id and env_vars.get("COMPOSER_KEY_ID"):
        threading.Thread(target=perform_account_liquidation, args=(account_id, env_vars.get("COMPOSER_KEY_ID"), env_vars.get("COMPOSER_SECRET"), live_mode)).start()
        return jsonify({"status": "success", "message": "Liquidation initiated."})
    return jsonify({"status": "error", "message": "Missing credentials or account ID."}), 400

@app.route("/api/portfolio_overhaul", methods=["POST"])
def portfolio_overhaul():
    import threading
    data = request.json
    account_id = data.get("account_id")

    if not account_id:
        return jsonify({"status": "error", "message": "Missing account ID payload."}), 400
        
    if not database.acquire_lock(lease_duration=300):
        return jsonify({"status": "error", "message": "Database currently locked by active execution loop. Try again in 60s."}), 409
        
    try:
        def overhaul_worker():
            try:
                database.execute_system_flush(account_id)
            finally:
                database.release_lock()
                
        threading.Thread(target=overhaul_worker, daemon=True).start()
        return jsonify({"status": "success", "message": "Ecosystem flush and synchronization successfully initiated in background thread."})
    except Exception as e:
        database.release_lock()
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 4. Tabbed Settings / Control Panel Routes ---
@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Returns Globals from .env and Symphony Strategies from SQLite."""
    env_vars = dotenv_values(ENV_FILE_PATH)
    globals_data = {
        "LIVE_EXECUTION": env_vars.get("LIVE_EXECUTION", "False"),
        "EXECUTION_START_TIME": env_vars.get("EXECUTION_START_TIME", "09:30"),
        "COMPOSER_KEY_ID": env_vars.get("COMPOSER_KEY_ID", ""),
        "COMPOSER_SECRET": env_vars.get("COMPOSER_SECRET", ""),
        "ALPACA_KEY": env_vars.get("ALPACA_KEY", ""),
        "ALPACA_SECRET": env_vars.get("ALPACA_SECRET", ""),
        "ACCOUNT_INDIVIDUAL": env_vars.get("ACCOUNT_INDIVIDUAL", ""),
        "ACCOUNT_INDIVIDUAL_ENABLED": env_vars.get("ACCOUNT_INDIVIDUAL_ENABLED", "True"),
        "ACCOUNT_ROTH": env_vars.get("ACCOUNT_ROTH", ""),
        "ACCOUNT_ROTH_ENABLED": env_vars.get("ACCOUNT_ROTH_ENABLED", "True"),
        "ACCOUNT_TRAD": env_vars.get("ACCOUNT_TRAD", ""),
        "ACCOUNT_TRAD_ENABLED": env_vars.get("ACCOUNT_TRAD_ENABLED", "True"),
        "DISCORD_WEBHOOK_URL": env_vars.get("DISCORD_WEBHOOK_URL", ""),
    }

    # Fetch unique symphony names from the current bot_state
    state_data = database.load_state()
    symphony_names = set()
    for data in state_data.values():
        if isinstance(data, dict) and "name" in data:
            symphony_names.add(database.normalize_name(data["name"]))

    symphonies_data = {}
    for name in symphony_names:
        symphonies_data[name] = database.get_symphony_strategy(name)

    return jsonify({"globals": globals_data, "symphonies": symphonies_data})

@app.route("/api/settings", methods=["POST"])
def save_settings():
    """Saves Globals to .env and Symphony Strategies to SQLite."""
    if not database.acquire_lock(lease_duration=15):
        return jsonify({"status": "error", "message": "Cannot save configurations while the bot is executing real-time calculations. Please wait 15 seconds."}), 409

    payload = request.json
    try:
        # Save Globals
        for key, val in payload.get("globals", {}).items():
            cleaned_val = str(val).strip()
            if cleaned_val:
                try:
                    # Let's check if it represents a float and is not a UUID (containing multiple dashes)
                    if "-" not in cleaned_val or (cleaned_val.startswith("-") and cleaned_val.count("-") == 1):
                        f_val = float(cleaned_val)
                        if f_val.is_integer():
                            cleaned_val = str(int(f_val))
                        else:
                            cleaned_val = f"{f_val:.4f}"
                except ValueError:
                    pass
            set_key(ENV_FILE_PATH, key, cleaned_val)
            os.environ[key] = cleaned_val

        # Save Symphony Strategies
        for sym_name, strategy_data in payload.get("symphonies", {}).items():
            params = {}
            for k, v in strategy_data.get("params", {}).items():
                if v is not None and str(v).strip() != "":
                    try:
                        val = float(v)
                        if val.is_integer():
                            params[k] = int(val)
                        else:
                            params[k] = round(val, 4)
                    except (ValueError, TypeError):
                        if isinstance(v, str):
                            params[k] = v
            locked = strategy_data.get("locked_vars", [])
            database.save_symphony_strategy(sym_name, params, locked)

        return jsonify({"status": "success", "message": "Variables updated successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        database.release_lock()

if __name__ == "__main__":
    # Start the scheduler thread
    threading.Thread(target=run_scheduler, daemon=True).start()
    print("\nStarting Alpha Bot Control Center at http://localhost:5000\n", flush=True)
    
    # Disable use_reloader to ensure the background thread runs once and only once
    app.run(port=5000, debug=False, use_reloader=False)
