import os
import json
import time
import threading
import subprocess
import schedule
import requests
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from dotenv import dotenv_values, set_key, find_dotenv

app = Flask(__name__)

# --- 1. Bot Execution Logic ---
def trigger_alpha_bot(force=False):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Triggering Alpha Bot...")
    try:
        cmd = ["python", "alpha_bot_execution.py"]
        if force:
            cmd.append("--force")
        subprocess.run(cmd, check=True)
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Execution failed: {e}")

# --- 2. Background Scheduler ---
def run_scheduler():
    schedule.every(5).minutes.do(trigger_alpha_bot, force=False)
    while True:
        schedule.run_pending()
        time.sleep(1)

# --- 3. Web Dashboard Routes ---
@app.route('/')
def dashboard():
    return render_template('index.html')

@app.route('/api/state')
def get_state():
    try:
        if not os.path.exists('bot_state.json'):
            return jsonify({"status": "waiting", "message": "bot_state.json not created yet."})
            
        with open('bot_state.json', 'r') as f:
            state_data = json.load(f)
            
        env_vars = dotenv_values('.env')
        live_mode = env_vars.get("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")
            
        next_run_seconds = 0
        jobs = schedule.get_jobs()
        if jobs and jobs[0].next_run:
            delta = jobs[0].next_run - datetime.now()
            next_run_seconds = max(0, int(delta.total_seconds()))

        return jsonify({
            "status": "active", 
            "state": state_data,
            "live_mode": live_mode,
            "next_run_seconds": next_run_seconds
        })
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/trigger', methods=['POST'])
def manual_trigger():
    threading.Thread(target=trigger_alpha_bot, args=(True,)).start()
    return jsonify({"status": "success", "message": "Bot execution forced (bypassing gatekeeper)."})

# --- 4. Account Liquidation Route ---
def perform_account_liquidation(account_id, key, secret, live_mode):
    headers = {
        "x-api-key-id": key,
        "authorization": f"Bearer {secret}",
        "Content-Type": "application/json"
    }
    url = f"https://api.composer.trade/api/v0.1/portfolio/accounts/{account_id}/symphony-stats-meta"
    
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            symphonies = resp.json().get("symphonies", [])
            print(f"Found {len(symphonies)} symphonies to liquidate in account {account_id}...")
            
            for sym in symphonies:
                if live_mode:
                    sell_url = f"https://api.composer.trade/api/v0.1/deploy/accounts/{account_id}/symphonies/{sym['id']}/go-to-cash"
                    # Added json={} to satisfy the Content-Type header expectation
                    sell_resp = requests.post(sell_url, headers=headers, json={})
                    
                    if sell_resp.status_code in [200, 201, 202]:
                        print(f"✅ Liquidated {sym.get('name', sym['id'])} (HTTP {sell_resp.status_code})")
                    else:
                        print(f"❌ Failed to liquidate {sym.get('name', sym['id'])}: HTTP {sell_resp.status_code} - {sell_resp.text}")
                    
                    time.sleep(1.5)  
    except Exception as e:
        print(f"Liquidation Error: {e}")

@app.route('/api/sell_account', methods=['POST'])
def sell_account():
    data = request.json
    account_id = data.get('account_id')
    if not account_id:
        return jsonify({"status": "error", "message": "No account ID provided"}), 400
        
    env_vars = dotenv_values('.env')
    key = env_vars.get("COMPOSER_KEY_ID")
    secret = env_vars.get("COMPOSER_SECRET")
    live_mode = env_vars.get("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")

    if not key or not secret:
        return jsonify({"status": "error", "message": "Composer API keys missing in settings."}), 400
        
    threading.Thread(target=perform_account_liquidation, args=(account_id, key, secret, live_mode)).start()
    mode_text = "LIVE EXECUTION" if live_mode else "DRY RUN"
    return jsonify({"status": "success", "message": f"[{mode_text}] Initiated account liquidation."})

# --- 5. Settings/Control Panel Routes ---
@app.route('/api/settings', methods=['GET'])
def get_settings():
    env_vars = dotenv_values('.env')
    return jsonify({
        "LIVE_EXECUTION": env_vars.get("LIVE_EXECUTION", "False"),
        "COMPOSER_KEY_ID": env_vars.get("COMPOSER_KEY_ID", ""),
        "COMPOSER_SECRET": env_vars.get("COMPOSER_SECRET", ""),
        "ALPACA_KEY": env_vars.get("ALPACA_KEY", ""),
        "ALPACA_SECRET": env_vars.get("ALPACA_SECRET", ""),
        "ACCOUNT_UUIDS": env_vars.get("ACCOUNT_UUIDS", ""),
        "DISCORD_WEBHOOK_URL": env_vars.get("DISCORD_WEBHOOK_URL", ""),
        "TRIGGER_THRESHOLD_PCT": env_vars.get("TRIGGER_THRESHOLD_PCT", "15.0"),
        "TRAILING_STOP_PCT": env_vars.get("TRAILING_STOP_PCT", "1.5"),
        "BREAKEVEN_ACTIVATION_PCT": env_vars.get("BREAKEVEN_ACTIVATION_PCT", "2.0")
    })

@app.route('/api/settings', methods=['POST'])
def save_settings():
    data = request.json
    env_file = find_dotenv()
    if not env_file:
        env_file = '.env'
    
    allowed_keys = [
        "LIVE_EXECUTION", "COMPOSER_KEY_ID", "COMPOSER_SECRET", "ALPACA_KEY", "ALPACA_SECRET",
        "ACCOUNT_UUIDS", "DISCORD_WEBHOOK_URL", "TRIGGER_THRESHOLD_PCT", 
        "TRAILING_STOP_PCT", "BREAKEVEN_ACTIVATION_PCT"
    ]
    
    try:
        for key in allowed_keys:
            if key in data:
                set_key(env_file, key, str(data[key]))
        return jsonify({"status": "success", "message": "Variables updated successfully! Applied to next run."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    print("\n🚀 Starting Alpha Bot Control Center at http://localhost:5000\n")
    app.run(port=5000, debug=False)
