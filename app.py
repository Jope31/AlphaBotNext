import os
import json
import time
import threading
import subprocess
import schedule
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from dotenv import dotenv_values, set_key, find_dotenv

app = Flask(__name__)

# --- 1. Bot Execution Logic ---
def trigger_alpha_bot():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Triggering Alpha Bot...")
    try:
        subprocess.run(["python", "alpha_bot_final.py"], check=True)
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Execution failed: {e}")

# --- 2. Background Scheduler ---
def run_scheduler():
    schedule.every(5).minutes.do(trigger_alpha_bot)
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
            
        # We also pass the LIVE_EXECUTION state down to the UI so it can display the badge
        env_vars = dotenv_values('.env')
        live_mode = env_vars.get("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")
            
        return jsonify({
            "status": "active", 
            "state": state_data,
            "live_mode": live_mode
        })
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/trigger', methods=['POST'])
def manual_trigger():
    threading.Thread(target=trigger_alpha_bot).start()
    return jsonify({"status": "success", "message": "Bot execution started in background."})

# --- 4. Settings/Control Panel Routes ---
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
        "ATR_LOOKBACK_DAYS": env_vars.get("ATR_LOOKBACK_DAYS", "14"),
        "BASE_ATR_MULTIPLIER": env_vars.get("BASE_ATR_MULTIPLIER", "2.0"),
        "RED_DAY_ATR_MULTIPLIER": env_vars.get("RED_DAY_ATR_MULTIPLIER", "0.75"),
        "MIN_MULTIPLIER_FLOOR": env_vars.get("MIN_MULTIPLIER_FLOOR", "0.5")
    })

@app.route('/api/settings', methods=['POST'])
def save_settings():
    data = request.json
    env_file = find_dotenv()
    if not env_file:
        env_file = '.env'
    
    allowed_keys = [
        "LIVE_EXECUTION",
        "COMPOSER_KEY_ID", "COMPOSER_SECRET", "ALPACA_KEY", "ALPACA_SECRET",
        "ACCOUNT_UUIDS", "DISCORD_WEBHOOK_URL", "TRIGGER_THRESHOLD_PCT", 
        "ATR_LOOKBACK_DAYS", "BASE_ATR_MULTIPLIER", "RED_DAY_ATR_MULTIPLIER", "MIN_MULTIPLIER_FLOOR"
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
