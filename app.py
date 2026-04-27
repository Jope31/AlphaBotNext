"""Flask application for AlphaBot Control Center with Account-Level settings."""

import os
import time
import threading
import subprocess
from datetime import datetime
import schedule
import requests
from flask import Flask, render_template, jsonify, request
from dotenv import dotenv_values, set_key, find_dotenv

import database

app = Flask(__name__)
COMPOSER_BASE_URL = "https://api.composer.trade/api/v0.1"

# --- 1. Bot Execution Logic ---
def trigger_alpha_bot(force=False):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Triggering Alpha Bot...")
    try:
        cmd = ["python", "alpha_bot_execution.py"]
        if force:
            cmd.append("--force")
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Execution failed: {e}")

def threaded_trigger():
    threading.Thread(target=trigger_alpha_bot, daemon=True).start()

def run_scheduler():
    schedule.every().minute.at(":00").do(threaded_trigger)
    while True:
        schedule.run_pending()
        time.sleep(1)

# --- 2. Web Dashboard Routes ---
@app.route("/")
def dashboard():
    return render_template("index.html")

@app.route("/api/state")
def get_state():
    try:
        state_data = database.load_state()
        if not state_data:
            return jsonify({"status": "waiting", "message": "Bot state initializing."})

        env_vars = dotenv_values(".env")
        live_mode = env_vars.get("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")

        next_run_seconds = 0
        valid_jobs = [job for job in schedule.get_jobs() if job.next_run]
        if valid_jobs:
            delta = min(job.next_run for job in valid_jobs) - datetime.now()
            next_run_seconds = max(0, int(delta.total_seconds()))

        return jsonify({
            "status": "active",
            "state": state_data,
            "live_mode": live_mode,
            "execution_start_time": env_vars.get("EXECUTION_START_TIME", "09:30"),
            "next_run_seconds": next_run_seconds,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

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

# --- 3. Account Liquidation ---
def perform_account_liquidation(account_id, key, secret, live_mode):
    headers = {"x-api-key-id": key, "authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    url = f"{COMPOSER_BASE_URL}/portfolio/accounts/{account_id}/symphony-stats-meta"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            for sym in resp.json().get("symphonies", []):
                if live_mode:
                    sell_url = f"{COMPOSER_BASE_URL}/deploy/accounts/{account_id}/symphonies/{sym.get('symphony_id', sym['id'])}/go-to-cash"
                    sell_resp = requests.post(sell_url, headers=headers, json={}, timeout=10)
                    print(f"Liquidated {sym.get('name')} (HTTP {sell_resp.status_code})")
                    time.sleep(1.5)
    except Exception as e:
        print(f"Liquidation Error: {e}")

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

# --- 4. Tabbed Settings / Control Panel Routes ---
@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Returns Globals from .env and Account Strategies from SQLite."""
    env_vars = dotenv_values(".env")
    globals_data = {
        "LIVE_EXECUTION": env_vars.get("LIVE_EXECUTION", "False"),
        "EXECUTION_START_TIME": env_vars.get("EXECUTION_START_TIME", "09:30"),
        "COMPOSER_KEY_ID": env_vars.get("COMPOSER_KEY_ID", ""),
        "COMPOSER_SECRET": env_vars.get("COMPOSER_SECRET", ""),
        "ALPACA_KEY": env_vars.get("ALPACA_KEY", ""),
        "ALPACA_SECRET": env_vars.get("ALPACA_SECRET", ""),
        "ACCOUNT_UUIDS": env_vars.get("ACCOUNT_UUIDS", ""),
        "DISCORD_WEBHOOK_URL": env_vars.get("DISCORD_WEBHOOK_URL", ""),
    }

    # Fetch DB strategies and ensure newly added accounts in .env have a DB entry
    account_uuids = [uid.strip() for uid in globals_data["ACCOUNT_UUIDS"].split(",") if uid.strip()]
    accounts_data = {}
    for acc in account_uuids:
        accounts_data[acc] = database.get_account_strategy(acc)

    return jsonify({"globals": globals_data, "accounts": accounts_data})

@app.route("/api/settings", methods=["POST"])
def save_settings():
    """Saves Globals to .env and Account Strategies to SQLite."""
    payload = request.json
    env_file = find_dotenv() or ".env"

    try:
        # Save Globals
        for key, val in payload.get("globals", {}).items():
            set_key(env_file, key, str(val))

        # Save Account Strategies
        for acc_id, strategy_data in payload.get("accounts", {}).items():
            params = {k: float(v) for k, v in strategy_data.get("params", {}).items()}
            locked = strategy_data.get("locked_vars", [])
            database.save_account_strategy(acc_id, params, locked)

        return jsonify({"status": "success", "message": "Variables updated successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    print("\n🚀 Starting Alpha Bot Control Center at http://localhost:5000\n")
    app.run(port=5000, debug=False)