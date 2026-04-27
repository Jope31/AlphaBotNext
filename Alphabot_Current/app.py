"""Flask application for the Alpha Bot Control Center with SQLite."""

import time
import threading
import subprocess
from datetime import datetime
import schedule
import requests
from flask import Flask, render_template, jsonify, request
from dotenv import dotenv_values, set_key, find_dotenv
from alpha_bot_execution import get_composer_headers, COMPOSER_BASE_URL

import database

app = Flask(__name__)


# --- 1. Bot Execution Logic ---
def trigger_alpha_bot(force=False):
    """Triggers the alpha bot execution script."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Triggering Alpha Bot...")
    try:
        cmd = ["python", "alpha_bot_execution.py"]
        if force:
            cmd.append("--force")
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Execution failed: {e}")


# --- 2. Background Scheduler ---
def threaded_trigger():
    """Launches the bot execution in a background thread to prevent scheduler blocking."""
    threading.Thread(target=trigger_alpha_bot, daemon=True).start()


def run_scheduler():
    """Runs the scheduler every 1-minute to support Multi-Tick confirmations."""
    schedule.every().minute.at(":00").do(threaded_trigger)

    while True:
        schedule.run_pending()
        time.sleep(1)


# --- 3. Web Dashboard Routes ---
@app.route("/")
def dashboard():
    """Renders the dashboard template."""
    return render_template("index.html")


@app.route("/api/state")
def get_state():
    """Returns the current state of the bot directly from SQLite."""
    try:
        state_data = database.load_state()

        if not state_data:
            return jsonify(
                {
                    "status": "waiting",
                    "message": "Bot state initializing. Please wait for the first run.",
                }
            )

        env_vars = dotenv_values(".env")
        live_mode = env_vars.get("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")

        next_run_seconds = 0
        jobs = schedule.get_jobs()
        valid_jobs = [job for job in jobs if job.next_run]
        if valid_jobs:
            next_run_time = min(job.next_run for job in valid_jobs)
            delta = next_run_time - datetime.now()
            next_run_seconds = max(0, int(delta.total_seconds()))

        return jsonify(
            {
                "status": "active",
                "state": state_data,
                "live_mode": live_mode,
                "execution_start_time": env_vars.get("EXECUTION_START_TIME", "09:30"),
                "next_run_seconds": next_run_seconds,
            }
        )

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/chart/<symphony_id>")
def get_chart_data(symphony_id):
    """Returns the intraday timeseries chart data from SQLite."""
    try:
        chart_data = database.load_chart_history()
        if not chart_data:
            return jsonify({"status": "waiting", "data": []})

        symphony_data = chart_data.get("symphonies", {}).get(symphony_id, [])
        return jsonify({"status": "success", "data": symphony_data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/trigger", methods=["POST"])
def manual_trigger():
    """Manually triggers the bot execution."""
    threading.Thread(target=trigger_alpha_bot, args=(True,)).start()
    return jsonify({"status": "success", "message": "Bot execution forced (bypassing gatekeeper)."})


# --- 4. Account Liquidation Route ---
def perform_account_liquidation(account_id, key, secret, live_mode):
    """Performs account liquidation via the Composer API."""
    headers = get_composer_headers(key=key, secret=secret)
    url = f"{COMPOSER_BASE_URL}/portfolio/accounts/{account_id}/symphony-stats-meta"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            symphonies = resp.json().get("symphonies", [])
            print(f"Found {len(symphonies)} symphonies to liquidate in account {account_id}...")

            for sym in symphonies:
                if live_mode:
                    act_sym_id = sym.get("symphony_id", sym["id"])
                    sell_url = f"{COMPOSER_BASE_URL}/deploy/accounts/{account_id}/symphonies/{act_sym_id}/go-to-cash"
                    sell_resp = requests.post(sell_url, headers=headers, json={}, timeout=10)

                    sym_name = sym.get("name", sym["id"])
                    if sell_resp.status_code in [200, 201, 202]:
                        print(f"✅ Liquidated {sym_name} (HTTP {sell_resp.status_code})")
                    else:
                        print(
                            f"❌ Failed to liquidate {sym_name}: HTTP {sell_resp.status_code} - {sell_resp.text}"
                        )
                    time.sleep(1.5)
    except requests.RequestException as e:
        print(f"Liquidation Error: {e}")


@app.route("/api/sell_account", methods=["POST"])
def sell_account():
    """Initiates account liquidation."""
    data = request.json
    account_id = data.get("account_id")
    if not account_id:
        return jsonify({"status": "error", "message": "No account ID provided"}), 400

    env_vars = dotenv_values(".env")
    key = env_vars.get("COMPOSER_KEY_ID")
    secret = env_vars.get("COMPOSER_SECRET")
    live_mode = env_vars.get("LIVE_EXECUTION", "False").lower() in ("true", "1", "yes")

    if not key or not secret:
        return (
            jsonify({"status": "error", "message": "Composer API keys missing in settings."}),
            400,
        )

    threading.Thread(
        target=perform_account_liquidation, args=(account_id, key, secret, live_mode)
    ).start()
    mode_text = "LIVE EXECUTION" if live_mode else "DRY RUN"
    return jsonify(
        {
            "status": "success",
            "message": f"[{mode_text}] Initiated account liquidation. Watch terminal for queue confirmations.",
        }
    )


# --- 5. Settings/Control Panel Routes ---
@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Returns the current settings."""
    env_vars = dotenv_values(".env")
    return jsonify(
        {
            "LIVE_EXECUTION": env_vars.get("LIVE_EXECUTION", "False"),
            "EXECUTION_START_TIME": env_vars.get("EXECUTION_START_TIME", "09:30"),
            "COMPOSER_KEY_ID": env_vars.get("COMPOSER_KEY_ID", ""),
            "COMPOSER_SECRET": env_vars.get("COMPOSER_SECRET", ""),
            "ALPACA_KEY": env_vars.get("ALPACA_KEY", ""),
            "ALPACA_SECRET": env_vars.get("ALPACA_SECRET", ""),
            "ACCOUNT_UUIDS": env_vars.get("ACCOUNT_UUIDS", ""),
            "DISCORD_WEBHOOK_URL": env_vars.get("DISCORD_WEBHOOK_URL", ""),
            "TRIGGER_THRESHOLD_PCT": env_vars.get("TRIGGER_THRESHOLD_PCT", "15.0"),
            "TAKE_PROFIT_MC_PCT": env_vars.get("TAKE_PROFIT_MC_PCT", "5.0"),
            "LOSS_ARM_PCT": env_vars.get("LOSS_ARM_PCT", "1.5"),
            "MAX_SQUEEZE_FLOOR": env_vars.get("MAX_SQUEEZE_FLOOR", "0.20"),
            "VIX_LOW_THRESHOLD": env_vars.get("VIX_LOW_THRESHOLD", "15.0"),
            "VIX_HIGH_THRESHOLD": env_vars.get("VIX_HIGH_THRESHOLD", "25.0"),
            "VIX_LOW_MULT": env_vars.get("VIX_LOW_MULT", "1.5"),
            "VIX_MID_MULT": env_vars.get("VIX_MID_MULT", "2.0"),
            "VIX_HIGH_MULT": env_vars.get("VIX_HIGH_MULT", "2.5"),
            "MIN_MULTIPLIER_FLOOR": env_vars.get("MIN_MULTIPLIER_FLOOR", "0.5"),
            "TRAILING_STOP_PCT": env_vars.get("TRAILING_STOP_PCT", "1.5"),
            "ENDING_STOP_PCT": env_vars.get("ENDING_STOP_PCT", "0.5"),
            "BREAKEVEN_ACTIVATION_PCT": env_vars.get("BREAKEVEN_ACTIVATION_PCT", "2.0"),
            "VWAP_CROSS_HWM_PCT": env_vars.get("VWAP_CROSS_HWM_PCT", "1.0"),
            "PARABOLIC_VELOCITY_THRESHOLD": env_vars.get("PARABOLIC_VELOCITY_THRESHOLD", "2.0"),
            "MAX_PARABOLIC_SQUEEZE": env_vars.get("MAX_PARABOLIC_SQUEEZE", "0.50"),
            "SIMULATION_PATHS": env_vars.get("SIMULATION_PATHS", "5000"),
            "NEIGHBOR_K": env_vars.get("NEIGHBOR_K", "150"),
        }
    )


@app.route("/api/settings", methods=["POST"])
def save_settings():
    """Saves the settings."""
    data = request.json
    env_file = find_dotenv()
    if not env_file:
        env_file = ".env"

    allowed_keys = [
        "LIVE_EXECUTION",
        "EXECUTION_START_TIME",
        "COMPOSER_KEY_ID",
        "COMPOSER_SECRET",
        "ALPACA_KEY",
        "ALPACA_SECRET",
        "ACCOUNT_UUIDS",
        "DISCORD_WEBHOOK_URL",
        "TRIGGER_THRESHOLD_PCT",
        "TAKE_PROFIT_MC_PCT",
        "LOSS_ARM_PCT",
        "MAX_SQUEEZE_FLOOR",
        "VIX_LOW_THRESHOLD",
        "VIX_HIGH_THRESHOLD",
        "VIX_LOW_MULT",
        "VIX_MID_MULT",
        "VIX_HIGH_MULT",
        "MIN_MULTIPLIER_FLOOR",
        "TRAILING_STOP_PCT",
        "ENDING_STOP_PCT",
        "BREAKEVEN_ACTIVATION_PCT",
        "VWAP_CROSS_HWM_PCT",
        "PARABOLIC_VELOCITY_THRESHOLD",
        "MAX_PARABOLIC_SQUEEZE",
        "SIMULATION_PATHS",
        "NEIGHBOR_K",
    ]

    try:
        for key in allowed_keys:
            if key in data:
                set_key(env_file, key, str(data[key]))
        return jsonify(
            {"status": "success", "message": "Variables updated successfully! Applied to next run."}
        )
    except OSError as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    print("\n🚀 Starting Alpha Bot Control Center at http://localhost:5000\n")
    app.run(port=5000, debug=False)